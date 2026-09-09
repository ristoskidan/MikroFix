"""Fail-closed production readiness check for the Linux workstation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mikrofix_backup_runtime import load_backup_passphrase
from mikrofix_data_encryption import is_gocryptfs_mount, unmount_encrypted_data
from mikrofix_printing import preferred_a4_printer, preferred_receipt_printer
from mikrofix_runtime import load_runtime
from mikrofix_v30_backup import inspect_backup, latest_daily_backup


APP_DIR = Path(__file__).resolve().parent
RELEASE_MANIFEST = APP_DIR / "production-release.json"


def source_digest() -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in APP_DIR.glob("*.py")
        if not path.name.startswith("test_") and path.name != "nastavi_produkcijsko_zascito.py"
    )
    for path in files:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _db_integrity(path: Path) -> bool:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def _service_state(name: str, state: str) -> bool:
    completed = subprocess.run(
        ["systemctl", state, name], capture_output=True, text=True, timeout=10, check=False,
    )
    return completed.returncode == 0


def collect() -> list[tuple[bool, str, str]]:
    settings_path = APP_DIR / "mikrofix_settings.json"
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    data_dir = Path(raw["database"]["data_dir"]).expanduser().resolve()
    was_mounted = is_gocryptfs_mount(data_dir)
    runtime = load_runtime(APP_DIR)
    results: list[tuple[bool, str, str]] = []
    try:
        def add(ok: bool, label: str, detail: str) -> None:
            results.append((bool(ok), label, detail))

        add(runtime.settings["environment"] == "PRODUCTION", "Način", runtime.settings["environment"])
        encrypted = is_gocryptfs_mount(runtime.data_dir)
        add(encrypted, "Šifrirani podatki", "gocryptfs priklopljen" if encrypted else "ni priklopljen")
        for name in ("service", "auth", "inventory", "office"):
            path = runtime.db_path(name)
            add(path.is_file() and _db_integrity(path), f"Baza {name}", path.name)

        furs = runtime.settings["furs"]
        cert = Path(str(furs.get("certificate_path") or "")).expanduser()
        furs_ok = bool(
            furs.get("enabled") and furs.get("environment") == "PRODUCTION"
            and furs.get("production_activation_confirmed") and cert.is_file()
        )
        add(furs_ok, "FURS produkcija", "aktivirana; namensko potrdilo obstaja")

        business = runtime.settings["business"]
        readiness = dict(runtime.settings.get("production_readiness") or {})
        expected_pair = f"{business['active_premise']}/{business['electronic_device']}"
        add(
            bool(readiness.get("furs_premise_confirmed"))
            and readiness.get("furs_premise_pair") == expected_pair,
            "eDavki prostor/naprava",
            expected_pair if readiness.get("furs_premise_pair") == expected_pair
            else f"čaka potrditev zapisa {expected_pair}",
        )

        pending = 0
        office = runtime.connect_sqlite("office", read_only=True)
        try:
            table = office.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='furs_outbox_v30'"
            ).fetchone()
            if table:
                pending = int(office.execute(
                    "SELECT COUNT(*) FROM furs_outbox_v30 WHERE status='PENDING_RECONCILIATION'"
                ).fetchone()[0])
        finally:
            office.close()
        add(pending == 0, "FURS čakalna vrsta", f"{pending} nepotrjenih")

        backup_rule = runtime.settings["backup"]
        backup = latest_daily_backup(backup_rule.get("target_dir") or "")
        try:
            info = inspect_backup(backup, load_backup_passphrase(runtime.data_dir)) if backup else None
            backup_ok = bool(info)
            backup_detail = f"{backup.name}; {info.file_count} datotek" if info else "manjka"
        except Exception as exc:
            backup_ok, backup_detail = False, str(exc)
        add(backup_ok, "Šifrirani zunanji backup", backup_detail)

        thermal = preferred_receipt_printer(runtime.settings["printer"].get("printer_name", ""))
        a4 = preferred_a4_printer(runtime.settings["printer"].get("a4_printer_name", ""))
        add(bool(thermal and readiness.get("thermal_print_confirmed")), "Termični tisk", thermal or "ni najden")
        add(bool(a4 and readiness.get("a4_print_confirmed")), "A4 tisk", a4 or "ni najden")

        add(
            _service_state("mikrofix-sata-performance.service", "is-enabled")
            and _service_state("mikrofix-sata-performance.service", "is-active"),
            "SATA stabilnost", "max_performance ob zagonu",
        )

        try:
            release = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
            release_ok = (
                release.get("status") == "APPROVED"
                and release.get("source_sha256") == source_digest()
                and int(release.get("tests_passed", 0)) >= 150
            )
            release_detail = (
                f"{release.get('status', 'BREZ STATUSA')}; "
                f"{release.get('tests_passed', 0)} testov; {release.get('created_at', '-') }"
            )
        except Exception:
            release_ok, release_detail = False, "končni test po zadnjih spremembah še ni zabeležen"
        add(release_ok, "Izdajni preizkus", release_detail)

        filesystem = subprocess.run(
            ["findmnt", "-T", str(backup_rule.get("target_dir") or "/"), "-n", "-o", "FSTYPE"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        add(filesystem == "ext4", "Datotečni sistem backupa", filesystem or "neznan")
    finally:
        if not was_mounted and is_gocryptfs_mount(runtime.data_dir):
            unmount_encrypted_data(runtime.data_dir)
    return results


def main() -> int:
    print("MikroFIX – Linux produkcijski predpregled")
    print("=" * 48)
    try:
        results = collect()
    except Exception as exc:
        print(f"[NAPAKA] Predpregled: {exc}")
        return 1
    for ok, label, detail in results:
        print(f"[{'OK' if ok else 'ODPRTO'}] {label}: {detail}")
    ready = all(ok for ok, _, _ in results)
    print("\nREZULTAT: " + ("PRIPRAVLJEN ZA PRODUKCIJO" if ready else "PRODUKCIJA ŠE NI ODKLENJENA"))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
