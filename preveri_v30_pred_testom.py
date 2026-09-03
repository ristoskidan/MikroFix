"""Samodejni, neprodukcijski pregled MikroFIX v30 pred ročnim testom."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent


def check(name, condition, detail):
    print(f"[{'OK' if condition else 'NAPAKA'}] {name}: {detail}")
    return bool(condition)


def integrity(path):
    connection = sqlite3.connect(str(path))
    value = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    return str(value).lower() == "ok"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = []
    results.append(check(
        "Python", sys.version_info[:2] == (3, 13) and sys.maxsize > 2**32,
        f"{sys.version.split()[0]} {'64-bit' if sys.maxsize > 2**32 else '32-bit'}",
    ))
    for module in (
        "tkinter", "reportlab", "PIL", "fitz", "segno", "cryptography",
        "requests", "win32print", "pymobiledevice3",
    ):
        try:
            importlib.import_module(module)
            results.append(check(f"Dodatek {module}", True, "nameščen"))
        except Exception as exc:
            results.append(check(f"Dodatek {module}", False, str(exc)))

    with tempfile.TemporaryDirectory(prefix="mikrofix_v30_check_") as raw_temp:
        temp = Path(raw_temp)
        config = temp / "settings.json"
        config.write_text(json.dumps({
            "environment": "TEST",
            "database": {"backend": "sqlite", "data_dir": str(temp / "data"), "server_url": ""},
        }), encoding="utf-8")
        env = os.environ.copy()
        env["MIKROFIX_CONFIG"] = str(config)
        env["MIKROFIX_SKIP_FILE_PROTECTION"] = "1"
        env["MIKROFIX_SMOKE_TEST"] = "1"

        tests = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"], cwd=APP_DIR,
            env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
        )
        test_output = (tests.stdout + tests.stderr).strip().splitlines()
        results.append(check(
            "Avtomatski testi", tests.returncode == 0,
            test_output[-1] if test_output else "brez rezultata",
        ))

        smoke = subprocess.run(
            [sys.executable, "mikrofix.py"], cwd=APP_DIR, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        smoke_detail = "glavni meni + Office v isti seji"
        if smoke.returncode != 0:
            smoke_detail = (smoke.stdout + smoke.stderr).strip()[-800:]
        results.append(check("Windows/Tk zagon", smoke.returncode == 0, smoke_detail))

        db_dir = temp / "data" / "db"
        for name in ("mikrofix.db", "mikrofix_office.db", "mikrofix_inventory.db", "mikrofix_auth.db"):
            path = db_dir / name
            results.append(check(f"Testna baza {name}", path.is_file() and integrity(path), name))

    from mikrofix_runtime import APP_VERSION, DEFAULT_SETTINGS
    results.append(check("Različica", APP_VERSION.startswith("30."), APP_VERSION))
    results.append(check("FURS produkcija", not DEFAULT_SETTINGS["furs"]["enabled"], "izklopljena"))
    results.append(check("SI Price Report", not DEFAULT_SETTINGS["weekly_report"]["enabled"], "zamrznjen"))
    print("\nREZULTAT: " + ("PRIPRAVLJENO ZA ROČNI TEST v30" if all(results) else "NAJPREJ ODPRAVI NAPAKE"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
