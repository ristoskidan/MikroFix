"""Idempotentno naloži umetno zalogo iPhone 11 Pro samo v TEST okolje."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

from mikrofix_runtime import load_runtime


APP_DIR = Path(__file__).resolve().parent
RUNTIME = load_runtime(APP_DIR)

TEST_ITEMS = (
    (
        "TEST-IP11P-SCR-SOLED-001",
        "[TEST] iPhone 11 Pro – zaslon Soft OLED FixPremium",
        "IP11P-SCR-SOLED-TEST",
        3,
        67.43,
        "TEST-IP11P-LOT-SCR-01",
    ),
    (
        "TEST-IP11P-SCR-REFURB-001",
        "[TEST] iPhone 11 Pro – obnovljen originalni zaslon",
        "IP11P-SCR-REFURB-TEST",
        2,
        109.90,
        "TEST-IP11P-LOT-SCR-02",
    ),
    (
        "TEST-IP11P-BAT-FP-001",
        "[TEST] iPhone 11 Pro – baterija 3046 mAh FixPremium",
        "IP11P-BAT-FP-TEST",
        5,
        12.87,
        "TEST-IP11P-LOT-BAT-01",
    ),
    (
        "TEST-IP11P-SEAL-DISPLAY-001",
        "[TEST] iPhone 11 Pro – tesnilo zaslona",
        "IP11P-SEAL-DISPLAY-TEST",
        10,
        1.50,
        "TEST-IP11P-LOT-SEAL-01",
    ),
    (
        "TEST-IP11P-ADH-BAT-001",
        "[TEST] iPhone 11 Pro – lepilni trakovi baterije",
        "IP11P-ADH-BAT-TEST",
        10,
        0.90,
        "TEST-IP11P-LOT-ADH-01",
    ),
    (
        "TEST-IP11P-CHARGE-001",
        "[TEST] iPhone 11 Pro – modul priključka za polnjenje",
        "IP11P-CHARGE-TEST",
        2,
        18.90,
        "TEST-IP11P-LOT-CHG-01",
    ),
    (
        "TEST-IP11P-CAM-REAR-001",
        "[TEST] iPhone 11 Pro – modul zadnjih kamer",
        "IP11P-CAM-REAR-TEST",
        1,
        64.90,
        "TEST-IP11P-LOT-CAM-01",
    ),
    (
        "TEST-IP11P-TAPTIC-001",
        "[TEST] iPhone 11 Pro – Taptic Engine",
        "IP11P-TAPTIC-TEST",
        2,
        19.90,
        "TEST-IP11P-LOT-TAP-01",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Obstoječe izključno TEST artikle vrne na začetne količine.",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not RUNTIME.is_test:
        raise SystemExit(
            "ZAVRNJENO: umetno zalogo je dovoljeno naložiti samo v TEST okolje."
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = RUNTIME.connect_sqlite("inventory", foreign_keys=True)
    inserted = 0
    existing = 0
    try:
        connection.execute("BEGIN")
        for barcode, name, code, qty, price, lot in TEST_ITEMS:
            if args.reset:
                cursor = connection.execute(
                    """
                    INSERT INTO inventory_items(
                        barcode, article_name, internal_code, stock_qty,
                        price, lot_serial, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(barcode) DO UPDATE SET
                        article_name=excluded.article_name,
                        internal_code=excluded.internal_code,
                        stock_qty=excluded.stock_qty,
                        price=excluded.price,
                        lot_serial=excluded.lot_serial,
                        updated_at=excluded.updated_at
                    """,
                    (barcode, name, code, qty, price, lot, now, now),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO inventory_items(
                        barcode, article_name, internal_code, stock_qty,
                        price, lot_serial, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (barcode, name, code, qty, price, lot, now, now),
                )
            if cursor.rowcount == 1:
                inserted += 1
            else:
                existing += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    if args.reset:
        print("TESTNA ZALOGA IP11P: začetne količine so ponastavljene.")
    else:
        print(f"TESTNA ZALOGA IP11P: dodano {inserted}, že obstaja {existing}.")
    print("V programu v zavihku Zaloga delov poišči: [TEST] ali IP11P.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
