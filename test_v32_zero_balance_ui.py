import inspect
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from mikrofix_v30_pos import PosError, PosLine, generate_pos_preview_pdfs
from mikrofix_v30_pos_ui import PosWindow, zero_balance_preview_lines


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Cashier:
    def __init__(self):
        self.calls = []

    def issue_service_invoice(self, **payload):
        self.calls.append(payload)
        return SimpleNamespace(
            operation_id="operation-1",
            invoice={
                "invoice_id": "invoice-1", "invoice_number": "MFD-PIS-1",
                "total_amount": "0.00", "status": "ISSUED",
            },
        )


class _ReadyServiceCashier:
    def ready_service_drafts(self):
        return [{
            "draft_id": "draft-1",
            "service_order_id": "11111111-1111-1111-1111-111111111111",
            "status": "READY",
            "created_at": "2026-09-19T10:15:00+02:00",
            "locked_payload": {
                "service_no": "MF-SN-2026-0042",
                "customer": {"name": "Testna stranka"},
                "service": {
                    "device_model": "Prenosnik",
                    "device_serial": "SER-42",
                },
                "billing_lines": [{"quantity": "1", "unit_price": "30.00"}],
                "parts": [{"quantity": "2", "unit_price": "12.50"}],
            },
        }]


class ZeroBalanceUiPolicyTests(unittest.TestCase):
    def lines(self):
        return [
            PosLine(
                line_kind="ARTICLE", name="Zaslon OLED iPhone 11",
                quantity=Decimal("1"), base_unit_price=Decimal("110"),
                acquisition_cost=Decimal("50"), source_locked=True,
                central_line_id="11111111-1111-1111-1111-111111111111",
            ),
            PosLine(
                line_kind="SERVICE", name="T2", service_tier="T2",
                quantity=Decimal("1"), base_unit_price=Decimal("30"),
                source_locked=True,
                central_line_id="22222222-2222-2222-2222-222222222222",
            ),
        ]

    def test_preview_keeps_all_positive_lines_then_all_negative_lines(self):
        result = zero_balance_preview_lines(self.lines())
        self.assertEqual([line.name for line in result], [
            "Zaslon OLED iPhone 11", "T2", "Zaslon OLED iPhone 11", "T2",
        ])
        self.assertEqual(
            [line.quantity for line in result],
            [Decimal("1"), Decimal("1"), Decimal("-1"), Decimal("-1")],
        )
        self.assertTrue(all(line.source_locked for line in result))
        self.assertTrue(all(
            line.origin_kind == "ZERO_BALANCE" for line in result[2:]
        ))

    def test_preview_rejects_unlocked_or_nonpositive_source_lines(self):
        unlocked = PosLine(
            line_kind="SERVICE", name="T2", service_tier="T2",
            quantity=Decimal("1"), base_unit_price=Decimal("30"),
        )
        with self.assertRaises(PosError):
            zero_balance_preview_lines([unlocked])
        with self.assertRaises(PosError):
            zero_balance_preview_lines([])

    def test_zero_balance_payment_payload_is_empty(self):
        window = PosWindow.__new__(PosWindow)
        window.central_mode = True
        window.central_service_draft_id = "draft-1"
        window.service_settlement_mode = _Value("ZERO_BALANCE")
        self.assertEqual(window._central_payment_payload(), [])
        self.assertEqual(window._checkout_total(), Decimal("0.00"))

    def test_direct_sale_cannot_select_zero_balance(self):
        window = PosWindow.__new__(PosWindow)
        window.central_mode = True
        window.central_service_draft_id = None
        window.service_settlement_mode = _Value("ZERO_BALANCE")
        window.total = Decimal("25.00")
        self.assertFalse(window._zero_balance_selected())
        self.assertEqual(window._checkout_total(), Decimal("25.00"))

    def test_issue_sends_only_zero_mode_and_empty_payments(self):
        window = PosWindow.__new__(PosWindow)
        cashier = _Cashier()
        window.central_cashier = cashier
        window.central_operation_id = None
        window.central_service_draft_id = "draft-1"
        window.central_mode = True
        window.service_settlement_mode = _Value("ZERO_BALANCE")
        window.runtime = SimpleNamespace(electronic_device="PIS")
        window.selected_premise = lambda: "MFD"
        window._central_billing_customer = lambda: {
            "source": "SERVICE_CUSTOMER", "confirmed": True,
        }
        window.header_status = _Value("")
        window.premise_combo = SimpleNamespace(configure=lambda **_kwargs: None)
        window.refresh_stock = lambda: None
        window._make_original_pdfs_and_print = lambda: None
        window._issue_central_invoice()
        self.assertEqual(len(cashier.calls), 1)
        self.assertEqual(cashier.calls[0]["payments"], [])
        self.assertEqual(cashier.calls[0]["settlement_mode"], "ZERO_BALANCE")
        self.assertNotIn("lines", cashier.calls[0])

    def test_zero_balance_preview_pdf_contains_auditable_lines(self):
        lines = zero_balance_preview_lines(self.lines())
        with tempfile.TemporaryDirectory() as directory:
            a4, _receipt = generate_pos_preview_pdfs(
                lines=lines, payments={}, customer={"name": "Test"},
                labor_floor=22, output_dir=Path(directory), company={
                    "legal_name": "MikroFix", "short_name": "MikroFix",
                }, premise="MFD", device="PIS", token="zero-preview",
            )
            from pypdf import PdfReader
            text = "\n".join(
                page.extract_text() or "" for page in PdfReader(str(a4)).pages
            )
        self.assertEqual(text.count("Zaslon OLED iPhone 11"), 2)
        self.assertEqual(text.count("T2"), 2)
        self.assertIn("-110.00", text)
        self.assertIn("-30.00", text)
        self.assertIn("SKUPAJ: 0.00 EUR", text)

    def test_ui_wires_explicit_choice_and_settlement_mode_to_runtime(self):
        source = inspect.getsource(PosWindow)
        self.assertIn("IZRAVNALNI RAČUN 0,00 EUR", source)
        self.assertIn("settlement_mode=self.service_settlement_mode.get()", source)
        self.assertIn('"service_settlement_mode": self.service_settlement_mode.get()', source)
        self.assertIn('payload.get("service_settlement_mode")', source)

    def test_ready_devices_use_locked_identity_status_and_decimal_total(self):
        window = PosWindow.__new__(PosWindow)
        window.central_mode = True
        window.central_cashier = _ReadyServiceCashier()

        rows = window.ready_services()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service_no"], "MF-SN-2026-0042")
        self.assertEqual(rows[0]["status"], "PRIPRAVLJENA ZA OBRAČUN")
        self.assertEqual(rows[0]["total"], "55.00 EUR")
        self.assertEqual(rows[0]["serial"], "SER-42")

        draft = _ReadyServiceCashier().ready_service_drafts()[0]
        draft["locked_payload"].pop("service_no")
        draft["locked_payload"]["order_no"] = "MF-SN-2026-0043"
        window.central_cashier = SimpleNamespace(
            ready_service_drafts=lambda: [draft]
        )
        self.assertEqual(
            window.ready_services()[0]["service_no"], "MF-SN-2026-0043"
        )
        draft["locked_payload"].pop("order_no")
        self.assertEqual(
            window.ready_services()[0]["service_no"], draft["service_order_id"]
        )

    def test_ready_devices_dialog_keeps_legacy_rows_compatible(self):
        source = inspect.getsource(PosWindow.choose_ready_service)
        self.assertIn('dialog.title("Naprave za prevzem")', source)
        self.assertIn('"status", "total"', source)
        self.assertIn('row.get("status", "")', source)
        self.assertIn('row.get("total", "")', source)


if __name__ == "__main__":
    unittest.main()
