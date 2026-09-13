import unittest
from pathlib import Path

from mikrofix_central_office_ui import (
    central_invoice_detail_text,
    central_office_rows,
    central_partner_payload,
    normalized_central_office_tab,
)


class CentralOfficePresentationTests(unittest.TestCase):
    def test_main_shell_routes_configured_office_to_central_window(self):
        source = (Path(__file__).parent / "mikrofix.py").read_text(encoding="utf-8")
        launch = source[source.index("def launch_office("):source.index("def launch_pos(")]
        self.assertIn('if central_url and "--new-invoice" not in arguments:', launch)
        self.assertIn("build_central_office(", launch)
        self.assertIn("open_central_office(", launch)

    def test_legacy_tab_names_route_to_safe_central_views(self):
        self.assertEqual(normalized_central_office_tab("stock"), "inventory")
        self.assertEqual(normalized_central_office_tab("documents"), "receipts")
        self.assertEqual(normalized_central_office_tab("unknown"), "inventory")

    def test_inventory_rows_expose_no_acquisition_cost(self):
        rows = central_office_rows("inventory", [{
            "article_id": "a-1", "description": "Zaslon", "main_barcode": "MAIN",
            "supplier_barcode": "SUP", "article_quantity_available": "2",
            "current_sale_price": "44", "blocked": False,
            "landed_unit_cost": "20",
        }])
        self.assertEqual(rows[0][1], "Zaslon")
        self.assertEqual(rows[0][5], "44.00 €")
        self.assertNotIn("20", rows[0])

    def test_receipt_and_partner_states_are_human_readable(self):
        receipt = central_office_rows("receipts", [{
            "receipt_id": "r-1", "receipt_number": "DOB-2026-1",
            "received_date": "2026-09-13", "supplier": {"name": "Dobavitelj"},
            "external_document_no": "D-1", "document_total": "10",
            "status": "PENDING_INVOICE",
        }])[0]
        partner = central_office_rows("partners", [{
            "partner_id": "p-1", "name": "Kupec", "active": False,
        }])[0]
        self.assertEqual(receipt[-1], "ČAKA NA RAČUN")
        self.assertEqual(partner[-1], "NEAKTIVEN")

    def test_invoice_detail_is_business_readable_not_raw_json(self):
        text = central_invoice_detail_text({
            "invoice_number": "MFD-PIS-7", "issued_at": "2026-09-13T12:00:00+02:00",
            "status": "ISSUED", "payment_status": "PAID", "furs_eor": "EOR-1",
            "snapshot": {
                "billing_customer": {"name": "Janez Novak", "post_city": "Ptuj"},
                "lines": [{
                    "description": "Zaslon", "quantity": "1",
                    "unit_price": "50.00", "line_total": "50.00",
                }],
                "payments": [{"method": "CARD", "amount": "50.00"}],
                "total_amount": "50.00",
            },
        })
        self.assertIn("RAČUN: MFD-PIS-7", text)
        self.assertIn("Kupec: Janez Novak", text)
        self.assertIn("• Zaslon", text)
        self.assertNotIn("{", text)

    def test_partner_edit_keeps_identity_and_expected_version(self):
        payload = central_partner_payload(
            {"name": "Popravljen partner", "country": "Slovenija"},
            existing={"partner_id": "partner-1", "row_version": 7},
        )
        self.assertEqual(payload["partner_id"], "partner-1")
        self.assertEqual(payload["expected_version"], 7)
        self.assertTrue(payload["review_confirmed"])
        self.assertEqual(payload["verification_source"], "MANUAL_REVIEW")

    def test_new_partner_gets_stable_uuid_for_one_command(self):
        payload = central_partner_payload({"name": "Novi partner"})
        self.assertEqual(len(payload["partner_id"]), 36)
        self.assertNotIn("expected_version", payload)


if __name__ == "__main__":
    unittest.main()
