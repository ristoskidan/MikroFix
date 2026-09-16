"""MikroFIX v30 – enotni prejem blaga in prejetih računov.

Ta modul vsebuje poslovna pravila brez Tk vmesnika. Potrjena dobavnica sama
ne poveča razpoložljive zaloge; količina se sprosti natanko enkrat, ko je
povezan in v celoti usklajen prejeti račun.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Sequence

from mikrofix_v29_core import recommend_price
from mikrofix_v30_lots import (
    LotError,
    ensure_lot_schema,
    maximum_available_cost,
    receive_lot,
    reverse_unconsumed_receipt_lot,
)


CENT = Decimal("0.01")
FOUR = Decimal("0.0001")
PART_FLOOR = Decimal("1.05")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DOCUMENT_TYPES = {"DOBAVNICA", "RACUN", "DOBAVNICA_IN_RACUN", "DRUGO"}
INVOICE_TYPES = {"RACUN", "DOBAVNICA_IN_RACUN"}


class ReceiptError(ValueError):
    """Poslovno pravilo prejema ni izpolnjeno."""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def decimal_value(value: Any, label: str, *, scale: Decimal = FOUR) -> Decimal:
    raw = str(value if value not in (None, "") else "0").strip()
    normalized = (
        raw.replace("\u00a0", "")
        .replace(" ", "")
        .replace("EUR", "")
        .replace("eur", "")
        .replace("€", "")
    )
    # Sprejmi slovenski zapis 1.234,56 in mednarodni zapis 1,234.56.
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    else:
        normalized = normalized.replace(",", ".")
    try:
        result = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ReceiptError(
            f"{label} ni veljaven znesek: »{raw}«. Vpiši samo znesek, na primer "
            "1234,56 (presledki, oznaka EUR in znak € so dovoljeni)."
        ) from exc
    if not result.is_finite() or result < 0:
        raise ReceiptError(f"{label} ne sme biti negativna.")
    return result.quantize(scale, rounding=ROUND_HALF_UP)


def money(value: Any, label: str) -> Decimal:
    return decimal_value(value, label, scale=CENT)


def quantity(value: Any) -> Decimal:
    result = decimal_value(value, "Količina", scale=FOUR)
    if result <= 0:
        raise ReceiptError("Količina mora biti večja od nič.")
    return result


def _text(value: Decimal, scale: Decimal = FOUR) -> str:
    return format(value.quantize(scale, rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True)
class ReceiptLineV30:
    article_id: int
    quantity: Any
    net_unit_price: Any
    discount_percent: Any
    vat_rate: Any
    supplier_line_total: Any
    confirmed_sale_price: Any
    supplier_barcode: str = ""
    lot_serial: str = ""
    location: str = ""


@dataclass(frozen=True)
class CheckedLine:
    article_id: int
    quantity: Decimal
    net_unit_price: Decimal
    discount_percent: Decimal
    vat_rate: Decimal
    calculated_total: Decimal
    supplier_line_total: Decimal
    allocated_shipping: Decimal
    landed_unit_cost: Decimal
    confirmed_sale_price: Decimal
    supplier_barcode: str
    lot_serial: str
    location: str


def _connect(path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=20)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def ensure_receipt_schema(office_db: Path | str) -> None:
    connection = _connect(office_db)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS receipts_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_key TEXT NOT NULL UNIQUE,
            receipt_no TEXT UNIQUE,
            partner_id INTEGER,
            partner_snapshot_json TEXT NOT NULL DEFAULT '{}',
            partner_name_snapshot TEXT NOT NULL DEFAULT '',
            document_type TEXT NOT NULL,
            custom_document_type TEXT NOT NULL DEFAULT '',
            external_document_no TEXT NOT NULL DEFAULT '',
            document_date TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT 'EUR',
            document_total TEXT NOT NULL DEFAULT '0.00',
            shipping_total TEXT NOT NULL DEFAULT '0.0000',
            invoice_external_no TEXT NOT NULL DEFAULT '',
            invoice_date TEXT NOT NULL DEFAULT '',
            invoice_due_date TEXT NOT NULL DEFAULT '',
            invoice_payment_status TEXT NOT NULL DEFAULT 'NEPLACAN',
            status TEXT NOT NULL DEFAULT 'DRAFT',
            stock_released INTEGER NOT NULL DEFAULT 0,
            correction_of_id INTEGER,
            storno_reason TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            draft_payload_json TEXT NOT NULL DEFAULT '{}',
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmed_at TEXT,
            storno_at TEXT,
            FOREIGN KEY(correction_of_id) REFERENCES receipts_v30(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_v30_supplier_document
            ON receipts_v30(partner_name_snapshot, document_type, external_document_no)
            WHERE status <> 'DRAFT' AND external_document_no <> '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_v30_supplier_invoice
            ON receipts_v30(partner_name_snapshot, invoice_external_no)
            WHERE status <> 'DRAFT' AND invoice_external_no <> '';
        CREATE TABLE IF NOT EXISTS receipt_lines_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,
            net_unit_price TEXT NOT NULL,
            discount_percent TEXT NOT NULL,
            vat_rate TEXT NOT NULL,
            calculated_total TEXT NOT NULL,
            supplier_line_total TEXT NOT NULL,
            allocated_shipping TEXT NOT NULL,
            landed_unit_cost TEXT NOT NULL,
            confirmed_sale_price TEXT NOT NULL,
            supplier_barcode TEXT NOT NULL DEFAULT '',
            lot_serial TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            stock_movement_id INTEGER,
            inventory_lot_id INTEGER,
            released_at TEXT,
            UNIQUE(receipt_id, line_no),
            FOREIGN KEY(receipt_id) REFERENCES receipts_v30(id)
        );
        CREATE TABLE IF NOT EXISTS receipt_attachments_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            attachment_kind TEXT NOT NULL,
            original_name TEXT NOT NULL,
            archived_path TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            operator_code TEXT NOT NULL,
            FOREIGN KEY(receipt_id) REFERENCES receipts_v30(id)
        );
        CREATE TABLE IF NOT EXISTS pending_stock_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            receipt_line_id INTEGER NOT NULL UNIQUE,
            article_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING_INVOICE',
            created_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(receipt_id) REFERENCES receipts_v30(id),
            FOREIGN KEY(receipt_line_id) REFERENCES receipt_lines_v30(id)
        );
        CREATE INDEX IF NOT EXISTS idx_receipts_v30_status
            ON receipts_v30(status, received_at);
        CREATE INDEX IF NOT EXISTS idx_pending_stock_v30_status
            ON pending_stock_v30(status, article_id);
        """
    )
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(receipts_v30)")}
    if "draft_payload_json" not in columns:
        connection.execute(
            "ALTER TABLE receipts_v30 ADD COLUMN draft_payload_json TEXT NOT NULL DEFAULT '{}'"
        )
    line_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(receipt_lines_v30)")
    }
    if "inventory_lot_id" not in line_columns:
        connection.execute("ALTER TABLE receipt_lines_v30 ADD COLUMN inventory_lot_id INTEGER")
    connection.commit()
    connection.close()


def _mime_from_content(path: Path) -> str:
    if not path.is_file():
        raise ReceiptError(f"Priloga ne obstaja: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ATTACHMENT_BYTES:
        raise ReceiptError(f"Priloga {path.name} mora biti velika med 1 B in 25 MB.")
    with path.open("rb") as handle:
        head = handle.read(16)
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"
    }:
        return "image/heic"
    raise ReceiptError(
        f"Priloga {path.name} ni prepoznan PDF, JPG, PNG ali HEIC dokument."
    )


def _archive_attachments(
    connection: sqlite3.Connection,
    *,
    receipt_id: int,
    receipt_no: str,
    archive_dir: Path | str,
    attachment_paths: Sequence[Path | str],
    attachment_kind: str,
    operator_code: str,
) -> list[Path]:
    if not attachment_paths:
        # Digitalna priloga je koristna, ni pa pogoj za knjiženje, kadar se
        # papirni izvirnik ali drugo dokazilo hrani zunaj programa.
        return []
    target_dir = Path(archive_dir) / receipt_no
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    stamp = now_text()
    try:
        for index, raw_path in enumerate(attachment_paths, start=1):
            source = Path(raw_path).resolve()
            mime = _mime_from_content(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            suffix = {
                "application/pdf": ".pdf", "image/jpeg": ".jpg",
                "image/png": ".png", "image/heic": ".heic",
            }[mime]
            destination = target_dir / f"{attachment_kind.lower()}_{index:02d}_{digest[:12]}{suffix}"
            if not destination.exists():
                shutil.copy2(source, destination)
                created.append(destination)
            connection.execute(
                """
                INSERT INTO receipt_attachments_v30(
                    receipt_id, attachment_kind, original_name, archived_path,
                    mime_type, byte_size, sha256, created_at, operator_code
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    receipt_id, attachment_kind, source.name, str(destination),
                    mime, source.stat().st_size, digest, stamp, operator_code,
                ),
            )
        return created
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _next_receipt_no(connection: sqlite3.Connection) -> str:
    year = datetime.now().year
    prefix = f"PRE-{year}-"
    rows = connection.execute(
        "SELECT receipt_no FROM receipts_v30 WHERE receipt_no LIKE ?", (f"{prefix}%",)
    ).fetchall()
    highest = 0
    for (value,) in rows:
        try:
            highest = max(highest, int(str(value).rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            pass
    return f"{prefix}{highest + 1:04d}"


def create_draft(
    office_db: Path | str, *, operator_code: str = "DR", draft_key: str | None = None
) -> tuple[int, str]:
    """Ustvari idempotenten osnutek brez številke in brez vpliva na zalogo."""
    ensure_receipt_schema(office_db)
    key = (draft_key or uuid.uuid4().hex).strip()
    connection = _connect(office_db)
    stamp = now_text()
    connection.execute(
        """
        INSERT OR IGNORE INTO receipts_v30(
            draft_key, document_type, operator_code, created_at, updated_at
        ) VALUES(?, 'DOBAVNICA_IN_RACUN', ?, ?, ?)
        """,
        (key, operator_code, stamp, stamp),
    )
    row = connection.execute(
        "SELECT id FROM receipts_v30 WHERE draft_key=?", (key,)
    ).fetchone()
    connection.commit()
    connection.close()
    return int(row[0]), key


def delete_draft(office_db: Path | str, receipt_id: int) -> None:
    connection = _connect(office_db)
    row = connection.execute(
        "SELECT status, receipt_no FROM receipts_v30 WHERE id=?", (receipt_id,)
    ).fetchone()
    if not row or row[0] != "DRAFT" or row[1]:
        connection.close()
        raise ReceiptError("Izbrisati je mogoče samo nepotrjen osnutek brez številke.")
    connection.execute("DELETE FROM receipt_lines_v30 WHERE receipt_id=?", (receipt_id,))
    connection.execute("DELETE FROM receipts_v30 WHERE id=?", (receipt_id,))
    connection.commit()
    connection.close()


def save_draft_payload(
    office_db: Path | str, receipt_id: int, payload: dict[str, Any]
) -> None:
    """Samodejno shrani podatke zaslona; nima vpliva na zalogo ali številčenje."""
    connection = _connect(office_db)
    cursor = connection.execute(
        """
        UPDATE receipts_v30 SET draft_payload_json=?, updated_at=?
        WHERE id=? AND (
            (status='DRAFT' AND receipt_no IS NULL) OR status='PENDING_INVOICE'
        )
        """,
        (json.dumps(payload, ensure_ascii=False), now_text(), receipt_id),
    )
    if cursor.rowcount != 1:
        connection.rollback()
        connection.close()
        raise ReceiptError("Osnutka ni več mogoče spreminjati.")
    connection.commit()
    connection.close()


def load_draft_payload(office_db: Path | str, receipt_id: int) -> dict[str, Any]:
    connection = _connect(office_db)
    row = connection.execute(
        """
        SELECT draft_payload_json FROM receipts_v30
        WHERE id=? AND (
            (status='DRAFT' AND receipt_no IS NULL) OR status='PENDING_INVOICE'
        )
        """,
        (receipt_id,),
    ).fetchone()
    connection.close()
    if not row:
        raise ReceiptError("Odprti osnutek ne obstaja.")
    try:
        value = json.loads(row[0] or "{}")
    except json.JSONDecodeError as exc:
        raise ReceiptError("Shranjeni osnutek je poškodovan.") from exc
    return value if isinstance(value, dict) else {}


def _check_lines(
    lines: Sequence[ReceiptLineV30], shipping_total: Any, document_total: Any
) -> list[CheckedLine]:
    if not lines:
        raise ReceiptError("Dokument nima nobene postavke.")
    shipping = decimal_value(shipping_total, "Skupni strošek prevoza", scale=FOUR)
    total = money(document_total, "Skupni znesek dokumenta")
    raw: list[dict[str, Any]] = []
    total_qty = Decimal("0")
    supplier_sum = Decimal("0")
    for index, line in enumerate(lines, start=1):
        qty = quantity(line.quantity)
        net = decimal_value(line.net_unit_price, f"Postavka {index}: neto cena", scale=FOUR)
        discount = decimal_value(
            line.discount_percent, f"Postavka {index}: rabat", scale=FOUR
        )
        vat = decimal_value(line.vat_rate, f"Postavka {index}: DDV", scale=FOUR)
        if discount > 100 or vat > 100:
            raise ReceiptError(f"Postavka {index}: odstotek mora biti med 0 in 100.")
        base = qty * net * (Decimal("1") - discount / Decimal("100"))
        calculated = (base * (Decimal("1") + vat / Decimal("100"))).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        supplier = money(line.supplier_line_total, f"Postavka {index}: dobaviteljev znesek")
        if calculated != supplier:
            raise ReceiptError(
                f"Postavka {index}: izračun {calculated:.2f} EUR se ne ujema z "
                f"dobaviteljevim zneskom {supplier:.2f} EUR. Dovoljeno odstopanje je 0,00 EUR."
            )
        sale = money(line.confirmed_sale_price, f"Postavka {index}: prodajna cena")
        barcode = line.supplier_barcode.strip()
        lot = line.lot_serial.strip()
        raw.append({
            "line": line, "qty": qty, "net": net, "discount": discount,
            "vat": vat, "calculated": calculated, "supplier": supplier,
            "sale": sale, "barcode": barcode, "lot": lot,
        })
        total_qty += qty
        supplier_sum += supplier
    if (supplier_sum + shipping).quantize(CENT, rounding=ROUND_HALF_UP) != total:
        actual = (supplier_sum + shipping).quantize(CENT, rounding=ROUND_HALF_UP)
        raise ReceiptError(
            f"Vsota postavk in prevoza je {actual:.2f} EUR, dokument pa {total:.2f} EUR. "
            "Dovoljeno odstopanje je 0,00 EUR."
        )
    checked: list[CheckedLine] = []
    allocated_so_far = Decimal("0")
    for index, item in enumerate(raw):
        if index == len(raw) - 1:
            allocated = shipping - allocated_so_far
        else:
            allocated = (shipping * item["qty"] / total_qty).quantize(
                FOUR, rounding=ROUND_HALF_UP
            )
            allocated_so_far += allocated
        allocated = allocated.quantize(FOUR, rounding=ROUND_HALF_UP)
        landed = ((item["supplier"] + allocated) / item["qty"]).quantize(
            FOUR, rounding=ROUND_HALF_UP
        )
        floor = (landed * PART_FLOOR).quantize(CENT, rounding=ROUND_HALF_UP)
        if item["sale"] < floor:
            raise ReceiptError(
                f"Postavka {index + 1}: potrjena prodajna cena mora biti najmanj "
                f"nabavna vrednost s prevozom + 5 % ({floor:.2f} EUR)."
            )
        line = item["line"]
        checked.append(CheckedLine(
            article_id=int(line.article_id), quantity=item["qty"],
            net_unit_price=item["net"], discount_percent=item["discount"],
            vat_rate=item["vat"], calculated_total=item["calculated"],
            supplier_line_total=item["supplier"], allocated_shipping=allocated,
            landed_unit_cost=landed, confirmed_sale_price=item["sale"],
            supplier_barcode=item["barcode"], lot_serial=item["lot"],
            location=line.location.strip(),
        ))
    return checked


def _validate_articles(connection: sqlite3.Connection, lines: Sequence[CheckedLine]) -> None:
    seen_serials: set[str] = set()
    for index, line in enumerate(lines, start=1):
        row = connection.execute(
            """
            SELECT barcode, track_mode FROM inv.inventory_items
            WHERE id=? AND active=1
            """,
            (line.article_id,),
        ).fetchone()
        if not row:
            raise ReceiptError(f"Postavka {index}: artikel ne obstaja ali ni aktiven.")
        stored_barcode, track_mode = str(row[0] or ""), str(row[1] or "QUANTITY")
        if line.supplier_barcode and line.supplier_barcode != stored_barcode:
            raise ReceiptError(
                f"Postavka {index}: dobaviteljeva barkoda ne pripada izbranemu artiklu."
            )
        if not line.supplier_barcode and not stored_barcode:
            raise ReceiptError(f"Postavka {index}: manjka dobaviteljeva ali interna barkoda.")
        if track_mode == "SERIAL":
            if line.quantity != Decimal("1.0000") or not line.lot_serial:
                raise ReceiptError(
                    f"Postavka {index}: serijsko sleden artikel zahteva količino 1 in serijsko številko."
                )
            if line.lot_serial in seen_serials:
                raise ReceiptError(f"Postavka {index}: serijska številka je podvojena.")
            seen_serials.add(line.lot_serial)
            exists = connection.execute(
                """
                SELECT 1 FROM receipt_lines_v30 l JOIN receipts_v30 r ON r.id=l.receipt_id
                WHERE l.lot_serial=? AND r.status NOT IN ('DRAFT','STORNIRANO') LIMIT 1
                """,
                (line.lot_serial,),
            ).fetchone()
            if exists:
                raise ReceiptError(f"Postavka {index}: serijska številka je že evidentirana.")


def _write_lines(
    connection: sqlite3.Connection, receipt_id: int, lines: Sequence[CheckedLine],
    *, receipt_no: str,
) -> list[int]:
    connection.execute("DELETE FROM pending_stock_v30 WHERE receipt_id=?", (receipt_id,))
    connection.execute("DELETE FROM receipt_lines_v30 WHERE receipt_id=?", (receipt_id,))
    ids: list[int] = []
    for line_no, line in enumerate(lines, start=1):
        lot_serial = line.lot_serial or f"LOT-{receipt_no}-{line_no:03d}"
        cursor = connection.execute(
            """
            INSERT INTO receipt_lines_v30(
                receipt_id, line_no, article_id, quantity, net_unit_price,
                discount_percent, vat_rate, calculated_total, supplier_line_total,
                allocated_shipping, landed_unit_cost, confirmed_sale_price,
                supplier_barcode, lot_serial, location
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id, line_no, line.article_id, _text(line.quantity),
                _text(line.net_unit_price), _text(line.discount_percent),
                _text(line.vat_rate), _text(line.calculated_total, CENT),
                _text(line.supplier_line_total, CENT), _text(line.allocated_shipping),
                _text(line.landed_unit_cost), _text(line.confirmed_sale_price, CENT),
                line.supplier_barcode, lot_serial, line.location,
            ),
        )
        ids.append(int(cursor.lastrowid))
    return ids


def _next_movement_no(connection: sqlite3.Connection) -> str:
    year = datetime.now().year
    prefix = f"PREM-{year}-"
    rows = connection.execute(
        "SELECT movement_no FROM inv.stock_movements WHERE movement_no LIKE ?",
        (f"{prefix}%",),
    ).fetchall()
    highest = 0
    for (value,) in rows:
        try:
            highest = max(highest, int(str(value).rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            pass
    return f"{prefix}{highest + 1:04d}"


def _release_stock(
    connection: sqlite3.Connection, *, receipt_id: int, receipt_no: str,
    operator_code: str,
) -> None:
    row = connection.execute(
        "SELECT stock_released FROM receipts_v30 WHERE id=?", (receipt_id,)
    ).fetchone()
    if not row:
        raise ReceiptError("Prejem ne obstaja.")
    if int(row[0]):
        raise ReceiptError("Zaloga tega prejema je že bila sproščena.")
    stamp = now_text()
    ensure_lot_schema(connection, "inv")
    lines = connection.execute(
        """
        SELECT id, article_id, quantity, landed_unit_cost, confirmed_sale_price,
               lot_serial, location
        FROM receipt_lines_v30 WHERE receipt_id=? ORDER BY line_no
        """,
        (receipt_id,),
    ).fetchall()
    for line_id, article_id, qty_text, landed_text, sale_text, lot, location in lines:
        current = connection.execute(
            "SELECT stock_qty, price_profile_code FROM inv.inventory_items WHERE id=? AND active=1",
            (article_id,),
        ).fetchone()
        if not current:
            raise ReceiptError(f"Artikel ID {article_id} ne obstaja ali ni aktiven.")
        qty = Decimal(qty_text)
        new_balance = (Decimal(str(current[0] or 0)) + qty).quantize(FOUR)
        movement = connection.execute(
            """
            INSERT INTO inv.stock_movements(
                movement_no, created_at, article_id, quantity_delta, balance_after,
                movement_type, source_type, source_id, source_line_id,
                lot_serial, location_from, location_to, reason, operator_code
            ) VALUES(?,?,?,?,?,'RECEIPT_IN','RECEIPT_V30',?,?,?,'',?,?,?)
            """,
            (
                _next_movement_no(connection), stamp, article_id, float(qty),
                float(new_balance), receipt_id, line_id, lot or "", location or "",
                f"Prejem {receipt_no}", operator_code,
            ),
        )
        inventory_lot_id = receive_lot(
            connection,
            schema="inv",
            article_id=int(article_id),
            source_type="RECEIPT_V30",
            source_id=int(receipt_id),
            source_line_id=int(line_id),
            lot_serial=str(lot or ""),
            quantity=qty,
            acquisition_cost=Decimal(landed_text),
            sale_price=Decimal(sale_text),
            location=str(location or ""),
            received_at=stamp,
        )
        protected_cost = maximum_available_cost(
            connection, schema="inv", article_id=int(article_id)
        ) or Decimal(landed_text)
        connection.execute(
            """
            UPDATE inv.inventory_items
            SET stock_qty=?, acquisition_cost=?, price=?, price_status='POTRJENA',
                lot_serial=CASE WHEN ?<>'' THEN ? ELSE lot_serial END,
                location=CASE WHEN ?<>'' THEN ? ELSE location END, updated_at=?
            WHERE id=?
            """,
            (
                float(new_balance), float(protected_cost), float(Decimal(sale_text)),
                lot or "", lot or "", location or "", location or "", stamp, article_id,
            ),
        )
        profile = connection.execute(
            """
            SELECT v.code, v.version_no, v.tiers_json, v.warranty_reserve
            FROM inv.inventory_items i
            LEFT JOIN inv.price_profile_versions v
              ON v.code=i.price_profile_code AND v.active=1
            WHERE i.id=?
            ORDER BY v.version_no DESC LIMIT 1
            """,
            (article_id,),
        ).fetchone()
        if profile and profile[0]:
            recommendation = recommend_price(
                Decimal(landed_text),
                {
                    "version": profile[1], "tiers": json.loads(profile[2]),
                    "warranty_reserve": profile[3],
                },
                profile_code=str(profile[0]),
            )
            recommended_price = recommendation.recommended_price
            profile_code = recommendation.profile_code
        else:
            recommended_price = float(Decimal(sale_text))
            profile_code = "SPLOŠNO"
        connection.execute(
            """
            INSERT INTO inv.price_history(
                article_id, active_from, acquisition_cost, recommended_price,
                confirmed_price, profile_code, reason, source_type, source_id,
                operator_code, created_at, profile_version,
                target_margin_snapshot, warranty_reserve_snapshot, actual_margin
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                article_id, stamp, float(Decimal(landed_text)), recommended_price,
                float(Decimal(sale_text)), profile_code, f"Prejem {receipt_no}",
                "RECEIPT_V30", receipt_id, operator_code, stamp,
                recommendation.profile_version if profile and profile[0] else 1,
                recommendation.target_margin if profile and profile[0] else 0,
                recommendation.warranty_reserve if profile and profile[0] else 0,
                (
                    round(1 - recommendation.landed_cost / float(Decimal(sale_text)), 6)
                    if profile and profile[0] and Decimal(sale_text) else 0
                ),
            ),
        )
        connection.execute(
            """
            UPDATE receipt_lines_v30
            SET stock_movement_id=?, inventory_lot_id=?, released_at=? WHERE id=?
            """,
            (int(movement.lastrowid), inventory_lot_id, stamp, line_id),
        )
        connection.execute(
            "UPDATE pending_stock_v30 SET status='RELEASED', released_at=? WHERE receipt_line_id=?",
            (stamp, line_id),
        )
    connection.execute(
        "UPDATE receipts_v30 SET stock_released=1, status='BOOKED', updated_at=? WHERE id=?",
        (stamp, receipt_id),
    )


def confirm_receipt(
    *, office_db: Path | str, inventory_db: Path | str, archive_dir: Path | str,
    receipt_id: int, partner_id: int | None, partner_name: str,
    partner_snapshot: dict[str, Any] | None, document_type: str,
    custom_document_type: str, external_document_no: str, document_date: str,
    received_at: str, currency: str, document_total: Any, shipping_total: Any,
    invoice_external_no: str, invoice_date: str, invoice_due_date: str,
    invoice_payment_status: str, lines: Sequence[ReceiptLineV30],
    attachment_paths: Sequence[Path | str], notes: str = "",
    operator_code: str = "DR", correction_of_id: int | None = None,
) -> tuple[int, str]:
    ensure_receipt_schema(office_db)
    kind = str(document_type or "").strip().upper().replace("Č", "C")
    if kind not in DOCUMENT_TYPES:
        raise ReceiptError("Neveljavna vrsta prevzemnega dokumenta.")
    if kind == "DRUGO" and not custom_document_type.strip():
        raise ReceiptError("Pri drugem prevzemnem dokumentu je obvezen naziv vrste.")
    if not partner_name.strip():
        raise ReceiptError("Manjka dobavitelj.")
    if not external_document_no.strip():
        raise ReceiptError("Manjka dobaviteljeva številka dokumenta.")
    if not document_date.strip() or not received_at.strip():
        raise ReceiptError("Manjkata datum dokumenta ali datum fizičnega prejema.")
    if str(currency or "EUR").strip().upper() != "EUR":
        raise ReceiptError("v30 trenutno podpira samo valuto EUR.")
    has_invoice = kind in INVOICE_TYPES
    if has_invoice and (not invoice_external_no.strip() or not invoice_date.strip()):
        raise ReceiptError("Za finančno zaključen prejem sta obvezna številka in datum računa.")
    checked = _check_lines(lines, shipping_total, document_total)
    connection = _connect(office_db)
    created_files: list[Path] = []
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        row = connection.execute(
            "SELECT status, receipt_no FROM receipts_v30 WHERE id=?", (receipt_id,)
        ).fetchone()
        if not row or row[0] != "DRAFT" or row[1]:
            raise ReceiptError("Potrditi je mogoče samo odprt osnutek brez številke.")
        _validate_articles(connection, checked)
        receipt_no = _next_receipt_no(connection)
        stamp = now_text()
        line_ids = _write_lines(connection, receipt_id, checked, receipt_no=receipt_no)
        connection.execute(
            """
            UPDATE receipts_v30 SET
                receipt_no=?, partner_id=?, partner_snapshot_json=?,
                partner_name_snapshot=?, document_type=?, custom_document_type=?,
                external_document_no=?, document_date=?, received_at=?, currency='EUR',
                document_total=?, shipping_total=?, invoice_external_no=?, invoice_date=?,
                invoice_due_date=?, invoice_payment_status=?, status=?, correction_of_id=?,
                notes=?, operator_code=?, updated_at=?, confirmed_at=?
            WHERE id=?
            """,
            (
                receipt_no, partner_id, json.dumps(partner_snapshot or {}, ensure_ascii=False),
                partner_name.strip(), kind, custom_document_type.strip(),
                external_document_no.strip(), document_date.strip(), received_at.strip(),
                _text(money(document_total, "Skupni znesek"), CENT),
                _text(decimal_value(shipping_total, "Prevoz", scale=FOUR)),
                invoice_external_no.strip(), invoice_date.strip(), invoice_due_date.strip(),
                invoice_payment_status.strip().upper() or "NEPLACAN",
                "BOOKED" if has_invoice else "PENDING_INVOICE", correction_of_id,
                notes.strip(), operator_code, stamp, stamp, receipt_id,
            ),
        )
        created_files = _archive_attachments(
            connection, receipt_id=receipt_id, receipt_no=receipt_no,
            archive_dir=archive_dir, attachment_paths=attachment_paths,
            attachment_kind="RACUN" if has_invoice else "PREVZEM",
            operator_code=operator_code,
        )
        if has_invoice:
            _release_stock(
                connection, receipt_id=receipt_id, receipt_no=receipt_no,
                operator_code=operator_code,
            )
        else:
            for line_id, line in zip(line_ids, checked):
                connection.execute(
                    """
                    INSERT INTO pending_stock_v30(
                        receipt_id, receipt_line_id, article_id, quantity, created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (receipt_id, line_id, line.article_id, _text(line.quantity), stamp),
                )
        connection.commit()
        return receipt_id, receipt_no
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        if "idx_receipts_v30_supplier_document" in str(exc) or "UNIQUE" in str(exc):
            raise ReceiptError(
                "Ta dobaviteljev dokument je pri istem dobavitelju že evidentiran."
            ) from exc
        raise
    except Exception:
        connection.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def complete_with_invoice(
    *, office_db: Path | str, inventory_db: Path | str, archive_dir: Path | str,
    receipt_id: int, invoice_external_no: str, invoice_date: str,
    invoice_due_date: str, invoice_payment_status: str, document_total: Any,
    shipping_total: Any, lines: Sequence[ReceiptLineV30],
    attachment_paths: Sequence[Path | str], operator_code: str = "DR",
) -> None:
    """Uskladi dobavnico z računom in sprosti isto količino natanko enkrat."""
    if not invoice_external_no.strip() or not invoice_date.strip():
        raise ReceiptError("Manjkata številka ali datum prejetega računa.")
    checked = _check_lines(lines, shipping_total, document_total)
    connection = _connect(office_db)
    created_files: list[Path] = []
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        header = connection.execute(
            """
            SELECT receipt_no, status, stock_released FROM receipts_v30 WHERE id=?
            """,
            (receipt_id,),
        ).fetchone()
        if not header or header[1] != "PENDING_INVOICE" or int(header[2]):
            raise ReceiptError("Račun je mogoče dodati samo prejemu, ki čaka na račun.")
        old = connection.execute(
            """
            SELECT article_id, quantity FROM receipt_lines_v30
            WHERE receipt_id=? ORDER BY line_no
            """,
            (receipt_id,),
        ).fetchall()
        identity = [(int(row[0]), Decimal(str(row[1]))) for row in old]
        incoming = [(line.article_id, line.quantity) for line in checked]
        if identity != incoming:
            raise ReceiptError(
                "Račun se po artiklih ali količinah ne ujema z dobavnico. "
                "Obstoječi prejem je treba stornirati in ustvariti novega."
            )
        _validate_articles(connection, checked)
        _write_lines(connection, receipt_id, checked, receipt_no=str(header[0]))
        # _write_lines je zamenjal vrstice; čakajoče stanje ustvarimo za nove ID-je.
        stamp = now_text()
        for row in connection.execute(
            "SELECT id, article_id, quantity FROM receipt_lines_v30 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchall():
            connection.execute(
                """
                INSERT INTO pending_stock_v30(
                    receipt_id, receipt_line_id, article_id, quantity, created_at
                ) VALUES(?,?,?,?,?)
                """,
                (receipt_id, int(row[0]), int(row[1]), str(row[2]), stamp),
            )
        connection.execute(
            """
            UPDATE receipts_v30 SET document_total=?, shipping_total=?,
                invoice_external_no=?, invoice_date=?, invoice_due_date=?,
                invoice_payment_status=?, draft_payload_json='{}', updated_at=? WHERE id=?
            """,
            (
                _text(money(document_total, "Skupni znesek"), CENT),
                _text(decimal_value(shipping_total, "Prevoz", scale=FOUR)),
                invoice_external_no.strip(), invoice_date.strip(), invoice_due_date.strip(),
                invoice_payment_status.strip().upper() or "NEPLACAN", stamp, receipt_id,
            ),
        )
        created_files = _archive_attachments(
            connection, receipt_id=receipt_id, receipt_no=str(header[0]),
            archive_dir=archive_dir, attachment_paths=attachment_paths,
            attachment_kind="RACUN", operator_code=operator_code,
        )
        _release_stock(
            connection, receipt_id=receipt_id, receipt_no=str(header[0]),
            operator_code=operator_code,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def add_supplement(
    *, office_db: Path | str, archive_dir: Path | str, receipt_id: int,
    attachment_paths: Sequence[Path | str], operator_code: str = "DR",
) -> None:
    """Doda nespremenljivo dopolnilno prilogo brez spreminjanja dokumenta."""
    connection = _connect(office_db)
    created_files: list[Path] = []
    try:
        row = connection.execute(
            "SELECT receipt_no, status FROM receipts_v30 WHERE id=?", (receipt_id,)
        ).fetchone()
        if not row or not row[0] or row[1] == "DRAFT":
            raise ReceiptError("Dopolnilo je mogoče dodati samo potrjenemu dokumentu.")
        created_files = _archive_attachments(
            connection, receipt_id=receipt_id, receipt_no=str(row[0]),
            archive_dir=archive_dir, attachment_paths=attachment_paths,
            attachment_kind="DOPOLNILO", operator_code=operator_code,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def storno_receipt(
    *, office_db: Path | str, inventory_db: Path | str, receipt_id: int,
    reason: str, operator_code: str = "DR",
) -> str:
    """Stornira cel potrjen dokument; potrjene vsebine nikoli ne popravlja."""
    if not reason.strip():
        raise ReceiptError("Pri stornu je obvezen razlog.")
    connection = _connect(office_db)
    try:
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        header = connection.execute(
            """
            SELECT receipt_no, status, stock_released FROM receipts_v30 WHERE id=?
            """,
            (receipt_id,),
        ).fetchone()
        if not header or header[1] not in {"BOOKED", "PENDING_INVOICE"}:
            raise ReceiptError("Stornirati je mogoče samo veljaven potrjen dokument.")
        stamp = now_text()
        if int(header[2]):
            ensure_lot_schema(connection, "inv")
            lines = connection.execute(
                """
                SELECT id, article_id, quantity, stock_movement_id, lot_serial, location
                FROM receipt_lines_v30 WHERE receipt_id=? ORDER BY line_no
                """,
                (receipt_id,),
            ).fetchall()
            for line_id, article_id, qty_text, original_movement, lot, location in lines:
                try:
                    reverse_unconsumed_receipt_lot(
                        connection,
                        schema="inv",
                        source_id=int(receipt_id),
                        source_line_id=int(line_id),
                        updated_at=stamp,
                    )
                except LotError as exc:
                    raise ReceiptError(str(exc)) from exc
                item = connection.execute(
                    "SELECT stock_qty FROM inv.inventory_items WHERE id=? AND active=1",
                    (article_id,),
                ).fetchone()
                qty = Decimal(str(qty_text))
                if not item or Decimal(str(item[0] or 0)) < qty:
                    raise ReceiptError(
                        "Storno bi ustvaril negativno zalogo. Najprej je treba razrešiti "
                        f"porabo/prodajo artikla ID {article_id}."
                    )
                new_balance = (Decimal(str(item[0])) - qty).quantize(FOUR)
                connection.execute(
                    """
                    INSERT INTO inv.stock_movements(
                        movement_no, created_at, article_id, quantity_delta, balance_after,
                        movement_type, source_type, source_id, source_line_id,
                        lot_serial, location_from, location_to, reason, operator_code,
                        reversal_of_id
                    ) VALUES(?,?,?,?,?,'RECEIPT_STORNO','RECEIPT_V30',?,?,?,?, '',?,?,?)
                    """,
                    (
                        _next_movement_no(connection), stamp, article_id, float(-qty),
                        float(new_balance), receipt_id, line_id, lot or "", location or "",
                        reason.strip(), operator_code, original_movement,
                    ),
                )
                connection.execute(
                    "UPDATE inv.inventory_items SET stock_qty=?, updated_at=? WHERE id=?",
                    (float(new_balance), stamp, article_id),
                )
                remaining_cost = maximum_available_cost(
                    connection, schema="inv", article_id=int(article_id)
                )
                if remaining_cost is not None:
                    connection.execute(
                        "UPDATE inv.inventory_items SET acquisition_cost=? WHERE id=?",
                        (float(remaining_cost), int(article_id)),
                    )
        connection.execute(
            "UPDATE pending_stock_v30 SET status='STORNIRANO' WHERE receipt_id=?",
            (receipt_id,),
        )
        connection.execute(
            """
            UPDATE receipts_v30 SET status='STORNIRANO', storno_reason=?,
                storno_at=?, updated_at=? WHERE id=?
            """,
            (reason.strip(), stamp, stamp, receipt_id),
        )
        connection.commit()
        return str(header[0])
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_receipts(office_db: Path | str, status: str = "") -> list[sqlite3.Row]:
    ensure_receipt_schema(office_db)
    connection = _connect(office_db)
    connection.row_factory = sqlite3.Row
    query = """
        SELECT r.*,
               COALESCE((SELECT SUM(CAST(quantity AS REAL)) FROM receipt_lines_v30 l
                         WHERE l.receipt_id=r.id),0) AS total_quantity
        FROM receipts_v30 r
    """
    params: list[Any] = []
    if status:
        query += " WHERE r.status=?"
        params.append(status)
    query += " ORDER BY r.id DESC"
    rows = connection.execute(query, params).fetchall()
    connection.close()
    return rows
