import unittest
from unittest.mock import patch

from mikrofix_printing import PrinterInfo, preferred_windows_printer
from mikrofix_v30_pos import payment_method_label, payment_status_label


class V30PrintingTests(unittest.TestCase):
    @patch("mikrofix_printing.default_windows_printer", return_value="")
    @patch("mikrofix_printing.list_windows_printers")
    def test_epson_tm_t20_is_selected_before_other_physical_printers(
        self, list_printers, _default
    ):
        list_printers.return_value = [
            PrinterInfo("HP LaserJet 1020", "USB001", "HP LaserJet", False),
            PrinterInfo(
                "EPSON TM-T20II Receipt", "ESDPRT001",
                "EPSON TM-T20II Receipt5", False,
            ),
            PrinterInfo(
                "Microsoft Print to PDF", "PORTPROMPT:",
                "Microsoft Print To PDF", True,
            ),
        ]
        self.assertEqual(
            preferred_windows_printer(), "EPSON TM-T20II Receipt"
        )

    def test_invoice_labels_are_in_slovenian(self):
        self.assertEqual(payment_method_label("CASH"), "Gotovina")
        self.assertEqual(
            payment_method_label("BANK_TRANSFER"), "Bančno nakazilo (TRR)"
        )
        self.assertEqual(payment_status_label("PAID"), "Plačano")


if __name__ == "__main__":
    unittest.main()
