import sqlite3
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from mikrofix_cashier_client import CashierCommandJournal, RequestsCashierTransport
from mikrofix_sync_contracts import ContractError
from mikrofix_sync_store import outbox_operation


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
            "aggregate_version": 2,
            "central_sequence": 42,
            "error_code": None,
            "error_message": None,
            "result": {"invoice_number": "MFD-PIS-1"},
        }


def invoice_payload(draft_id):
    return {
        "draft_id": draft_id,
        "expected_version": 1,
        "premise_id": "MFD",
        "electronic_device_id": "PIS",
        "billing_customer": {
            "source": "SERVICE_CUSTOMER",
            "confirmed": True,
        },
        "payments": [{
            "method": "BANK_TRANSFER", "amount": "55", "confirmed": False,
        }],
    }


def direct_sale_payload(sale_id):
    return {
        "sale_id": sale_id, "expected_version": 0,
        "premise_id": "MFD", "electronic_device_id": "PIS",
        "billing_customer": {"source": "WALK_IN", "confirmed": True},
        "payments": [{
            "method": "CASH", "amount": "25", "confirmed": True,
        }],
        "lines": [{
            "line_id": str(uuid4()), "barcode": "SUP-1", "quantity": "1",
            "requested_discount_percent": "0", "lot_serial": "",
        }],
    }


class CashierCommandJournalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.transport = LostResponseTransport()
        self.journal = CashierCommandJournal(
            self.connection,
            self.transport,
            actor_id="blagajnik-a",
            device_id="POS-PC-A",
        )

    def tearDown(self):
        self.connection.close()

    def test_lost_response_retries_exact_same_invoice_command(self):
        operation_id = self.journal.prepare_issue_service_invoice(
            invoice_payload(str(uuid4()))
        )

        first = self.journal.dispatch(operation_id)
        second = self.journal.dispatch(operation_id)
        third = self.journal.dispatch(operation_id)

        self.assertTrue(first.uncertain)
        self.assertFalse(second.uncertain)
        self.assertEqual(second.receipt["result"]["invoice_number"], "MFD-PIS-1")
        self.assertEqual(third.receipt, second.receipt)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual(self.transport.requests[0], self.transport.requests[1])
        self.assertEqual(
            outbox_operation(self.connection, operation_id)["attempt_count"], 2
        )

    def test_prepare_with_same_identity_accepts_only_same_body(self):
        operation_id = str(uuid4())
        draft_id = str(uuid4())
        self.journal.prepare_issue_service_invoice(
            invoice_payload(draft_id), operation_id=operation_id
        )
        self.assertEqual(
            self.journal.prepare_issue_service_invoice(
                invoice_payload(draft_id), operation_id=operation_id
            ),
            operation_id,
        )
        changed = invoice_payload(draft_id)
        changed["payments"][0]["amount"] = "56"
        with self.assertRaisesRegex(Exception, "different sync operation"):
            self.journal.prepare_issue_service_invoice(
                changed, operation_id=operation_id
            )

    def test_direct_sale_is_durably_prepared_before_dispatch(self):
        sale_id = str(uuid4())
        operation_id = self.journal.prepare_issue_direct_sale_invoice(
            direct_sale_payload(sale_id)
        )
        row = outbox_operation(self.connection, operation_id)
        self.assertEqual(row["operation_type"], "IssueDirectSaleInvoice")
        self.assertEqual(row["aggregate_type"], "DIRECT_SALE")
        self.assertEqual(row["aggregate_uuid"], sale_id)
        self.assertEqual(row["status"], "PENDING")

    def test_invalid_partner_is_not_written_to_journal(self):
        with self.assertRaises(ContractError):
            self.journal.prepare_create_partner({
                "partner_id": str(uuid4()),
                "verification_source": "MANUAL_REVIEW",
                "review_confirmed": False,
                "name": "Nepotrjen kupec",
            })
        count = self.connection.execute(
            "SELECT COUNT(*) FROM sync_outbox_v32"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    @patch("requests.get")
    def test_ajpes_read_is_only_an_unconfirmed_suggestion(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "suggestion": {
                "name": "Predlagani partner d.o.o.",
                "tax_number": "12345678",
                "review_confirmed": False,
            }
        }
        get.return_value = response
        transport = RequestsCashierTransport("https://central.example", "token")

        suggestion = transport.suggest_partner_from_ajpes("12345678")

        self.assertFalse(suggestion["review_confirmed"])
        get.assert_called_once_with(
            "https://central.example/v1/cashier/partners/ajpes/12345678",
            params={},
            headers={"Authorization": "Bearer token"},
            timeout=15.0,
        )


if __name__ == "__main__":
    unittest.main()
