"""Samodejni, neprodukcijski pregled MikroFIX v29 pred ročnim testom."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mikrofix_runtime import APP_VERSION, load_runtime
from mikrofix_v29_core import initialize_v29


APP_DIR = Path(__file__).resolve().parent
RUNTIME = load_runtime(APP_DIR)


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
    initialize_v29(
        inventory_db=RUNTIME.db_path("inventory"),
        office_db=RUNTIME.db_path("office"),
        service_db=RUNTIME.db_path("service"),
        pricing=RUNTIME.settings["pricing"],
    )
    results = []
    results.append(check("Različica", APP_VERSION.startswith("29."), APP_VERSION))
    results.append(check("TEST okolje", RUNTIME.is_test, RUNTIME.settings["environment"]))
    furs_off = not bool(RUNTIME.settings["furs"].get("enabled"))
    results.append(check("FURS produkcija", furs_off, "izklopljena" if furs_off else "NEVARNO VKLOPLJENA"))
    for role in ("service", "inventory", "office", "auth"):
        path = RUNTIME.db_path(role)
        results.append(check(f"Baza {role}", path.exists() and integrity(path), path.name))

    inventory = RUNTIME.connect_sqlite("inventory", read_only=True)
    negative = inventory.execute("SELECT COUNT(*) FROM inventory_items WHERE stock_qty < 0").fetchone()[0]
    duplicate_sources = inventory.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT source_type, source_id, source_line_id, movement_type, COUNT(*) AS n
            FROM stock_movements
            WHERE source_id IS NOT NULL AND source_line_id IS NOT NULL
            GROUP BY source_type, source_id, source_line_id, movement_type
            HAVING n>1
        )
        """
    ).fetchone()[0]
    profiles = inventory.execute("SELECT COUNT(*) FROM price_profiles WHERE active=1").fetchone()[0]
    inventory.close()
    results.append(check("Negativna zaloga", negative == 0, str(negative)))
    results.append(check("Podvojeni premiki", duplicate_sources == 0, str(duplicate_sources)))
    results.append(check("Cenovni profili", profiles >= 1, f"{profiles} aktivnih"))

    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "test_v29_core.py", "test_v28_core.py"],
        cwd=APP_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150,
    )
    results.append(check("Avtomatski testi", tests.returncode == 0, (tests.stdout + tests.stderr).strip().splitlines()[-1]))

    env = os.environ.copy()
    env["MIKROFIX_SMOKE_TEST"] = "1"
    env["MIKROFIX_SKIP_FILE_PROTECTION"] = "1"
    smoke_ok = True
    for script in ("mikrofix.py", "mikrofix_office.py"):
        run = subprocess.run([sys.executable, script], cwd=APP_DIR, env=env, timeout=45)
        smoke_ok = smoke_ok and run.returncode == 0
    results.append(check("Odpiranje obeh programov", smoke_ok, "Service + Office"))
    print("\nREZULTAT: " + ("PRIPRAVLJENO ZA ROČNI TEST" if all(results) else "NAJPREJ ODPRAVI NAPAKE"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
