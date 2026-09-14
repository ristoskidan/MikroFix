import sqlite3
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from mikrofix_central_office_bootstrap import (
    CentralOfficeBootstrapError,
    build_central_office,
)
from mikrofix_office_runtime import (
    CentralOfficeWorkflow,
    OfficeCommandRejected,
    OfficeResultUncertain,
    PendingOfficeCommand,
)


def partner_payload(partner_id=None):
    return {
        "partner_id": str(partner_id or uuid4()),
        "verification_source": "MANUAL_REVIEW", "review_confirmed": True,
        "name": "Partner", "address": "", "post_city": "",
        "country": "Slovenija", "tax_number": "",
        "registration_number": "", "phone": "040000000", "email": "",
        "source_reference": "OFFICE",
    }


def receipt_payload(receipt_id=None):
    return {
        "receipt_id": str(receipt_id or uuid4()),
        "supplier_id": str(uuid4()), "supplier_expected_version": 1,
        "document_type": "DOB", "custom_type_code": "",
        "external_document_no": "DOB-TEST-1",
        "document_date": "2026-09-13", "received_date": "2026-09-13",
        "document_total": "12.20", "shipping_total": "0.0000",
        "document_review_confirmed": True, "currency": "EUR",
        "lines": [{
            "line_id": str(uuid4()), "article_id": str(uuid4()),
            "quantity": "1", "net_unit_price": "10.0000",
            "discount_percent": "0.0000", "vat_rate": "22.0000",
            "supplier_line_total": "12.20", "confirmed_sale_price": "15.00",
            "supplier_barcode": "SUP-1", "lot_serial": "",
        }],
        "attachments": [],
    }


class FakeOfficeTransport:
    def __init__(self):
        self.submitted = []
        self.mode = "ACCEPT"

    def submit_operation(self, operation):
        self.submitted.append(operation)
        if self.mode == "UNCERTAIN":
            raise RuntimeError("response lost")
        accepted = self.mode == "ACCEPT"
        return {
            "operation_id": operation["operation_id"],
            "result_code": "ACCEPTED" if accepted else "REJECTED",
            "error_code": None if accepted else "VERSION_CONFLICT",
            "error_message": None if accepted else "Zapis se je vmes spremenil.",
            "result": {"partner_id": operation["aggregate_id"]},
        }

    def search_partners(self, query, *, limit=50):
        return [{"name": query, "limit": limit}]

    def search_inventory(self, query, *, limit=100):
        return [{"description": query, "limit": limit}]

    def list_purchase_receipts(self, *, status="", limit=100):
        return [{"status": status, "limit": limit}]

    def list_issued_invoices(self, query="", *, payment_status="", limit=100):
        return [{"query": query, "payment_status": payment_status, "limit": limit}]

    def get_issued_invoice(self, invoice_id):
        return {"invoice_id": invoice_id, "snapshot": {}}

    def list_custom_document_types(self):
        return [{"type_code": "PREV", "display_name": "Prevzemnica"}]


class CentralOfficeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.transport = FakeOfficeTransport()
        self.workflow = CentralOfficeWorkflow(
            self.connection, self.transport,
            actor_id="office-a", device_id="OFFICE-PC-A",
        )

    def tearDown(self):
        self.connection.close()

    def test_reads_are_delegated_without_creating_commands(self):
        self.assertEqual(self.workflow.partners("dob", limit=3)[0]["name"], "dob")
        self.assertEqual(self.workflow.inventory("zaslon", limit=4)[0]["limit"], 4)
        self.assertEqual(
            self.workflow.purchase_receipts(status="RELEASED")[0]["status"],
            "RELEASED",
        )
        self.assertEqual(
            self.workflow.invoices("kupec", payment_status="UNPAID")[0]["query"],
            "kupec",
        )
        self.assertEqual(self.workflow.invoice("invoice-1")["invoice_id"], "invoice-1")
        self.assertEqual(self.workflow.receipt_document_types()[0]["type_code"], "PREV")
        self.assertEqual(self.workflow.unresolved_commands(), [])

    def test_accepted_partner_command_returns_central_result(self):
        payload = partner_payload()
        result = self.workflow.create_partner(payload)
        self.assertEqual(result["partner_id"], payload["partner_id"])
        self.assertEqual(self.workflow.unresolved_commands(), [])

    def test_inventory_item_creation_uses_the_ordered_central_journal(self):
        article_id = str(uuid4())
        result = self.workflow.create_inventory_item({
            "article_id": article_id, "main_barcode": "ART-NEW-2",
            "description": "Artikel iz Office", "track_mode": "QUANTITY",
        })
        self.assertEqual(result["partner_id"], article_id)
        submitted = self.transport.submitted[0]
        self.assertEqual(submitted["operation_type"], "CreateInventoryItem")
        self.assertEqual(submitted["aggregate_type"], "INVENTORY_ITEM")

    def test_receipt_commands_use_the_central_journal(self):
        receipt = receipt_payload()
        self.workflow.confirm_purchase_receipt(receipt)
        attach = {
            "receipt_id": receipt["receipt_id"], "expected_version": 1,
            "invoice_external_no": "PRA-TEST-1", "invoice_date": "2026-09-13",
            "invoice_due_date": "2026-09-21", "invoice_payment_status": "NEPLACAN",
            "document_total": receipt["document_total"],
            "shipping_total": receipt["shipping_total"],
            "document_review_confirmed": True, "currency": "EUR",
            "lines": receipt["lines"], "attachments": [],
        }
        self.workflow.attach_purchase_invoice(attach)
        self.assertEqual(
            [item["operation_type"] for item in self.transport.submitted],
            ["ConfirmPurchaseReceipt", "AttachPurchaseInvoice"],
        )

    def test_uncertain_command_blocks_new_mutation_and_retries_same_identity(self):
        self.transport.mode = "UNCERTAIN"
        payload = partner_payload()
        with self.assertRaises(OfficeResultUncertain) as first:
            self.workflow.create_partner(payload)
        operation_id = first.exception.operation_id
        with self.assertRaises(PendingOfficeCommand):
            self.workflow.create_partner(partner_payload())

        self.transport.mode = "ACCEPT"
        result = self.workflow.retry_command(operation_id)
        self.assertEqual(result["partner_id"], payload["partner_id"])
        self.assertEqual(
            self.transport.submitted[0]["operation_id"],
            self.transport.submitted[1]["operation_id"],
        )
        self.assertEqual(
            self.transport.submitted[0]["payload"], self.transport.submitted[1]["payload"]
        )

    def test_rejected_command_is_not_reported_as_success(self):
        self.transport.mode = "REJECT"
        with self.assertRaises(OfficeCommandRejected) as rejected:
            self.workflow.create_partner(partner_payload())
        self.assertEqual(rejected.exception.code, "VERSION_CONFLICT")
        self.assertEqual(self.workflow.unresolved_commands(), [])


class CentralOfficeBootstrapTests(unittest.TestCase):
    def test_empty_url_keeps_explicit_legacy_mode(self):
        self.assertIsNone(build_central_office(
            settings={"environment": "TEST", "database": {"server_url": ""}},
            office_db=":memory:", actor_id="office-a", device_id="OFFICE-PC-A",
            token="",
        ))

    def test_configured_url_without_secret_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CentralOfficeBootstrapError, "lokalno knjiženje"):
                build_central_office(
                    settings={
                        "environment": "TEST",
                        "database": {"server_url": "http://127.0.0.1:8765"},
                    },
                    office_db=":memory:", actor_id="office-a", device_id="OFFICE-PC-A",
                )

    def test_development_configuration_builds_durable_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            handle = build_central_office(
                settings={
                    "environment": "TEST",
                    "database": {"server_url": "http://127.0.0.1:8765/"},
                },
                office_db=Path(directory) / "office.sqlite",
                actor_id="office-a", device_id="OFFICE-PC-A",
                token="development-token-with-24-chars",
            )
            self.assertIsNotNone(handle)
            self.assertEqual(handle.workflow.transport.base_url, "http://127.0.0.1:8765")
            self.assertEqual(handle.workflow.journal.device_id, "OFFICE-PC-A")
            handle.close()

    def test_production_rejects_development_secret(self):
        with self.assertRaisesRegex(CentralOfficeBootstrapError, "Produkcijska"):
            build_central_office(
                settings={
                    "environment": "PRODUCTION",
                    "database": {"server_url": "https://central.example"},
                },
                office_db=":memory:", actor_id="office-a", device_id="OFFICE-PC-A",
                token="development-token-with-24-chars",
            )

if __name__ == "__main__":
    unittest.main()
