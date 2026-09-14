import sqlite3
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from mikrofix_office_client import OfficeCommandJournal, RequestsOfficeTransport
from mikrofix_sync_contracts import ContractError
from mikrofix_sync_store import outbox_operation


def receipt_payload(receipt_id=None):
    return {
        "receipt_id": str(receipt_id or uuid4()),
        "supplier_id": str(uuid4()),
        "supplier_expected_version": 1,
        "document_type": "DOB",
        "custom_type_code": "",
        "external_document_no": "DOB-TEST-1",
        "document_date": "2026-09-12",
        "received_date": "2026-09-12",
        "currency": "EUR",
        "document_total": "11.10",
        "shipping_total": "0",
        "document_review_confirmed": True,
        "lines": [{
            "line_id": str(uuid4()), "article_id": str(uuid4()),
            "quantity": "1", "net_unit_price": "10",
            "discount_percent": "0", "vat_rate": "11",
            "supplier_line_total": "11.10", "confirmed_sale_price": "12",
            "supplier_barcode": "SUP-1", "lot_serial": "",
        }],
        "attachments": [],
    }


class LostResponseTransport:
    def __init__(self):
        self.requests = []

    def submit_operation(self, operation):
        self.requests.append(operation)
        if len(self.requests) == 1:
            raise RuntimeError("response lost after central commit")
        return {
            "operation_id": operation["operation_id"],
            "result_code": "ACCEPTED",
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": 1,
            "central_sequence": 51,
            "error_code": None,
            "error_message": None,
            "result": {"receipt_number": "DOB-2026-0001"},
        }


class OfficeCommandJournalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.transport = LostResponseTransport()
        self.journal = OfficeCommandJournal(
            self.connection, self.transport,
            actor_id="office-a", device_id="OFFICE-PC-A",
        )

    def tearDown(self):
        self.connection.close()

    def test_optional_attachment_and_lost_response_keep_one_exact_command(self):
        operation_id = self.journal.prepare_confirm_purchase_receipt(receipt_payload())
        first = self.journal.dispatch(operation_id)
        second = self.journal.dispatch(operation_id)
        third = self.journal.dispatch(operation_id)

        self.assertTrue(first.uncertain)
        self.assertFalse(second.uncertain)
        self.assertEqual(second.receipt["result"]["receipt_number"], "DOB-2026-0001")
        self.assertEqual(third.receipt, second.receipt)
        self.assertEqual(self.transport.requests[0], self.transport.requests[1])
        self.assertEqual(
            outbox_operation(self.connection, operation_id)["attempt_count"], 2
        )

    def test_financially_invalid_receipt_never_enters_journal(self):
        payload = receipt_payload()
        payload["document_review_confirmed"] = False
        with self.assertRaises(ContractError):
            self.journal.prepare_confirm_purchase_receipt(payload)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM sync_outbox_v32"
        ).fetchone()[0], 0)

    def test_partner_commands_keep_partner_aggregate_and_versions(self):
        partner_id = str(uuid4())
        common = {
            "partner_id": partner_id, "verification_source": "MANUAL_REVIEW",
            "review_confirmed": True, "name": "Partner", "address": "",
            "post_city": "", "country": "Slovenija", "tax_number": "",
            "registration_number": "", "phone": "040000000", "email": "",
            "source_reference": "OFFICE",
        }
        created = self.journal.prepare_create_partner(common)
        updated = self.journal.prepare_update_partner({
            **common, "expected_version": 1, "name": "Partner popravljen",
        })
        deactivated = self.journal.prepare_set_partner_active({
            "partner_id": partner_id, "expected_version": 2,
            "active": False, "reason": "Podvojen zapis",
        })

        rows = [outbox_operation(self.connection, item) for item in (
            created, updated, deactivated,
        )]
        self.assertEqual(
            [row["operation_type"] for row in rows],
            ["CreateOfficePartner", "UpdatePartner", "SetPartnerActive"],
        )
        self.assertEqual({row["aggregate_type"] for row in rows}, {"PARTNER"})
        self.assertEqual([row["expected_version"] for row in rows], [0, 1, 2])

    def test_inventory_item_creation_is_a_zero_version_inventory_command(self):
        article_id = str(uuid4())
        operation_id = self.journal.prepare_create_inventory_item({
            "article_id": article_id, "main_barcode": "ART-NEW-1",
            "description": "Nov artikel", "track_mode": "SERIAL",
        })
        row = outbox_operation(self.connection, operation_id)
        self.assertEqual(row["operation_type"], "CreateInventoryItem")
        self.assertEqual(row["aggregate_type"], "INVENTORY_ITEM")
        self.assertEqual(row["aggregate_uuid"], article_id)
        self.assertEqual(row["expected_version"], 0)

    def test_inventory_price_change_keeps_article_version_and_reason(self):
        article_id = str(uuid4())
        operation_id = self.journal.prepare_update_inventory_price({
            "article_id": article_id, "expected_version": 4,
            "new_sale_price": "19.90", "reason": "Spremenjen cenik",
        })
        row = outbox_operation(self.connection, operation_id)
        self.assertEqual(row["operation_type"], "UpdateInventorySalePrice")
        self.assertEqual(row["aggregate_type"], "INVENTORY_ITEM")
        self.assertEqual(row["aggregate_uuid"], article_id)
        self.assertEqual(row["expected_version"], 4)

    @patch("requests.get")
    def test_office_reads_pending_receipts(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"receipts": [{"status": "PENDING_INVOICE"}]}
        get.return_value = response
        transport = RequestsOfficeTransport("https://central.example", "token")

        receipts = transport.list_purchase_receipts(status="PENDING_INVOICE")

        self.assertEqual(receipts[0]["status"], "PENDING_INVOICE")
        get.assert_called_once_with(
            "https://central.example/v1/office/purchase-receipts",
            params={"status": "PENDING_INVOICE", "limit": 100},
            headers={"Authorization": "Bearer token"}, timeout=15.0,
        )

    @patch("requests.get")
    def test_office_reads_and_opens_central_invoices(self, get):
        first = Mock(status_code=200)
        first.json.return_value = {
            "invoices": [{"invoice_id": "invoice-1", "payment_status": "UNPAID"}]
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "invoice_id": "invoice-1", "invoice_number": "MFD-PIS-1",
            "snapshot": {"lines": []},
        }
        get.side_effect = [first, second]
        transport = RequestsOfficeTransport("https://central.example", "token")

        invoices = transport.list_issued_invoices(
            "kupec", payment_status="UNPAID", limit=25,
        )
        invoice = transport.get_issued_invoice("invoice-1")

        self.assertEqual(invoices[0]["payment_status"], "UNPAID")
        self.assertEqual(invoice["invoice_number"], "MFD-PIS-1")
        self.assertEqual(get.call_count, 2)

    @patch("requests.get")
    def test_office_reads_central_inventory_price_history(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "article_id": "article-1", "description": "Artikel",
            "main_barcode": "ART-1", "price_history": [{
                "previous_price": "10.00", "confirmed_price": "12.00",
            }],
        }
        get.return_value = response
        transport = RequestsOfficeTransport("https://central.example", "token")

        result = transport.inventory_price_history("article-1")

        self.assertEqual(result["price_history"][0]["confirmed_price"], "12.00")
        get.assert_called_once_with(
            "https://central.example/v1/office/inventory/article-1/price-history",
            params={}, headers={"Authorization": "Bearer token"}, timeout=15.0,
        )


if __name__ == "__main__":
    unittest.main()
