"""Varno predprodukcijsko preverjanje Linux pretvorbe MikroFIX."""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mikrofix_device import find_usbmuxd_binary


APP_DIR = Path(__file__).resolve().parent


def check(label: str, ok: bool, detail: str) -> bool:
    print(f"[{'OK' if ok else 'NAPAKA'}] {label}: {detail}")
    return bool(ok)


def command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15, check=False,
    )


def main() -> int:
    results: list[bool] = []
    results.append(check("Operacijski sistem", sys.platform.startswith("linux"), platform.platform()))
    results.append(check(
        "Python", sys.version_info[:2] == (3, 13) and sys.maxsize > 2**32,
        f"{platform.python_version()} {'64-bit' if sys.maxsize > 2**32 else '32-bit'}",
    ))
    for module in (
        "tkinter", "reportlab", "PIL", "fitz", "segno", "cryptography",
        "requests", "pymobiledevice3",
    ):
        try:
            importlib.import_module(module)
            results.append(check(f"Python dodatek {module}", True, "nameščen"))
        except Exception as exc:
            results.append(check(f"Python dodatek {module}", False, str(exc)))

    cups = command("lpstat", "-r") if shutil.which("lpstat") else None
    results.append(check(
        "CUPS", bool(cups and cups.returncode == 0),
        (cups.stdout or cups.stderr).strip() if cups else "manjka lpstat/cups-client",
    ))
    listed = command("lpstat", "-v") if shutil.which("lpstat") else None
    results.append(check(
        "Tiskalniška vrsta", bool(listed and listed.returncode == 0 and listed.stdout.strip()),
        listed.stdout.strip() if listed and listed.stdout.strip() else "tiskalnik še ni dodan",
    ))

    mux_binary = find_usbmuxd_binary()
    mux_socket = Path("/var/run/usbmuxd").exists()
    mux = command("systemctl", "is-active", "usbmuxd") if shutil.which("systemctl") else None
    mux_active = mux_socket or bool(mux and mux.stdout.strip() == "active")
    results.append(check(
        "usbmuxd", bool(mux_binary),
        "nameščen in aktiven" if mux_active else
        "nameščen; aktivira se ob priklopu iPhona" if mux_binary else "usbmuxd ni najden",
    ))
    results.append(check(
        "Secret Service", shutil.which("secret-tool") is not None,
        "secret-tool je na voljo" if shutil.which("secret-tool") else "namesti libsecret-tools",
    ))

    settings = json.loads((APP_DIR / "mikrofix_settings_linux.example.json").read_text(encoding="utf-8"))
    results.append(check(
        "FURS produkcija", not settings["furs"]["enabled"] and
        settings["furs"]["environment"] == "TEST" and
        not os.environ.get("MIKROFIX_ALLOW_FURS_PRODUCTION"),
        "izključena",
    ))
    private_files = list(APP_DIR.rglob("*.p12")) + list(APP_DIR.rglob("*.pfx"))
    results.append(check(
        "Zasebna potrdila", not private_files,
        "niso vključena" if not private_files else ", ".join(map(str, private_files)),
    ))

    with tempfile.TemporaryDirectory(prefix="mikrofix_linux_test_") as temp_dir:
        config = Path(temp_dir) / "settings.json"
        config.write_text(json.dumps({
            "environment": "TEST",
            "database": {"backend": "sqlite", "data_dir": str(Path(temp_dir) / "data")},
            "furs": {"enabled": False, "environment": "TEST", "certificate_path": ""},
        }), encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "MIKROFIX_CONFIG": str(config),
            "MIKROFIX_SKIP_FILE_PROTECTION": "1",
            "MIKROFIX_SMOKE_TEST": "1",
        })
        tests = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"], cwd=APP_DIR, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        output = (tests.stdout + tests.stderr).strip().splitlines()
        results.append(check(
            "Avtomatski testi", tests.returncode == 0,
            output[-1] if output else "brez izpisa",
        ))

    print("\nREZULTAT: " + ("LINUX TEST JE PRIPRAVLJEN" if all(results) else "ODPRAVI OZNAČENE NAPAKE"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
