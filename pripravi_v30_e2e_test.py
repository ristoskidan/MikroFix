"""Pripravi izolirano, ponovljivo MikroFIX v30 E2E TEST bazo.

Ta pripravljalnik namerno ne pozna produkcijskega FURS naslova, ne uporablja
produkcijskega potrdila in ne spreminja običajne razvojne ali prihodnje
produkcijske baze.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("MIKROFIX_CONFIG") or APP_DIR / "mikrofix_settings_e2e_test.json"
).expanduser().resolve()
SEED_VERSION = "MFI-E2E-1"

# Konfiguracijo določimo pred uvozom modulov, ki ustvarijo RuntimeContext.
os.environ["MIKROFIX_CONFIG"] = str(CONFIG_PATH)
os.environ.pop("MIKROFIX_ALLOW_FURS_PRODUCTION", None)
os.environ["MIKROFIX_E2E_TEST"] = "YES"

from mikrofix_auth import AuthStore  # noqa: E402
from mikrofix_furs import FursTestStore  # noqa: E402
from mikrofix_runtime import load_runtime  # noqa: E402
from mikrofix_v29_core import (  # noqa: E402
    complete_refurbishment,
    initialize_v29,
    register_buyback_device,
    start_refurbishment,
)
from mikrofix_v30_lots import allocate_fifo  # noqa: E402
from mikrofix_v30_pos import ensure_pos_schema  # noqa: E402
from mikrofix_v30_receipts import (  # noqa: E402
    ReceiptLineV30,
    confirm_receipt,
    create_draft,
    ensure_receipt_schema,
)
from mikrofix_workflows import (  # noqa: E402
    complete_buyback,
    create_buyback,
    create_service_order,
    ensure_workflow_schema,
    link_service_result,
)


RUNTIME = load_runtime(APP_DIR)
EXPECTED_DATA_DIR = RUNTIME.data_dir.resolve()
MANIFEST_PATH = EXPECTED_DATA_DIR / "E2E_TEST_MANIFEST.json"
CENT = Decimal("0.01")

LONG_ARTICLE_NAME = (
    "[TEST E2E] Univerzalni diagnostični komplet za mobilne telefone, tablične "
    "računalnike, prenosnike, namizne računalnike in igralne konzole – različica "
    "z izjemno dolgim nazivom za preverjanje preloma besedila, šumnikov, količine, "
    "rabata in končnega zneska na 80-mm termičnem računu"
)


@dataclass(frozen=True)
class SeedArticle:
    barcode: str
    name: str
    code: str
    category: str
    quantity: Decimal
    net_unit_cost: Decimal
    vat_rate: Decimal
    sale_price: Decimal
    location: str = "GLAVNA"

    @property
    def supplier_total(self) -> Decimal:
        return (
            self.quantity
            * self.net_unit_cost
            * (Decimal("1") + self.vat_rate / Decimal("100"))
        ).quantize(CENT, rounding=ROUND_HALF_UP)


BOOKED_ARTICLES = (
    SeedArticle(
        "TEST-E2E-LONG-0001", LONG_ARTICLE_NAME, "E2E-LONG-80MM",
        "SPLOŠNO", Decimal("3"), Decimal("8.20"), Decimal("22"), Decimal("24.90"),
    ),
    SeedArticle(
        "TEST-E2E-IP11P-SOLED", "[TEST E2E] iPhone 11 Pro – zaslon Soft OLED FixPremium",
        "IP11P-SOLED-E2E", "Soft OLED", Decimal("4"), Decimal("62.00"),
        Decimal("22"), Decimal("104.90"),
    ),
    SeedArticle(
        "TEST-E2E-IP11P-BAT", "[TEST E2E] iPhone 11 Pro – nova baterija FixPremium 3046 mAh",
        "IP11P-BAT-E2E", "Battery FixPremium", Decimal("8"), Decimal("13.20"),
        Decimal("22"), Decimal("34.90"),
    ),
    SeedArticle(
        "TEST-E2E-IP11P-SEAL", "[TEST E2E] iPhone 11 Pro – tesnilo zaslona za vsako odpiranje",
        "IP11P-SEAL-E2E", "SPLOŠNO", Decimal("15"), Decimal("1.10"),
        Decimal("22"), Decimal("5.00"),
    ),
    SeedArticle(
        "TEST-E2E-PC-THERMAL", "[TEST E2E] Termalna pasta za procesor – servisna poraba 4 g",
        "PC-THERMAL-E2E", "SPLOŠNO", Decimal("6"), Decimal("4.50"),
        Decimal("22"), Decimal("12.90"),
    ),
    SeedArticle(
        "TEST-E2E-CONSOLE-HDMI", "[TEST E2E] Nadomestni HDMI priključek za igralno konzolo",
        "CONSOLE-HDMI-E2E", "SPLOŠNO", Decimal("5"), Decimal("5.00"),
        Decimal("22"), Decimal("15.00"),
    ),
)

PENDING_ARTICLE = SeedArticle(
    "TEST-E2E-PENDING-CAMERA", "[TEST E2E] Modul kamere – dobavnica prejeta, račun še manjka",
    "CAM-PENDING-E2E", "SPLOŠNO", Decimal("2"), Decimal("18.00"),
    Decimal("22"), Decimal("39.90"),
)


def guard_isolated_test() -> None:
    problems: list[str] = []
    if not RUNTIME.is_test:
        problems.append("okolje ni TEST")
    if RUNTIME.settings.get("furs", {}).get("environment") != "TEST":
        problems.append("FURS okolje ni TEST")
    if bool(RUNTIME.settings.get("furs", {}).get("enabled")):
        problems.append("FURS produkcijsko pošiljanje je vključeno")
    if os.environ.get("MIKROFIX_ALLOW_FURS_PRODUCTION"):
        problems.append("nastavljena so produkcijska FURS vrata")
    if RUNTIME.data_dir.resolve() != EXPECTED_DATA_DIR:
        problems.append(f"napačna podatkovna mapa: {RUNTIME.data_dir}")
    if EXPECTED_DATA_DIR.name != "MikroFixData_V30_E2E_TEST":
        problems.append("testna podatkovna mapa nima varnega E2E imena")
    if problems:
        raise RuntimeError("E2E TEST JE VARNOSTNO ZAVRNJEN: " + "; ".join(problems))


def rebuild_recoverably() -> Path | None:
    if not EXPECTED_DATA_DIR.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = EXPECTED_DATA_DIR.with_name(f"MikroFixData_V30_E2E_TEST_ARHIV_{stamp}")
    EXPECTED_DATA_DIR.replace(archive)
    for directory in (
        RUNTIME.db_dir,
        RUNTIME.service_data_dir / "photos",
        RUNTIME.service_data_dir / "pdf",
        RUNTIME.service_data_dir / "backups",
        RUNTIME.office_data_dir / "pdf",
        RUNTIME.office_data_dir / "received_documents",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return archive


def ensure_base_office_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS partners(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT, post_city TEXT, country TEXT, tax_no TEXT,
            registration_no TEXT, iban TEXT, phone TEXT, email TEXT,
            contact_person TEXT, partner_type TEXT NOT NULL DEFAULT 'DOBAVITELJ',
            pcd_days REAL NOT NULL DEFAULT 0, notes TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no TEXT NOT NULL UNIQUE, doc_type TEXT NOT NULL,
            partner_id INTEGER, partner_name_snapshot TEXT,
            partner_address_snapshot TEXT, partner_tax_snapshot TEXT,
            partner_pcd_snapshot REAL NOT NULL DEFAULT 0, external_no TEXT,
            date TEXT NOT NULL, order_date TEXT, received_date TEXT, due_date TEXT,
            currency TEXT NOT NULL DEFAULT 'EUR', delivery_cost REAL NOT NULL DEFAULT 0,
            stock_effect INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OSNUTEK', linked_claim_id INTEGER,
            linked_claim_no TEXT, linked_service_id INTEGER, notes TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, booked_at TEXT,
            FOREIGN KEY(partner_id) REFERENCES partners(id)
        );
        CREATE TABLE IF NOT EXISTS document_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL, article_id INTEGER, barcode TEXT,
            article_name TEXT NOT NULL, internal_code TEXT,
            qty REAL NOT NULL DEFAULT 1, unit_price REAL NOT NULL DEFAULT 0,
            discount_pct REAL NOT NULL DEFAULT 0, vat_pct REAL NOT NULL DEFAULT 0,
            delivery_share REAL NOT NULL DEFAULT 0,
            actual_unit_cost REAL NOT NULL DEFAULT 0,
            margin_pct REAL NOT NULL DEFAULT 0,
            calculated_price REAL NOT NULL DEFAULT 0,
            recommended_price REAL NOT NULL DEFAULT 0,
            sell_price REAL NOT NULL DEFAULT 0,
            line_net REAL NOT NULL DEFAULT 0, line_vat REAL NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS document_links(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_document_id INTEGER NOT NULL, to_document_id INTEGER NOT NULL,
            link_type TEXT,
            FOREIGN KEY(from_document_id) REFERENCES documents(id),
            FOREIGN KEY(to_document_id) REFERENCES documents(id)
        );
        CREATE TABLE IF NOT EXISTS supplier_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL, service_claim_id INTEGER,
            service_claim_no TEXT, supplier_resolution TEXT, status TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS e2e_seed_meta(
            seed_version TEXT PRIMARY KEY, prepared_at TEXT NOT NULL,
            summary_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS e2e_test_scenarios(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario_code TEXT NOT NULL UNIQUE, area TEXT NOT NULL,
            title TEXT NOT NULL, expected_result TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PRIPRAVLJENO'
        );
        CREATE INDEX IF NOT EXISTS idx_partner_name ON partners(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_doc_date ON documents(date);
        CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(doc_type);
        CREATE INDEX IF NOT EXISTS idx_doc_service ON documents(linked_service_id);
        """
    )
    connection.commit()
    connection.close()


def ensure_base_service_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS service(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_no TEXT UNIQUE, date TEXT, customer TEXT,
            customer_address TEXT, customer_phone TEXT, model TEXT,
            serial TEXT, imei TEXT, ios TEXT, work TEXT, part_id TEXT,
            invoice_no TEXT, warranty_until TEXT, warranty_scope TEXT,
            warranty_months INTEGER, service_order_id INTEGER,
            device_type TEXT, identity_method TEXT,
            labor_price REAL NOT NULL DEFAULT 0,
            pricing_mode TEXT NOT NULL DEFAULT 'MANUAL_LINES_V29',
            office_invoice_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS service_parts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL, part_name TEXT, part_id TEXT,
            barcode TEXT, article_id INTEGER, lot_serial TEXT, unit_price REAL,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS service_manual_lines(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL, line_kind TEXT NOT NULL,
            description TEXT NOT NULL, quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS service_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL, stage TEXT NOT NULL,
            test_name TEXT NOT NULL, status TEXT NOT NULL,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS service_photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL, stage TEXT, rel_path TEXT NOT NULL,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL, claim_no TEXT, date TEXT NOT NULL,
            description TEXT, intake_condition TEXT, customer_request TEXT,
            finding TEXT, resolution TEXT, status TEXT,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_service_serial ON service(serial);
        CREATE INDEX IF NOT EXISTS idx_service_imei ON service(imei);
        CREATE INDEX IF NOT EXISTS idx_claims_service ON claims(service_id);
        CREATE INDEX IF NOT EXISTS idx_service_order_id ON service(service_order_id);
        """
    )
    ensure_workflow_schema(connection)
    connection.commit()
    connection.close()


def initialize_schemas() -> None:
    ensure_base_office_schema(RUNTIME.db_path("office"))
    ensure_base_service_schema(RUNTIME.db_path("service"))
    initialize_v29(
        inventory_db=RUNTIME.db_path("inventory"),
        office_db=RUNTIME.db_path("office"),
        service_db=RUNTIME.db_path("service"),
        pricing=RUNTIME.settings["pricing"],
    )
    ensure_receipt_schema(RUNTIME.db_path("office"))
    ensure_pos_schema(RUNTIME.db_path("office"))
    AuthStore(RUNTIME.db_path("auth")).init()
    FursTestStore(RUNTIME.db_path("office")).init()


def seed_already_present() -> bool:
    connection = sqlite3.connect(RUNTIME.db_path("office"))
    row = connection.execute(
        "SELECT 1 FROM e2e_seed_meta WHERE seed_version=?", (SEED_VERSION,)
    ).fetchone()
    connection.close()
    return bool(row)


def make_attachment(path: Path, title: str, lines: list[str]) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas

    # Vgrajeni Helvetica fonti nimajo slovenskih znakov. Pisavo vdelamo v PDF,
    # da so testne priloge pravilne tudi na računalniku brez lokalnih pisav.
    font_dir = Path("/usr/share/fonts/truetype/dejavu")
    regular_name = "MikroFIXDejaVu"
    bold_name = "MikroFIXDejaVuBold"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, str(font_dir / "DejaVuSans.ttf")))
    if bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, str(font_dir / "DejaVuSans-Bold.ttf")))

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle(title)
    canvas.setFont(bold_name, 15)
    canvas.drawString(45, 800, title)
    canvas.setFont(regular_name, 9)
    y = 775
    for line in lines:
        words = str(line).split()
        wrapped: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and pdfmetrics.stringWidth(candidate, regular_name, 9) > 505:
                wrapped.append(current)
                current = word
            else:
                current = candidate
        if current:
            wrapped.append(current)
        for index, visual_line in enumerate(wrapped or [""]):
            canvas.drawString(45 if index == 0 else 57, y, visual_line)
            y -= 13
        y -= 2
    canvas.setFont(bold_name, 10)
    canvas.drawString(45, 65, "TESTNI DOKUMENT - NI PRAVI RACUN ALI DOBAVNICA")
    canvas.save()
    return path


def seed_partners() -> tuple[int, int]:
    stamp = datetime.now().isoformat(timespec="seconds")
    connection = sqlite3.connect(RUNTIME.db_path("office"))
    supplier = connection.execute(
        """
        INSERT INTO partners(
            name,address,post_city,country,tax_no,registration_no,iban,phone,email,
            contact_person,partner_type,pcd_days,notes,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "[TEST E2E] Dobavitelj rezervnih delov d.o.o.", "Testna cesta 10",
            "1000 Ljubljana", "Slovenija", "SI99999999", "9999999000",
            "SI56000000000000000", "01 000 00 00", "dobavitelj@example.invalid",
            "Testna Kontaktna Oseba", "DOBAVITELJ", 8,
            "Izmišljeni podatki samo za celovit preizkus vseh polj.", stamp, stamp,
        ),
    ).lastrowid
    customer = connection.execute(
        """
        INSERT INTO partners(
            name,address,post_city,country,tax_no,registration_no,iban,phone,email,
            contact_person,partner_type,pcd_days,notes,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "[TEST E2E] Kupec z vsemi kontaktnimi podatki", "Preizkusna ulica 22",
            "2000 Maribor", "Slovenija", "12345678", "", "",
            "040 000 222", "kupec@example.invalid", "Testni Kupec", "STRANKA", 0,
            "Ne obstaja; uporablja se samo za pregled postavitve dokumentov.", stamp, stamp,
        ),
    ).lastrowid
    connection.commit()
    connection.close()
    return int(supplier), int(customer)


def seed_article_shells() -> dict[str, int]:
    stamp = datetime.now().isoformat(timespec="seconds")
    all_articles = (*BOOKED_ARTICLES, PENDING_ARTICLE)
    connection = sqlite3.connect(RUNTIME.db_path("inventory"))
    ids: dict[str, int] = {}
    for article in all_articles:
        cursor = connection.execute(
            """
            INSERT INTO inventory_items(
                barcode,article_name,internal_code,stock_qty,price,lot_serial,
                created_at,updated_at,article_kind,category,supplier_name,
                supplier_sku,track_mode,condition_grade,location,reserved_qty,
                acquisition_cost,price_profile_code,price_status,source_kind,
                source_id,tax_classification,tax_rate,active
            ) VALUES(?,?,?,0,?,'',?,?, 'PART',?,?,?,'QUANTITY','NOVO',?,0,0,?,
                     'POTRJENA','SUPPLIER',NULL,'TEST',NULL,1)
            """,
            (
                article.barcode, article.name, article.code, float(article.sale_price),
                stamp, stamp, article.category,
                "[TEST E2E] Dobavitelj rezervnih delov d.o.o.", article.barcode,
                article.location, article.category,
            ),
        )
        ids[article.barcode] = int(cursor.lastrowid)
    connection.commit()
    connection.close()
    return ids


def receipt_lines(articles: tuple[SeedArticle, ...], ids: dict[str, int]) -> list[ReceiptLineV30]:
    return [
        ReceiptLineV30(
            article_id=ids[item.barcode], quantity=str(item.quantity),
            net_unit_price=str(item.net_unit_cost), discount_percent="0",
            vat_rate=str(item.vat_rate), supplier_line_total=str(item.supplier_total),
            confirmed_sale_price=str(item.sale_price), supplier_barcode=item.barcode,
            lot_serial=f"LOT-{item.barcode}", location=item.location,
        )
        for item in articles
    ]


def seed_receipts(supplier_id: int, ids: dict[str, int]) -> tuple[int, int]:
    today = date.today().isoformat()
    archive = RUNTIME.office_data_dir / "received_documents"
    booked_pdf = make_attachment(
        RUNTIME.office_data_dir / "seed_sources" / "TEST_PREJETI_RACUN_001.pdf",
        "TEST E2E - DOBAVNICA IN RACUN",
        [item.name for item in BOOKED_ARTICLES],
    )
    booked_lines = receipt_lines(BOOKED_ARTICLES, ids)
    shipping = Decimal("12.20")
    document_total = sum((item.supplier_total for item in BOOKED_ARTICLES), Decimal("0")) + shipping
    receipt_id, _ = create_draft(
        RUNTIME.db_path("office"), draft_key="E2E-BOOKED-RECEIPT", operator_code="DR"
    )
    confirm_receipt(
        office_db=RUNTIME.db_path("office"), inventory_db=RUNTIME.db_path("inventory"),
        archive_dir=archive, receipt_id=receipt_id, partner_id=supplier_id,
        partner_name="[TEST E2E] Dobavitelj rezervnih delov d.o.o.",
        partner_snapshot={
            "name": "[TEST E2E] Dobavitelj rezervnih delov d.o.o.",
            "address": "Testna cesta 10", "post_city": "1000 Ljubljana",
            "tax_no": "SI99999999", "registration_no": "9999999000",
            "iban": "SI56000000000000000", "phone": "01 000 00 00",
            "email": "dobavitelj@example.invalid",
        },
        document_type="DOBAVNICA_IN_RACUN", custom_document_type="",
        external_document_no="TEST-DOB-RA-001", document_date=today,
        received_at=today, currency="EUR", document_total=str(document_total),
        shipping_total=str(shipping), invoice_external_no="TEST-PR-001",
        invoice_date=today, invoice_due_date=(date.today() + timedelta(days=8)).isoformat(),
        invoice_payment_status="NEPLACAN", lines=booked_lines,
        attachment_paths=[booked_pdf],
        notes="[TEST E2E] Knjižen skupni dokument; ustvarja sledljivo prodajno zalogo.",
        operator_code="DR",
    )

    pending_pdf = make_attachment(
        RUNTIME.office_data_dir / "seed_sources" / "TEST_DOBAVNICA_BREZ_RACUNA_002.pdf",
        "TEST E2E - DOBAVNICA BREZ RACUNA",
        [PENDING_ARTICLE.name],
    )
    pending_id, _ = create_draft(
        RUNTIME.db_path("office"), draft_key="E2E-PENDING-INVOICE", operator_code="DR"
    )
    confirm_receipt(
        office_db=RUNTIME.db_path("office"), inventory_db=RUNTIME.db_path("inventory"),
        archive_dir=archive, receipt_id=pending_id, partner_id=supplier_id,
        partner_name="[TEST E2E] Dobavitelj rezervnih delov d.o.o.",
        partner_snapshot={"name": "[TEST E2E] Dobavitelj rezervnih delov d.o.o."},
        document_type="DOBAVNICA", custom_document_type="",
        external_document_no="TEST-DOB-002", document_date=today,
        received_at=today, currency="EUR",
        document_total=str(PENDING_ARTICLE.supplier_total), shipping_total="0",
        invoice_external_no="", invoice_date="", invoice_due_date="",
        invoice_payment_status="", lines=receipt_lines((PENDING_ARTICLE,), ids),
        attachment_paths=[pending_pdf],
        notes="[TEST E2E] Blago ostane nedostopno za prodajo, dokler ni pripet račun.",
        operator_code="DR",
    )
    return int(receipt_id), int(pending_id)


def service_payload(serial: str, imei: str, *, manual: bool = False) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "customer_name": "[TEST E2E] Stranka Servis",
        "customer_address": "Preizkusna ulica 22, 2000 Maribor",
        "customer_phone": "040 000 222", "customer_email": "servis@example.invalid",
        "device_model": "Namizni računalnik po meri" if manual else "iPhone 11 Pro",
        "device_serial": serial, "device_imei": "" if manual else imei,
        "device_os": "Windows 11 TEST" if manual else "iOS 18 TEST",
        "device_storage": "1 TB" if manual else "256 GB",
        "device_type": "RACUNALNIK" if manual else "APPLE_MOBILE",
        "identity_method": "MANUAL_OTHER" if manual else "MICRODETECT",
        "manual_identity_confirmed": manual,
        "customer_request": (
            "Menjava procesorja, čiščenje hladilnega sistema in preverjanje stabilnosti."
            if manual else
            "Menjava zaslona in tesnila ter celovit test vseh pomembnih modulov."
        ),
        "intake_condition": "Vidne sledi uporabe; fotografije so za ta umetni primer izpuščene.",
        "accessories": "Brez dodatne opreme", "quote_eur": "149.80",
        "logistics_eur": "0", "expected_due": (date.today() + timedelta(days=2)).isoformat(),
        "contract_context": "MFD", "immediate_start_requested": True,
        "withdrawal_loss_ack": True, "delivery_channel": "EMAIL_PDF",
        "microdetect_confirmed": not manual, "microdetect_at": now,
        "operator_code": "DR", "notes": "[TEST E2E] Izmišljeni servisni podatki.",
    }


def consume_service_part(service_id: int, article_id: int, part_line_id: int) -> str:
    stamp = datetime.now().isoformat(timespec="seconds")
    connection = sqlite3.connect(RUNTIME.db_path("inventory"))
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT stock_qty FROM inventory_items WHERE id=?", (article_id,)
        ).fetchone()
        if not row or Decimal(str(row[0])) < Decimal("1"):
            raise RuntimeError("TEST servis nima zahtevanega dela na zalogi.")
        allocations = allocate_fifo(
            connection, schema="main", article_id=article_id, quantity=1,
            allocation_type="SERVICE_PART", source_id=service_id,
            source_line_id=part_line_id, created_at=stamp,
        )
        balance = Decimal(str(row[0])) - Decimal("1")
        lot = str(allocations[0]["lot_serial"] if allocations else "")
        connection.execute(
            "UPDATE inventory_items SET stock_qty=?, updated_at=? WHERE id=?",
            (float(balance), stamp, article_id),
        )
        connection.execute(
            """
            INSERT INTO stock_movements(
                movement_no,created_at,article_id,quantity_delta,balance_after,
                movement_type,source_type,source_id,source_line_id,lot_serial,
                location_from,location_to,reason,operator_code,reversal_of_id
            ) VALUES(?,?,?,?,?,'SERVICE_OUT','SERVICE',?,?,?,'GLAVNA','SERVIS',?,?,NULL)
            """,
            (
                f"TEST-SERV-{service_id:06d}-{part_line_id:03d}", stamp, article_id,
                -1, float(balance), service_id, part_line_id, lot,
                "[TEST E2E] Del vgrajen v servis, ki čaka na račun", "DR",
            ),
        )
        connection.commit()
        return lot
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def seed_services(ids: dict[str, int]) -> tuple[int, int, int]:
    service_db = RUNTIME.db_path("service")
    connection = sqlite3.connect(service_db)
    connection.execute("PRAGMA foreign_keys=ON")
    open_order_id, _ = create_service_order(
        connection, service_payload("TEST-PC-SERIAL-OPEN", "", manual=True)
    )
    ready_order_id, _ = create_service_order(
        connection, service_payload("TEST-IP11P-SERIAL-READY", "000000000000111")
    )
    now = datetime.now().isoformat(timespec="seconds")
    cursor = connection.execute(
        """
        INSERT INTO service(
            service_no,date,customer,customer_address,customer_phone,model,serial,
            imei,ios,work,part_id,invoice_no,warranty_until,warranty_scope,
            warranty_months,service_order_id,device_type,identity_method,labor_price,
            pricing_mode,office_invoice_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"MF-TEST-{date.today().year}-0001", now,
            "[TEST E2E] Stranka Servis", "Preizkusna ulica 22, 2000 Maribor",
            "040 000 222", "iPhone 11 Pro", "TEST-IP11P-SERIAL-READY",
            "000000000000111", "iOS 18 TEST",
            "Menjava zaslona, tesnila in preverjanje vseh modulov pred vračilom.",
            "", None, (date.today() + timedelta(days=90)).isoformat(),
            "VGRAJENI_DELI", 3, ready_order_id, "APPLE_MOBILE", "MICRODETECT",
            30.00, "MANUAL_LINES_V29", None,
        ),
    )
    ready_service_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO service_manual_lines(service_id,line_kind,description,quantity,unit_price)
        VALUES(?, 'T1', ?, 1, 30.00)
        """,
        (
            ready_service_id,
            "[TEST E2E] T1 – strokovno servisno delo z daljšim opisom za preverjanje prenosa iz servisa v blagajno",
        ),
    )
    article_id = ids["TEST-E2E-IP11P-SOLED"]
    inv = sqlite3.connect(RUNTIME.db_path("inventory"))
    article = inv.execute(
        "SELECT article_name,internal_code,barcode,price FROM inventory_items WHERE id=?",
        (article_id,),
    ).fetchone()
    inv.close()
    part_cursor = connection.execute(
        """
        INSERT INTO service_parts(
            service_id,part_name,part_id,barcode,article_id,lot_serial,unit_price
        ) VALUES(?,?,?,?,?,'',?)
        """,
        (ready_service_id, article[0], article[1], article[2], article_id, article[3]),
    )
    part_line_id = int(part_cursor.lastrowid)
    for stage in ("PRED", "PO"):
        for test_name in ("Zaslon in dotik", "Kamere", "Zvočniki", "Polnjenje", "Mobilno omrežje"):
            connection.execute(
                "INSERT INTO service_tests(service_id,stage,test_name,status) VALUES(?,?,?,'OK')",
                (ready_service_id, stage, test_name),
            )
    link_service_result(connection, ready_order_id, ready_service_id)

    historical = connection.execute(
        """
        INSERT INTO service(
            service_no,date,customer,customer_address,customer_phone,model,serial,
            imei,ios,work,invoice_no,warranty_until,warranty_scope,warranty_months,
            device_type,identity_method,labor_price,pricing_mode,office_invoice_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"MF-TEST-{date.today().year}-ARHIV", now, "[TEST E2E] Reklamacijska stranka",
            "Reklamacijska 3, 3000 Celje", "040 000 333", "Tablični računalnik TEST",
            "TEST-CLAIM-SERIAL", "", "TEST OS", "Menjava polnilnega priključka",
            "TEST-ARHIV-RACUN", (date.today() + timedelta(days=45)).isoformat(),
            "VGRAJENI_DELI", 3, "TABLICA", "MANUAL_OTHER", 30, "MANUAL_LINES_V29", 999,
        ),
    ).lastrowid
    claim = connection.execute(
        """
        INSERT INTO claims(
            service_id,claim_no,date,description,intake_condition,customer_request,
            finding,resolution,status
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            historical, f"REK-TEST-{date.today().year}-0001", now,
            "[TEST E2E] Občasno prekinjanje polnjenja po predhodnem servisu.",
            "Naprava brez dodatkov; vidne običajne sledi uporabe.",
            "Pregled in odprava napake v okviru ustrezne odgovornosti.",
            "", "", "V_OBDELAVI",
        ),
    ).lastrowid
    connection.commit()
    connection.close()

    lot = consume_service_part(ready_service_id, article_id, part_line_id)
    connection = sqlite3.connect(service_db)
    connection.execute(
        "UPDATE service_parts SET lot_serial=? WHERE id=?", (lot, part_line_id)
    )
    connection.commit()
    connection.close()
    return int(open_order_id), int(ready_service_id), int(claim)


def buyback_payload(index: int, *, payout: str, price: str) -> dict:
    return {
        "seller_name": f"[TEST E2E] Prodajalec {index}",
        "seller_tax_number": f"1234567{index}",
        "seller_address": f"Odkupna ulica {index}, 1000 Ljubljana",
        "seller_contact": f"04000000{index}",
        "seller_iban": "SI56000000000000000" if payout == "BANK_TRANSFER" else "",
        "identity_checked": True, "device_model": "iPhone 11 Pro",
        "device_serial": f"TEST-BUYBACK-SERIAL-{index}",
        "device_imei": f"00000000000020{index}", "device_os": "iOS 18 TEST",
        "device_storage": "256 GB", "color_capacity": "testna barva, 256 GB",
        "accessories": "Samo naprava", "condition_text": (
            "[TEST E2E] Izmišljena naprava; zaslon ima manjše praske, ohišje je ohranjeno, "
            "vsi računi in zaklepi so za test označeni kot odstranjeni."
        ),
        "acquisition_method": "KUPLJENA_RABLJENA_FIZICNA_OSEBA",
        "acquisition_details": "[TEST E2E] Predhodna pisna pogodba.",
        "evidence_type": "PREJŠNJA_POGODBA", "imei_check_provider": "",
        "imei_check_result": "NOT_REQUIRED", "imei_check_reference": "",
        "imei_checked_at": datetime.now().isoformat(timespec="seconds"),
        "account_removed": True, "activation_lock_removed": True, "mdm_clear": True,
        "ownership_declaration": True, "privacy_notice_received": True,
        "risk_level": "GREEN", "risk_reason": "[TEST E2E] Brez dejanskega tveganja.",
        "purchase_price": price, "payout_method": payout, "operator_code": "DR",
        "microdetect_confirmed": True,
        "microdetect_at": datetime.now().isoformat(timespec="seconds"),
        "notes": "[TEST E2E] Ni dejanski odkup in ne vsebuje resničnih osebnih podatkov.",
    }


def seed_buybacks() -> tuple[int, int, int]:
    service_db = RUNTIME.db_path("service")
    inventory_db = RUNTIME.db_path("inventory")
    buyer = {
        "legal_name": RUNTIME.settings["business"]["legal_name"],
        "tax_number": RUNTIME.settings["business"]["tax_number"],
        "registration_number": RUNTIME.settings["business"]["registration_number"],
        "address": "Paradiž 73, 2282 Cirkulane", "representative": "Daniel Ristoski",
    }
    connection = sqlite3.connect(service_db)
    connection.execute("PRAGMA foreign_keys=ON")
    completed_id, _ = create_buyback(
        connection, buyback_payload(1, payout="BANK_TRANSFER", price="180.00"),
        test_mode=True,
    )
    complete_buyback(connection, completed_id)
    pending_cash_id, _ = create_buyback(
        connection, buyback_payload(2, payout="CASH", price="120.00"),
        test_mode=True,
    )
    donor_id, _ = create_buyback(
        connection, buyback_payload(3, payout="BANK_TRANSFER", price="90.00"),
        test_mode=True,
    )
    complete_buyback(connection, donor_id)
    connection.close()

    refurb_device_id, _ = register_buyback_device(
        service_db=service_db, inventory_db=inventory_db, buyback_id=completed_id,
        purpose="REFURBISH", buyer=buyer,
    )
    donor_device_id, _ = register_buyback_device(
        service_db=service_db, inventory_db=inventory_db, buyback_id=donor_id,
        purpose="PARTS", buyer=buyer,
    )
    order_id, _ = start_refurbishment(
        service_db=service_db, inventory_db=inventory_db, device_id=refurb_device_id,
        notes="[TEST E2E] Obnova z novo baterijo in zaključenimi testi.",
    )
    complete_refurbishment(
        service_db=service_db, inventory_db=inventory_db, order_id=order_id,
        labor_cost="20", parts_cost="35", other_cost="5", sale_price="349",
        tests_passed=True, notes="[TEST E2E] Nova baterija; vsi moduli delujejo.",
    )
    return int(pending_cash_id), int(refurb_device_id), int(donor_device_id)


def seed_scenarios() -> None:
    scenarios = (
        ("POS-LONG", "BLAGAJNA", "Dolg naziv na 80-mm računu", "Naziv se prelomi brez izgube šumnikov; F10 izda samo TEST dokument."),
        ("POS-DISCOUNT", "BLAGAJNA", "100 % zahtevan rabat", "Artikel se tiho omeji na nabavno vrednost + 5 %, storitev na L0."),
        ("POS-SPLIT", "BLAGAJNA", "Razdeljeno plačilo", "Vsota je do centa točna; TRR del mora biti potrjeno prejet."),
        ("SERVICE-READY", "SERVIS", "Servis čaka samo na račun", "Blagajna prenese T1 in vgrajeni del; zaloga se ne zmanjša dvakrat."),
        ("SERVICE-OPEN", "SERVIS", "Odprto naročilo za računalnik", "Možen je ročni prepis identitete in ročne servisne postavke."),
        ("CLAIM-OPEN", "REKLAMACIJA", "Odprta reklamacija", "Vidna sta povezava z originalnim servisom in stanje V_OBDELAVI."),
        ("BUYBACK-CASH", "ODKUP", "Pogodba in blagajniški izdatek", "Odkup je pred podpisom; zaloga še ne nastane."),
        ("BUYBACK-REFURB", "ODKUP/OBNOVA", "Obnovljena naprava za prodajo", "Naprava je v zalogi kot unikaten artikel in zahteva 12-mesečni podpis."),
        ("BUYBACK-DONOR", "ODKUP/DELI", "Donorska naprava v karanteni", "Naprava še ni umetno razdeljena v množico artiklov."),
        ("RECEIPT-BOOKED", "OFFICE", "Dobavnica in račun skupaj", "Zaloga, LOT, nabavna cena, prevoz in prodajna cena imajo sled."),
        ("RECEIPT-PENDING", "OFFICE", "Dobavnica brez računa", "Artikel ni na voljo za prodajo, dokler ni pripet natančno ujemajoč račun."),
        ("FURS-ISOLATED", "VARNOST", "Brez produkcijskega dostopa", "FURS enabled=false, okolje TEST, produkcijska vrata in potrdilo niso nastavljeni."),
    )
    connection = sqlite3.connect(RUNTIME.db_path("office"))
    connection.executemany(
        "INSERT INTO e2e_test_scenarios(scenario_code,area,title,expected_result) VALUES(?,?,?,?)",
        scenarios,
    )
    connection.commit()
    connection.close()


def write_manifest(summary: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    connection = sqlite3.connect(RUNTIME.db_path("office"))
    connection.execute(
        "INSERT INTO e2e_seed_meta(seed_version,prepared_at,summary_json) VALUES(?,?,?)",
        (
            SEED_VERSION, datetime.now().isoformat(timespec="seconds"),
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.commit()
    connection.close()


def existing_test_can_continue() -> bool:
    """Prepoznaj že uporabljeno, vendar še vedno izolirano E2E bazo.

    Začetni scenariji se med testiranjem namenoma porabijo, zato njihovih
    začetnih števcev ob vsakem zagonu ne smemo ponovno zahtevati.
    """
    if not MANIFEST_PATH.is_file():
        return False
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    if manifest.get("seed_version") != SEED_VERSION:
        return False
    if manifest.get("production_access") != "BLOCKED":
        return False
    return all(
        RUNTIME.db_path(kind).is_file()
        for kind in ("service", "office", "inventory", "auth")
    )


def verify() -> dict:
    guard_isolated_test()
    office = sqlite3.connect(RUNTIME.db_path("office"))
    inventory = sqlite3.connect(RUNTIME.db_path("inventory"))
    service = sqlite3.connect(RUNTIME.db_path("service"))
    try:
        result = {
            "environment": RUNTIME.settings["environment"],
            "data_dir": str(RUNTIME.data_dir),
            "furs_enabled": bool(RUNTIME.settings["furs"]["enabled"]),
            "furs_environment": RUNTIME.settings["furs"]["environment"],
            "partners": office.execute("SELECT COUNT(*) FROM partners WHERE name LIKE '[TEST E2E]%' ").fetchone()[0],
            "booked_receipts": office.execute("SELECT COUNT(*) FROM receipts_v30 WHERE status='BOOKED'").fetchone()[0],
            "pending_receipts": office.execute("SELECT COUNT(*) FROM receipts_v30 WHERE status='PENDING_INVOICE'").fetchone()[0],
            "test_scenarios": office.execute("SELECT COUNT(*) FROM e2e_test_scenarios").fetchone()[0],
            "sellable_articles": inventory.execute("SELECT COUNT(*) FROM inventory_items WHERE active=1 AND stock_qty>0").fetchone()[0],
            "long_article_chars": inventory.execute("SELECT length(article_name) FROM inventory_items WHERE barcode='TEST-E2E-LONG-0001'").fetchone()[0],
            "ready_services": service.execute("SELECT COUNT(*) FROM service WHERE invoice_no IS NULL AND service_order_id IS NOT NULL").fetchone()[0],
            "open_orders": service.execute("SELECT COUNT(*) FROM service_orders WHERE status='OPEN'").fetchone()[0],
            "open_claims": service.execute("SELECT COUNT(*) FROM claims WHERE status='V_OBDELAVI'").fetchone()[0],
            "pending_buybacks": service.execute("SELECT COUNT(*) FROM buybacks WHERE status='CONTRACT_READY'").fetchone()[0],
            "completed_buybacks": service.execute("SELECT COUNT(*) FROM buybacks WHERE status='COMPLETED'").fetchone()[0],
            "refurbished_for_sale": inventory.execute("SELECT COUNT(*) FROM used_devices WHERE status='READY_FOR_SALE'").fetchone()[0],
            "production_cert_path": RUNTIME.settings["furs"].get("certificate_path", ""),
        }
    finally:
        office.close()
        inventory.close()
        service.close()
    required = {
        "environment": "TEST", "furs_enabled": False, "furs_environment": "TEST",
        "production_cert_path": "",
    }
    for key, expected in required.items():
        if result.get(key) != expected:
            raise RuntimeError(f"E2E varovalka ni uspešna: {key}={result.get(key)!r}")
    for key in (
        "partners", "booked_receipts", "pending_receipts", "test_scenarios",
        "sellable_articles", "ready_services", "open_orders", "open_claims",
        "pending_buybacks", "completed_buybacks", "refurbished_for_sale",
    ):
        if int(result[key]) < 1:
            raise RuntimeError(f"E2E priprava nima zahtevanih podatkov: {key}")
    if int(result["long_article_chars"]) < 180:
        raise RuntimeError("Dolgi testni naziv ni dovolj dolg za preizkus preloma.")
    return result


def prepare() -> dict:
    initialize_schemas()
    if seed_already_present():
        return verify()
    supplier_id, _customer_id = seed_partners()
    ids = seed_article_shells()
    booked_receipt, pending_receipt = seed_receipts(supplier_id, ids)
    open_order, ready_service, claim = seed_services(ids)
    pending_buyback, refurb_device, donor_device = seed_buybacks()
    seed_scenarios()
    summary = {
        "seed_version": SEED_VERSION,
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "booked_receipt_id": booked_receipt,
        "pending_receipt_id": pending_receipt,
        "open_service_order_id": open_order,
        "ready_service_id": ready_service,
        "claim_id": claim,
        "pending_buyback_id": pending_buyback,
        "refurbished_device_id": refurb_device,
        "donor_device_id": donor_device,
        "long_article_name": LONG_ARTICLE_NAME,
        "production_access": "BLOCKED",
    }
    write_manifest(summary)
    return verify()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Obstoječo E2E bazo varno preimenuje v arhiv in pripravi novo.",
    )
    parser.add_argument("--verify", action="store_true", help="Samo preveri obstoječo E2E bazo.")
    args = parser.parse_args()
    guard_isolated_test()
    archive = rebuild_recoverably() if args.rebuild else None
    if archive:
        print(f"Prejšnja E2E baza je ohranjena v: {archive}")
    if not args.rebuild and not args.verify and existing_test_can_continue():
        print("\nMIKROFIX V30 E2E TEST: NADALJEVANJE OBSTOJEČE TESTNE BAZE")
        print(f"Podatkovna mapa: {RUNTIME.data_dir}")
        print("Produkcijski FURS: NEDOSEGLJIV")
        return 0
    result = verify() if args.verify else prepare()
    print("\nMIKROFIX V30 E2E TEST: PRIPRAVLJENO")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Produkcijski FURS: NEDOSEGLJIV")
    print("Vsi osebni in poslovni testni vnosi so izmišljeni in označeni [TEST E2E].")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
