"""MikroFIX v29: sledljivost cen, zaloge, prejemov in odkupljenih naprav.

Modul je namenoma ločen od Tk uporabniškega vmesnika. Poslovna pravila se tako
lahko avtomatsko preizkusijo in jih ni mogoče obiti z drugim zaslonom.
"""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from mikrofix_v30_lots import ensure_lot_schema, receive_lot


SCHEMA_VERSION = 1
PART_FLOOR_FACTOR = 1.05
MAX_DOCUMENT_QUANTITY = 1_000_000


class V29Error(ValueError):
    """Poslovno pravilo v29 ni izpolnjeno."""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _money(value: Any) -> float:
    try:
        result = round(float(str(value or "0").replace(",", ".")), 2)
    except (TypeError, ValueError) as exc:
        raise V29Error("Vrednost mora biti številka.") from exc
    if result < 0:
        raise V29Error("Vrednost ne sme biti negativna.")
    return result


def _cost4(value: Any) -> float:
    try:
        result = round(float(str(value or "0").replace(",", ".")), 4)
    except (TypeError, ValueError) as exc:
        raise V29Error("Vrednost mora biti številka.") from exc
    if result < 0:
        raise V29Error("Vrednost ne sme biti negativna.")
    return result


def _qty(value: Any) -> float:
    result = _money(value)
    if result <= 0:
        raise V29Error("Količina mora biti večja od nič.")
    return result


def round_up(value: float, step: float = 5.0) -> float:
    if step <= 0:
        raise V29Error("Korak zaokroževanja mora biti večji od nič.")
    return round(math.ceil((max(0.0, value) - 1e-9) / step) * step, 2)


@dataclass(frozen=True)
class PriceRecommendation:
    landed_cost: float
    recommended_price: float
    hard_floor: float
    profile_code: str
    target_margin: float = 0.0
    warranty_reserve: float = 0.0
    profile_version: int = 1


@dataclass(frozen=True)
class ReceiptLine:
    article_id: int
    quantity: float
    purchase_unit_cost: float
    confirmed_sale_price: float
    lot_serial: str = ""
    location: str = ""


@dataclass(frozen=True)
class HarvestPart:
    article_id: int | None = None
    condition_grade: str = "RABLJEN_TESTIRAN"
    test_result: str = "OK"
    lot_serial: str = ""
    location: str = ""
    module_code: str = ""
    module_name: str = ""
    device_model: str = ""
    recommended_sale_price: float = 0
    price_source_kind: str = ""
    price_source_url: str = ""
    price_observed_on: str = ""


PULLED_MODULES: tuple[tuple[str, str], ...] = (
    ("DISPLAY", "zaslon"),
    ("BATTERY", "baterija"),
    ("REAR_CAMERA", "zadnja kamera"),
    ("FRONT_CAMERA", "sprednja kamera / Face ID sklop"),
    ("EARPIECE", "slušalka in senzorski sklop"),
    ("CHARGING_PORT", "polnilni priključek"),
    ("LOUDSPEAKER", "zvočnik"),
    ("TAPTIC", "vibracijski motor"),
    ("HOUSING", "ohišje"),
    ("LOGIC_BOARD", "matična plošča"),
    ("WIRELESS_CHARGING", "brezžično polnjenje"),
)


def pulled_module_catalog(_model: str) -> list[tuple[str, str]]:
    """Vrne telefonske module; model ostane del identitete artikla."""
    return list(PULLED_MODULES)


def _pulled_internal_code(model: str, module_code: str) -> str:
    digest = hashlib.sha1(
        f"{model.strip().casefold()}|{module_code.strip().upper()}".encode("utf-8")
    ).hexdigest()[:12].upper()
    return f"PP-{digest}"


def _ensure_pulled_part_article(
    connection: sqlite3.Connection, *, model: str, module_code: str,
    module_name: str, recommended_sale_price: Any,
) -> int:
    internal_code = _pulled_internal_code(model, module_code)
    row = connection.execute(
        "SELECT id FROM inventory_items WHERE internal_code=?", (internal_code,)
    ).fetchone()
    price = _money(recommended_sale_price)
    if row:
        # Zunanji vir spremeni priporočilo za novo pridobljeni kos in razdelitev
        # donorjevega stroška, nikoli pa samodejno ne prepiše aktivnega cenika.
        return int(row[0])
    if price <= 0:
        raise V29Error(
            f"Za uporabni modul »{module_name}« manjka preverjena priporočena prodajna cena."
        )
    stamp = now_text()
    public_name = f"Rabljeni izvlečeni del – {module_name} za {model}"
    cursor = connection.execute(
        """
        INSERT INTO inventory_items(
            barcode,article_name,internal_code,stock_qty,price,created_at,updated_at,
            article_kind,category,track_mode,condition_grade,location,
            price_status,source_kind,active
        ) VALUES(?,?,?,0,?,?,?,'PART','RABLJENI IZVLEČENI DELI','UNIQUE',
                 'RABLJEN_TESTIRAN','KARANTENA-DELI','POTRJENA','DONOR_DEVICE',1)
        """,
        (f"{internal_code}-MASTER", public_name, internal_code, price, stamp, stamp),
    )
    return int(cursor.lastrowid)


@dataclass(frozen=True)
class InventoryExceptionLine:
    article_id: int
    quantity_delta: float
    lot_serial: str = ""


def recommend_price(
    acquisition_cost: Any,
    profile: dict[str, Any],
    *,
    profile_code: str = "SPLOŠNO",
) -> PriceRecommendation:
    """Potrjena tristopenjska maržna formula; rezultat ostane priporočilo."""
    try:
        cost = round(float(str(acquisition_cost or "0").replace(",", ".")), 4)
    except (TypeError, ValueError) as exc:
        raise V29Error("Nabavna vrednost mora biti številka.") from exc
    if cost < 0:
        raise V29Error("Nabavna vrednost ne sme biti negativna.")
    reserve = max(0.0, float(profile.get("warranty_reserve", 0)))
    tiers = list(profile.get("tiers") or [])
    if tiers:
        if len(tiers) != 3:
            raise V29Error("Cenovni profil mora imeti natanko tri stroškovne razrede.")
        selected = next(
            (
                tier for tier in tiers
                if tier.get("max_cost_eur") is None
                or cost <= float(tier["max_cost_eur"])
            ),
            None,
        )
        if selected is None:
            raise V29Error("Cenovni profil nima odprtega zadnjega razreda.")
        target_margin = float(selected.get("target_margin", -1))
    else:
        # Združljivost pri uvozu stare baze: uporablja se samo zapisana ciljna
        # marža. Stari pavšal, minimalni prispevek in hard_margin se ignorirajo.
        target_margin = float(profile.get("target_margin", 0))
    if not 0 <= target_margin < 0.80:
        raise V29Error("Ciljna marža mora biti med 0 in manj kot 80 %.")

    landed = round(cost * (1.0 + reserve), 4)
    target = landed / (1.0 - target_margin)
    floor = round(cost * PART_FLOOR_FACTOR, 2)
    return PriceRecommendation(
        landed_cost=landed,
        recommended_price=round_up(target, 1.0),
        hard_floor=floor,
        profile_code=profile_code,
        target_margin=target_margin,
        warranty_reserve=reserve,
        profile_version=int(profile.get("version") or profile.get("version_no") or 1),
    )


def discounted_unit_price(base_price: Any, discount_pct: Any) -> float:
    base = _money(base_price)
    discount = float(str(discount_pct or "0").replace(",", "."))
    if not 0 <= discount <= 100:
        raise V29Error("Rabat mora biti med 0 in 100 odstotki.")
    return round(base * (1.0 - discount / 100.0), 2)


def validate_document_line_price(
    *,
    line_kind: str,
    base_price: Any,
    discount_pct: Any,
    acquisition_cost: Any = 0,
    labor_floor_eur: Any = 22,
) -> float:
    """Del: nabavna vrednost + 5 %. Delo/storitev: L0."""
    final = discounted_unit_price(base_price, discount_pct)
    kind = str(line_kind or "").strip().upper()
    if kind == "PART":
        floor = round(_money(acquisition_cost) * PART_FLOOR_FACTOR, 2)
        if final + 1e-9 < floor:
            raise V29Error(
                f"Cena rezervnega dela po rabatu ne sme biti nižja od nabavne vrednosti + 5 % ({floor:.2f} EUR)."
            )
    elif kind in {"LABOR", "SERVICE", "DIAGNOSTIC", "LOGISTICS"}:
        floor = _money(labor_floor_eur)
        if final + 1e-9 < floor:
            raise V29Error(
                f"Cena storitve po rabatu ne sme biti nižja od L0 ({floor:.2f} EUR)."
            )
    else:
        raise V29Error("Neveljavna vrsta postavke.")
    return final


def validate_document_quantity(value: Any) -> int:
    """Reject malformed or implausibly large Office document quantities."""
    try:
        quantity = float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise V29Error("Količina mora biti celo število večje od 0.") from exc
    if not math.isfinite(quantity) or quantity <= 0 or abs(quantity - round(quantity)) > 1e-9:
        raise V29Error("Količina mora biti celo število večje od 0.")
    quantity = int(round(quantity))
    if quantity > MAX_DOCUMENT_QUANTITY:
        raise V29Error(
            f"Količina je previsoka. Največja dovoljena količina je {MAX_DOCUMENT_QUANTITY:,}."
            .replace(",", ".")
        )
    return quantity


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(connection: sqlite3.Connection, table: str, wanted: dict[str, str]) -> None:
    existing = _columns(connection, table)
    for name, definition in wanted.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def connect(path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=20)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def ensure_inventory_schema(inventory_db: Path | str, pricing: dict[str, Any]) -> None:
    connection = connect(inventory_db)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL UNIQUE,
            article_name TEXT NOT NULL,
            internal_code TEXT,
            stock_qty REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            lot_serial TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _add_columns(
        connection,
        "inventory_items",
        {
            "article_kind": "TEXT NOT NULL DEFAULT 'PART'",
            "category": "TEXT NOT NULL DEFAULT 'SPLOŠNO'",
            "supplier_name": "TEXT",
            "supplier_sku": "TEXT",
            "track_mode": "TEXT NOT NULL DEFAULT 'QUANTITY'",
            "condition_grade": "TEXT NOT NULL DEFAULT 'NOVO'",
            "location": "TEXT",
            "reserved_qty": "REAL NOT NULL DEFAULT 0",
            "acquisition_cost": "REAL NOT NULL DEFAULT 0",
            "price_profile_code": "TEXT NOT NULL DEFAULT 'SPLOŠNO'",
            "price_status": "TEXT NOT NULL DEFAULT 'POTRJENA'",
            "source_kind": "TEXT",
            "source_id": "INTEGER",
            "tax_classification": "TEXT NOT NULL DEFAULT 'UNCLASSIFIED'",
            "tax_rate": "REAL",
            "active": "INTEGER NOT NULL DEFAULT 1",
        },
    )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            target_margin REAL NOT NULL,
            hard_margin REAL NOT NULL,
            min_contribution_eur REAL NOT NULL,
            consumables_eur REAL NOT NULL,
            warranty_reserve REAL NOT NULL,
            round_step_eur REAL NOT NULL DEFAULT 5,
            needs_review INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_profile_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            label TEXT NOT NULL,
            version_no INTEGER NOT NULL,
            tiers_json TEXT NOT NULL,
            warranty_reserve REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'SYSTEM',
            UNIQUE(code, version_no)
        );
        CREATE TABLE IF NOT EXISTS price_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            active_from TEXT NOT NULL,
            acquisition_cost REAL NOT NULL,
            recommended_price REAL NOT NULL,
            confirmed_price REAL NOT NULL,
            profile_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES inventory_items(id)
        );
        CREATE TABLE IF NOT EXISTS stock_movements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movement_no TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            article_id INTEGER NOT NULL,
            quantity_delta REAL NOT NULL,
            balance_after REAL NOT NULL,
            movement_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            source_line_id INTEGER,
            lot_serial TEXT,
            location_from TEXT,
            location_to TEXT,
            reason TEXT,
            operator_code TEXT NOT NULL,
            reversal_of_id INTEGER,
            FOREIGN KEY(article_id) REFERENCES inventory_items(id),
            FOREIGN KEY(reversal_of_id) REFERENCES stock_movements(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_source_once
            ON stock_movements(source_type, source_id, source_line_id, movement_type)
            WHERE source_id IS NOT NULL AND source_line_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_stock_article_time
            ON stock_movements(article_id, id);
        CREATE TABLE IF NOT EXISTS used_devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_code TEXT NOT NULL UNIQUE,
            buyback_id INTEGER NOT NULL UNIQUE,
            buyback_no TEXT NOT NULL,
            model TEXT NOT NULL,
            serial TEXT NOT NULL,
            imei TEXT NOT NULL,
            storage TEXT,
            condition_text TEXT,
            purpose TEXT NOT NULL DEFAULT 'UNDECIDED',
            status TEXT NOT NULL DEFAULT 'QUARANTINE',
            acquisition_cost REAL NOT NULL,
            allocated_cost REAL NOT NULL DEFAULT 0,
            sale_price REAL,
            buyer_name TEXT,
            sale_document_id INTEGER,
            warranty_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_used_device_identity
            ON used_devices(serial, imei, status);
        CREATE TABLE IF NOT EXISTS donor_harvests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            harvest_no TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            usable_parts INTEGER NOT NULL DEFAULT 0,
            allocated_unit_cost REAL NOT NULL DEFAULT 0,
            notes TEXT,
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(device_id) REFERENCES used_devices(id)
        );
        CREATE TABLE IF NOT EXISTS donor_parts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            harvest_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            allocated_cost REAL NOT NULL,
            condition_grade TEXT NOT NULL,
            test_result TEXT NOT NULL,
            lot_serial TEXT,
            location TEXT,
            stock_movement_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(harvest_id) REFERENCES donor_harvests(id),
            FOREIGN KEY(article_id) REFERENCES inventory_items(id),
            FOREIGN KEY(stock_movement_id) REFERENCES stock_movements(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_donor_part_piece_barcode
            ON donor_parts(lot_serial) WHERE lot_serial IS NOT NULL AND lot_serial<>'';
        CREATE TABLE IF NOT EXISTS donor_waste_records_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            harvest_id INTEGER NOT NULL,
            device_id INTEGER NOT NULL,
            waste_type TEXT NOT NULL,
            description TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT 'kos',
            status TEXT NOT NULL DEFAULT 'AWAITING_HANDOFF',
            collector_document_path TEXT,
            created_at TEXT NOT NULL,
            handed_over_at TEXT,
            FOREIGN KEY(harvest_id) REFERENCES donor_harvests(id),
            FOREIGN KEY(device_id) REFERENCES used_devices(id)
        );
        CREATE TABLE IF NOT EXISTS inventory_exception_documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_no TEXT NOT NULL UNIQUE,
            document_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            reason TEXT NOT NULL,
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            booked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory_exception_lines(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            direction INTEGER NOT NULL,
            lot_serial TEXT,
            stock_movement_id INTEGER,
            FOREIGN KEY(document_id) REFERENCES inventory_exception_documents(id),
            FOREIGN KEY(article_id) REFERENCES inventory_items(id),
            FOREIGN KEY(stock_movement_id) REFERENCES stock_movements(id)
        );
        """
    )
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "documents" in tables:
        _add_columns(
            connection,
            "documents",
            {"used_goods_12m_signed": "INTEGER NOT NULL DEFAULT 0"},
        )
    _add_columns(
        connection,
        "used_devices",
        {"inventory_article_id": "INTEGER"},
    )
    _add_columns(
        connection,
        "donor_harvests",
        {
            "acquisition_cost": "REAL NOT NULL DEFAULT 0",
            "t1_cost": "REAL NOT NULL DEFAULT 0",
            "direct_costs": "REAL NOT NULL DEFAULT 0",
            "total_cost": "REAL NOT NULL DEFAULT 0",
            "allocation_basis_json": "TEXT NOT NULL DEFAULT '{}'",
            "reversed_at": "TEXT",
            "reversed_by": "TEXT",
            "reversal_reason": "TEXT",
        },
    )
    _add_columns(
        connection,
        "donor_parts",
        {
            "module_code": "TEXT NOT NULL DEFAULT ''",
            "recommended_sale_value": "REAL NOT NULL DEFAULT 0",
            "price_source_kind": "TEXT NOT NULL DEFAULT ''",
            "price_source_url": "TEXT NOT NULL DEFAULT ''",
            "price_observed_on": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "price_history",
        {
            "profile_version": "INTEGER NOT NULL DEFAULT 1",
            "target_margin_snapshot": "REAL NOT NULL DEFAULT 0",
            "warranty_reserve_snapshot": "REAL NOT NULL DEFAULT 0",
            "actual_margin": "REAL NOT NULL DEFAULT 0",
        },
    )

    profiles = dict(pricing.get("categories", {}))
    stamp = now_text()
    for code, rule in profiles.items():
        tiers = list(rule.get("tiers") or [])
        if not tiers:
            tiers = [
                {"max_cost_eur": 0, "target_margin": float(rule.get("target_margin", 0))},
                {"max_cost_eur": 0, "target_margin": float(rule.get("target_margin", 0))},
                {"max_cost_eur": None, "target_margin": float(rule.get("target_margin", 0))},
            ]
        version_no = int(rule.get("version") or 1)
        tiers_json = json.dumps(tiers, ensure_ascii=False, separators=(",", ":"))
        # Potrjena sistemska različica v1 namensko nadomesti stare razvojne
        # profile. Poznejših uporabniških različic inicializacija ne prepiše.
        connection.execute(
            "UPDATE price_profile_versions SET active=0 WHERE code=? AND version_no<?",
            (code, version_no),
        )
        connection.execute(
            """
            INSERT INTO price_profile_versions(
                code,label,version_no,tiers_json,warranty_reserve,active,created_at,created_by
            ) VALUES(?,?,?,?,?,1,?,'SYSTEM')
            ON CONFLICT(code,version_no) DO NOTHING
            """,
            (code, code, version_no, tiers_json, float(rule.get("warranty_reserve", 0)), stamp),
        )
        connection.execute(
            """
            INSERT INTO price_profiles(
                code, label, target_margin, hard_margin,
                min_contribution_eur, consumables_eur, warranty_reserve,
                round_step_eur, needs_review, active, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,1,1,?,?)
            ON CONFLICT(code) DO NOTHING
            """,
            (
                code,
                code,
                float(tiers[0].get("target_margin", 0)),
                0.0,
                0.0,
                0.0,
                float(rule.get("warranty_reserve", 0)),
                1.0,
                stamp,
                stamp,
            ),
        )

    rows = connection.execute(
        "SELECT id, stock_qty, lot_serial, created_at FROM inventory_items"
    ).fetchall()
    for article_id, balance, lot_serial, created_at in rows:
        exists = connection.execute(
            "SELECT 1 FROM stock_movements WHERE article_id=? LIMIT 1", (article_id,)
        ).fetchone()
        if not exists and abs(float(balance or 0)) > 1e-9:
            _insert_movement(
                connection,
                article_id=int(article_id),
                quantity_delta=float(balance),
                movement_type="OPENING_BALANCE",
                source_type="V28_MIGRATION",
                source_id=int(article_id),
                source_line_id=int(article_id),
                lot_serial=str(lot_serial or ""),
                reason="Začetno stanje, preneseno iz v28.",
                operator_code="SYSTEM",
                created_at=str(created_at or stamp),
                update_balance=False,
            )
    connection.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    ensure_lot_schema(connection, "main")
    connection.commit()
    connection.close()


def ensure_office_schema(office_db: Path | str) -> None:
    connection = connect(office_db)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS partners(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            post_city TEXT,
            country TEXT,
            tax_no TEXT,
            registration_no TEXT,
            iban TEXT,
            phone TEXT,
            email TEXT,
            contact_person TEXT,
            partner_type TEXT NOT NULL DEFAULT 'DOBAVITELJ',
            pcd_days REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _add_columns(
        connection,
        "partners",
        {
            "address": "TEXT",
            "post_city": "TEXT",
            "country": "TEXT",
            "tax_no": "TEXT",
            "registration_no": "TEXT",
            "iban": "TEXT",
            "phone": "TEXT",
            "email": "TEXT",
            "contact_person": "TEXT",
            "partner_type": "TEXT NOT NULL DEFAULT 'DOBAVITELJ'",
            "pcd_days": "REAL NOT NULL DEFAULT 0",
            "notes": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        },
    )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS receipts_v29(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_no TEXT NOT NULL UNIQUE,
            partner_id INTEGER,
            partner_name_snapshot TEXT NOT NULL,
            received_at TEXT NOT NULL,
            source_document_type TEXT NOT NULL,
            source_external_no TEXT,
            invoice_status TEXT NOT NULL,
            invoice_external_no TEXT,
            invoice_date TEXT,
            provisional_cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            correction_of_id INTEGER,
            storno_reason TEXT,
            notes TEXT,
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            booked_at TEXT,
            storno_at TEXT,
            FOREIGN KEY(partner_id) REFERENCES partners(id),
            FOREIGN KEY(correction_of_id) REFERENCES receipts_v29(id)
        );
        CREATE TABLE IF NOT EXISTS receipt_documents_v29(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            external_no TEXT,
            document_date TEXT,
            file_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(receipt_id) REFERENCES receipts_v29(id)
        );
        CREATE TABLE IF NOT EXISTS receipt_lines_v29(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            purchase_unit_cost REAL NOT NULL,
            confirmed_sale_price REAL NOT NULL,
            lot_serial TEXT,
            location TEXT,
            stock_movement_id INTEGER,
            FOREIGN KEY(receipt_id) REFERENCES receipts_v29(id)
        );
        CREATE INDEX IF NOT EXISTS idx_receipt_status
            ON receipts_v29(status, invoice_status, received_at);
        CREATE TABLE IF NOT EXISTS document_links_v29(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_type TEXT NOT NULL,
            from_id INTEGER NOT NULL,
            to_type TEXT NOT NULL,
            to_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(from_type, from_id, to_type, to_id, link_type)
        );
        """
    )
    connection.commit()
    connection.close()


def ensure_service_schema(service_db: Path | str) -> None:
    connection = connect(service_db)
    # Bazi brez starih tabel dovolimo varen prazen začetek.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS buybacks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyback_no TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            seller_name TEXT NOT NULL DEFAULT '',
            seller_tax_number TEXT NOT NULL DEFAULT '',
            seller_birth_date TEXT NOT NULL DEFAULT '',
            seller_address TEXT NOT NULL DEFAULT '',
            seller_iban TEXT NOT NULL DEFAULT '',
            identity_checked INTEGER NOT NULL DEFAULT 0,
            device_model TEXT NOT NULL DEFAULT '',
            device_serial TEXT NOT NULL DEFAULT '',
            device_imei TEXT NOT NULL DEFAULT '',
            condition_text TEXT NOT NULL DEFAULT '',
            acquisition_method TEXT NOT NULL DEFAULT '',
            imei_check_provider TEXT NOT NULL DEFAULT '',
            imei_check_result TEXT NOT NULL DEFAULT '',
            imei_check_reference TEXT NOT NULL DEFAULT '',
            imei_checked_at TEXT NOT NULL DEFAULT '',
            account_removed INTEGER NOT NULL DEFAULT 0,
            activation_lock_removed INTEGER NOT NULL DEFAULT 0,
            mdm_clear INTEGER NOT NULL DEFAULT 0,
            ownership_declaration INTEGER NOT NULL DEFAULT 0,
            privacy_notice_received INTEGER NOT NULL DEFAULT 0,
            risk_level TEXT NOT NULL DEFAULT '',
            purchase_price REAL NOT NULL DEFAULT 0,
            payout_method TEXT NOT NULL DEFAULT '',
            operator_code TEXT NOT NULL DEFAULT 'DR',
            microdetect_confirmed INTEGER NOT NULL DEFAULT 0,
            microdetect_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _add_columns(
        connection,
        "buybacks",
        {
            "seller_tax_number": "TEXT NOT NULL DEFAULT ''",
            "device_storage": "TEXT",
            "purpose": "TEXT NOT NULL DEFAULT 'UNDECIDED'",
            "buyer_legal_name": "TEXT",
            "buyer_tax_number": "TEXT",
            "buyer_registration_number": "TEXT",
            "buyer_address": "TEXT",
            "buyer_representative": "TEXT",
            "agent_name": "TEXT",
            "authorization_reference": "TEXT",
            "device_inventory_id": "INTEGER",
            "handover_at": "TEXT",
            "payment_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        },
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS refurbishment_orders_v29(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT NOT NULL UNIQUE,
            used_device_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'DIAGNOSTICS',
            labor_cost REAL NOT NULL DEFAULT 0,
            parts_cost REAL NOT NULL DEFAULT 0,
            other_cost REAL NOT NULL DEFAULT 0,
            tests_passed INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    if "service_orders" in {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        _add_columns(
            connection,
            "service_orders",
            {
                "authorized_limit_eur": "REAL",
                "approval_status": "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
                "workflow_status": "TEXT NOT NULL DEFAULT 'RECEIVED'",
            },
        )
    connection.commit()
    connection.close()


def initialize_v29(
    *, inventory_db: Path | str, office_db: Path | str, service_db: Path | str,
    pricing: dict[str, Any]
) -> None:
    ensure_inventory_schema(inventory_db, pricing)
    ensure_office_schema(office_db)
    ensure_service_schema(service_db)


def _next_number(connection: sqlite3.Connection, table: str, column: str, prefix: str) -> str:
    year = datetime.now().year
    base = f"{prefix}-{year}-"
    values = connection.execute(
        f"SELECT {column} FROM {table} WHERE {column} LIKE ?", (f"{base}%",)
    ).fetchall()
    highest = 0
    for (value,) in values:
        try:
            highest = max(highest, int(str(value).rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            pass
    return f"{base}{highest + 1:04d}"


def _insert_movement(
    connection: sqlite3.Connection,
    *, article_id: int, quantity_delta: float, movement_type: str,
    source_type: str, source_id: int | None, source_line_id: int | None,
    lot_serial: str = "", location_from: str = "", location_to: str = "",
    reason: str = "", operator_code: str = "DR", reversal_of_id: int | None = None,
    created_at: str | None = None, update_balance: bool = True,
) -> int:
    row = connection.execute(
        "SELECT stock_qty FROM inventory_items WHERE id=? AND active=1", (article_id,)
    ).fetchone()
    if not row:
        raise V29Error(f"Artikel ID {article_id} ne obstaja ali ni aktiven.")
    old = float(row[0] or 0)
    new = round(old + float(quantity_delta), 4)
    if new < -1e-9:
        raise V29Error("Premik bi ustvaril negativno zalogo.")
    movement_no = _next_number(connection, "stock_movements", "movement_no", "PREM")
    cursor = connection.execute(
        """
        INSERT INTO stock_movements(
            movement_no, created_at, article_id, quantity_delta, balance_after,
            movement_type, source_type, source_id, source_line_id,
            lot_serial, location_from, location_to, reason, operator_code,
            reversal_of_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            movement_no, created_at or now_text(), int(article_id),
            float(quantity_delta), new, movement_type, source_type,
            source_id, source_line_id, lot_serial, location_from, location_to,
            reason, operator_code, reversal_of_id,
        ),
    )
    if update_balance:
        connection.execute(
            "UPDATE inventory_items SET stock_qty=?, updated_at=? WHERE id=?",
            (new, now_text(), article_id),
        )
    return int(cursor.lastrowid)


def _profile(connection: sqlite3.Connection, code: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT code, version_no, tiers_json, warranty_reserve
        FROM price_profile_versions
        WHERE code=? AND active=1
        ORDER BY version_no DESC LIMIT 1
        """,
        (code,),
    ).fetchone()
    if not row and code != "SPLOŠNO":
        row = connection.execute(
            """
            SELECT code, version_no, tiers_json, warranty_reserve
            FROM price_profile_versions
            WHERE code='SPLOŠNO' AND active=1
            ORDER BY version_no DESC LIMIT 1
            """
        ).fetchone()
    if row:
        try:
            tiers = json.loads(row[2])
        except (TypeError, json.JSONDecodeError) as exc:
            raise V29Error("Shranjena cenovna matrika je poškodovana.") from exc
        return {
            "code": str(row[0]),
            "version": int(row[1]),
            "tiers": tiers,
            "warranty_reserve": float(row[3]),
            "round_step_eur": 1.0,
        }

    # Samo migracijski izhod za zelo stare baze brez različic profilov.
    row = connection.execute(
        """
        SELECT target_margin, hard_margin, min_contribution_eur,
               consumables_eur, warranty_reserve, round_step_eur
        FROM price_profiles WHERE code=? AND active=1
        """,
        (code,),
    ).fetchone()
    if not row:
        row = connection.execute(
            """
            SELECT target_margin, hard_margin, min_contribution_eur,
                   consumables_eur, warranty_reserve, round_step_eur
            FROM price_profiles WHERE code='SPLOŠNO' AND active=1
            """
        ).fetchone()
        code = "SPLOŠNO"
    if not row:
        raise V29Error("Manjka aktiven cenovni profil.")
    return {
        "code": code,
        "target_margin": row[0], "hard_margin": row[1],
        "min_contribution_eur": row[2], "consumables_eur": row[3],
        "warranty_reserve": row[4], "round_step_eur": row[5],
        "version": 1,
    }


def confirm_article_price(
    inventory_db: Path | str, *, article_id: int, acquisition_cost: Any,
    confirmed_price: Any, profile_code: str, reason: str,
    operator_code: str = "DR", source_type: str = "OFFICE",
    source_id: int | None = None,
) -> PriceRecommendation:
    connection = connect(inventory_db)
    try:
        profile = _profile(connection, profile_code)
        rec = recommend_price(acquisition_cost, profile, profile_code=profile["code"])
        confirmed = _money(confirmed_price)
        acquisition = _cost4(acquisition_cost)
        minimum = round(acquisition * PART_FLOOR_FACTOR, 2)
        if confirmed + 1e-9 < minimum:
            raise V29Error(
                f"Potrjena cena ne sme biti nižja od nabavne vrednosti + 5 % ({minimum:.2f} EUR)."
            )
        stamp = now_text()
        connection.execute(
            """
            UPDATE inventory_items
            SET acquisition_cost=?, price=?, price_profile_code=?,
                price_status='POTRJENA', updated_at=?
            WHERE id=? AND active=1
            """,
            (acquisition, confirmed, rec.profile_code, stamp, article_id),
        )
        if connection.total_changes == 0:
            raise V29Error("Artikla ni mogoče najti.")
        connection.execute(
            """
            INSERT INTO price_history(
                article_id, active_from, acquisition_cost, recommended_price,
                confirmed_price, profile_code, reason, source_type, source_id,
                operator_code, created_at, profile_version,
                target_margin_snapshot, warranty_reserve_snapshot, actual_margin
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                article_id, stamp, acquisition, rec.recommended_price,
                confirmed, rec.profile_code, reason, source_type, source_id,
                operator_code, stamp, rec.profile_version, rec.target_margin,
                rec.warranty_reserve,
                round(1.0 - rec.landed_cost / confirmed, 6) if confirmed else 0.0,
            ),
        )
        connection.commit()
        return rec
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_and_book_receipt(
    *, office_db: Path | str, inventory_db: Path | str,
    partner_id: int | None, partner_name: str, source_document_type: str,
    source_external_no: str, invoice_external_no: str,
    invoice_date: str, received_at: str, provisional_cost: bool,
    lines: Sequence[ReceiptLine], notes: str = "", operator_code: str = "DR",
    correction_of_id: int | None = None,
    source_file_path: str = "", invoice_file_path: str = "",
) -> tuple[int, str]:
    allowed = {"DOBAVNICA_IN_RAČUN", "DOBAVNICA", "RAČUN", "DRUGO"}
    source_kind = str(source_document_type or "").strip().upper()
    if source_kind not in allowed:
        raise V29Error("Neveljavna vrsta dokumenta ob prejemu.")
    if not str(partner_name or "").strip():
        raise V29Error("Pri prejemu manjka dobavitelj.")
    if not lines:
        raise V29Error("Prejem nima nobene postavke.")
    if provisional_cost and source_kind in {"DOBAVNICA_IN_RAČUN", "RAČUN"}:
        raise V29Error("Prejem z računom ne more imeti začasne nabavne cene.")

    connection = connect(office_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        receipt_no = _next_number(connection, "receipts_v29", "receipt_no", "PRE")
        stamp = now_text()
        invoice_status = "RECEIVED" if source_kind in {"DOBAVNICA_IN_RAČUN", "RAČUN"} else "WAITING"
        cursor = connection.execute(
            """
            INSERT INTO receipts_v29(
                receipt_no, partner_id, partner_name_snapshot, received_at,
                source_document_type, source_external_no, invoice_status,
                invoice_external_no, invoice_date, provisional_cost, status,
                correction_of_id, notes, operator_code, created_at, booked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?, 'BOOKED', ?,?,?,?,?)
            """,
            (
                receipt_no, partner_id, partner_name.strip(), received_at,
                source_kind, source_external_no.strip(), invoice_status,
                invoice_external_no.strip(), invoice_date.strip(),
                int(bool(provisional_cost)), correction_of_id, notes.strip(),
                operator_code, stamp, stamp,
            ),
        )
        receipt_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO receipt_documents_v29(
                receipt_id, document_type, external_no, document_date,
                file_path, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                receipt_id, source_kind, source_external_no.strip(), received_at,
                source_file_path.strip(), stamp,
            ),
        )
        if invoice_status == "RECEIVED":
            connection.execute(
                """
                INSERT INTO receipt_documents_v29(
                    receipt_id, document_type, external_no, document_date,
                    file_path, created_at
                ) VALUES(?, 'RAČUN', ?,?,?,?)
                """,
                (
                    receipt_id, invoice_external_no.strip(), invoice_date.strip(),
                    invoice_file_path.strip(), stamp,
                ),
            )

        for index, raw in enumerate(lines, start=1):
            qty = _qty(raw.quantity)
            cost = _money(raw.purchase_unit_cost)
            sale = _money(raw.confirmed_sale_price)
            floor = round(cost * PART_FLOOR_FACTOR, 2)
            if sale + 1e-9 < floor:
                raise V29Error(
                    f"Postavka {index}: prodajna cena mora biti najmanj {floor:.2f} EUR."
                )
            article = connection.execute(
                "SELECT id, price_profile_code FROM inv.inventory_items WHERE id=? AND active=1",
                (int(raw.article_id),),
            ).fetchone()
            if not article:
                raise V29Error(f"Postavka {index}: artikel ne obstaja.")
            line_cursor = connection.execute(
                """
                INSERT INTO receipt_lines_v29(
                    receipt_id, article_id, quantity, purchase_unit_cost,
                    confirmed_sale_price, lot_serial, location
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    receipt_id, int(raw.article_id), qty, cost, sale,
                    raw.lot_serial.strip(), raw.location.strip(),
                ),
            )
            line_id = int(line_cursor.lastrowid)
            movement_id = _insert_movement(
                connection,
                article_id=int(raw.article_id), quantity_delta=qty,
                movement_type="RECEIPT_IN", source_type="RECEIPT",
                source_id=receipt_id, source_line_id=line_id,
                lot_serial=raw.lot_serial.strip(), location_to=raw.location.strip(),
                reason=f"Prejem {receipt_no}", operator_code=operator_code,
            )
            connection.execute(
                "UPDATE receipt_lines_v29 SET stock_movement_id=? WHERE id=?",
                (movement_id, line_id),
            )
            price_status = "ZAČASNA" if provisional_cost else "POTRJENA"
            connection.execute(
                """
                UPDATE inv.inventory_items
                SET acquisition_cost=?, price=?, price_status=?,
                    lot_serial=CASE WHEN ?<>'' THEN ? ELSE lot_serial END,
                    location=CASE WHEN ?<>'' THEN ? ELSE location END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    cost, sale, price_status,
                    raw.lot_serial.strip(), raw.lot_serial.strip(),
                    raw.location.strip(), raw.location.strip(), stamp,
                    int(raw.article_id),
                ),
            )
            profile = _profile(connection, str(article[1] or "SPLOŠNO"))
            recommendation = recommend_price(cost, profile, profile_code=profile["code"])
            connection.execute(
                """
                INSERT INTO inv.price_history(
                    article_id, active_from, acquisition_cost, recommended_price,
                    confirmed_price, profile_code, reason, source_type, source_id,
                    operator_code, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(raw.article_id), stamp, cost,
                    recommendation.recommended_price, sale,
                    recommendation.profile_code,
                    "Prejem blaga – začasna cena" if provisional_cost else "Prejem blaga",
                    "RECEIPT", receipt_id, operator_code, stamp,
                ),
            )
        connection.commit()
        return receipt_id, receipt_no
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def attach_invoice_to_receipt(
    *, office_db: Path | str, receipt_id: int, invoice_external_no: str,
    invoice_date: str, file_path: str = "", operator_code: str = "DR",
) -> None:
    if not invoice_external_no.strip():
        raise V29Error("Manjka številka prejetega računa.")
    connection = connect(office_db)
    try:
        row = connection.execute(
            "SELECT status FROM receipts_v29 WHERE id=?", (receipt_id,)
        ).fetchone()
        if not row or row[0] != "BOOKED":
            raise V29Error("Račun je mogoče povezati samo z veljavnim knjiženim prejemom.")
        stamp = now_text()
        connection.execute(
            """
            UPDATE receipts_v29
            SET invoice_status='RECEIVED', invoice_external_no=?, invoice_date=?,
                provisional_cost=0
            WHERE id=?
            """,
            (invoice_external_no.strip(), invoice_date.strip(), receipt_id),
        )
        connection.execute(
            """
            INSERT INTO receipt_documents_v29(
                receipt_id, document_type, external_no, document_date,
                file_path, created_at
            ) VALUES(?, 'RAČUN', ?,?,?,?)
            """,
            (receipt_id, invoice_external_no.strip(), invoice_date.strip(), file_path, stamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def storno_receipt(
    *, office_db: Path | str, inventory_db: Path | str, receipt_id: int,
    reason: str, operator_code: str = "DR",
) -> str:
    if not reason.strip():
        raise V29Error("Pri stornu je obvezen razlog.")
    connection = connect(office_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        receipt = connection.execute(
            "SELECT receipt_no, status FROM receipts_v29 WHERE id=?", (receipt_id,)
        ).fetchone()
        if not receipt:
            raise V29Error("Prejema ni mogoče najti.")
        if receipt[1] != "BOOKED":
            raise V29Error("Stornirati je mogoče samo knjižen prejem.")
        lines = connection.execute(
            """
            SELECT id, article_id, quantity, lot_serial, stock_movement_id
            FROM receipt_lines_v29 WHERE receipt_id=? ORDER BY id
            """,
            (receipt_id,),
        ).fetchall()
        for line_id, article_id, qty, _lot, _movement_id in lines:
            row = connection.execute(
                "SELECT stock_qty, reserved_qty FROM inv.inventory_items WHERE id=?",
                (article_id,),
            ).fetchone()
            if not row or float(row[0] or 0) - float(row[1] or 0) + 1e-9 < float(qty):
                raise V29Error(
                    "Storno je blokiran, ker je artikel že rezerviran, porabljen ali prodan."
                )
        stamp = now_text()
        for line_id, article_id, qty, lot, movement_id in lines:
            _insert_movement(
                connection, article_id=int(article_id), quantity_delta=-float(qty),
                movement_type="RECEIPT_STORNO", source_type="RECEIPT_STORNO",
                source_id=receipt_id, source_line_id=int(line_id), lot_serial=str(lot or ""),
                reason=reason.strip(), operator_code=operator_code,
                reversal_of_id=int(movement_id) if movement_id else None,
            )
        connection.execute(
            """
            UPDATE receipts_v29
            SET status='STORNIRANO', storno_reason=?, storno_at=?
            WHERE id=?
            """,
            (reason.strip(), stamp, receipt_id),
        )
        connection.commit()
        return str(receipt[0])
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def register_buyback_device(
    *, service_db: Path | str, inventory_db: Path | str, buyback_id: int,
    purpose: str, buyer: dict[str, str], operator_code: str = "DR",
) -> tuple[int, str]:
    allowed = {"REFURBISH", "PARTS", "UNDECIDED"}
    purpose = str(purpose or "UNDECIDED").upper()
    if purpose not in allowed:
        raise V29Error("Neveljaven namen odkupljene naprave.")
    connection = connect(service_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        row = connection.execute(
            """
            SELECT buyback_no, status, device_model, device_serial, device_imei,
                   COALESCE(device_storage,''), condition_text, purchase_price,
                   COALESCE(payment_status, 'PENDING')
            FROM buybacks WHERE id=?
            """,
            (buyback_id,),
        ).fetchone()
        if not row:
            raise V29Error("Odkupa ni mogoče najti.")
        if str(row[1] or "") != "COMPLETED" or str(row[8] or "") != "PAID":
            raise V29Error(
                "Naprava lahko vstopi v zalogo šele po podpisu pogodbe in "
                "potrjenem celotnem izplačilu odkupa."
            )
        existing = connection.execute(
            "SELECT id, inventory_code FROM inv.used_devices WHERE buyback_id=?",
            (buyback_id,),
        ).fetchone()
        if existing:
            return int(existing[0]), str(existing[1])
        stamp = now_text()
        code = _next_number(connection, "inv.used_devices", "inventory_code", "NAP")
        cursor = connection.execute(
            """
            INSERT INTO inv.used_devices(
                inventory_code, buyback_id, buyback_no, model, serial, imei,
                storage, condition_text, purpose, status, acquisition_cost,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'QUARANTINE',?,?,?)
            """,
            (
                code, buyback_id, row[0], row[2], row[3], row[4], row[5], row[6],
                purpose, float(row[7] or 0), stamp, stamp,
            ),
        )
        device_id = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE buybacks
            SET purpose=?, buyer_legal_name=?, buyer_tax_number=?,
                buyer_registration_number=?, buyer_address=?, buyer_representative=?,
                device_inventory_id=?, handover_at=COALESCE(handover_at, ?)
            WHERE id=?
            """,
            (
                purpose, buyer.get("legal_name", ""), buyer.get("tax_number", ""),
                buyer.get("registration_number", ""), buyer.get("address", ""),
                buyer.get("representative", "Daniel Ristoski"), device_id, stamp,
                buyback_id,
            ),
        )
        connection.commit()
        return device_id, code
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sync_completed_buybacks_to_inventory(
    *, service_db: Path | str, inventory_db: Path | str,
    buyer: dict[str, str], operator_code: str = "DR",
) -> list[tuple[int, str]]:
    """Samodejno popravi prekinitev med pogodbo odkupa in karanteno.

    Odkup je lahko bil uspešno shranjen tik pred izpadom programa. V tem
    primeru v29 ob naslednjem zagonu ustvari manjkajočo enolično zalogovno
    napravo. Obstoječih povezav ne podvaja.
    """
    connection = connect(service_db)
    rows = connection.execute(
        """
        SELECT id, COALESCE(purpose, 'UNDECIDED')
        FROM buybacks
        WHERE device_inventory_id IS NULL
          AND COALESCE(status, '')='COMPLETED'
          AND trim(COALESCE(device_serial, ''))<>''
        ORDER BY id
        """
    ).fetchall()
    connection.close()
    synced: list[tuple[int, str]] = []
    for buyback_id, purpose in rows:
        _device_id, inventory_code = register_buyback_device(
            service_db=service_db,
            inventory_db=inventory_db,
            buyback_id=int(buyback_id),
            purpose=str(purpose or "UNDECIDED"),
            buyer=buyer,
            operator_code=operator_code,
        )
        synced.append((int(buyback_id), inventory_code))
    return synced


def update_price_profile(
    inventory_db: Path | str, *, code: str, label: str,
    target_margin: Any = 0, hard_margin: Any = 0, min_contribution_eur: Any = 0,
    consumables_eur: Any = 0, warranty_reserve: Any = 0, round_step_eur: Any = 1,
    tiers: Sequence[dict[str, Any]] | None = None,
    operator_code: str = "DR",
) -> None:
    target = float(str(target_margin).replace(",", "."))
    reserve = float(str(warranty_reserve).replace(",", "."))
    if tiers is None:
        tiers = (
            {"max_cost_eur": 0, "target_margin": target},
            {"max_cost_eur": 0, "target_margin": target},
            {"max_cost_eur": None, "target_margin": target},
        )
    tiers = [dict(tier) for tier in tiers]
    if len(tiers) != 3:
        raise V29Error("Cenovni profil mora imeti natanko tri stroškovne razrede.")
    previous = -1.0
    for index, tier in enumerate(tiers):
        margin = float(str(tier.get("target_margin", "")).replace(",", "."))
        if not 0 <= margin < 0.80:
            raise V29Error("Vsaka ciljna marža mora biti med 0 in manj kot 80 %.")
        tier["target_margin"] = margin
        limit = tier.get("max_cost_eur")
        if index == 2:
            if limit is not None:
                raise V29Error("Tretji cenovni razred mora biti odprt.")
        else:
            limit = float(str(limit).replace(",", "."))
            if limit < previous:
                raise V29Error("Stroškovne meje morajo naraščati.")
            tier["max_cost_eur"] = limit
            previous = limit
    if not 0 <= reserve < 1:
        raise V29Error("Garancijska rezerva mora biti med 0 in 100 %.")
    code = code.strip()
    if not code:
        raise V29Error("Koda cenovnega profila je obvezna.")
    connection = connect(inventory_db)
    stamp = now_text()
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM price_profile_versions WHERE code=?",
            (code,),
        ).fetchone()
        version_no = int(row[0] or 0) + 1
        connection.execute("UPDATE price_profile_versions SET active=0 WHERE code=?", (code,))
        connection.execute(
            """
            INSERT INTO price_profile_versions(
                code,label,version_no,tiers_json,warranty_reserve,active,created_at,created_by
            ) VALUES(?,?,?,?,?,1,?,?)
            """,
            (
                code, label.strip() or code, version_no,
                json.dumps(tiers, ensure_ascii=False, separators=(",", ":")),
                reserve, stamp, str(operator_code or "DR"),
            ),
        )
        connection.execute(
            """
            INSERT INTO price_profiles(
                code,label,target_margin,hard_margin,min_contribution_eur,
                consumables_eur,warranty_reserve,round_step_eur,
                needs_review,active,created_at,updated_at
            ) VALUES(?,?,?,0,0,0,?,1,0,1,?,?)
            ON CONFLICT(code) DO UPDATE SET
                label=excluded.label,target_margin=excluded.target_margin,
                hard_margin=0,min_contribution_eur=0,consumables_eur=0,
                warranty_reserve=excluded.warranty_reserve,round_step_eur=1,
                needs_review=0,active=1,updated_at=excluded.updated_at
            """,
            (code, label.strip() or code, tiers[0]["target_margin"], reserve, stamp, stamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_inventory_exception(
    *, inventory_db: Path | str, document_type: str, reason: str,
    lines: Sequence[InventoryExceptionLine], operator_code: str = "DR",
) -> tuple[int, str]:
    allowed = {"ODPIS", "INTERNA_PORABA", "INVENTURNI_ZAPISNIK"}
    document_type = str(document_type or "").upper()
    if document_type not in allowed:
        raise V29Error("Neveljavna vrsta izrednega zalogovnega dokumenta.")
    if not reason.strip() or not lines:
        raise V29Error("Razlog in vsaj ena postavka sta obvezna.")
    connection = connect(inventory_db)
    try:
        stamp = now_text()
        prefix = {"ODPIS": "ODP", "INTERNA_PORABA": "INP", "INVENTURNI_ZAPISNIK": "INV"}[document_type]
        document_no = _next_number(
            connection, "inventory_exception_documents", "document_no", prefix
        )
        cursor = connection.execute(
            """
            INSERT INTO inventory_exception_documents(
                document_no, document_type, status, reason,
                operator_code, created_at, booked_at
            ) VALUES(?,?,'BOOKED',?,?,?,?)
            """,
            (document_no, document_type, reason.strip(), operator_code, stamp, stamp),
        )
        document_id = int(cursor.lastrowid)
        for item in lines:
            delta = round(float(item.quantity_delta), 4)
            if abs(delta) < 1e-9:
                continue
            if document_type in {"ODPIS", "INTERNA_PORABA"} and delta >= 0:
                raise V29Error("Odpis in interna poraba morata zmanjšati zalogo.")
            line_cursor = connection.execute(
                """
                INSERT INTO inventory_exception_lines(
                    document_id, article_id, quantity, direction, lot_serial
                ) VALUES(?,?,?,?,?)
                """,
                (
                    document_id, int(item.article_id), abs(delta),
                    1 if delta > 0 else -1, item.lot_serial.strip(),
                ),
            )
            line_id = int(line_cursor.lastrowid)
            movement_id = _insert_movement(
                connection, article_id=int(item.article_id), quantity_delta=delta,
                movement_type=document_type, source_type="INVENTORY_EXCEPTION",
                source_id=document_id, source_line_id=line_id,
                lot_serial=item.lot_serial.strip(), reason=reason.strip(),
                operator_code=operator_code,
            )
            connection.execute(
                "UPDATE inventory_exception_lines SET stock_movement_id=? WHERE id=?",
                (movement_id, line_id),
            )
        connection.commit()
        return document_id, document_no
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def storno_inventory_exception(
    *, inventory_db: Path | str, document_id: int, reason: str,
    operator_code: str = "DR",
) -> str:
    if not reason.strip():
        raise V29Error("Razlog storna je obvezen.")
    connection = connect(inventory_db)
    try:
        document = connection.execute(
            "SELECT document_no, status FROM inventory_exception_documents WHERE id=?",
            (int(document_id),),
        ).fetchone()
        if not document or document[1] != "BOOKED":
            raise V29Error("Stornirati je mogoče samo knjižen izredni dokument.")
        lines = connection.execute(
            """
            SELECT l.id, l.article_id, l.quantity * l.direction,
                   l.lot_serial, l.stock_movement_id, i.stock_qty
            FROM inventory_exception_lines l
            JOIN inventory_items i ON i.id=l.article_id
            WHERE l.document_id=? ORDER BY l.id
            """,
            (int(document_id),),
        ).fetchall()
        for _line_id, _article_id, original_delta, _lot, _movement_id, stock in lines:
            reversal = -float(original_delta)
            if reversal < 0 and float(stock or 0) + reversal < -1e-9:
                raise V29Error("Storno je blokiran, ker je zaloga že porabljena ali prodana.")
        for line_id, article_id, original_delta, lot, movement_id, _stock in lines:
            _insert_movement(
                connection, article_id=int(article_id),
                quantity_delta=-float(original_delta),
                movement_type="INVENTORY_EXCEPTION_STORNO",
                source_type="INVENTORY_EXCEPTION_STORNO",
                source_id=int(document_id), source_line_id=int(line_id),
                lot_serial=str(lot or ""), reason=reason.strip(),
                operator_code=operator_code, reversal_of_id=int(movement_id),
            )
        connection.execute(
            """
            UPDATE inventory_exception_documents
            SET status='STORNIRANO', reason=reason || ' | STORNO: ' || ?
            WHERE id=?
            """,
            (reason.strip(), int(document_id)),
        )
        connection.commit()
        return str(document[0])
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def start_refurbishment(
    *, service_db: Path | str, inventory_db: Path | str, device_id: int,
    notes: str = "", operator_code: str = "DR",
) -> tuple[int, str]:
    connection = connect(service_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        device = connection.execute(
            "SELECT inventory_code, status FROM inv.used_devices WHERE id=?",
            (int(device_id),),
        ).fetchone()
        if not device or device[1] not in {"QUARANTINE", "DIAGNOSTICS"}:
            raise V29Error("Naprava ni na voljo za začetek obnove.")
        existing = connection.execute(
            """
            SELECT id, order_no FROM refurbishment_orders_v29
            WHERE used_device_id=? AND status<>'COMPLETED' ORDER BY id DESC LIMIT 1
            """,
            (int(device_id),),
        ).fetchone()
        if existing:
            return int(existing[0]), str(existing[1])
        number = _next_number(
            connection, "refurbishment_orders_v29", "order_no", "OBN"
        )
        stamp = now_text()
        cursor = connection.execute(
            """
            INSERT INTO refurbishment_orders_v29(
                order_no, used_device_id, status, notes,
                operator_code, created_at
            ) VALUES(?,?,'DIAGNOSTICS',?,?,?)
            """,
            (number, int(device_id), notes.strip(), operator_code, stamp),
        )
        connection.execute(
            """
            UPDATE inv.used_devices
            SET purpose='REFURBISH', status='DIAGNOSTICS', updated_at=? WHERE id=?
            """,
            (stamp, int(device_id)),
        )
        connection.commit()
        return int(cursor.lastrowid), number
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_refurbishment(
    *, service_db: Path | str, inventory_db: Path | str, order_id: int,
    labor_cost: Any, parts_cost: Any, other_cost: Any, sale_price: Any,
    tests_passed: bool, notes: str = "",
) -> tuple[str, float]:
    if not tests_passed:
        raise V29Error("Obnove ni mogoče zaključiti brez uspešno opravljenega končnega testa.")
    connection = connect(service_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        row = connection.execute(
            """
            SELECT r.order_no, r.used_device_id, r.status, d.acquisition_cost,
                   d.inventory_code, d.model, d.storage, d.serial,
                   d.inventory_article_id
            FROM refurbishment_orders_v29 r
            JOIN inv.used_devices d ON d.id=r.used_device_id
            WHERE r.id=?
            """,
            (int(order_id),),
        ).fetchone()
        if not row or row[2] == "COMPLETED":
            raise V29Error("Naloga obnove ne obstaja ali je že zaključena.")
        labor = _money(labor_cost)
        parts = _money(parts_cost)
        other = _money(other_cost)
        total_cost = round(float(row[3] or 0) + labor + parts + other, 2)
        price = _money(sale_price)
        floor = round(total_cost * PART_FLOOR_FACTOR, 2)
        if price + 1e-9 < floor:
            raise V29Error(
                f"Prodajna cena obnovljene naprave ne sme biti nižja od skupnega stroška + 5 % ({floor:.2f} EUR)."
            )
        stamp = now_text()
        connection.execute(
            """
            UPDATE refurbishment_orders_v29
            SET status='COMPLETED', labor_cost=?, parts_cost=?, other_cost=?,
                tests_passed=1, notes=?, completed_at=? WHERE id=?
            """,
            (labor, parts, other, notes.strip(), stamp, int(order_id)),
        )
        connection.execute(
            """
            UPDATE inv.used_devices
            SET status='READY_FOR_SALE', sale_price=?, updated_at=? WHERE id=?
            """,
            (price, stamp, int(row[1])),
        )
        if not row[8]:
            barcode = str(row[4])
            article_name = " ".join(
                part for part in ("Obnovljena naprava", str(row[5]), str(row[6] or ""))
                if part
            )
            article_cursor = connection.execute(
                """
                INSERT INTO inv.inventory_items(
                    barcode, article_name, internal_code, stock_qty, price,
                    lot_serial, created_at, updated_at, article_kind, category,
                    track_mode, condition_grade, location, acquisition_cost,
                    price_status, source_kind, source_id, active
                ) VALUES(?,?,?,1,?,?,?,?, 'PART','OBNOVLJENA NAPRAVA',
                         'UNIQUE','REFURBISHED','OBNOVLJENE',?,'POTRJENA',
                         'BUYBACK_DEVICE',?,1)
                """,
                (
                    barcode, article_name, barcode, price, str(row[7] or ""),
                    stamp, stamp, total_cost, int(row[1]),
                ),
            )
            article_id = int(article_cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO inv.stock_movements(
                    movement_no, created_at, article_id, quantity_delta,
                    balance_after, movement_type, source_type, source_id,
                    source_line_id, lot_serial, location_from, location_to,
                    reason, operator_code, reversal_of_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    f"PREM-OBN-{int(row[1]):06d}", stamp, article_id, 1, 1,
                    "REFURBISHED_DEVICE_IN", "REFURBISHMENT", int(order_id),
                    int(row[1]), str(row[7] or ""), "KARANTENA", "OBNOVLJENE",
                    f"Zaključena obnova {row[0]}", "DR",
                ),
            )
            connection.execute(
                "UPDATE inv.used_devices SET inventory_article_id=? WHERE id=?",
                (article_id, int(row[1])),
            )
        connection.commit()
        return str(row[0]), total_cost
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def harvest_donor_device(
    *, inventory_db: Path | str, device_id: int, parts: Sequence[HarvestPart],
    notes: str = "", operator_code: str = "DR", t1_hour_eur: Any = 30,
    direct_costs_eur: Any = 0, full_writeoff_confirmed: bool = False,
    waste_modules: Sequence[tuple[str, str]] = (),
) -> tuple[int, str, float]:
    connection = connect(inventory_db)
    try:
        device = connection.execute(
            """
            SELECT inventory_code, status, purpose, acquisition_cost, model
            FROM used_devices WHERE id=?
            """,
            (device_id,),
        ).fetchone()
        if not device:
            raise V29Error("Donorske naprave ni mogoče najti.")
        if device[1] not in {"QUARANTINE", "DIAGNOSTICS", "PARTS"}:
            raise V29Error("Naprava v trenutnem statusu ni na voljo za razstavljanje.")
        if any(str(p.test_result).upper() != "OK" for p in parts):
            raise V29Error("V zalogo se lahko prenesejo samo uspešno testirani deli.")
        acquisition_cost = _cost4(device[3] or 0)
        t1_cost = _money(t1_hour_eur)
        direct_costs = _cost4(direct_costs_eur)
        total_cost = round(acquisition_cost + t1_cost + direct_costs, 2)
        if not parts:
            if not full_writeoff_confirmed:
                raise V29Error(
                    "Razstavljanja brez uporabnih delov ni mogoče zaključiti brez "
                    "izrecne potrditve POPOLNEGA ODPISA."
                )
            harvest_no = _next_number(connection, "donor_harvests", "harvest_no", "RAZ")
            stamp = now_text()
            cursor = connection.execute(
                """
                INSERT INTO donor_harvests(
                    device_id,harvest_no,status,usable_parts,allocated_unit_cost,
                    notes,operator_code,created_at,completed_at,acquisition_cost,
                    t1_cost,direct_costs,total_cost,allocation_basis_json
                ) VALUES(?,?,'FULL_WRITEOFF',0,0,?,?,?,?,?,?,?,?,?)
                """,
                (
                    device_id, harvest_no, notes.strip(), operator_code, stamp, stamp,
                    acquisition_cost, t1_cost, direct_costs, total_cost,
                    json.dumps({"method": "FULL_WRITEOFF"}, separators=(",", ":")),
                ),
            )
            connection.execute(
                "UPDATE used_devices SET purpose='PARTS',status='WRITTEN_OFF',"
                "allocated_cost=?,updated_at=? WHERE id=?",
                (total_cost, stamp, device_id),
            )
            for waste_type, description in (
                ("BATTERY", f"Baterija – popolni odpis {device[4]}"),
                ("ELECTRONIC", f"Preostanek naprave – popolni odpis {device[4]}"),
            ):
                connection.execute(
                    """
                    INSERT INTO donor_waste_records_v30(
                        harvest_id,device_id,waste_type,description,created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (int(cursor.lastrowid), device_id, waste_type, description, stamp),
                )
            connection.commit()
            return int(cursor.lastrowid), harvest_no, 0.0
        article_rows: list[tuple[HarvestPart, int, float]] = []
        for part in parts:
            article_id = part.article_id
            if part.module_code:
                source_url = str(part.price_source_url or "").strip()
                source = urllib.parse.urlparse(source_url)
                allowed_price_hosts = {
                    "www.ifixit.com", "ifixit.com", "fixshop.si", "www.fixshop.si",
                }
                if source.scheme != "https" or source.hostname not in allowed_price_hosts:
                    raise V29Error(
                        f"Za modul »{part.module_name}« manjka dovoljen spletni vir cene."
                    )
                try:
                    observed = datetime.strptime(
                        str(part.price_observed_on or ""), "%Y-%m-%d"
                    ).date()
                except ValueError as exc:
                    raise V29Error(
                        f"Za modul »{part.module_name}« manjka veljaven datum spletne cene."
                    ) from exc
                age_days = (datetime.now().date() - observed).days
                if age_days < 0 or age_days > 8:
                    raise V29Error(
                        f"Spletna cena za modul »{part.module_name}« ni dovolj sveža."
                    )
            if article_id is None:
                if not part.module_code or not part.module_name:
                    raise V29Error("Uporabni del nima določene vrste modula.")
                article_id = _ensure_pulled_part_article(
                    connection,
                    model=part.device_model or str(device[4]),
                    module_code=part.module_code,
                    module_name=part.module_name,
                    recommended_sale_price=part.recommended_sale_price,
                )
            article = connection.execute(
                "SELECT price FROM inventory_items WHERE id=? AND active=1",
                (int(article_id),),
            ).fetchone()
            if not article:
                raise V29Error(f"Artikel ID {article_id} ne obstaja.")
            current_recommendation = _money(part.recommended_sale_price)
            article_rows.append((
                part, int(article_id),
                current_recommendation
                if current_recommendation > 0 else max(0.0, float(article[0] or 0)),
            ))
        basis_total = sum(value for _part, _article_id, value in article_rows)
        if basis_total <= 0:
            weights = [1.0 / len(article_rows)] * len(article_rows)
        else:
            weights = [value / basis_total for _part, _article_id, value in article_rows]
        allocations: list[float] = []
        allocated_total = 0.0
        for index, weight in enumerate(weights, start=1):
            part_cost = round(total_cost * weight, 2)
            if index == len(weights):
                part_cost = round(total_cost - allocated_total, 2)
            allocations.append(part_cost)
            allocated_total = round(allocated_total + part_cost, 2)
        average_allocated = round(total_cost / len(parts), 2)
        harvest_no = _next_number(connection, "donor_harvests", "harvest_no", "RAZ")
        stamp = now_text()
        cursor = connection.execute(
            """
            INSERT INTO donor_harvests(
                device_id, harvest_no, status, usable_parts, allocated_unit_cost,
                notes, operator_code, created_at, completed_at,
                acquisition_cost,t1_cost,direct_costs,total_cost,allocation_basis_json
            ) VALUES(?,?, 'COMPLETED', ?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id, harvest_no, len(parts), average_allocated, notes.strip(),
                operator_code, stamp, stamp, acquisition_cost, t1_cost, direct_costs,
                total_cost,
                json.dumps(
                    {
                        "method": "PROPORTIONAL_RECOMMENDED_SALE_VALUE",
                        "values": [v for _p, _article_id, v in article_rows],
                    },
                    ensure_ascii=False, separators=(",", ":"),
                ),
            ),
        )
        harvest_id = int(cursor.lastrowid)
        for index, ((part, article_id, recommended_value), part_cost) in enumerate(
            zip(article_rows, allocations), start=1
        ):
            unique_lot = part.lot_serial.strip() or f"PP{harvest_id:06d}{index:02d}"
            movement_id = _insert_movement(
                connection, article_id=int(article_id), quantity_delta=1,
                movement_type="DONOR_PART_IN", source_type="DONOR_HARVEST",
                source_id=harvest_id, source_line_id=index,
                lot_serial=unique_lot, location_to=part.location.strip(),
                reason=f"Pridobljeno iz donorske naprave {device[0]}",
                operator_code=operator_code,
            )
            connection.execute(
                """
                INSERT INTO donor_parts(
                    harvest_id, article_id, allocated_cost, condition_grade,
                    test_result, lot_serial, location, stock_movement_id, created_at,
                    module_code,recommended_sale_value,price_source_url,price_observed_on
                    ,price_source_kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    harvest_id, int(article_id), part_cost,
                    part.condition_grade, part.test_result,
                    unique_lot, part.location.strip(), movement_id, stamp,
                    part.module_code, recommended_value,
                    part.price_source_url, part.price_observed_on,
                    part.price_source_kind,
                ),
            )
            # Obstoječa aktivna prodajna cena ostane; spremeni se samo sled izvora.
            connection.execute(
                """
                UPDATE inventory_items
                SET source_kind='DONOR_DEVICE', source_id=?, condition_grade=?,
                    updated_at=?
                WHERE id=?
                """,
                (device_id, part.condition_grade, stamp, int(article_id)),
            )
            receive_lot(
                connection, schema="main", article_id=int(article_id),
                source_type="DONOR_HARVEST", source_id=harvest_id,
                source_line_id=index, lot_serial=unique_lot, quantity=1,
                acquisition_cost=part_cost, sale_price=recommended_value,
                location=part.location.strip() or "KARANTENA-DELI",
                received_at=stamp,
            )
        recorded_waste = list(waste_modules)
        recorded_waste.append(
            ("ELECTRONIC", f"Preostanek donorske naprave {device[4]}")
        )
        for waste_type, description in recorded_waste:
            waste_type = str(waste_type or "ELECTRONIC").strip().upper()
            if waste_type not in {"BATTERY", "ELECTRONIC"}:
                raise V29Error("Vrsta odpadka mora biti BATTERY ali ELECTRONIC.")
            connection.execute(
                """
                INSERT INTO donor_waste_records_v30(
                    harvest_id,device_id,waste_type,description,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (harvest_id, device_id, waste_type, str(description).strip(), stamp),
            )
        connection.execute(
            """
            UPDATE used_devices
            SET purpose='PARTS', status='HARVESTED', allocated_cost=?, updated_at=?
            WHERE id=?
            """,
            (total_cost, stamp, device_id),
        )
        connection.commit()
        return harvest_id, harvest_no, average_allocated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reverse_full_writeoff(
    *, inventory_db: Path | str, device_id: int, operator_code: str = "DR",
    reason: str = "Napačno potrjen popolni odpis",
) -> str:
    """Stornira popolni odpis brez brisanja revizijske sledi.

    Dovoljen je samo odpis brez ustvarjenih delov in brez že oddanega odpada.
    Naprava se vrne v karanteno, prvotni zapis pa ostane v bazi kot STORNIRAN.
    """
    connection = connect(inventory_db)
    try:
        device = connection.execute(
            "SELECT status FROM used_devices WHERE id=?", (int(device_id),)
        ).fetchone()
        if not device:
            raise V29Error("Donorske naprave ni mogoče najti.")
        if str(device[0]) != "WRITTEN_OFF":
            raise V29Error("Stornirati je mogoče samo napravo v statusu WRITTEN_OFF.")
        harvest = connection.execute(
            """
            SELECT id,harvest_no,status FROM donor_harvests
            WHERE device_id=? ORDER BY id DESC LIMIT 1
            """,
            (int(device_id),),
        ).fetchone()
        if not harvest or str(harvest[2]) != "FULL_WRITEOFF":
            raise V29Error("Za napravo ni aktivnega popolnega odpisa za storno.")
        harvest_id = int(harvest[0])
        if connection.execute(
            "SELECT 1 FROM donor_parts WHERE harvest_id=? LIMIT 1", (harvest_id,)
        ).fetchone():
            raise V29Error("Odpisa ni mogoče stornirati, ker so bili že ustvarjeni deli.")
        handed_over = connection.execute(
            """
            SELECT 1 FROM donor_waste_records_v30
            WHERE harvest_id=? AND (handed_over_at IS NOT NULL OR status<>'AWAITING_HANDOFF')
            LIMIT 1
            """,
            (harvest_id,),
        ).fetchone()
        if handed_over:
            raise V29Error("Odpisa ni mogoče stornirati, ker je bil odpad že obdelan.")
        stamp = now_text()
        connection.execute(
            """
            UPDATE donor_harvests
            SET status='STORNIRAN',reversed_at=?,reversed_by=?,reversal_reason=?
            WHERE id=?
            """,
            (stamp, str(operator_code).strip(), str(reason).strip(), harvest_id),
        )
        connection.execute(
            "UPDATE donor_waste_records_v30 SET status='CANCELLED' WHERE harvest_id=?",
            (harvest_id,),
        )
        connection.execute(
            """
            UPDATE used_devices
            SET purpose='PARTS',status='QUARANTINE',allocated_cost=0,updated_at=?
            WHERE id=?
            """,
            (stamp, int(device_id)),
        )
        connection.commit()
        return str(harvest[1])
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def trace_article(inventory_db: Path | str, article_id: int) -> dict[str, Any]:
    connection = connect(inventory_db)
    connection.row_factory = sqlite3.Row
    try:
        item = connection.execute(
            "SELECT * FROM inventory_items WHERE id=?", (article_id,)
        ).fetchone()
        if not item:
            raise V29Error("Artikla ni mogoče najti.")
        movements = connection.execute(
            "SELECT * FROM stock_movements WHERE article_id=? ORDER BY id",
            (article_id,),
        ).fetchall()
        prices = connection.execute(
            "SELECT * FROM price_history WHERE article_id=? ORDER BY id",
            (article_id,),
        ).fetchall()
        donors = connection.execute(
            """
            SELECT dp.*, dh.harvest_no, ud.inventory_code, ud.buyback_no, ud.imei
            FROM donor_parts dp
            JOIN donor_harvests dh ON dh.id=dp.harvest_id
            JOIN used_devices ud ON ud.id=dh.device_id
            WHERE dp.article_id=? ORDER BY dp.id
            """,
            (article_id,),
        ).fetchall()
        return {
            "article": dict(item),
            "movements": [dict(row) for row in movements],
            "prices": [dict(row) for row in prices],
            "donor_sources": [dict(row) for row in donors],
        }
    finally:
        connection.close()


def dump_trace_json(inventory_db: Path | str, article_id: int) -> str:
    return json.dumps(trace_article(inventory_db, article_id), ensure_ascii=False, indent=2)
