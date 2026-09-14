import unittest
from uuid import UUID

from mikrofix_central_receipt_ui import (
    attach_invoice_form_payload,
    confirmed_attach_invoice_payload,
    confirmed_receipt_payload,
    new_receipt_form_payload,
)
from mikrofix_sync_contracts import (
    validated_attach_receipt_invoice_payload,
    validated_confirm_receipt_payload,
)


class CentralReceiptPayloadTests(unittest.TestCase):
    def test_new_receipt_has_stable_document_and_operation_id(self):
        form = new_receipt_form_payload("2026-09-14")
        UUID(form["receipt_id"])
        UUID(form["local_operation_id"])
        self.assertEqual(form["document_date"], "2026-09-14")
        self.assertEqual(form["received_date"], "2026-09-14")
        self.assertEqual(form["lines"], [])

        posted = confirmed_receipt_payload(form)
        self.assertNotIn("local_operation_id", posted)
        self.assertTrue(posted["document_review_confirmed"])
        self.assertEqual(posted["invoice_due_date"], "")
        self.assertEqual(posted["invoice_payment_status"], "")

    def test_dra_keeps_invoice_fields(self):
        form = new_receipt_form_payload("2026-09-14")
        form.update({
            "document_type": "DRA", "invoice_due_date": "2026-10-14",
            "invoice_payment_status": "NEPLACAN",
        })
        posted = confirmed_receipt_payload(form)
        self.assertEqual(posted["invoice_due_date"], "2026-10-14")
        self.assertEqual(posted["invoice_payment_status"], "NEPLACAN")

    def test_completed_form_matches_the_server_receipt_contract(self):
        form = new_receipt_form_payload("2026-09-14")
        form.update({
            "supplier_id": "cebcfa42-6338-4168-92de-b504645ece14",
            "supplier_expected_version": 1, "external_document_no": "DOB-UI-1",
            "document_total": "12.20", "shipping_total": "0",
            "lines": [{
                "line_id": "d91c57d4-779c-4e27-81fb-da04f825198e",
                "article_id": "61a9f829-3f24-4d6a-bfd7-b50593e7c1fc",
                "quantity": "1", "net_unit_price": "10",
                "discount_percent": "0", "vat_rate": "22",
                "supplier_line_total": "12.20", "confirmed_sale_price": "15",
                "supplier_barcode": "SUP-UI-1", "lot_serial": "",
            }],
        })
        normalized = validated_confirm_receipt_payload(
            confirmed_receipt_payload(form)
        )
        self.assertEqual(normalized["document_total"], "12.20")
        self.assertEqual(normalized["lines"][0]["quantity"], "1.0000")

    def test_attached_invoice_reuses_immutable_receipt_lines(self):
        receipt = {
            "receipt_id": "60eed1c0-14de-427f-bfac-57f8d4606c59",
            "row_version": 1, "document_total": "12.20",
            "shipping_total": "0.0000", "lines": [{
                "line_id": "d91c57d4-779c-4e27-81fb-da04f825198e",
                "article_id": "61a9f829-3f24-4d6a-bfd7-b50593e7c1fc",
                "quantity": "1.0000", "net_unit_price": "10.0000",
                "discount_percent": "0.0000", "vat_rate": "22.0000",
                "supplier_line_total": "12.20", "confirmed_sale_price": "15.00",
                "supplier_barcode": "SUP-1", "lot_serial": "",
                "calculated_total": "12.20", "allocated_shipping": "0.00",
            }],
        }
        form = attach_invoice_form_payload(receipt)
        self.assertEqual(form["expected_version"], 1)
        self.assertEqual(form["lines"][0]["supplier_barcode"], "SUP-1")
        self.assertNotIn("calculated_total", form["lines"][0])
        posted = confirmed_attach_invoice_payload(form)
        self.assertNotIn("local_operation_id", posted)
        self.assertTrue(posted["document_review_confirmed"])
        form.update({
            "invoice_external_no": "PRA-UI-1",
            "invoice_date": "2026-09-14", "invoice_due_date": "2026-10-14",
        })
        normalized = validated_attach_receipt_invoice_payload(
            confirmed_attach_invoice_payload(form)
        )
        self.assertEqual(normalized["expected_version"], 1)


if __name__ == "__main__":
    unittest.main()
