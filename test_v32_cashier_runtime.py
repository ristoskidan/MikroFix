import sqlite3
import unittest
from uuid import uuid4

from mikrofix_cashier_runtime import (
    CashierCommandRejected, CashierResultUncertain, CentralCashierWorkflow,
    PendingCashierCommand, direct_sale_lines_from_pos,
)
from mikrofix_v30_pos import PosLine


class FakeCashierTransport:
    def __init__(self, result="ACCEPTED"):
        self.result = result
        self.submissions = []

    def submit_operation(self, operation):
        self.submissions.append(operation)
        if self.result == "UNCERTAIN":
            raise RuntimeError("lost response")
        invoice_id = str(uuid4())
        if operation["operation_type"] == "OpenCashSession":
            result = {
                "session_id": operation["aggregate_id"],
                "session_no": "BD-2026-0001", "status": "OPEN",
            }
        elif operation["operation_type"] == "CloseCashSession":
            result = {
                "session_id": operation["aggregate_id"],
                "session_no": "BD-2026-0001", "status": "CLOSED",
            }
        elif operation["operation_type"] == "RecordManualCashEntry":
            result = {
                "entry_id": operation["aggregate_id"], "entry_no": 1,
                "balance_after": "110.00",
            }
        elif operation["operation_type"] == "StornoInvoice":
            result = {
                "invoice_id": operation["aggregate_id"],
                "reversal_invoice_id": invoice_id,
            }
        else:
            result = {"invoice_id": invoice_id}
        return {
            "operation_id": operation["operation_id"],
            "result_code": self.result,
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": 1,
            "central_sequence": 7 if self.result == "ACCEPTED" else None,
            "error_code": None if self.result == "ACCEPTED" else "INSUFFICIENT_STOCK",
            "error_message": None if self.result == "ACCEPTED" else "Ni zaloge.",
            "result": result if self.result == "ACCEPTED" else {},
        }

    def get_invoice(self, invoice_id):
        return {
            "invoice_id": invoice_id, "invoice_number": "MFD-PIS-1",
            "snapshot": {"lines": [], "total_amount": "0.00"},
        }

    def search_inventory(self, query, *, limit):
        return [{"main_barcode": query, "article_quantity_available": "1"}]

    def list_ready_service_drafts(self, *, limit):
        return [{"draft_id": str(uuid4())}]

    def search_partners(self, query, *, limit):
        return [{"name": query}]

    def quote_direct_sale(self, lines):
        return {"lines": lines, "total_amount": "15.00"}

    def current_cash_session(self, premise_id, electronic_device_id):
        return None

    def list_cash_sessions(self, *, premise_id, electronic_device_id, limit):
        return []

    def get_cash_session(self, session_id):
        return {"session_id": session_id, "entries": []}


def direct_payload():
    return {
        "sale_id": str(uuid4()), "premise_id": "MFD",
        "electronic_device_id": "PIS",
        "billing_customer": {"source": "WALK_IN", "confirmed": True},
        "payments": [{"method": "BANK_TRANSFER", "amount": "15", "confirmed": False}],
        "lines": [{
            "line_id": str(uuid4()), "line_kind": "ARTICLE",
            "barcode": "SUP-1", "quantity": "1",
            "requested_discount_percent": "0", "lot_serial": "",
        }],
    }


class CentralCashierWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")

    def tearDown(self):
        self.connection.close()

    def workflow(self, transport):
        return CentralCashierWorkflow(
            self.connection, transport, actor_id="cashier-a", device_id="POS-A"
        )

    def test_accepted_command_is_followed_by_authoritative_invoice_read(self):
        workflow = self.workflow(FakeCashierTransport())
        result = workflow.issue_direct_sale(**direct_payload())
        self.assertEqual(result.invoice["invoice_number"], "MFD-PIS-1")
        self.assertEqual(workflow.unresolved_commands(), [])

    def test_uncertain_command_remains_retriable_with_the_same_identity(self):
        transport = FakeCashierTransport("UNCERTAIN")
        workflow = self.workflow(transport)
        with self.assertRaises(CashierResultUncertain) as raised:
            workflow.issue_direct_sale(**direct_payload())
        unresolved = workflow.unresolved_commands()
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["operation_id"], raised.exception.operation_id)

        with self.assertRaises(PendingCashierCommand) as blocked:
            workflow.issue_direct_sale(**direct_payload())
        self.assertEqual(blocked.exception.operation_id, raised.exception.operation_id)

        transport.result = "ACCEPTED"
        recovered = workflow.retry(raised.exception.operation_id)
        self.assertEqual(recovered.operation_id, raised.exception.operation_id)
        self.assertEqual(workflow.unresolved_commands(), [])

    def test_business_rejection_is_not_reported_as_an_uncertain_issue(self):
        workflow = self.workflow(FakeCashierTransport("REJECTED"))
        with self.assertRaises(CashierCommandRejected) as raised:
            workflow.issue_direct_sale(**direct_payload())
        self.assertEqual(raised.exception.code, "INSUFFICIENT_STOCK")

    def test_pos_lines_are_reduced_to_server_owned_pricing_inputs(self):
        article = PosLine(
            line_kind="ARTICLE", name="Del", barcode="SUP-1",
            base_unit_price=999, acquisition_cost=888,
        )
        service = PosLine(
            line_kind="SERVICE", name="Poljubno lokalno ime",
            service_tier="T1", service_elapsed_minutes=75,
            service_billable_minutes=90, base_unit_price=999,
        )
        lines = direct_sale_lines_from_pos([article, service])
        self.assertEqual(lines[0]["barcode"], "SUP-1")
        self.assertEqual(lines[1]["service_tier"], "T1")
        self.assertEqual(lines[1]["elapsed_minutes"], 75)
        for line in lines:
            self.assertNotIn("base_unit_price", line)
            self.assertNotIn("acquisition_cost", line)

    def test_reviewed_partner_creation_uses_the_same_durable_command_path(self):
        workflow = self.workflow(FakeCashierTransport())
        # The fake returns an invoice-shaped accepted result, but this still proves
        # the reviewed partner command is durably dispatched and acknowledged.
        result = workflow.create_partner({
            "partner_id": str(uuid4()), "verification_source": "MANUAL_REVIEW",
            "review_confirmed": True, "name": "Kupec d.o.o.",
            "address": "Naslov 1", "post_city": "1000 Ljubljana",
            "country": "Slovenija", "tax_number": "12345678",
            "registration_number": "", "phone": "", "email": "",
            "source_reference": "F9",
        })
        self.assertIn("invoice_id", result)
        self.assertEqual(workflow.unresolved_commands(), [])

    def test_cash_session_commands_use_the_ordered_durable_path(self):
        transport = FakeCashierTransport()
        workflow = self.workflow(transport)
        session_id = str(uuid4())

        opened = workflow.open_cash_session(
            session_id=session_id, premise_id="MFD",
            electronic_device_id="PIS", opening_cash="100",
        )
        entry = workflow.record_manual_cash_entry(
            session_id=session_id, direction="RECEIPT", amount="10",
            reference_no="BP-1", description="Polog",
        )
        closed = workflow.close_cash_session(
            session_id=session_id, counted_cash="110", note="Brez razlike",
        )

        self.assertEqual(opened["status"], "OPEN")
        self.assertEqual(entry["balance_after"], "110.00")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(
            [row["operation_type"] for row in transport.submissions],
            ["OpenCashSession", "RecordManualCashEntry", "CloseCashSession"],
        )

    def test_late_bank_payment_uses_same_durable_invoice_identity(self):
        transport = FakeCashierTransport()
        workflow = self.workflow(transport)
        invoice_id = str(uuid4())

        result = workflow.settle_unpaid_bank_invoice(
            invoice_id=invoice_id, method="BANK_TRANSFER"
        )

        operation = transport.submissions[0]
        self.assertEqual(operation["operation_type"], "SettleUnpaidBankInvoice")
        self.assertEqual(operation["aggregate_id"], invoice_id)
        self.assertEqual(operation["payload"], {
            "invoice_id": invoice_id, "expected_version": 1,
            "method": "BANK_TRANSFER",
        })
        self.assertEqual(result.invoice["invoice_number"], "MFD-PIS-1")

    def test_storno_uses_versioned_durable_command_and_reads_reversal(self):
        transport = FakeCashierTransport()
        workflow = self.workflow(transport)
        invoice_id = str(uuid4())

        result = workflow.storno_invoice(
            invoice_id=invoice_id, expected_version=4,
            reason="Napačno izdan račun",
        )

        operation = transport.submissions[0]
        self.assertEqual(operation["operation_type"], "StornoInvoice")
        self.assertEqual(operation["aggregate_id"], invoice_id)
        self.assertEqual(operation["expected_version"], 4)
        self.assertNotIn("return_stock", operation["payload"])
        self.assertEqual(result.invoice["invoice_number"], "MFD-PIS-1")
