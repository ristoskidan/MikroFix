"""Samodejni pregled pripravljenosti MikroFIX v28 pred ročnim testom."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from mikrofix_backup import backup_sqlite_file, verify_restore_roundtrip
from mikrofix_runtime import APP_VERSION, load_runtime
from mikrofix_printing import default_windows_printer, list_windows_printers
from mikrofix_workflows import (
    complete_service_order,
    create_buyback,
    create_service_order,
    ensure_workflow_schema,
    generate_buyback_pdf,
    generate_service_order_pdf,
    get_buyback,
    get_service_order,
)


APP_DIR = Path(__file__).resolve().parent
RUNTIME = load_runtime(APP_DIR)


def service_fixture() -> dict:
    return {
        "customer_name": "TEST Stranka",
        "customer_address": "Testna 1",
        "customer_phone": "040000000",
        "customer_email": "test@example.invalid",
        "device_model": "iPhone 11 Pro",
        "device_serial": "TEST-SERIAL-SERVICE",
        "device_imei": "000000000000011",
        "device_os": "TEST iOS",
        "device_storage": "256 GB",
        "customer_request": "Testna menjava zaslona.",
        "intake_condition": "Testna praska na okvirju.",
        "accessories": "brez opreme",
        "quote_eur": 120,
        "logistics_eur": 20,
        "expected_due": "1-2 delovna dneva",
        "contract_context": "MFT_TEREN",
        "immediate_start_requested": True,
        "withdrawal_loss_ack": True,
        "delivery_channel": "EMAIL_PDF",
        "microdetect_confirmed": True,
        "microdetect_at": "2026-08-27 12:00:00",
        "operator_code": "DR",
        "notes": "Samodejni test - ni stranka.",
    }


def buyback_fixture() -> dict:
    return {
        "seller_name": "TEST Prodajalec",
        "seller_tax_number": "12345678",
        "seller_birth_date": "1990-01-01",
        "seller_address": "Testna 2",
        "seller_contact": "040000001",
        "seller_iban": "SI56000000000000000",
        "identity_checked": True,
        "device_model": "iPhone 11 Pro",
        "device_serial": "TEST-SERIAL-BUYBACK",
        "device_imei": "000000000000012",
        "device_os": "TEST iOS",
        "device_storage": "256 GB",
        "color_capacity": "testna barva",
        "accessories": "naprava",
        "condition_text": "Testna naprava deluje.",
        "acquisition_method": "KUPLJENA_RABLJENA_FIZICNA_OSEBA",
        "acquisition_details": "Testna pogodba.",
        "evidence_type": "PREJŠNJA_POGODBA",
        "imei_check_provider": "TESTNI SIMULATOR",
        "imei_check_result": "TEST_CLEAR",
        "imei_check_reference": "TEST-READY-001",
        "imei_checked_at": "2026-08-27 12:00:00",
        "account_removed": True,
        "activation_lock_removed": True,
        "mdm_clear": True,
        "ownership_declaration": True,
        "privacy_notice_received": True,
        "risk_level": "GREEN",
        "risk_reason": "Samodejni test.",
        "purchase_price": 250,
        "payout_method": "BANK_TRANSFER",
        "operator_code": "DR",
        "microdetect_confirmed": True,
        "microdetect_at": "2026-08-27 12:00:00",
        "notes": "Ni dejanski odkup.",
    }


def record(checks: list[dict], name: str, passed: bool, detail: str) -> None:
    checks.append({"check": name, "passed": bool(passed), "detail": detail})
    label = "OK" if passed else "NAPAKA"
    print(f"[{label}] {name}: {detail}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    checks: list[dict] = []
    record(checks, "Različica", APP_VERSION == "28.0-dev14", APP_VERSION)
    record(checks, "TEST okolje", RUNTIME.is_test, RUNTIME.settings["environment"])
    record(
        checks, "FURS produkcija",
        not bool(RUNTIME.settings["furs"].get("enabled")),
        "izklopljena" if not RUNTIME.settings["furs"].get("enabled") else "NEVARNO VKLOPLJENA",
    )

    try:
        import reportlab  # noqa: F401
        import requests  # noqa: F401
        import cryptography  # noqa: F401
        import win32print  # noqa: F401
        record(
            checks, "Osnovne knjižnice", True,
            "reportlab, requests, cryptography, pywin32",
        )
    except ImportError as exc:
        record(checks, "Osnovne knjižnice", False, str(exc))

    printers = list_windows_printers()
    physical = [item for item in printers if not item.is_virtual]
    default_printer = default_windows_printer()
    record(
        checks,
        "Windows tiskalniki",
        bool(physical),
        (
            f"{len(physical)} fizičnih; privzeti: {default_printer or 'ni nastavljen - izbira v programu'}"
            if printers else "Windows ni vrnil nameščenega tiskalnika"
        ),
    )

    try:
        inventory = RUNTIME.connect_sqlite("inventory", read_only=True)
        count, pieces = inventory.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(stock_qty),0)
            FROM inventory_items WHERE barcode LIKE 'TEST-IP11P-%'
            """
        ).fetchone()
        negative = inventory.execute(
            "SELECT COUNT(*) FROM inventory_items WHERE stock_qty < 0"
        ).fetchone()[0]
        inventory.close()
        record(
            checks, "Testna zaloga iPhone 11 Pro",
            int(count) >= 8 and int(pieces) >= 35 and int(negative) == 0,
            f"{count} artiklov, {pieces} kosov, negativnih zalog {negative}",
        )
    except Exception as exc:
        record(checks, "Testna zaloga iPhone 11 Pro", False, str(exc))

    try:
        test_environment = os.environ.copy()
        # Jedrni testi vedno preverjajo priloženo privzeto TEST konfiguracijo,
        # ne morebitne začasne razvojne preusmeritve podatkovne mape.
        test_environment.pop("MIKROFIX_CONFIG", None)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(APP_DIR), "-p", "test_*.py"],
            cwd=APP_DIR,
            env=test_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        tail = (completed.stdout + completed.stderr).strip().splitlines()[-1]
        record(checks, "Avtomatski testi", completed.returncode == 0, tail)
    except Exception as exc:
        record(checks, "Avtomatski testi", False, str(exc))

    try:
        with tempfile.TemporaryDirectory(prefix="mikrofix_ready_") as raw_dir:
            base = Path(raw_dir)
            database = base / "workflow.db"
            connection = sqlite3.connect(database)
            ensure_workflow_schema(connection)
            service_id, _ = create_service_order(connection, service_fixture())
            buyback_id, _ = create_buyback(connection, buyback_fixture(), test_mode=True)
            service_pdf = generate_service_order_pdf(
                get_service_order(connection, service_id), base / "pdf", test_mode=True
            )
            buyback_pdf = generate_buyback_pdf(
                get_buyback(connection, buyback_id), base / "pdf", test_mode=True
            )
            complete_service_order(connection, service_id, 999)
            connection.close()
            pdf_ok = all(
                p.is_file() and p.stat().st_size > 5_000 and p.read_bytes()[:4] == b"%PDF"
                for p in (service_pdf, buyback_pdf)
            )
            backup = backup_sqlite_file(database, base / "backup.db")
            restored = verify_restore_roundtrip(backup, base / "restored.db")
            restored_connection = sqlite3.connect(restored)
            restored_counts = (
                restored_connection.execute("SELECT COUNT(*) FROM service_orders").fetchone()[0],
                restored_connection.execute("SELECT COUNT(*) FROM buybacks").fetchone()[0],
            )
            restored_connection.close()
            record(
                checks, "Dokumentni tok in obnovitev",
                pdf_ok and restored_counts == (1, 1),
                f"2 PDF, obnovljeni zapisi {restored_counts}",
            )
    except Exception as exc:
        record(checks, "Dokumentni tok in obnovitev", False, str(exc))

    passed = all(item["passed"] for item in checks)
    report_dir = RUNTIME.service_data_dir / "test_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"pripravljenost_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "version": APP_VERSION,
                "result": "READY_FOR_MANUAL_TEST" if passed else "NOT_READY",
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nPoročilo: {report_path}")
    print("REZULTAT: " + ("PRIPRAVLJENO ZA ROČNI TEST" if passed else "NAJPREJ ODPRAVI NAPAKE"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
