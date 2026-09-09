from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mikrofix_backup_runtime as backup_runtime
import mikrofix_device as device
import mikrofix_printing as printing
import mikrofix_platform as platform_helpers
from mikrofix_v30_pos import ensure_pos_schema


def completed(args, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class LinuxPrintingTests(unittest.TestCase):
    @patch.object(printing, "_is_windows", return_value=False)
    @patch.object(printing, "_cups_command")
    def test_cups_printers_are_parsed_and_virtual_queues_excluded(self, command, _platform):
        command.return_value = completed([], stdout=(
            "device for EPSON_TM_T20II: usb://EPSON/TM-T20II\n"
            "device for PDF: cups-pdf:/\n"
        ))
        rows = printing.list_printers()
        self.assertEqual([row.name for row in rows], ["EPSON_TM_T20II", "PDF"])
        self.assertFalse(rows[0].is_virtual)
        self.assertTrue(rows[1].is_virtual)

    @patch.object(printing, "_is_windows", return_value=False)
    @patch.object(printing, "default_cups_printer", return_value="")
    @patch.object(printing, "list_cups_printers")
    def test_epson_thermal_queue_is_preferred(self, printers, _default, _platform):
        printers.return_value = [
            printing.PrinterInfo("Office", "ipp://office", "CUPS", False),
            printing.PrinterInfo("EPSON_TM_T20II", "usb://EPSON/TM-T20II", "CUPS", False),
        ]
        self.assertEqual(printing.preferred_printer(), "EPSON_TM_T20II")

    @patch.object(printing, "list_printers")
    def test_a4_and_receipt_printers_are_routed_separately(self, printers):
        printers.return_value = [
            printing.PrinterInfo("HP-LaserJet-1020", "usb://HP/LaserJet%201020", "CUPS", False),
            printing.PrinterInfo("TM-T20II", "usb://EPSON/TM-T20II", "CUPS", False),
        ]
        self.assertEqual(printing.preferred_a4_printer(), "HP-LaserJet-1020")
        self.assertEqual(printing.preferred_receipt_printer(), "TM-T20II")

    @patch.object(printing, "_is_windows", return_value=False)
    @patch.object(printing, "list_cups_printers")
    @patch.object(printing, "_cups_command")
    def test_cups_submission_requires_completed_job(self, command, printers, _platform):
        printers.return_value = [
            printing.PrinterInfo("Office", "usb://HP/LaserJet", "CUPS", False)
        ]
        command.side_effect = [
            completed([], stdout="request id is EPSON_TM_T20II-42 (1 file(s))\n"),
            completed([], code=1),
            completed([], stdout="EPSON_TM_T20II-42 user 100 Thu 01 Jan 00:00\n"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            pdf = Path(raw) / "racun.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = printing.send_pdf_to_printer(pdf, "Office")
        self.assertTrue(result.completed)
        self.assertEqual(result.job_id, 42)

    def test_escpos_raster_has_valid_dimensions_and_header(self):
        from PIL import Image
        payload = printing._image_to_escpos_raster(Image.new("1", (576, 10), 1))
        self.assertEqual(payload[:4], b"\x1d\x76\x30\x00")
        self.assertEqual(payload[4:8], bytes((72, 0, 10, 0)))
        self.assertEqual(len(payload), 8 + 72 * 10)

    def test_escpos_white_stays_unprinted_and_black_prints(self):
        from PIL import Image
        white = printing._image_to_escpos_raster(Image.new("L", (8, 1), 255))
        black = printing._image_to_escpos_raster(Image.new("L", (8, 1), 0))
        self.assertEqual(white[8], 0x00)
        self.assertEqual(black[8], 0xFF)


class LinuxBackupSecretTests(unittest.TestCase):
    @patch.object(backup_runtime, "_is_windows", return_value=False)
    @patch.object(backup_runtime, "_save_linux_secret")
    def test_linux_marker_never_contains_passphrase(self, save_secret, _platform):
        with tempfile.TemporaryDirectory() as raw:
            path = backup_runtime.save_backup_passphrase(raw, "zelo-dolgo-testno-geslo")
            self.assertTrue(path.is_file())
            self.assertNotIn("zelo-dolgo-testno-geslo", path.read_text(encoding="utf-8"))
            save_secret.assert_called_once()


class LinuxUsbMuxTests(unittest.TestCase):
    @patch.object(device.Path, "is_file", return_value=True)
    @patch.object(device.shutil, "which", return_value=None)
    def test_usbmuxd_is_found_in_system_sbin_outside_user_path(self, _which, is_file):
        self.assertEqual(device.find_usbmuxd_binary(), "/usr/sbin/usbmuxd")
        is_file.assert_called_once()


class LinuxDesktopHelpersTests(unittest.TestCase):
    @patch.object(platform_helpers.subprocess, "Popen")
    def test_open_local_file_is_non_blocking(self, popen):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "predogled.pdf"
            target.write_bytes(b"%PDF-1.4\n%%EOF\n")
            platform_helpers.open_local_file(target)
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class PosBootstrapTests(unittest.TestCase):
    def test_pos_schema_bootstraps_an_empty_office_database(self):
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "office.db"
            ensure_pos_schema(database)
            import sqlite3
            with sqlite3.connect(database) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                document_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(documents)")
                }
            self.assertTrue({"partners", "documents", "document_items"} <= tables)
            self.assertIn("pos_issue_token", document_columns)


if __name__ == "__main__":
    unittest.main()
