"""MikroFIX v30 POS: calculations, atomic invoice issue and customer PDFs."""

from __future__ import annotations

import sqlite3
import uuid
import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.sax.saxutils import escape

from mikrofix_v30_lots import (
    allocate_fifo, ensure_lot_schema, maximum_available_cost, restore_allocations,
)
from mikrofix_v30_cash import (
    CashJournalError, ensure_cash_schema_connection, record_cash_entry,
    require_open_cash_session,
)


CENT = Decimal("0.01")
STORNO_DOCUMENT_PERMISSION = "STORNO_DOCUMENT"

PAYMENT_METHOD_LABELS = {
    "CASH": "Gotovina",
    "CARD": "Plačilna kartica",
    "BANK_TRANSFER": "Bančno nakazilo (TRR)",
}
PAYMENT_STATUS_LABELS = {
    "PAID": "Plačano",
    "PENDING": "Čaka na plačilo",
    "UNPAID": "Neplačano",
    "REVERSED": "Stornirano",
}


def payment_method_label(value: object) -> str:
    text = str(value or "").strip()
    return PAYMENT_METHOD_LABELS.get(text, text or "–")


def payment_status_label(value: object) -> str:
    text = str(value or "").strip()
    return PAYMENT_STATUS_LABELS.get(text, text or "–")


class PosError(RuntimeError):
    pass


def _actor_username(value: object) -> str:
    actor = str(value or "").strip()
    if not actor:
        raise PosError("Dejanski prijavljeni uporabnik ni znan.")
    return actor


def _require_action_permission(
    action_permissions: Iterable[str] | None, permission: str
) -> None:
    granted = {
        str(value or "").strip().upper() for value in (action_permissions or ())
    }
    if permission not in granted:
        raise PosError("Prijavljeni uporabnik nima dovoljenja za storno dokumentov.")


def dec(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0).replace(",", ".")).quantize(CENT, ROUND_HALF_UP)
    except Exception as exc:
        raise PosError(f"Neveljaven denarni znesek: {value}") from exc


@dataclass(frozen=True)
class PosLine:
    line_kind: str
    name: str
    code: str = ""
    barcode: str = ""
    article_id: int | None = None
    quantity: Decimal = Decimal("1.00")
    base_unit_price: Decimal = Decimal("0.00")
    acquisition_cost: Decimal = Decimal("0.00")
    requested_discount_pct: Decimal = Decimal("0.00")
    service_tier: str = ""
    service_billable_minutes: int = 0
    service_tariff_version: str = ""
    service_first_rate: Decimal = Decimal("0.00")
    service_extra_half_hour_rate: Decimal = Decimal("0.00")
    stock_already_consumed: bool = False
    origin_kind: str = "POS_ADDED"
    origin_id: int | None = None
    source_locked: bool = False
    requested_lot_serial: str = ""
    service_elapsed_minutes: int = 0
    central_line_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class CalculatedLine:
    source: PosLine
    applied_discount_pct: Decimal
    final_unit_price: Decimal
    line_total: Decimal


def calculate_line(line: PosLine, labor_floor: object = 22) -> CalculatedLine:
    qty = dec(line.quantity)
    base = dec(line.base_unit_price)
    requested = max(Decimal("0"), min(Decimal("100"), dec(line.requested_discount_pct)))
    if qty == 0 or base <= 0:
        raise PosError(f"Postavka »{line.name}« nima veljavne količine ali cene.")
    if qty < 0 and str(line.origin_kind or "") != "POS_CORRECTION":
        raise PosError(
            f"Negativna postavka »{line.name}« mora biti evidentirana kot korekcija."
        )

    if line.line_kind == "ARTICLE":
        floor = (dec(line.acquisition_cost) * Decimal("1.05")).quantize(CENT, ROUND_HALF_UP)
        if base < floor:
            raise PosError(
                f"Prodajna cena artikla »{line.name}« je nižja od nabavne cene + 5 %. "
                "Ceno popravi v Office in nato ponovno odpri blagajno."
            )
    elif line.line_kind == "SERVICE":
        if line.service_tier not in {"T1", "T2"}:
            raise PosError("Storitev mora biti vezana na T1 ali T2.")
        floor = dec(labor_floor)
    else:
        raise PosError(f"Neznana vrsta postavke: {line.line_kind}")

    requested_price = (base * (Decimal("1") - requested / Decimal("100"))).quantize(
        CENT, ROUND_HALF_UP
    )
    final = max(floor, requested_price)
    applied = ((base - final) / base * Decimal("100")).quantize(CENT, ROUND_HALF_UP)
    applied = max(Decimal("0"), applied)
    return CalculatedLine(line, applied, final, (final * qty).quantize(CENT, ROUND_HALF_UP))


def net_public_pos_lines(lines: Iterable[PosLine]) -> list[PosLine]:
    """Pretvori sledljive interne +/- vrstice v pozitivne javne postavke računa."""
    from dataclasses import replace

    result: list[PosLine] = []
    positions: dict[tuple, int] = {}
    for line in lines:
        if line.line_kind != "ARTICLE" or line.source_locked:
            result.append(line)
            continue
        key = (
            line.article_id, line.barcode, line.code, line.name,
            str(dec(line.base_unit_price)), str(dec(line.requested_discount_pct)),
            line.requested_lot_serial,
        )
        if key not in positions:
            positions[key] = len(result)
            result.append(replace(line, quantity=Decimal("0"), origin_kind="POS_ADDED"))
        position = positions[key]
        result[position] = replace(
            result[position], quantity=dec(result[position].quantity) + dec(line.quantity)
        )
    public: list[PosLine] = []
    for line in result:
        quantity = dec(line.quantity)
        if quantity < 0:
            raise PosError(
                f"Korekcije artikla »{line.name}« presegajo dodano količino."
            )
        if quantity > 0:
            public.append(line)
    if not public:
        raise PosError("Po količinskih korekcijah račun nima nobene postavke.")
    return public


def service_tariff_snapshot(tier: str, extra_half_hours: int, settings: dict) -> dict:
    extra = max(0, int(extra_half_hours or 0))
    pricing = settings.get("pricing", {})
    if tier == "T1":
        first = dec(pricing.get("t1_hour_eur", 30))
    elif tier == "T2":
        first = dec(pricing.get("onsite_first_hour_eur", 90))
    else:
        raise PosError("Storitev mora biti T1 ali T2.")
    extra_rate = dec(pricing.get("extra_half_hour_eur", 15))
    return {
        "price": first + extra_rate * extra,
        "billable_minutes": 60 + 30 * extra,
        "tariff_version": str(
            pricing.get("tariff_version") or "T1T2-v1-2026-09-04"
        ),
        "first_rate": first,
        "extra_half_hour_rate": extra_rate,
        "extra_half_hours": extra,
    }


def service_labor_from_minutes(
    contract_context: str,
    elapsed_minutes: int,
    settings: dict,
    *,
    description: str = "Delo",
) -> dict:
    """Pretvori dejanski čas zaključka v eno zaklenjeno postavko dela."""
    try:
        elapsed = int(elapsed_minutes)
    except (TypeError, ValueError) as exc:
        raise PosError("Čas dela mora biti vpisan v celih minutah.") from exc
    if elapsed < 1:
        raise PosError("Čas dela mora biti najmanj 1 minuta.")

    context = str(contract_context or "").strip().upper()
    tier = "T2" if context == "MFT_TEREN" else "T1"
    return service_labor_for_tier_minutes(
        tier, elapsed, settings, description=description
    )


def service_labor_for_tier_minutes(
    tier: str,
    elapsed_minutes: int,
    settings: dict,
    *,
    description: str = "Delo",
) -> dict:
    """Izračun ročno dodane T1/T2 postavke iz dejanskih minut."""
    try:
        elapsed = int(elapsed_minutes)
    except (TypeError, ValueError) as exc:
        raise PosError("Čas dela mora biti vpisan v celih minutah.") from exc
    if elapsed < 1:
        raise PosError("Čas dela mora biti najmanj 1 minuta.")
    tier = str(tier or "").strip().upper()
    if tier not in {"T1", "T2"}:
        raise PosError("Storitev mora biti T1 ali T2.")
    extra_half_hours = max(0, (elapsed - 60 + 29) // 30)
    tariff = service_tariff_snapshot(tier, extra_half_hours, settings)
    return {
        **tariff,
        "tier": tier,
        "description": str(description or "").strip() or "Delo",
        "elapsed_minutes": elapsed,
    }


def service_price(tier: str, extra_half_hours: int, settings: dict) -> Decimal:
    return service_tariff_snapshot(tier, extra_half_hours, settings)["price"]


def service_line_public_name(line: PosLine | dict) -> str:
    """Javni opis storitve dopolni s preverljivim časom in tarifnim izračunom."""
    get = line.get if isinstance(line, dict) else lambda key, default=None: getattr(line, key, default)
    name = str(get("article_name", get("name", "")) or "")
    tier = str(get("service_tier", "") or "").upper()
    minutes = int(get("service_billable_minutes", 0) or 0)
    if tier not in {"T1", "T2"} or minutes < 60:
        return name
    extra = max(0, (minutes - 60) // 30)
    first_rate = dec(get("service_first_rate", 0))
    extra_rate = dec(get("service_extra_half_hour_rate", 0))
    version = str(get("service_tariff_version", "") or "")
    duration = "1 začeta ura" + (f" + {extra} × 30 min" if extra else "")
    calculation = f"{first_rate:.2f} EUR"
    if extra:
        calculation += f" + {extra} × {extra_rate:.2f} EUR"
    suffix = f"; tarifa {version}" if version else ""
    return f"{name} ({tier}: {duration}; {calculation}{suffix})"


def service_line_public_quantity(line: PosLine | dict) -> str:
    """Na dokumentu je merska količina dela čas v minutah, ne umetna količina 1."""
    get = line.get if isinstance(line, dict) else lambda key, default=None: getattr(line, key, default)
    minutes = int(get("service_billable_minutes", 0) or 0)
    tier = str(get("service_tier", "") or "").upper()
    if tier in {"T1", "T2"} and minutes > 0:
        return f"{minutes} min"
    return f"{dec(get('qty', get('quantity', 0))):.2f}"


def default_service_description(tier: str) -> str:
    """Stalni javni opis tarife, da blagajnik ne prepisuje istega besedila."""
    labels = {
        "T1": "Delo v delavnici",
        "T2": "Delo na terenu",
    }
    try:
        return labels[str(tier).strip().upper()]
    except KeyError as exc:
        raise PosError("Storitev mora biti vezana na T1 ali T2.") from exc


def ensure_pos_schema(office_db: Path | str) -> None:
    """Pripravi osnovno Office in razširjeno POS shemo tudi v povsem prazni bazi.

    Blagajna se lahko zažene neposredno iz glavnega menija, zato ne sme biti
    odvisna od tega, ali je bil modul Office v tej seji že odprt.
    """
    c = sqlite3.connect(str(office_db))
    try:
        c.executescript(
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_no TEXT NOT NULL UNIQUE,
                doc_type TEXT NOT NULL,
                partner_id INTEGER,
                partner_name_snapshot TEXT,
                partner_address_snapshot TEXT,
                partner_tax_snapshot TEXT,
                partner_pcd_snapshot REAL NOT NULL DEFAULT 0,
                external_no TEXT,
                date TEXT NOT NULL,
                order_date TEXT,
                received_date TEXT,
                due_date TEXT,
                currency TEXT NOT NULL DEFAULT 'EUR',
                delivery_cost REAL NOT NULL DEFAULT 0,
                stock_effect INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OSNUTEK',
                linked_claim_id INTEGER,
                linked_claim_no TEXT,
                linked_service_id INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                booked_at TEXT,
                FOREIGN KEY(partner_id) REFERENCES partners(id)
            );
            CREATE TABLE IF NOT EXISTS document_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                article_id INTEGER,
                barcode TEXT,
                article_name TEXT NOT NULL,
                internal_code TEXT,
                qty REAL NOT NULL DEFAULT 1,
                unit_price REAL NOT NULL DEFAULT 0,
                discount_pct REAL NOT NULL DEFAULT 0,
                vat_pct REAL NOT NULL DEFAULT 0,
                delivery_share REAL NOT NULL DEFAULT 0,
                actual_unit_cost REAL NOT NULL DEFAULT 0,
                margin_pct REAL NOT NULL DEFAULT 0,
                calculated_price REAL NOT NULL DEFAULT 0,
                recommended_price REAL NOT NULL DEFAULT 0,
                sell_price REAL NOT NULL DEFAULT 0,
                line_net REAL NOT NULL DEFAULT 0,
                line_vat REAL NOT NULL DEFAULT 0,
                line_total REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_partner_name ON partners(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_doc_date ON documents(date);
            CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(doc_type);
            CREATE INDEX IF NOT EXISTS idx_doc_service ON documents(linked_service_id);
            CREATE TABLE IF NOT EXISTS sales_claims_v31(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_no TEXT NOT NULL UNIQUE,
                document_id INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                opened_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                resolved_at TEXT,
                resolution TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sales_claim_document_v31
                ON sales_claims_v31(document_id,status);
            """
        )
        doc_cols = {row[1] for row in c.execute("PRAGMA table_info(documents)")}
        additions = {
            "pos_issue_token": "TEXT",
            "payment_method": "TEXT",
            "payment_status": "TEXT",
            "invoice_total": "REAL NOT NULL DEFAULT 0",
            "customer_phone": "TEXT",
            "customer_email": "TEXT",
            "fiscal_status": "TEXT",
            "print_status": "TEXT",
            "print_confirmed_at": "TEXT",
            "print_copy_count": "INTEGER NOT NULL DEFAULT 0",
            "used_goods_12m_signed": "INTEGER NOT NULL DEFAULT 0",
            "fiscal_premise": "TEXT",
            "fiscal_device": "TEXT",
            "fiscal_year": "INTEGER",
            "fiscal_sequence": "INTEGER",
            "fiscal_invoice_no": "TEXT",
            "operator_code_snapshot": "TEXT",
            "operator_tax_number_snapshot": "TEXT",
            "fiscal_issued_at": "TEXT",
            "storno_reason": "TEXT",
            "storno_at": "TEXT",
            "storno_by": "TEXT",
            "linked_invoice_id": "INTEGER",
            "replacement_of_invoice_id": "INTEGER",
            "payment_reference": "TEXT",
            "cash_session_id": "INTEGER",
            "furs_zoi": "TEXT",
            "furs_eor": "TEXT",
            "furs_qr_payload": "TEXT",
            "furs_message_id": "TEXT",
            "furs_error_code": "TEXT",
            "furs_error_message": "TEXT",
            "payment_received_at": "TEXT",
            "payment_received_method": "TEXT",
            "furs_submission_deadline": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in doc_cols:
                c.execute(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")

        item_cols = {row[1] for row in c.execute("PRAGMA table_info(document_items)")}
        item_additions = {
            "line_kind": "TEXT NOT NULL DEFAULT 'ARTICLE'",
            "service_tier": "TEXT",
            "service_billable_minutes": "INTEGER NOT NULL DEFAULT 0",
            "service_tariff_version": "TEXT",
            "service_first_rate": "REAL NOT NULL DEFAULT 0",
            "service_extra_half_hour_rate": "REAL NOT NULL DEFAULT 0",
            "requested_discount_pct": "REAL NOT NULL DEFAULT 0",
            "stock_already_consumed": "INTEGER NOT NULL DEFAULT 0",
            "origin_kind": "TEXT NOT NULL DEFAULT 'POS_ADDED'",
            "origin_id": "INTEGER",
            "source_locked": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, ddl in item_additions.items():
            if name not in item_cols:
                c.execute(f"ALTER TABLE document_items ADD COLUMN {name} {ddl}")

        c.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_issue_token
                ON documents(pos_issue_token) WHERE pos_issue_token IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_fiscal_sequence
                ON documents(fiscal_year, fiscal_premise, fiscal_device, fiscal_sequence)
                WHERE fiscal_sequence IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_fiscal_invoice_no
                ON documents(fiscal_year, fiscal_invoice_no)
                WHERE fiscal_invoice_no IS NOT NULL;
            CREATE TABLE IF NOT EXISTS pos_payments_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS pos_print_events_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                copy_no INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS pos_audit_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER,
                event_type TEXT NOT NULL,
                detail TEXT,
                operator_code TEXT NOT NULL,
                actor_username TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_operation_locks_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lock_type TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                reference TEXT,
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE TABLE IF NOT EXISTS used_goods_agreements_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agreement_no TEXT NOT NULL UNIQUE,
                issue_token TEXT NOT NULL,
                device_article_ids TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_address TEXT,
                pdf_path TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PREPARED',
                operator_code TEXT NOT NULL,
                prepared_at TEXT NOT NULL,
                signed_at TEXT,
                sale_document_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_used_agreement_token_v30
                ON used_goods_agreements_v30(issue_token,id);
            CREATE TABLE IF NOT EXISTS pos_drafts_v31(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_token TEXT NOT NULL UNIQUE,
                operator_code TEXT NOT NULL,
                actor_username TEXT,
                premise_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                final_document_id INTEGER,
                abandoned_at TEXT,
                FOREIGN KEY(final_document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_pos_drafts_recovery_v31
                ON pos_drafts_v31(status,operator_code,updated_at);
            CREATE TABLE IF NOT EXISTS furs_outbox_v30(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL UNIQUE,
                message_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                response_json TEXT NOT NULL DEFAULT '{}',
                attempt_count INTEGER NOT NULL DEFAULT 1,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_furs_outbox_status_v30
                ON furs_outbox_v30(status,id);
            """
        )
        ensure_cash_schema_connection(c)
        payment_cols = {
            row[1] for row in c.execute("PRAGMA table_info(pos_payments_v30)")
        }
        if "reversed_at" not in payment_cols:
            c.execute("ALTER TABLE pos_payments_v30 ADD COLUMN reversed_at TEXT")
        if "reversal_of_id" not in payment_cols:
            c.execute("ALTER TABLE pos_payments_v30 ADD COLUMN reversal_of_id INTEGER")
        audit_cols = {row[1] for row in c.execute("PRAGMA table_info(pos_audit_v30)")}
        if "actor_username" not in audit_cols:
            c.execute("ALTER TABLE pos_audit_v30 ADD COLUMN actor_username TEXT")
        draft_cols = {row[1] for row in c.execute("PRAGMA table_info(pos_drafts_v31)")}
        if "actor_username" not in draft_cols:
            c.execute("ALTER TABLE pos_drafts_v31 ADD COLUMN actor_username TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_pos_drafts_actor_recovery_v31 "
            "ON pos_drafts_v31(status,actor_username,device_id,updated_at)"
        )
        agreement_cols = {
            row[1] for row in c.execute("PRAGMA table_info(used_goods_agreements_v30)")
        }
        if "customer_partner_id" not in agreement_cols:
            c.execute(
                "ALTER TABLE used_goods_agreements_v30 ADD COLUMN customer_partner_id INTEGER"
            )
        c.commit()
    finally:
        c.close()


def customer_partner_by_id(
    office_db: Path | str, partner_id: int,
) -> dict[str, Any] | None:
    """Vrne enoten kupčev zapis za F9, račun in garancijski dogovor."""
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            """
            SELECT id,name,address,post_city,country,tax_no,registration_no,
                   phone,email,partner_type
            FROM partners WHERE id=?
            """,
            (int(partner_id),),
        ).fetchone()
    finally:
        c.close()
    if not row:
        return None
    street = str(row["address"] or "").strip()
    post_city = str(row["post_city"] or "").strip()
    address = ", ".join(value for value in (street, post_city) if value)
    return {
        "id": int(row["id"]), "name": str(row["name"] or "").strip(),
        "address": address, "street": street, "post_city": post_city,
        "country": str(row["country"] or "").strip(),
        "tax_number": str(row["tax_no"] or "").strip(),
        "registration_number": str(row["registration_no"] or "").strip(),
        "phone": str(row["phone"] or "").strip(),
        "email": str(row["email"] or "").strip(),
        "partner_type": str(row["partner_type"] or "").strip(),
    }


def list_customer_partners(office_db: Path | str) -> list[dict[str, Any]]:
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    try:
        ids = [
            int(row[0]) for row in c.execute(
                "SELECT id FROM partners ORDER BY name COLLATE NOCASE,id"
            ).fetchall()
        ]
    finally:
        c.close()
    return [partner for pid in ids if (partner := customer_partner_by_id(office_db, pid))]


def save_customer_partner(
    office_db: Path | str, customer: dict[str, str], *,
    preferred_partner_id: int | None = None,
) -> int:
    """Poišče ali ustvari partnerja in vrne ID, ki se veže na trenutni račun."""
    ensure_pos_schema(office_db)
    name = str(customer.get("name") or "").strip()
    address = str(customer.get("address") or "").strip()
    tax = re.sub(r"\D", "", str(customer.get("tax_number") or ""))
    phone = str(customer.get("phone") or "").strip()
    email = str(customer.get("email") or "").strip()
    if not name or name == "Končni kupec":
        raise PosError("Vpiši ali izberi konkretnega kupca.")
    c = sqlite3.connect(str(office_db))
    try:
        if preferred_partner_id is not None:
            row = c.execute(
                "SELECT id FROM partners WHERE id=?", (int(preferred_partner_id),)
            ).fetchone()
            if not row:
                raise PosError("Izbrani partner ne obstaja več.")
            return int(row[0])
        row = None
        if tax:
            row = c.execute(
                "SELECT id FROM partners WHERE replace(replace(tax_no,'SI',''),' ','')=? "
                "ORDER BY id LIMIT 1", (tax,),
            ).fetchone()
        if row is None:
            row = c.execute(
                """
                SELECT id FROM partners
                WHERE lower(trim(name))=lower(?)
                  AND lower(trim(COALESCE(address,'')))=lower(?)
                ORDER BY id LIMIT 1
                """,
                (name, address),
            ).fetchone()
        if row:
            partner_id = int(row[0])
            c.execute(
                """
                UPDATE partners SET phone=CASE WHEN ?<>'' THEN ? ELSE phone END,
                    email=CASE WHEN ?<>'' THEN ? ELSE email END,
                    partner_type=CASE WHEN partner_type='DOBAVITELJ' THEN 'OBOJE' ELSE partner_type END,
                    updated_at=? WHERE id=?
                """,
                (phone, phone, email, email, datetime.now().isoformat(timespec="seconds"), partner_id),
            )
        else:
            now = datetime.now().isoformat(timespec="seconds")
            cursor = c.execute(
                """
                INSERT INTO partners(
                    name,address,post_city,country,tax_no,registration_no,iban,
                    phone,email,contact_person,partner_type,pcd_days,notes,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (name,address,"","Slovenija",tax,"","",phone,email,"","KUPEC",0,
                 "Ustvarjeno in pripeto prek F9 v Blagajni.",now,now),
            )
            partner_id = int(cursor.lastrowid)
        c.commit()
        return partner_id
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _draft_payload(payload: dict) -> tuple[str, str]:
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise PosError("Osnutka računa ni mogoče zapisati.") from exc
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pos_draft_data_authority(payload: dict) -> str:
    """Return the accounting authority of a recoverable POS draft.

    Drafts written before central Cashier existed have no marker and are
    deliberately classified as LOCAL.  Unknown markers are rejected instead
    of guessing which stock and invoice authority owns the draft.
    """
    authority = str(payload.get("data_authority") or "LOCAL").strip().upper()
    if authority not in {"LOCAL", "CENTRAL"}:
        raise PosError(
            "Osnutek računa ima neznan izvor podatkov in ga ni varno obnoviti."
        )
    return authority


def save_pos_draft(
    office_db: Path | str,
    *,
    issue_token: str,
    operator_code: str,
    actor_username: str,
    premise_id: str,
    device_id: str,
    payload: dict,
) -> None:
    """Atomsko shrani trenutni POS osnutek brez izdaje ali vpliva na zalogo."""
    ensure_pos_schema(office_db)
    token = str(issue_token or "").strip()
    if not token:
        raise PosError("Osnutek računa nima enolične oznake.")
    pos_draft_data_authority(payload)
    actor = _actor_username(actor_username)
    payload_json, digest = _draft_payload(payload)
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT status,actor_username,device_id FROM pos_drafts_v31 WHERE issue_token=?",
            (token,),
        ).fetchone()
        if row:
            if str(row[0]) != "ACTIVE":
                raise PosError("Zaključenega ali opuščenega osnutka ni mogoče prepisati.")
            if not row[1] or str(row[1]) != actor or str(row[2]) != str(device_id or "").strip():
                raise PosError("Osnutek pripada drugemu uporabniku ali drugi napravi.")
        c.execute(
            """
            INSERT INTO pos_drafts_v31(
                issue_token,operator_code,actor_username,premise_id,device_id,status,
                payload_json,payload_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,'ACTIVE',?,?,?,?)
            ON CONFLICT(issue_token) DO UPDATE SET
                operator_code=excluded.operator_code,
                actor_username=excluded.actor_username,
                premise_id=excluded.premise_id,
                device_id=excluded.device_id,
                payload_json=excluded.payload_json,
                payload_sha256=excluded.payload_sha256,
                updated_at=excluded.updated_at
            WHERE pos_drafts_v31.status='ACTIVE'
              AND pos_drafts_v31.actor_username=excluded.actor_username
              AND pos_drafts_v31.device_id=excluded.device_id
            """,
            (
                token, str(operator_code or "-").strip() or "-", actor,
                str(premise_id or "").strip(), str(device_id or "").strip(),
                payload_json, digest, now, now,
            ),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def load_active_pos_draft(
    office_db: Path | str,
    *,
    actor_username: str,
    device_id: str,
) -> dict | None:
    """Naloži zadnji neizdani osnutek in preveri njegov kontrolni povzetek."""
    ensure_pos_schema(office_db)
    actor = _actor_username(actor_username)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            """
            SELECT * FROM pos_drafts_v31
            WHERE status='ACTIVE' AND actor_username=? AND device_id=?
            ORDER BY updated_at DESC,id DESC LIMIT 1
            """,
            (actor, str(device_id or "").strip()),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        raw = str(result["payload_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest != str(result["payload_sha256"]):
            raise PosError(
                "Shranjeni osnutek računa je poškodovan. Program ga ne bo samodejno uporabil."
            )
        try:
            result["payload"] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PosError("Shranjeni osnutek računa ni veljaven JSON.") from exc
        return result
    finally:
        c.close()


def abandon_pos_draft(
    office_db: Path | str, issue_token: str, *, actor_username: str, device_id: str
) -> bool:
    """Označi osnutek kot opuščen; zapis ostane v revizijski sledi."""
    ensure_pos_schema(office_db)
    actor = _actor_username(actor_username)
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        row = c.execute(
            "SELECT status,actor_username,device_id FROM pos_drafts_v31 WHERE issue_token=?",
            (str(issue_token),),
        ).fetchone()
        if not row:
            return False
        if not row[1] or str(row[1]) != actor or str(row[2]) != str(device_id or "").strip():
            raise PosError("Osnutek pripada drugemu uporabniku ali drugi napravi.")
        updated = c.execute(
            "UPDATE pos_drafts_v31 SET status='ABANDONED',abandoned_at=?,updated_at=? "
            "WHERE issue_token=? AND status='ACTIVE' AND actor_username=? AND device_id=?",
            (now, now, str(issue_token), actor, str(device_id or "").strip()),
        )
        c.commit()
        return updated.rowcount == 1
    finally:
        c.close()


def search_inventory(inventory_db: Path | str, query: str = "", limit: int = 100) -> list[dict]:
    c = sqlite3.connect(str(inventory_db))
    c.row_factory = sqlite3.Row
    try:
        ensure_lot_schema(c, "main")
        c.commit()
        q = str(query or "").strip()
        params: list[object] = []
        select_params: list[object] = []
        matched_lot = "'' AS requested_lot_serial"
        where = "i.active=1 AND (i.stock_qty-COALESCE(i.reserved_qty,0))>0"
        if q:
            where += " AND (i.barcode LIKE ? OR i.article_name LIKE ? OR i.internal_code LIKE ? OR i.supplier_sku LIKE ? OR EXISTS(SELECT 1 FROM inventory_lots_v30 lx WHERE lx.article_id=i.id AND lx.status='ACTIVE' AND lx.quantity_available>0 AND lx.lot_serial LIKE ?))"
            p = f"%{q}%"
            params.extend([p, p, p, p, p])
            matched_lot = "COALESCE((SELECT lx.lot_serial FROM inventory_lots_v30 lx WHERE lx.article_id=i.id AND lx.status='ACTIVE' AND lx.quantity_available>0 AND lx.lot_serial=? LIMIT 1),'') AS requested_lot_serial"
            select_params.append(q)
        params.append(int(limit))
        rows = c.execute(
            f"""
            SELECT i.id, i.barcode, i.article_name,
                   COALESCE(i.internal_code,'') AS internal_code,
                   i.stock_qty, COALESCE(i.reserved_qty,0) AS reserved_qty, i.price,
                   COALESCE((
                       SELECT MAX(l.acquisition_cost) FROM inventory_lots_v30 l
                       WHERE l.article_id=i.id AND l.status='ACTIVE' AND l.quantity_available>0
                   ), i.acquisition_cost, 0) AS acquisition_cost,
                   COALESCE(i.category,'') AS category,
                   COALESCE(i.lot_serial,'') AS lot_serial,
                   COALESCE(i.source_kind,'') AS source_kind,
                   COALESCE(i.source_id,0) AS source_id,
                   {matched_lot}
            FROM inventory_items i WHERE {where}
            ORDER BY i.article_name COLLATE NOCASE, i.barcode LIMIT ?
            """,
            select_params + params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def _next_invoice_no(c: sqlite3.Connection) -> str:
    year = datetime.now().year
    prefix = f"IRA-{year}-"
    rows = c.execute(
        "SELECT doc_no FROM documents WHERE doc_type='IZDANI RAČUN' AND doc_no LIKE ?",
        (prefix + "%",),
    ).fetchall()
    numbers = []
    for (number,) in rows:
        try:
            numbers.append(int(str(number).rsplit("-", 1)[-1]))
        except ValueError:
            pass
    return f"{prefix}{(max(numbers, default=0) + 1):04d}"


def _next_storno_no(c: sqlite3.Connection) -> str:
    year = datetime.now().year
    prefix = f"STR-{year}-"
    rows = c.execute(
        "SELECT doc_no FROM documents WHERE doc_type='STORNO RAČUNA' AND doc_no LIKE ?",
        (prefix + "%",),
    ).fetchall()
    numbers = []
    for (number,) in rows:
        try:
            numbers.append(int(str(number).rsplit("-", 1)[-1]))
        except ValueError:
            pass
    return f"{prefix}{(max(numbers, default=0) + 1):04d}"


def _fiscal_identifier(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z]{1,20}", result):
        raise PosError(f"{label} mora vsebovati od 1 do 20 črk ali številk.")
    return result


def _next_fiscal_number(
    c: sqlite3.Connection, *, premise_id: str, device_id: str,
    issued_at: datetime,
) -> tuple[int, int, str]:
    year = int(issued_at.year)
    row = c.execute(
        """
        SELECT COALESCE(MAX(fiscal_sequence), 0)
        FROM documents
        WHERE fiscal_year=? AND fiscal_premise=? AND fiscal_device=?
        """,
        (year, premise_id, device_id),
    ).fetchone()
    sequence = int(row[0] or 0) + 1
    return year, sequence, f"{premise_id}-{device_id}-{sequence}"


def _table_exists(c: sqlite3.Connection, schema: str, table: str) -> bool:
    row = c.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _validate_linked_service(c: sqlite3.Connection, service_id: int) -> tuple[int | None, str]:
    if not _table_exists(c, "servicedb", "service"):
        raise PosError("Servisna baza ni pripravljena; povezanega servisa ni mogoče zaračunati.")
    columns = {row[1] for row in c.execute("PRAGMA servicedb.table_info(service)")}
    required = {"id", "invoice_no", "office_invoice_id"}
    if not required.issubset(columns):
        raise PosError("Servisna baza nima vseh polj za varno povezavo z računom.")
    has_order = "service_order_id" in columns
    has_resolution = "resolution_status" in columns
    select_order = ", service_order_id" if has_order else ", NULL"
    select_resolution = ", resolution_status" if has_resolution else ", 'AWAITING_INVOICE'"
    row = c.execute(
        "SELECT invoice_no, office_invoice_id" + select_order + select_resolution
        + " FROM servicedb.service WHERE id=?",
        (int(service_id),),
    ).fetchone()
    if not row:
        raise PosError("Povezanega servisa ni mogoče najti.")
    if str(row[0] or "").strip() or row[1] is not None:
        raise PosError("Ta servis že ima izdan račun. Ponovna izdaja ni dovoljena.")
    if str(row[3] or "AWAITING_INVOICE") != "AWAITING_INVOICE":
        raise PosError("Ta servis je bil preusmerjen v odkup in ga ni dovoljeno zaračunati.")
    if row[2] is not None and _table_exists(c, "servicedb", "service_orders"):
        order = c.execute(
            "SELECT status FROM servicedb.service_orders WHERE id=?", (int(row[2]),)
        ).fetchone()
        if order and str(order[0]) != "OPEN":
            raise PosError("Servisno naročilo ni odprto za izdajo računa.")
    duplicate = c.execute(
        """
        SELECT doc_no FROM documents
        WHERE doc_type='IZDANI RAČUN' AND status='KNJIŽENO'
          AND linked_service_id=? LIMIT 1
        """,
        (int(service_id),),
    ).fetchone()
    if duplicate:
        raise PosError(f"Ta servis je že povezan z računom {duplicate[0]}.")
    return (int(row[2]) if row[2] is not None else None), str(row[0] or "")


def _complete_linked_service(
    c: sqlite3.Connection, *, service_id: int, service_order_id: int | None,
    invoice_no: str, document_id: int,
) -> None:
    service_columns = {
        row[1] for row in c.execute("PRAGMA servicedb.table_info(service)")
    }
    custody_sql = ""
    params: list[object] = [invoice_no, int(document_id)]
    if "ready_for_pickup_at" in service_columns:
        custody_sql = (
            ", ready_for_pickup_at=?, picked_up_at=NULL, "
            "final_notice_recorded_at=NULL"
        )
        params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(int(service_id))
    cursor = c.execute(
        "UPDATE servicedb.service SET invoice_no=?, office_invoice_id=?"
        + custody_sql
        + " WHERE id=? AND trim(COALESCE(invoice_no,''))='' "
          "AND office_invoice_id IS NULL",
        tuple(params),
    )
    if cursor.rowcount != 1:
        raise PosError("Servisa ni bilo mogoče enolično povezati z računom.")
    if service_order_id is None or not _table_exists(c, "servicedb", "service_orders"):
        return
    order_columns = {
        row[1] for row in c.execute("PRAGMA servicedb.table_info(service_orders)")
    }
    if not {"id", "status", "completed_at", "service_id"}.issubset(order_columns):
        raise PosError("Servisno naročilo nima vseh polj za varen zaključek.")
    order_cursor = c.execute(
        """
        UPDATE servicedb.service_orders
        SET status='COMPLETED', completed_at=?, service_id=?
        WHERE id=? AND status='OPEN'
        """,
        (
            datetime.now().isoformat(timespec="seconds"), int(service_id),
            int(service_order_id),
        ),
    )
    if order_cursor.rowcount != 1:
        raise PosError("Servisnega naročila ni bilo mogoče zaključiti z računom.")


def _validate_linked_service_lines(
    c: sqlite3.Connection, *, service_id: int,
    lines: list[CalculatedLine],
) -> None:
    """Vse izvorne servisne vrstice morajo ostati prisotne in nespremenjene."""
    expected_manual: dict[int, tuple] = {}
    if _table_exists(c, "servicedb", "service_manual_lines"):
        expected_manual = {
            int(row[0]): (
                str(row[1]), str(row[2]), dec(row[3]), dec(row[4]),
                str(row[5] or ""), int(row[6] or 0), str(row[7] or ""),
                dec(row[8]), dec(row[9]),
            )
            for row in c.execute(
                """
                SELECT id, line_kind, description, quantity, unit_price,
                       COALESCE(service_tier,''), COALESCE(billable_minutes,0),
                       COALESCE(tariff_version,''), COALESCE(first_rate,0),
                       COALESCE(extra_half_hour_rate,0)
                FROM servicedb.service_manual_lines WHERE service_id=?
                """,
                (int(service_id),),
            )
        }
    part_columns = {
        str(row[1]) for row in c.execute("PRAGMA servicedb.table_info(service_parts)")
    }
    part_id = "id" if "id" in part_columns else "rowid"
    article_id = "article_id" if "article_id" in part_columns else "NULL"
    barcode = "barcode" if "barcode" in part_columns else "''"
    part_name = "part_name" if "part_name" in part_columns else "''"
    part_code = "part_id" if "part_id" in part_columns else "''"
    unit_price = "unit_price" if "unit_price" in part_columns else "0"
    expected_parts = {
        int(row[0]): (
            int(row[1]) if row[1] is not None else None,
            str(row[2] or ""), str(row[3] or ""), str(row[4] or ""), dec(row[5]),
        )
        for row in c.execute(
            f"SELECT {part_id}, {article_id}, {barcode}, {part_name}, "
            f"{part_code}, {unit_price} FROM servicedb.service_parts WHERE service_id=?",
            (int(service_id),),
        )
    }
    seen_manual: set[int] = set()
    seen_parts: set[int] = set()
    for calculated in lines:
        line = calculated.source
        origin = str(line.origin_kind or "POS_ADDED")
        if origin == "SERVICE_MANUAL":
            if not line.source_locked or line.origin_id not in expected_manual:
                raise PosError("Servisna postavka nima veljavnega zaklenjenega izvora.")
            if int(line.origin_id) in seen_manual:
                raise PosError("Ista servisna postavka je na računu podvojena.")
            (
                kind, description, quantity, price, stored_tier, minutes,
                tariff_version, first_rate, extra_rate,
            ) = expected_manual[int(line.origin_id)]
            if (
                str(line.service_tier) != ("T2" if (stored_tier or kind).upper() == "T2" else "T1")
                or str(line.name) != description
                or dec(line.quantity) != quantity
                or dec(line.base_unit_price) != price
                or int(line.service_billable_minutes or 0) != minutes
                or str(line.service_tariff_version or "") != tariff_version
                or dec(line.service_first_rate) != first_rate
                or dec(line.service_extra_half_hour_rate) != extra_rate
            ):
                raise PosError("Zaklenjena servisna postavka je bila spremenjena.")
            seen_manual.add(int(line.origin_id))
        elif origin == "SERVICE_PART":
            if not line.source_locked or line.origin_id not in expected_parts:
                raise PosError("Servisni del nima veljavnega zaklenjenega izvora.")
            if int(line.origin_id) in seen_parts:
                raise PosError("Isti servisni del je na računu podvojen.")
            article_id, barcode, name, code, price = expected_parts[int(line.origin_id)]
            reservation = c.execute(
                """
                SELECT COALESCE(SUM(quantity),0)
                FROM inv.inventory_lot_allocations_v30
                WHERE allocation_type='SERVICE_RESERVATION' AND source_id=?
                  AND source_line_id=? AND reversed_at IS NULL
                """,
                (int(service_id), int(line.origin_id)),
            ).fetchone()
            has_reservation = dec(reservation[0] if reservation else 0) > 0
            if (
                line.article_id != article_id
                or str(line.barcode or "") != barcode
                or str(line.name or "") != (name or "Vgrajeni del")
                or str(line.code or "") != code
                or dec(line.quantity) != Decimal("1.00")
                or dec(line.base_unit_price) != price
                or bool(line.stock_already_consumed) == has_reservation
            ):
                raise PosError("Zaklenjeni servisni del je bil spremenjen.")
            seen_parts.add(int(line.origin_id))
        elif origin == "SERVICE_LEGACY":
            if not line.source_locked or int(line.origin_id or 0) != int(service_id):
                raise PosError("Starejša servisna postavka nima veljavnega izvora.")
        elif origin != "POS_ADDED":
            raise PosError(f"Neznan izvor postavke: {origin}")
    if seen_manual != set(expected_manual) or seen_parts != set(expected_parts):
        raise PosError(
            "Račun ne vsebuje vseh zaklenjenih postavk povezanega servisa. "
            "Servis ponovno prenesi v Blagajno."
        )


def _validate_live_article_prices(
    c: sqlite3.Connection, *, lines: list[CalculatedLine],
    linked_service_id: int | None,
) -> None:
    """Tik pred knjiženjem znova preveri Office ceno in zaščiteno LOT nabavno ceno."""
    for calculated in lines:
        src = calculated.source
        if src.line_kind != "ARTICLE" or not src.article_id:
            continue
        row = c.execute(
            "SELECT price, COALESCE(acquisition_cost,0), active "
            "FROM inv.inventory_items WHERE id=?",
            (int(src.article_id),),
        ).fetchone()
        if not row or int(row[2] or 0) != 1:
            raise PosError(f"Artikel »{src.name}« ni več aktiven v zalogi.")
        if str(src.origin_kind or "POS_ADDED") == "POS_ADDED":
            if dec(src.base_unit_price) != dec(row[0]):
                raise PosError(
                    f"Cena artikla »{src.name}« je bila v Office spremenjena. "
                    "Račun ponovno odpri, da prevzame novo zaklenjeno ceno."
                )
            protected_cost = maximum_available_cost(
                c, schema="inv", article_id=int(src.article_id)
            ) or dec(row[1])
        elif str(src.origin_kind) == "SERVICE_PART" and linked_service_id:
            lot_row = c.execute(
                """
                SELECT MAX(acquisition_cost)
                FROM inv.inventory_lot_allocations_v30
                WHERE allocation_type='SERVICE_RESERVATION'
                  AND source_id=? AND source_line_id=?
                  AND reversed_at IS NULL
                """,
                (int(linked_service_id), int(src.origin_id)),
            ).fetchone()
            protected_cost = (
                dec(lot_row[0]) if lot_row and lot_row[0] is not None
                else dec(src.acquisition_cost)
            )
        else:
            protected_cost = dec(src.acquisition_cost)
        protected_floor = (protected_cost * Decimal("1.05")).quantize(
            CENT, ROUND_HALF_UP
        )
        if calculated.final_unit_price < protected_floor:
            raise PosError(
                f"Rabat na artiklu »{src.name}« bi šel pod dejansko nabavno ceno + 5 %. "
                "Postavko ponovno naloži iz zaloge."
            )


def prepare_used_goods_agreement(
    *, office_db: Path | str, inventory_db: Path | str,
    article_ids: Iterable[int], customer: dict[str, str], issue_token: str,
    output_dir: Path | str, company: dict[str, str], operator_code: str,
    customer_partner_id: int | None = None,
) -> tuple[int, str, Path]:
    """Pripravi nespremenljiv A4 dogovor/garancijski list za podpis na papirju."""
    ensure_pos_schema(office_db)
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        raise PosError("Na računu ni obnovljene naprave.")
    name = str(customer.get("name") or "").strip()
    address = str(customer.get("address") or "").strip()
    if not name or name == "Končni kupec":
        raise PosError("Pred pripravo dogovora vpiši ime kupca.")
    inv = sqlite3.connect(str(inventory_db))
    inv.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = inv.execute(
            f"""
            SELECT i.id,i.article_name,d.imei,d.model,d.storage
            FROM inventory_items i JOIN used_devices d ON d.id=i.source_id
            WHERE i.id IN ({placeholders}) AND i.source_kind='BUYBACK_DEVICE'
            ORDER BY i.id
            """,
            ids,
        ).fetchall()
    finally:
        inv.close()
    if len(rows) != len(ids):
        raise PosError("Vse izbrane naprave nimajo sledljivega zapisa obnove.")
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        c.execute("BEGIN IMMEDIATE")
        seq = int(c.execute(
            "SELECT COUNT(*) FROM used_goods_agreements_v30 WHERE agreement_no LIKE ?",
            (f"DOG-{datetime.now().year}-%",),
        ).fetchone()[0]) + 1
        agreement_no = f"DOG-{datetime.now().year}-{seq:04d}"
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        pdf_path = output / f"{agreement_no}_dogovor_garancija.pdf"

        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        styles = getSampleStyleSheet()
        title = ParagraphStyle("MFTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=15)
        body = styles["BodyText"]
        body.leading = 14
        story = [
            Paragraph("DOGOVOR ZA RABLJENO OBNOVLJENO NAPRAVO IN GARANCIJSKI LIST", title),
            Spacer(1, 5 * mm),
            Paragraph(f"<b>Številka dokumenta:</b> {escape(agreement_no)}", body),
            Paragraph(f"<b>Prodajalec:</b> {escape(str(company.get('legal_name') or 'MikroFix'))}, "
                      f"{escape(str(company.get('street') or 'Paradiž 73'))}, "
                      f"{escape(str(company.get('city') or '2282 Cirkulane'))}", body),
            Paragraph(f"<b>Kupec:</b> {escape(name)}, {escape(address or '-')}", body),
            Spacer(1, 4 * mm),
        ]
        device_data = [["Naprava", "IMEI"]]
        for row in rows:
            label = " ".join(str(x or "") for x in (row["model"],row["storage"])).strip()
            device_data.append([label or str(row["article_name"]), str(row["imei"] or "-")])
        table = Table(device_data, colWidths=[110 * mm, 65 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E8EDF2")),
            ("GRID",(0,0),(-1,-1),0.5,colors.grey),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("FONTSIZE",(0,0),(-1,-1),9),
        ]))
        story.extend([
            table, Spacer(1, 6 * mm),
            Paragraph(
                "Kupec in prodajalec se izrecno dogovorita, da se pri navedenem rabljenem "
                "blagu obdobje odgovornosti prodajalca za neskladnost skrajša na <b>eno leto "
                "od dobave</b>. Dogovor ne izključuje drugih pravic, ki jih potrošniku dajejo "
                "prisilni predpisi.", body,
            ),
            Spacer(1, 3 * mm),
            Paragraph(
                "MikroFix za navedeno obnovljeno napravo zagotavlja 12-mesečno prostovoljno "
                "tržno garancijo pod objavljenimi pogoji. Običajna obraba zmogljivosti baterije "
                "ni napaka; napaka novo vgrajene baterije je v garancijskem obdobju obravnavana "
                "po garancijskih pogojih.", body,
            ),
            Spacer(1, 12 * mm),
            Table([
                ["Kraj in datum: ____________________", "Podpis kupca: ____________________"],
                ["", "Podpis prodajalca: _______________"],
            ], colWidths=[85 * mm, 90 * mm], rowHeights=[14 * mm,14 * mm]),
        ])
        SimpleDocTemplate(
            str(pdf_path), pagesize=A4, rightMargin=18*mm,leftMargin=18*mm,
            topMargin=16*mm,bottomMargin=16*mm,
        ).build(story)
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        cur = c.execute(
            """
            INSERT INTO used_goods_agreements_v30(
                agreement_no,issue_token,device_article_ids,customer_name,
                customer_address,pdf_path,pdf_sha256,status,operator_code,prepared_at,
                customer_partner_id
            ) VALUES(?,?,?,?,?,?,?,'PREPARED',?,?,?)
            """,
            (agreement_no,str(issue_token),json.dumps(ids),name,address,str(pdf_path),digest,
             operator_code,now,int(customer_partner_id) if customer_partner_id else None),
        )
        c.commit()
        return int(cur.lastrowid),agreement_no,pdf_path
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def set_used_goods_agreement_signed(
    office_db: Path | str, agreement_id: int, signed: bool,
) -> None:
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    try:
        row = c.execute(
            "SELECT pdf_path,pdf_sha256,sale_document_id FROM used_goods_agreements_v30 WHERE id=?",
            (int(agreement_id),),
        ).fetchone()
        if not row or row[2] is not None:
            raise PosError("Dogovora ni mogoče spremeniti.")
        path = Path(str(row[0]))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != str(row[1]):
            raise PosError("PDF dogovora manjka ali je bil spremenjen.")
        c.execute(
            "UPDATE used_goods_agreements_v30 SET status=?,signed_at=? WHERE id=?",
            ("SIGNED" if signed else "PREPARED", datetime.now().isoformat(timespec="seconds") if signed else None, int(agreement_id)),
        )
        c.commit()
    finally:
        c.close()
def issue_invoice(
    *, office_db: Path | str, inventory_db: Path | str, service_db: Path | str,
    lines: Iterable[PosLine], payments: dict[str, object], customer: dict[str, str],
    operator_code: str, operator_tax_number: str,
    premise_id: str, electronic_device_id: str,
    labor_floor: object, issue_token: str | None = None,
    linked_service_id: int | None = None, used_goods_12m_signed: bool = False,
    bank_transfer_received: bool = False,
    payment_reference: str = "",
    used_goods_agreement_id: int | None = None,
    customer_partner_id: int | None = None,
    replacement_of_invoice_id: int | None = None,
    authenticated_username: str | None = None,
    action_permissions: Iterable[str] | None = None,
    fiscalization_required: bool = False,
    fiscalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    numbering_structure: str = "B",
    test_mode: bool = True,
) -> tuple[int, str, Decimal]:
    actor = _actor_username(authenticated_username)
    if replacement_of_invoice_id is not None:
        _require_action_permission(action_permissions, STORNO_DOCUMENT_PERMISSION)
    ensure_pos_schema(office_db)
    token = issue_token or str(uuid.uuid4())
    premise = _fiscal_identifier(premise_id, "Oznaka poslovnega prostora")
    device = _fiscal_identifier(electronic_device_id, "Oznaka elektronske naprave")
    operator = _fiscal_identifier(operator_code, "Oznaka operaterja")
    operator_tax = str(operator_tax_number or "").strip()
    if not re.fullmatch(r"\d{8}", operator_tax):
        raise PosError("Davčna številka operaterja mora vsebovati 8 številk.")
    calculated = [calculate_line(line, labor_floor) for line in lines]
    if not calculated:
        raise PosError("Račun nima nobene postavke.")
    total = sum((line.line_total for line in calculated), Decimal("0.00")).quantize(CENT)

    normalized_payments = {str(k): dec(v) for k, v in payments.items() if dec(v) > 0}
    if set(normalized_payments) - {"CASH", "CARD", "BANK_TRANSFER"}:
        raise PosError("Nepodprt način plačila.")
    if sum(normalized_payments.values(), Decimal("0.00")) != total:
        raise PosError("Seštevek plačil mora biti natančno enak znesku računa.")
    payment_method = next(iter(normalized_payments)) if len(normalized_payments) == 1 else "SPLIT"
    has_bank_transfer = normalized_payments.get("BANK_TRANSFER", 0) > 0
    if payment_method == "SPLIT" and has_bank_transfer and not bank_transfer_received:
        raise PosError(
            "Pri razdeljenem plačilu mora biti TRR del pred izdajo dejansko prejet."
        )
    payment_status = "PAID" if not has_bank_transfer or bank_transfer_received else "UNPAID"
    needs_fiscalization = bool(fiscalization_required) and any(
        normalized_payments.get(method, 0) > 0 for method in ("CASH", "CARD")
    )
    if needs_fiscalization and fiscalizer is None:
        raise PosError("Produkcijska davčna potrditev je obvezna, FURS povezava pa ni pripravljena.")
    issued_at = datetime.now()
    now = issued_at.isoformat(timespec="seconds")

    c = sqlite3.connect(str(office_db))
    try:
        c.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        service_attached = Path(service_db).exists()
        if service_attached:
            c.execute("ATTACH DATABASE ? AS servicedb", (str(service_db),))
        c.execute("BEGIN IMMEDIATE")
        ensure_lot_schema(c, "inv")
        draft = c.execute(
            "SELECT status,actor_username,device_id FROM pos_drafts_v31 WHERE issue_token=?",
            (token,),
        ).fetchone()
        if draft and (
            not draft[1]
            or str(draft[1]) != actor
            or str(draft[2]) != device
        ):
            raise PosError("Osnutek računa pripada drugemu uporabniku ali drugi napravi.")
        if draft and str(draft[0]) == "ABANDONED":
            raise PosError("Opuščenega osnutka ni dovoljeno izdati.")
        existing = c.execute(
            "SELECT id, COALESCE(fiscal_invoice_no, doc_no), invoice_total "
            "FROM documents WHERE pos_issue_token=?",
            (token,),
        ).fetchone()
        if existing:
            c.rollback()
            return int(existing[0]), str(existing[1]), dec(existing[2])

        full_inventory = c.execute(
            """
            SELECT reference FROM inventory_operation_locks_v30
            WHERE lock_type='FULL_INVENTORY' AND active=1 AND released_at IS NULL
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if full_inventory:
            raise PosError(
                "Prodaja je zaklenjena zaradi polne inventure"
                + (f" ({full_inventory[0]})" if full_inventory[0] else "") + "."
            )
        try:
            cash_session_id = require_open_cash_session(
                c, premise_id=premise, device_id=device
            )
        except CashJournalError as exc:
            raise PosError(
                "Računa ni mogoče izdati brez odprtega blagajniškega dnevnika. "
                + str(exc)
            ) from exc
        cash_payment = dec(normalized_payments.get("CASH",0))

        replacement_source = None
        if replacement_of_invoice_id is not None:
            replacement_source = c.execute(
                "SELECT id,doc_type,status,COALESCE(fiscal_invoice_no,doc_no) "
                "FROM documents WHERE id=?",
                (int(replacement_of_invoice_id),),
            ).fetchone()
            if (
                not replacement_source
                or str(replacement_source[1]) != "IZDANI RAČUN"
                or str(replacement_source[2]) != "STORNIRANO"
            ):
                raise PosError(
                    "Novi račun je mogoče vezati samo na v celoti storniran izdani račun."
                )
            storno_document = c.execute(
                "SELECT id FROM documents WHERE doc_type='STORNO RAČUNA' "
                "AND linked_invoice_id=? AND status='KNJIŽENO' ORDER BY id DESC LIMIT 1",
                (int(replacement_of_invoice_id),),
            ).fetchone()
            if not storno_document:
                raise PosError("Stornirani račun nima povezanega knjiženega storno dokumenta.")
            existing_replacement = c.execute(
                "SELECT COALESCE(fiscal_invoice_no,doc_no) FROM documents "
                "WHERE doc_type='IZDANI RAČUN' AND replacement_of_invoice_id=? "
                "ORDER BY id LIMIT 1",
                (int(replacement_of_invoice_id),),
            ).fetchone()
            if existing_replacement:
                raise PosError(
                    f"Stornirani račun že ima nadomestni račun {existing_replacement[0]}."
                )
        pending_print = c.execute(
            """
            SELECT COALESCE(fiscal_invoice_no, doc_no) FROM documents
            WHERE pos_issue_token IS NOT NULL AND status='KNJIŽENO'
              AND COALESCE(print_status,'AWAITING_PRINT')='AWAITING_PRINT'
            ORDER BY id LIMIT 1
            """
        ).fetchone()
        if pending_print:
            raise PosError(
                f"Račun {pending_print[0]} še čaka na potrditev originalnega tiska. "
                "Najprej zaključi njegov tisk."
            )
        linked_service_order_id = None
        if linked_service_id:
            if not service_attached:
                raise PosError("Servisna baza ni dosegljiva; računa za servis ni mogoče izdati.")
            linked_service_order_id, _existing_invoice = _validate_linked_service(
                c, int(linked_service_id)
            )
            _validate_linked_service_lines(
                c, service_id=int(linked_service_id), lines=calculated
            )
        _validate_live_article_prices(
            c, lines=calculated, linked_service_id=linked_service_id
        )

        invoice_no = _next_invoice_no(c)
        fiscal_year, fiscal_sequence, fiscal_invoice_no = _next_fiscal_number(
            c,
            premise_id=premise,
            device_id=device,
            issued_at=issued_at,
        )
        customer_name = str(customer.get("name") or "Končni kupec").strip()
        customer_address = str(customer.get("address") or "").strip()
        customer_tax = str(customer.get("tax_number") or "").strip()
        notes = str(customer.get("notes") or "").strip()
        partner_id = int(customer_partner_id) if customer_partner_id is not None else None
        if partner_id is not None:
            partner = c.execute(
                """
                SELECT name,address,post_city,tax_no,phone,email
                FROM partners WHERE id=?
                """, (partner_id,),
            ).fetchone()
            if not partner:
                raise PosError("Kupec, izbran s F9, ne obstaja več v bazi partnerjev.")
            partner_address = ", ".join(
                value for value in (str(partner[1] or "").strip(), str(partner[2] or "").strip())
                if value
            )
            if (
                str(partner[0] or "").strip() != customer_name
                or partner_address != customer_address
                or re.sub(r"\D", "", str(partner[3] or ""))
                   != re.sub(r"\D", "", customer_tax)
                or str(partner[4] or "").strip() != str(customer.get("phone") or "").strip()
                or str(partner[5] or "").strip() != str(customer.get("email") or "").strip()
            ):
                raise PosError(
                    "Podatki kupca so bili po izbiri F9 spremenjeni. Kupca ponovno potrdi s F9."
                )
        used_article_ids: list[int] = []
        for calculated_line in calculated:
            article_id_value = calculated_line.source.article_id
            if not article_id_value:
                continue
            source = c.execute(
                "SELECT source_kind FROM inv.inventory_items WHERE id=?",
                (int(article_id_value),),
            ).fetchone()
            if source and str(source[0]) == "BUYBACK_DEVICE":
                used_article_ids.append(int(article_id_value))
        used_article_ids = sorted(set(used_article_ids))
        if used_article_ids:
            if not used_goods_12m_signed or not used_goods_agreement_id:
                raise PosError(
                    "Pred izdajo natisni in evidentiraj podpisan A4 dogovor za obnovljeno napravo."
                )
            agreement = c.execute(
                "SELECT issue_token,device_article_ids,customer_name,pdf_path,pdf_sha256,status,sale_document_id,customer_partner_id "
                "FROM used_goods_agreements_v30 WHERE id=?",
                (int(used_goods_agreement_id),),
            ).fetchone()
            if (
                not agreement or str(agreement[0]) != token
                or sorted(json.loads(str(agreement[1]))) != used_article_ids
                or str(agreement[2]).strip() != customer_name
                or str(agreement[5]) != "SIGNED" or agreement[6] is not None
                or partner_id is None or int(agreement[7] or 0) != partner_id
            ):
                raise PosError("Podpisani dogovor ne ustreza kupcu ali napravam na računu.")
            agreement_path = Path(str(agreement[3]))
            if (
                not agreement_path.is_file()
                or hashlib.sha256(agreement_path.read_bytes()).hexdigest() != str(agreement[4])
            ):
                raise PosError("PDF podpisanega dogovora manjka ali je bil spremenjen.")
        cur = c.execute(
            """
            INSERT INTO documents(
                doc_no, doc_type, partner_id, partner_name_snapshot, partner_address_snapshot,
                partner_tax_snapshot, partner_pcd_snapshot, external_no, date,
                currency, delivery_cost, stock_effect, status, linked_service_id,
                notes, created_at, updated_at, booked_at, pos_issue_token,
                payment_method, payment_status, invoice_total, customer_phone,
                customer_email, fiscal_status, print_status, used_goods_12m_signed,
                fiscal_premise, fiscal_device, fiscal_year, fiscal_sequence,
                fiscal_invoice_no, operator_code_snapshot,
                operator_tax_number_snapshot, fiscal_issued_at
                ,payment_reference
                ,cash_session_id
                ,replacement_of_invoice_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                invoice_no, "IZDANI RAČUN", partner_id, customer_name, customer_address,
                customer_tax, 0, "", date.today().isoformat(), "EUR", 0, 0,
                "KNJIŽENO", linked_service_id, notes, now, now, now, token,
                payment_method, payment_status, float(total),
                str(customer.get("phone") or "").strip(),
                str(customer.get("email") or "").strip(),
                (
                    "PENDING_SUBMISSION" if needs_fiscalization
                    else ("TEST_DISABLED" if test_mode else "NOT_REQUIRED")
                ),
                "AWAITING_PRINT", 1 if used_goods_12m_signed else 0,
                premise, device, fiscal_year, fiscal_sequence, fiscal_invoice_no,
                operator, operator_tax, now,
                str(payment_reference or "").strip(),
                cash_session_id,
                int(replacement_of_invoice_id) if replacement_of_invoice_id is not None else None,
            ),
        )
        document_id = int(cur.lastrowid)
        if used_goods_agreement_id:
            c.execute(
                "UPDATE used_goods_agreements_v30 SET sale_document_id=? "
                "WHERE id=? AND status='SIGNED' AND sale_document_id IS NULL",
                (document_id,int(used_goods_agreement_id)),
            )

        for item in calculated:
            src = item.source
            cur = c.execute(
                """
                INSERT INTO document_items(
                    document_id, article_id, barcode, article_name, internal_code,
                    qty, unit_price, discount_pct, vat_pct, delivery_share,
                    actual_unit_cost, margin_pct, calculated_price, recommended_price,
                    sell_price, line_net, line_vat, line_total, line_kind,
                    service_tier, service_billable_minutes, service_tariff_version,
                    service_first_rate, service_extra_half_hour_rate,
                    requested_discount_pct, stock_already_consumed,
                    origin_kind, origin_id, source_locked
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    document_id, src.article_id, src.barcode, src.name, src.code,
                    float(src.quantity), float(src.base_unit_price),
                    float(item.applied_discount_pct), 0, 0, float(src.acquisition_cost),
                    0, float(item.final_unit_price), float(item.final_unit_price),
                    float(item.final_unit_price), float(item.line_total), 0,
                    float(item.line_total), src.line_kind, src.service_tier,
                    int(src.service_billable_minutes or 0),
                    str(src.service_tariff_version or ""),
                    float(dec(src.service_first_rate)),
                    float(dec(src.service_extra_half_hour_rate)),
                    float(src.requested_discount_pct), 1 if src.stock_already_consumed else 0,
                    str(src.origin_kind or "POS_ADDED"), src.origin_id,
                    1 if src.source_locked else 0,
                ),
            )
            line_id = int(cur.lastrowid)
            if src.line_kind != "ARTICLE" or not src.article_id:
                continue
            qty_to_issue = dec(src.quantity)
            stock = c.execute(
                """
                SELECT stock_qty, COALESCE(reserved_qty,0), article_name, lot_serial,
                       location, source_kind, source_id
                FROM inv.inventory_items WHERE id=? AND active=1
                """,
                (int(src.article_id),),
            ).fetchone()
            is_reserved_service_part = (
                str(src.origin_kind or "") == "SERVICE_PART"
                and linked_service_id is not None
                and src.origin_id is not None
                and not src.stock_already_consumed
            )
            if is_reserved_service_part:
                reservation_rows = c.execute(
                    """
                    SELECT id,lot_serial,quantity,acquisition_cost
                    FROM inv.inventory_lot_allocations_v30
                    WHERE allocation_type='SERVICE_RESERVATION' AND source_id=?
                      AND source_line_id=? AND reversed_at IS NULL
                    ORDER BY id
                    """,
                    (int(linked_service_id), int(src.origin_id)),
                ).fetchall()
                reserved_quantity = sum(
                    (dec(row[2]) for row in reservation_rows), Decimal("0")
                )
                if (
                    not stock or reserved_quantity != qty_to_issue
                    or dec(stock[0]) < qty_to_issue
                    or dec(stock[1]) < qty_to_issue
                ):
                    raise PosError(
                        f"Rezervacija servisnega dela »{src.name}« ni več skladna z zalogo."
                    )
                balance = dec(stock[0]) - qty_to_issue
                reserved_after = dec(stock[1]) - qty_to_issue
                c.execute(
                    """
                    UPDATE inv.inventory_lot_allocations_v30
                    SET allocation_type='POS_INVOICE',source_id=?,source_line_id=?
                    WHERE allocation_type='SERVICE_RESERVATION' AND source_id=?
                      AND source_line_id=? AND reversed_at IS NULL
                    """,
                    (
                        int(document_id), int(line_id), int(linked_service_id),
                        int(src.origin_id),
                    ),
                )
                c.execute(
                    "UPDATE inv.inventory_items SET stock_qty=?,reserved_qty=?,updated_at=? "
                    "WHERE id=?",
                    (float(balance), float(reserved_after), now, int(src.article_id)),
                )
                allocation_lots = ", ".join(
                    str(row[1]) for row in reservation_rows if row[1]
                )
                actual_cost = max(dec(row[3]) for row in reservation_rows)
                c.execute(
                    "UPDATE document_items SET actual_unit_cost=? WHERE id=?",
                    (float(actual_cost), int(line_id)),
                )
                c.execute(
                    """
                    INSERT INTO inv.stock_movements(
                        movement_no,created_at,article_id,quantity_delta,balance_after,
                        movement_type,source_type,source_id,source_line_id,lot_serial,
                        location_from,location_to,reason,operator_code,reversal_of_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    """,
                    (
                        f"PREM-POS-{document_id:06d}-{line_id}", now,
                        int(src.article_id), -float(qty_to_issue), float(balance),
                        "SALE_OUT", "POS_INVOICE", document_id, line_id,
                        allocation_lots or stock[3] or "", stock[4] or "", "KUPEC",
                        f"Prodaja po računu {fiscal_invoice_no}", operator,
                    ),
                )
                continue
            if src.stock_already_consumed:
                # Zgodovinski servis je zalogo odpisal že ob shranitvi servisa.
                continue
            if not stock or dec(stock[0]) - dec(stock[1]) < qty_to_issue:
                raise PosError(f"Premalo zaloge za artikel »{src.name}«.")
            balance = dec(stock[0]) - qty_to_issue
            allocations = allocate_fifo(
                c,
                schema="inv",
                article_id=int(src.article_id),
                quantity=qty_to_issue,
                allocation_type="POS_INVOICE",
                source_id=int(document_id),
                source_line_id=int(line_id),
                created_at=now,
                requested_lot_serial=src.requested_lot_serial,
            )
            allocation_lots = ", ".join(
                str(entry["lot_serial"]) for entry in allocations if entry["lot_serial"]
            )
            if allocations:
                actual_cost = max(entry["acquisition_cost"] for entry in allocations)
                c.execute(
                    "UPDATE document_items SET actual_unit_cost=? WHERE id=?",
                    (float(actual_cost), int(line_id)),
                )
            c.execute(
                "UPDATE inv.inventory_items SET stock_qty=?, updated_at=? WHERE id=?",
                (float(balance), now, int(src.article_id)),
            )
            c.execute(
                """
                INSERT INTO inv.stock_movements(
                    movement_no, created_at, article_id, quantity_delta,
                    balance_after, movement_type, source_type, source_id,
                    source_line_id, lot_serial, location_from, location_to,
                    reason, operator_code, reversal_of_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    f"PREM-POS-{document_id:06d}-{line_id}", now, int(src.article_id),
                    -float(qty_to_issue), float(balance), "SALE_OUT", "POS_INVOICE",
                    document_id, line_id, allocation_lots or stock[3] or "",
                    stock[4] or "", "KUPEC",
                    f"Prodaja po računu {fiscal_invoice_no}", operator,
                ),
            )
            if stock[5] == "BUYBACK_DEVICE":
                if not used_goods_12m_signed:
                    raise PosError("Manjka podpisan dogovor za 12-mesečno obdobje obnovljene naprave.")
                c.execute(
                    "UPDATE inv.used_devices SET status='SOLD', buyer_name=?, sale_document_id=?, warranty_until=date(?,'+12 months'), updated_at=? WHERE id=?",
                    (customer_name, document_id, now, now, int(stock[6])),
                )

        for method, amount in normalized_payments.items():
            c.execute(
                "INSERT INTO pos_payments_v30(document_id,method,amount,status,created_at) VALUES(?,?,?,?,?)",
                (
                    document_id, method, float(amount),
                    "PAID" if method != "BANK_TRANSFER" or bank_transfer_received else "PENDING",
                    now,
                ),
            )
        if cash_session_id and cash_payment > 0:
            record_cash_entry(
                c,session_id=cash_session_id,entry_type="CASH_SALE",
                amount=cash_payment,reference_type="POS_INVOICE",
                reference_id=document_id,reference_no=fiscal_invoice_no,
                description=f"Gotovinsko plačilo računa {fiscal_invoice_no}",
                operator_code=operator,
            )
        fiscal_result = None
        if needs_fiscalization:
            fiscal_result = fiscalizer(
                {
                    "issue_datetime": issued_at,
                    "sequence": fiscal_sequence,
                    "public_invoice_number": fiscal_invoice_no,
                    "premise_id": premise,
                    "device_id": device,
                    "invoice_amount": total,
                    "payment_amount": total,
                    "operator_tax_number": operator_tax,
                    "customer_vat_number": customer_tax,
                    "numbering_structure": numbering_structure,
                }
            )
            status = str(fiscal_result.get("status") or "")
            if status not in {"CONFIRMED", "PENDING_RECONCILIATION"}:
                raise PosError("FURS povezava je vrnila neveljavno stanje.")
            from mikrofix_furs import build_fiscal_qr_payload
            qr_payload = build_fiscal_qr_payload(
                zoi=fiscal_result.get("zoi", ""),
                tax_number=str(fiscal_result.get("tax_number") or ""),
                issue_datetime=issued_at,
            )
            c.execute(
                """
                UPDATE documents SET fiscal_status=?,furs_zoi=?,furs_eor=?,
                    furs_qr_payload=?,furs_message_id=?,furs_error_code=?,
                    furs_error_message=?,updated_at=? WHERE id=?
                """,
                (
                    status, str(fiscal_result.get("zoi") or ""),
                    str(fiscal_result.get("eor") or ""), qr_payload,
                    str(fiscal_result.get("message_id") or ""),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    now, document_id,
                ),
            )
            c.execute(
                """
                INSERT INTO furs_outbox_v30(
                    document_id,message_id,status,payload_json,response_json,
                    error_code,error_message,created_at,updated_at,confirmed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    document_id, str(fiscal_result.get("message_id") or ""), status,
                    json.dumps(fiscal_result.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(fiscal_result.get("response") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    now, now, now if status == "CONFIRMED" else None,
                ),
            )
        c.execute(
            "INSERT INTO pos_audit_v30(document_id,event_type,detail,operator_code,actor_username,created_at) VALUES(?,?,?,?,?,?)",
            (
                document_id, "INVOICE_ISSUED",
                f"fiscal={fiscal_invoice_no}; internal={invoice_no}; total={total}; token={token}; "
                f"furs={(fiscal_result or {}).get('status','NOT_REQUIRED')}",
                operator, actor, now,
            ),
        )
        if replacement_source:
            c.execute(
                "INSERT INTO pos_audit_v30(document_id,event_type,detail,operator_code,actor_username,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    document_id,
                    "REPLACEMENT_INVOICE_LINKED",
                    f"replacement_of={int(replacement_of_invoice_id)}; "
                    f"original_no={replacement_source[3]}",
                    operator, actor,
                    now,
                ),
            )
        if linked_service_id and service_attached:
            _complete_linked_service(
                c,
                service_id=int(linked_service_id),
                service_order_id=linked_service_order_id,
                invoice_no=fiscal_invoice_no,
                document_id=document_id,
            )
        if draft:
            updated_draft = c.execute(
                "UPDATE pos_drafts_v31 SET status='ISSUED',final_document_id=?,updated_at=? "
                "WHERE issue_token=? AND status='ACTIVE' AND actor_username=? AND device_id=?",
                (document_id, now, token, actor, device),
            )
            if str(draft[0]) == "ACTIVE" and updated_draft.rowcount != 1:
                raise PosError("Osnutka računa ni bilo mogoče varno zaključiti.")
        c.commit()
        return document_id, fiscal_invoice_no, total
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def mark_printed(office_db: Path | str, document_id: int, operator_code: str) -> None:
    ensure_pos_schema(office_db)
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        row = c.execute(
            "SELECT print_status FROM documents WHERE id=?", (int(document_id),)
        ).fetchone()
        if not row:
            raise PosError("Račun ni najden.")
        if row[0] == "PRINTED":
            return
        c.execute(
            "UPDATE documents SET print_status='PRINTED', print_confirmed_at=?, updated_at=? WHERE id=? AND print_status!='PRINTED'",
            (now, now, int(document_id)),
        )
        c.execute(
            "INSERT INTO pos_print_events_v30(document_id,event_type,copy_no,detail,created_at) VALUES(?,?,?,?,?)",
            (int(document_id), "ORIGINAL_CONFIRMED", 0, operator_code, now),
        )
        c.commit()
    finally:
        c.close()


def mark_pdf_ready(office_db: Path | str, document_id: int, operator_code: str) -> None:
    """Potrdi, da sta nespremenljiva originalna PDF-ja varno izdelana.

    To ni potrditev fizičnega izpisa. Stanje PDF_READY sprosti naslednji račun,
    fizični tisk pa se lahko pozneje posebej potrdi s ``mark_printed``.
    """
    ensure_pos_schema(office_db)
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT print_status FROM documents WHERE id=?", (int(document_id),)
        ).fetchone()
        if not row:
            raise PosError("Račun ni najden.")
        if row[0] in ("PDF_READY", "PRINTED"):
            c.commit()
            return
        c.execute(
            "UPDATE documents SET print_status='PDF_READY', updated_at=? "
            "WHERE id=? AND COALESCE(print_status,'AWAITING_PRINT')='AWAITING_PRINT'",
            (now, int(document_id)),
        )
        c.execute(
            "INSERT INTO pos_print_events_v30"
            "(document_id,event_type,copy_no,detail,created_at) VALUES(?,?,?,?,?)",
            (int(document_id), "ORIGINAL_PDF_READY", 0, operator_code, now),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def register_copy(office_db: Path | str, document_id: int, operator_code: str) -> int:
    ensure_pos_schema(office_db)
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT print_status, print_copy_count FROM documents WHERE id=?", (int(document_id),)).fetchone()
        if not row or row[0] not in ("PDF_READY", "PRINTED"):
            raise PosError("Kopija je dovoljena šele po izdelavi originalnega PDF-ja.")
        number = int(row[1] or 0) + 1
        c.execute("UPDATE documents SET print_copy_count=?, updated_at=? WHERE id=?", (number, now, int(document_id)))
        c.execute(
            "INSERT INTO pos_print_events_v30(document_id,event_type,copy_no,detail,created_at) VALUES(?,?,?,?,?)",
            (int(document_id), "COPY_PRINTED", number, operator_code, now),
        )
        c.commit()
        return number
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def invoice_snapshot(office_db: Path | str, document_id: int) -> tuple[dict, list[dict], list[dict]]:
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        doc = c.execute("SELECT * FROM documents WHERE id=?", (int(document_id),)).fetchone()
        if not doc:
            raise PosError("Račun ni najden.")
        result = dict(doc)
        if result.get("replacement_of_invoice_id"):
            source = c.execute(
                "SELECT COALESCE(fiscal_invoice_no,doc_no) FROM documents WHERE id=?",
                (int(result["replacement_of_invoice_id"]),),
            ).fetchone()
            storno = c.execute(
                "SELECT doc_no FROM documents WHERE doc_type='STORNO RAČUNA' "
                "AND linked_invoice_id=? ORDER BY id DESC LIMIT 1",
                (int(result["replacement_of_invoice_id"]),),
            ).fetchone()
            result["replacement_original_no"] = str(source[0]) if source else ""
            result["replacement_storno_no"] = str(storno[0]) if storno else ""
        lines = c.execute("SELECT * FROM document_items WHERE document_id=? ORDER BY id", (int(document_id),)).fetchall()
        payments = c.execute("SELECT * FROM pos_payments_v30 WHERE document_id=? ORDER BY id", (int(document_id),)).fetchall()
        return result, [dict(r) for r in lines], [dict(r) for r in payments]
    finally:
        c.close()


def latest_pending_print(office_db: Path | str) -> dict | None:
    """Vrne zadnji izdani POS račun, ki še čaka na potrditev originalnega tiska."""
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            """
            SELECT id, COALESCE(fiscal_invoice_no, doc_no) AS doc_no,
                   invoice_total, linked_service_id, fiscal_premise, fiscal_device
            FROM documents
            WHERE pos_issue_token IS NOT NULL AND status='KNJIŽENO'
              AND COALESCE(print_status,'AWAITING_PRINT')='AWAITING_PRINT'
            ORDER BY id LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def search_issued_invoices(office_db: Path | str, query: str = "") -> list[dict]:
    """Računi in storni za blagajniški pregled; brez osnutkov in predračunov."""
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        term = f"%{str(query or '').strip()}%"
        rows = c.execute(
            """
            SELECT id,COALESCE(fiscal_invoice_no,doc_no) invoice_no,doc_type,date,
                   COALESCE(partner_name_snapshot,'Končni kupec') customer,
                   COALESCE(partner_address_snapshot,'') customer_address,
                   COALESCE(invoice_total,0) invoice_total,status,print_status,
                   COALESCE(print_copy_count,0) copy_count,linked_service_id
            FROM documents
            WHERE doc_type IN ('IZDANI RAČUN','STORNO RAČUNA')
              AND (?='%%' OR COALESCE(fiscal_invoice_no,doc_no) LIKE ?
                   OR COALESCE(partner_name_snapshot,'') LIKE ?
                   OR COALESCE(partner_address_snapshot,'') LIKE ? OR date LIKE ?)
            ORDER BY id DESC LIMIT 500
            """,
            (term, term, term, term, term),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def open_sales_claim(
    office_db: Path | str, *, document_id: int, reason: str, operator_code: str,
) -> tuple[int, str]:
    """Odpre sledljivo reklamacijo tudi za prodajo brez servisnega naloga."""
    ensure_pos_schema(office_db)
    reason = str(reason or "").strip()
    if len(reason) < 3:
        raise PosError("Za reklamacijo vpiši jasen opis razloga.")
    c = sqlite3.connect(str(office_db))
    try:
        c.execute("BEGIN IMMEDIATE")
        doc = c.execute(
            "SELECT doc_type,COALESCE(fiscal_invoice_no,doc_no) FROM documents WHERE id=?",
            (int(document_id),),
        ).fetchone()
        if not doc or str(doc[0]) != "IZDANI RAČUN":
            raise PosError("Reklamacijo je mogoče odpreti samo za izdani račun.")
        existing = c.execute(
            "SELECT id,claim_no FROM sales_claims_v31 WHERE document_id=? AND status='OPEN'",
            (int(document_id),),
        ).fetchone()
        if existing:
            c.rollback()
            return int(existing[0]), str(existing[1])
        year = datetime.now().year
        prefix = f"REK-POS-{year}-"
        numbers = []
        for (raw,) in c.execute(
            "SELECT claim_no FROM sales_claims_v31 WHERE claim_no LIKE ?", (prefix + "%",)
        ):
            try: numbers.append(int(str(raw).rsplit("-", 1)[-1]))
            except ValueError: pass
        number = f"{prefix}{max(numbers, default=0) + 1:04d}"
        cur = c.execute(
            "INSERT INTO sales_claims_v31(claim_no,document_id,opened_at,opened_by,reason) "
            "VALUES(?,?,?,?,?)",
            (number, int(document_id), datetime.now().isoformat(timespec="seconds"),
             str(operator_code or ""), reason),
        )
        c.commit()
        return int(cur.lastrowid), number
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def unpaid_bank_invoices(office_db: Path | str) -> list[dict]:
    """Vrne izdane TRR račune, pri katerih plačilo še ni bilo prejeto."""
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            """
            SELECT d.id,COALESCE(d.fiscal_invoice_no,d.doc_no) AS invoice_no,
                   d.date,COALESCE(d.partner_name_snapshot,'Končni kupec') AS customer,
                   d.invoice_total,d.payment_reference
            FROM documents d
            WHERE d.doc_type='IZDANI RAČUN' AND d.status='KNJIŽENO'
              AND d.payment_status='UNPAID'
              AND EXISTS(
                  SELECT 1 FROM pos_payments_v30 p
                  WHERE p.document_id=d.id AND p.method='BANK_TRANSFER'
                    AND p.status='PENDING' AND p.reversal_of_id IS NULL
              )
            ORDER BY d.date,d.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def pending_furs_documents(office_db: Path | str) -> list[dict]:
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            """
            SELECT d.id,COALESCE(d.fiscal_invoice_no,d.doc_no) AS invoice_no,
                   d.doc_type,d.invoice_total,d.furs_zoi,f.message_id,
                   f.attempt_count,f.error_code,f.error_message,f.updated_at
            FROM furs_outbox_v30 f
            JOIN documents d ON d.id=f.document_id
            WHERE f.status='PENDING_RECONCILIATION'
            ORDER BY f.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def reconcile_pending_furs_document(
    *, office_db: Path | str, document_id: int, fiscalizer: Any,
    operator_code: str, authenticated_username: str | None = None,
) -> dict[str, Any]:
    """Ponovi natančno isti shranjeni FURS zahtevek in ohrani revizijsko sled."""
    actor = _actor_username(authenticated_username)
    retry = getattr(fiscalizer, "retry_existing_payload", None)
    if not callable(retry):
        raise PosError("FURS povezava ne podpira varnega ponovnega pošiljanja istega zahtevka.")
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            """
            SELECT f.*,d.furs_zoi,d.fiscal_status
            FROM furs_outbox_v30 f JOIN documents d ON d.id=f.document_id
            WHERE f.document_id=?
            """,
            (int(document_id),),
        ).fetchone()
        if not row:
            raise PosError("FURS dogodek za izbrani račun ne obstaja.")
        if str(row["status"]) == "CONFIRMED":
            c.rollback()
            return {"status": "CONFIRMED", "document_id": int(document_id)}
        if str(row["status"]) != "PENDING_RECONCILIATION":
            raise PosError("Izbrani FURS dogodek ni v stanju za uskladitev.")
        try:
            payload = json.loads(str(row["payload_json"] or ""))
            payload_zoi = str(payload["InvoiceRequest"]["Invoice"]["ProtectedID"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PosError("Shranjeni FURS zahtevek je poškodovan.") from exc
        if not payload_zoi or payload_zoi != str(row["furs_zoi"] or ""):
            raise PosError("ZOI dokumenta se ne ujema s shranjenim FURS zahtevkom.")

        result = retry(payload)
        status = str(result.get("status") or "")
        if status not in {"CONFIRMED", "PENDING_RECONCILIATION"}:
            raise PosError("Ponovljeni FURS zahtevek je vrnil neveljavno stanje.")
        now = datetime.now().isoformat(timespec="seconds")
        c.execute(
            """
            UPDATE furs_outbox_v30 SET status=?,attempt_count=attempt_count+1,
                response_json=?,error_code=?,error_message=?,updated_at=?,
                confirmed_at=CASE WHEN ?='CONFIRMED' THEN ? ELSE confirmed_at END
            WHERE document_id=?
            """,
            (
                status,
                json.dumps(result.get("response") or {}, ensure_ascii=False, separators=(",", ":")),
                str(result.get("error_code") or ""),
                str(result.get("error_message") or "")[:1000],
                now, status, now, int(document_id),
            ),
        )
        c.execute(
            """
            UPDATE documents SET fiscal_status=?,furs_eor=?,furs_error_code=?,
                furs_error_message=?,updated_at=? WHERE id=?
            """,
            (
                status, str(result.get("eor") or ""),
                str(result.get("error_code") or ""),
                str(result.get("error_message") or "")[:1000],
                now, int(document_id),
            ),
        )
        c.execute(
            "INSERT INTO pos_audit_v30(document_id,event_type,detail,operator_code,actor_username,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                int(document_id), "FURS_RECONCILIATION_ATTEMPT",
                f"status={status}; message_id={row['message_id']}; attempt={int(row['attempt_count']) + 1}",
                operator_code, actor, now,
            ),
        )
        c.commit()
        return {
            "status": status, "document_id": int(document_id),
            "eor": str(result.get("eor") or ""),
            "error_message": str(result.get("error_message") or ""),
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _ten_working_day_deadline(value: datetime) -> str:
    """Varnostni rok; državni praznik lahko rok samo podaljša, nikoli skrajša."""
    cursor = value.date()
    remaining = 10
    while remaining:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor.isoformat()


def settle_unpaid_bank_invoice(
    *, office_db: Path | str, document_id: int, method: str,
    operator_code: str, fiscalization_required: bool,
    fiscalizer: Callable[[dict[str, Any]], dict[str, Any]] | None,
    authenticated_username: str | None = None,
    numbering_structure: str = "B", test_mode: bool = True,
) -> dict[str, Any]:
    """Evidentira poznejše gotovinsko/kartično plačilo istega TRR računa."""
    actor = _actor_username(authenticated_username)
    method = str(method or "").upper()
    if method not in {"BANK_TRANSFER", "CASH", "CARD"}:
        raise PosError(
            "Poznejše plačilo TRR računa je lahko TRR, gotovina ali kartica."
        )
    if method == "BANK_TRANSFER" and fiscalization_required:
        raise PosError("Prejem neposredno na TRR ne sme sprožiti davčnega potrjevanja.")
    if method in {"CASH", "CARD"} and fiscalization_required and fiscalizer is None:
        raise PosError("Naknadna davčna potrditev je obvezna, FURS povezava pa ni pripravljena.")
    ensure_pos_schema(office_db)
    paid_at = datetime.now()
    now = paid_at.isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        doc = c.execute(
            "SELECT * FROM documents WHERE id=?", (int(document_id),)
        ).fetchone()
        if not doc or str(doc["doc_type"]) != "IZDANI RAČUN" or str(doc["status"]) != "KNJIŽENO":
            raise PosError("Izbran ni veljaven knjižen izdani račun.")
        if str(doc["payment_status"]) == "PAID":
            c.rollback()
            return {
                "document_id": int(document_id), "status": "ALREADY_PAID",
                "fiscal_status": str(doc["fiscal_status"] or ""),
            }
        bank = c.execute(
            """
            SELECT id,amount FROM pos_payments_v30
            WHERE document_id=? AND method='BANK_TRANSFER' AND status='PENDING'
              AND reversal_of_id IS NULL
            ORDER BY id
            """,
            (int(document_id),),
        ).fetchall()
        total = dec(doc["invoice_total"])
        if len(bank) != 1 or dec(bank[0]["amount"]) != total:
            raise PosError(
                "Račun nima enega samega neplačanega TRR zneska, enakega celotnemu računu."
            )
        cash_session_id = None
        if method == "CASH":
            try:
                cash_session_id = require_open_cash_session(
                    c, premise_id=str(doc["fiscal_premise"] or ""),
                    device_id=str(doc["fiscal_device"] or ""),
                )
            except CashJournalError as exc:
                raise PosError("Pred prejemom gotovine odpri ustrezno blagajno. " + str(exc)) from exc

        fiscal_result = None
        if fiscalization_required:
            try:
                original_issue = datetime.fromisoformat(
                    str(doc["fiscal_issued_at"] or doc["date"] or "")
                )
            except ValueError as exc:
                raise PosError("Račun nima veljavnega prvotnega časa izdaje.") from exc
            fiscal_result = fiscalizer(
                {
                    "issue_datetime": original_issue,
                    "sequence": int(doc["fiscal_sequence"]),
                    "public_invoice_number": str(doc["fiscal_invoice_no"] or doc["doc_no"]),
                    "premise_id": str(doc["fiscal_premise"] or ""),
                    "device_id": str(doc["fiscal_device"] or ""),
                    "invoice_amount": total,
                    "payment_amount": total,
                    "operator_tax_number": str(doc["operator_tax_number_snapshot"] or ""),
                    "customer_vat_number": str(doc["partner_tax_snapshot"] or ""),
                    "numbering_structure": numbering_structure,
                    "subsequent_submit": True,
                }
            )
            fiscal_status = str(fiscal_result.get("status") or "")
            if fiscal_status not in {"CONFIRMED", "PENDING_RECONCILIATION"}:
                raise PosError("FURS povezava je vrnila neveljavno stanje.")
            from mikrofix_furs import build_fiscal_qr_payload
            qr_payload = build_fiscal_qr_payload(
                zoi=fiscal_result.get("zoi", ""),
                tax_number=str(fiscal_result.get("tax_number") or ""),
                issue_datetime=original_issue,
            )
            deadline = _ten_working_day_deadline(paid_at)
            c.execute(
                """
                UPDATE documents SET fiscal_status=?,furs_zoi=?,furs_eor=?,
                    furs_qr_payload=?,furs_message_id=?,furs_error_code=?,
                    furs_error_message=?,furs_submission_deadline=?,updated_at=?
                WHERE id=?
                """,
                (
                    fiscal_status, str(fiscal_result.get("zoi") or ""),
                    str(fiscal_result.get("eor") or ""), qr_payload,
                    str(fiscal_result.get("message_id") or ""),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    deadline, now, int(document_id),
                ),
            )
            c.execute(
                """
                INSERT INTO furs_outbox_v30(
                    document_id,message_id,status,payload_json,response_json,
                    error_code,error_message,created_at,updated_at,confirmed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(document_id), str(fiscal_result.get("message_id") or ""),
                    fiscal_status,
                    json.dumps(fiscal_result.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(fiscal_result.get("response") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    now, now, now if fiscal_status == "CONFIRMED" else None,
                ),
            )
        else:
            fiscal_status = (
                str(doc["fiscal_status"] or "NOT_REQUIRED")
                if method == "BANK_TRANSFER"
                else "TEST_DISABLED" if test_mode else "NOT_REQUIRED"
            )

        if method == "BANK_TRANSFER":
            c.execute(
                "UPDATE pos_payments_v30 SET status='PAID' WHERE id=?",
                (int(bank[0]["id"]),),
            )
        else:
            c.execute(
                "UPDATE pos_payments_v30 SET status='REPLACED_UNPAID',reversed_at=? "
                "WHERE id=?",
                (now, int(bank[0]["id"])),
            )
            c.execute(
                "INSERT INTO pos_payments_v30(document_id,method,amount,status,created_at) "
                "VALUES(?,?,?,'PAID',?)",
                (int(document_id), method, float(total), now),
            )
        c.execute(
            """
            UPDATE documents SET payment_method=?,payment_status='PAID',
                payment_received_at=?,payment_received_method=?,cash_session_id=COALESCE(?,cash_session_id),
                updated_at=? WHERE id=?
            """,
            (method, now, method, cash_session_id, now, int(document_id)),
        )
        if cash_session_id:
            record_cash_entry(
                c, session_id=cash_session_id, entry_type="CASH_SALE",
                amount=total, reference_type="POS_LATE_PAYMENT",
                reference_id=int(document_id),
                reference_no=str(doc["fiscal_invoice_no"] or doc["doc_no"]),
                description="Poznejše gotovinsko plačilo prvotnega TRR računa",
                operator_code=operator_code,
            )
        c.execute(
            "INSERT INTO pos_audit_v30(document_id,event_type,detail,operator_code,actor_username,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                int(document_id), "LATE_BANK_INVOICE_PAYMENT",
                f"method={method}; amount={total}; furs={fiscal_status}",
                operator_code, actor, now,
            ),
        )
        c.commit()
        return {
            "document_id": int(document_id), "status": "PAID",
            "fiscal_status": fiscal_status,
            "eor": str((fiscal_result or {}).get("eor") or ""),
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def eligible_storned_invoices(office_db: Path | str) -> list[dict]:
    """Vrne stornirane račune, ki še nimajo svojega nadomestnega računa."""
    ensure_pos_schema(office_db)
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            """
            SELECT d.id,
                   COALESCE(d.fiscal_invoice_no,d.doc_no) AS invoice_no,
                   d.date,
                   COALESCE(d.partner_name_snapshot,'Končni kupec') AS customer,
                   COALESCE(d.invoice_total,0) AS invoice_total,
                   (
                       SELECT s.doc_no FROM documents s
                       WHERE s.doc_type='STORNO RAČUNA'
                         AND s.linked_invoice_id=d.id
                         AND s.status='KNJIŽENO'
                       ORDER BY s.id DESC LIMIT 1
                   ) AS storno_no
            FROM documents d
            WHERE d.doc_type='IZDANI RAČUN'
              AND d.status='STORNIRANO'
              AND EXISTS(
                  SELECT 1 FROM documents s
                  WHERE s.doc_type='STORNO RAČUNA'
                    AND s.linked_invoice_id=d.id
                    AND s.status='KNJIŽENO'
              )
              AND NOT EXISTS(
                  SELECT 1 FROM documents r
                  WHERE r.doc_type='IZDANI RAČUN'
                    AND r.replacement_of_invoice_id=d.id
              )
            ORDER BY d.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def storno_invoice(
    *, office_db: Path | str, inventory_db: Path | str, service_db: Path | str,
    document_id: int, reason: str, operator_code: str,
    authenticated_username: str | None = None,
    action_permissions: Iterable[str] | None = None,
    fiscalization_required: bool = False,
    fiscalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    numbering_structure: str = "B",
    test_mode: bool = True,
) -> tuple[int, str]:
    """V celoti stornira račun, povrne njegove LOT-e in ohrani obe listini."""
    actor = _actor_username(authenticated_username)
    _require_action_permission(action_permissions, STORNO_DOCUMENT_PERMISSION)
    ensure_pos_schema(office_db)
    reason = str(reason or "").strip()
    if len(reason) < 3:
        raise PosError("Za storno vpiši jasen razlog.")
    now = datetime.now().isoformat(timespec="seconds")
    c = sqlite3.connect(str(office_db))
    c.row_factory = sqlite3.Row
    try:
        c.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        service_attached = Path(service_db).is_file()
        if service_attached:
            c.execute("ATTACH DATABASE ? AS servicedb", (str(service_db),))
        c.execute("BEGIN IMMEDIATE")
        ensure_lot_schema(c, "inv")
        doc = c.execute(
            "SELECT * FROM documents WHERE id=?", (int(document_id),)
        ).fetchone()
        if not doc or str(doc["doc_type"]) != "IZDANI RAČUN":
            raise PosError("Za storno ni izbran veljaven izdani račun.")
        if str(doc["status"]) == "STORNIRANO":
            linked = c.execute(
                "SELECT id,doc_no FROM documents WHERE doc_type='STORNO RAČUNA' "
                "AND linked_invoice_id=? ORDER BY id DESC LIMIT 1",
                (int(document_id),),
            ).fetchone()
            if linked:
                c.rollback()
                return int(linked[0]), str(linked[1])
            raise PosError("Račun je že storniran.")
        if str(doc["status"]) != "KNJIŽENO":
            raise PosError("Stornirati je mogoče samo knjižen račun.")

        linked_service_order_id = None
        if doc["linked_service_id"]:
            if not service_attached:
                raise PosError(
                    "Servisna baza ni dosegljiva; povezanega računa ni dovoljeno stornirati."
                )
            if not _table_exists(c, "servicedb", "service"):
                raise PosError("Servisna baza nima tabele povezanih servisov.")
            service_columns = {
                row[1] for row in c.execute("PRAGMA servicedb.table_info(service)")
            }
            if not {"id", "invoice_no", "office_invoice_id"}.issubset(service_columns):
                raise PosError("Servisna baza nima vseh polj za varen storno.")
            select_order = ",service_order_id" if "service_order_id" in service_columns else ",NULL"
            linked_service = c.execute(
                "SELECT invoice_no,office_invoice_id" + select_order
                + " FROM servicedb.service WHERE id=?",
                (int(doc["linked_service_id"]),),
            ).fetchone()
            if (
                not linked_service
                or int(linked_service[1] or 0) != int(document_id)
                or str(linked_service[0] or "").strip()
                != str(doc["fiscal_invoice_no"] or doc["doc_no"]).strip()
            ):
                raise PosError(
                    "Povezava računa s servisom ni skladna; storno je ustavljen brez sprememb."
                )
            linked_service_order_id = (
                int(linked_service[2]) if linked_service[2] is not None else None
            )
            if linked_service_order_id is not None:
                if not _table_exists(c, "servicedb", "service_orders"):
                    raise PosError("Servisna baza nima povezanega servisnega naročila.")
                linked_order = c.execute(
                    "SELECT status,service_id FROM servicedb.service_orders WHERE id=?",
                    (linked_service_order_id,),
                ).fetchone()
                if (
                    not linked_order
                    or str(linked_order[0]) != "COMPLETED"
                    or int(linked_order[1] or 0) != int(doc["linked_service_id"])
                ):
                    raise PosError(
                        "Povezano servisno naročilo ni skladno; storno je ustavljen brez sprememb."
                    )

        original_cash = dec(c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM pos_payments_v30 "
            "WHERE document_id=? AND method='CASH' AND reversal_of_id IS NULL",
            (int(document_id),),
        ).fetchone()[0])
        original_fiscal_payment = dec(c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM pos_payments_v30 "
            "WHERE document_id=? AND method IN ('CASH','CARD') "
            "AND reversal_of_id IS NULL",
            (int(document_id),),
        ).fetchone()[0])
        needs_fiscalization = bool(fiscalization_required) and original_fiscal_payment > 0
        if needs_fiscalization and fiscalizer is None:
            raise PosError(
                "Produkcijska davčna potrditev storna je obvezna, FURS povezava pa ni pripravljena."
            )
        storno_cash_session_id = None
        if original_cash > 0:
            try:
                storno_cash_session_id = require_open_cash_session(
                    c,premise_id=str(doc["fiscal_premise"] or ""),
                    device_id=str(doc["fiscal_device"] or ""),
                )
            except CashJournalError as exc:
                raise PosError(
                    "Pred gotovinskim stornom odpri blagajno istega prostora in naprave. "
                    + str(exc)
                ) from exc

        storno_no = _next_storno_no(c)
        storno_issued_at = datetime.fromisoformat(now)
        storno_year, storno_sequence, storno_fiscal_no = _next_fiscal_number(
            c,
            premise_id=str(doc["fiscal_premise"] or ""),
            device_id=str(doc["fiscal_device"] or ""),
            issued_at=storno_issued_at,
        )
        initial_fiscal_status = (
            "PENDING_SEND" if needs_fiscalization
            else ("TEST_DISABLED" if test_mode else "NOT_REQUIRED")
        )
        cur = c.execute(
            """
            INSERT INTO documents(
                doc_no,doc_type,partner_name_snapshot,partner_address_snapshot,
                partner_tax_snapshot,partner_pcd_snapshot,external_no,date,currency,
                delivery_cost,stock_effect,status,linked_service_id,notes,created_at,
                updated_at,booked_at,payment_method,payment_status,invoice_total,
                customer_phone,customer_email,fiscal_status,print_status,
                fiscal_premise,fiscal_device,operator_code_snapshot,
                operator_tax_number_snapshot,fiscal_issued_at,linked_invoice_id,
                cash_session_id,fiscal_year,fiscal_sequence,fiscal_invoice_no
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                storno_no, "STORNO RAČUNA", doc["partner_name_snapshot"],
                doc["partner_address_snapshot"], doc["partner_tax_snapshot"],
                doc["partner_pcd_snapshot"],
                str(doc["fiscal_invoice_no"] or doc["doc_no"]), date.today().isoformat(),
                doc["currency"], 0, 0, "KNJIŽENO", doc["linked_service_id"],
                f"Storno računa {doc['fiscal_invoice_no'] or doc['doc_no']}: {reason}",
                now, now, now, doc["payment_method"], "REVERSED",
                -float(dec(doc["invoice_total"])), doc["customer_phone"],
                doc["customer_email"], initial_fiscal_status, "AWAITING_PRINT",
                doc["fiscal_premise"], doc["fiscal_device"], operator_code,
                doc["operator_tax_number_snapshot"], now, int(document_id),
                storno_cash_session_id, storno_year, storno_sequence,
                storno_fiscal_no,
            ),
        )
        storno_id = int(cur.lastrowid)

        items = c.execute(
            "SELECT * FROM document_items WHERE document_id=? ORDER BY id",
            (int(document_id),),
        ).fetchall()
        for item in items:
            c.execute(
                """
                INSERT INTO document_items(
                    document_id,article_id,barcode,article_name,internal_code,qty,
                    unit_price,discount_pct,vat_pct,delivery_share,actual_unit_cost,
                    margin_pct,calculated_price,recommended_price,sell_price,line_net,
                    line_vat,line_total,line_kind,service_tier,requested_discount_pct,
                    service_billable_minutes,service_tariff_version,
                    service_first_rate,service_extra_half_hour_rate,
                    stock_already_consumed,origin_kind,origin_id,source_locked
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    storno_id,item["article_id"],item["barcode"],item["article_name"],
                    item["internal_code"],-float(dec(item["qty"])),
                    item["unit_price"],item["discount_pct"],item["vat_pct"],0,
                    item["actual_unit_cost"],item["margin_pct"],item["calculated_price"],
                    item["recommended_price"],item["sell_price"],
                    -float(dec(item["line_net"])), -float(dec(item["line_vat"])),
                    -float(dec(item["line_total"])),item["line_kind"],
                    item["service_tier"],item["requested_discount_pct"],
                    item["service_billable_minutes"],item["service_tariff_version"],
                    item["service_first_rate"],item["service_extra_half_hour_rate"],
                    item["stock_already_consumed"],item["origin_kind"],
                    item["origin_id"],item["source_locked"],
                ),
            )
            if not item["article_id"]:
                continue
            allocations = c.execute(
                """
                SELECT COALESCE(SUM(quantity),0) FROM inv.inventory_lot_allocations_v30
                WHERE allocation_type='POS_INVOICE' AND source_id=? AND source_line_id=?
                  AND reversed_at IS NULL
                """,
                (int(document_id), int(item["id"])),
            ).fetchone()
            restored_qty = dec(allocations[0] if allocations else 0)
            if restored_qty <= 0:
                continue
            service_part_reopened = (
                doc["linked_service_id"]
                and str(item["origin_kind"] or "") == "SERVICE_PART"
                and item["origin_id"] is not None
            )
            if service_part_reopened:
                # Storno ponovno odpre servis: isti fizični LOT zato ostane
                # rezerviran za isti servisni del, ne postane prosta zaloga.
                c.execute(
                    """
                    UPDATE inv.inventory_lot_allocations_v30
                    SET allocation_type='SERVICE_RESERVATION',source_id=?,source_line_id=?
                    WHERE allocation_type='POS_INVOICE' AND source_id=?
                      AND source_line_id=? AND reversed_at IS NULL
                    """,
                    (
                        int(doc["linked_service_id"]), int(item["origin_id"]),
                        int(document_id), int(item["id"]),
                    ),
                )
            else:
                restore_allocations(
                    c, schema="inv", allocation_type="POS_INVOICE",
                    source_id=int(document_id), source_line_id=int(item["id"]),
                    reversed_at=now,
                )
            stock = c.execute(
                "SELECT stock_qty,location,COALESCE(reserved_qty,0) "
                "FROM inv.inventory_items WHERE id=?",
                (int(item["article_id"]),),
            ).fetchone()
            if not stock:
                raise PosError("Artikel storniranega računa ni več v zalogi.")
            balance = dec(stock[0]) + restored_qty
            if service_part_reopened:
                c.execute(
                    "UPDATE inv.inventory_items SET stock_qty=?,reserved_qty=?,updated_at=? "
                    "WHERE id=?",
                    (
                        float(balance), float(dec(stock[2]) + restored_qty), now,
                        int(item["article_id"]),
                    ),
                )
            else:
                c.execute(
                    "UPDATE inv.inventory_items SET stock_qty=?,updated_at=? WHERE id=?",
                    (float(balance),now,int(item["article_id"])),
                )
            c.execute(
                """
                INSERT INTO inv.stock_movements(
                    movement_no,created_at,article_id,quantity_delta,balance_after,
                    movement_type,source_type,source_id,source_line_id,lot_serial,
                    location_from,location_to,reason,operator_code,reversal_of_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    f"PREM-STR-{storno_id:06d}-{int(item['id'])}",now,
                    int(item["article_id"]),float(restored_qty),float(balance),
                    "STORNO_SALE_IN","POS_STORNO",storno_id,int(item["id"]),"",
                    "KUPEC",stock[1] or "",f"Storno računa {doc['fiscal_invoice_no'] or doc['doc_no']}",
                    operator_code,
                ),
            )

        payments = c.execute(
            "SELECT id,method,amount FROM pos_payments_v30 WHERE document_id=? "
            "AND reversal_of_id IS NULL",
            (int(document_id),),
        ).fetchall()
        for payment in payments:
            c.execute(
                "UPDATE pos_payments_v30 SET status='REVERSED',reversed_at=? WHERE id=?",
                (now,int(payment["id"])),
            )
            c.execute(
                "INSERT INTO pos_payments_v30(document_id,method,amount,status,created_at,reversed_at,reversal_of_id) "
                "VALUES(?,?,?,'REVERSED',?,?,?)",
                (
                    storno_id,payment["method"],-float(dec(payment["amount"])),
                    now,now,int(payment["id"]),
                ),
            )
        if storno_cash_session_id and original_cash > 0:
            record_cash_entry(
                c,session_id=storno_cash_session_id,entry_type="CASH_REFUND",
                amount=-original_cash,reference_type="POS_STORNO",
                reference_id=storno_id,reference_no=storno_no,
                description=f"Vračilo gotovine – storno računa {doc['fiscal_invoice_no'] or doc['doc_no']}",
                operator_code=operator_code,
            )

        c.execute(
            "UPDATE inv.used_devices SET status='READY_FOR_SALE',buyer_name=NULL," 
            "sale_document_id=NULL,warranty_until=NULL,updated_at=? WHERE sale_document_id=?",
            (now,int(document_id)),
        )
        if doc["linked_service_id"]:
            service_id = int(doc["linked_service_id"])
            custody_reset = (
                ",ready_for_pickup_at=NULL,picked_up_at=NULL,final_notice_recorded_at=NULL"
                if "ready_for_pickup_at" in service_columns else ""
            )
            service_update = c.execute(
                "UPDATE servicedb.service SET invoice_no=NULL,office_invoice_id=NULL"
                + custody_reset
                + " WHERE id=? AND office_invoice_id=? "
                  "AND trim(COALESCE(invoice_no,''))<>''",
                (service_id, int(document_id)),
            )
            if service_update.rowcount != 1:
                raise PosError("Servisa ni bilo mogoče enolično ponovno odpreti.")
            if linked_service_order_id is not None:
                order_update = c.execute(
                    """
                    UPDATE servicedb.service_orders SET status='OPEN',completed_at=NULL
                    WHERE id=? AND service_id=? AND status='COMPLETED'
                    """,
                    (linked_service_order_id, service_id),
                )
                if order_update.rowcount != 1:
                    raise PosError(
                        "Servisnega naročila ni bilo mogoče enolično ponovno odpreti."
                    )
        fiscal_result = None
        if needs_fiscalization:
            original_issue_text = str(doc["fiscal_issued_at"] or doc["date"] or "")
            try:
                original_issue = datetime.fromisoformat(original_issue_text)
            except ValueError as exc:
                raise PosError(
                    "Izvirni račun nima veljavnega časa izdaje za FURS referenco."
                ) from exc
            fiscal_result = fiscalizer(
                {
                    "issue_datetime": storno_issued_at,
                    "sequence": storno_sequence,
                    "public_invoice_number": storno_fiscal_no,
                    "premise_id": str(doc["fiscal_premise"] or ""),
                    "device_id": str(doc["fiscal_device"] or ""),
                    "invoice_amount": -dec(doc["invoice_total"]),
                    "payment_amount": -dec(doc["invoice_total"]),
                    "operator_tax_number": str(doc["operator_tax_number_snapshot"] or ""),
                    "customer_vat_number": str(doc["partner_tax_snapshot"] or ""),
                    "numbering_structure": numbering_structure,
                    "reference_invoice": {
                        "ReferenceInvoiceIdentifier": {
                            "BusinessPremiseID": str(doc["fiscal_premise"] or ""),
                            "ElectronicDeviceID": str(doc["fiscal_device"] or ""),
                            "InvoiceNumber": str(doc["fiscal_sequence"]),
                        },
                        "ReferenceInvoiceIssueDateTime": original_issue.strftime(
                            "%Y-%m-%dT%H:%M:%S"
                        ),
                    },
                }
            )
            fiscal_status = str(fiscal_result.get("status") or "")
            if fiscal_status not in {"CONFIRMED", "PENDING_RECONCILIATION"}:
                raise PosError("FURS povezava je pri stornu vrnila neveljavno stanje.")
            from mikrofix_furs import build_fiscal_qr_payload
            storno_qr = build_fiscal_qr_payload(
                zoi=fiscal_result.get("zoi", ""),
                tax_number=str(fiscal_result.get("tax_number") or ""),
                issue_datetime=storno_issued_at,
            )
            c.execute(
                """
                UPDATE documents SET fiscal_status=?,furs_zoi=?,furs_eor=?,
                    furs_qr_payload=?,furs_message_id=?,furs_error_code=?,
                    furs_error_message=?,updated_at=? WHERE id=?
                """,
                (
                    fiscal_status, str(fiscal_result.get("zoi") or ""),
                    str(fiscal_result.get("eor") or ""), storno_qr,
                    str(fiscal_result.get("message_id") or ""),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    now, storno_id,
                ),
            )
            c.execute(
                """
                INSERT INTO furs_outbox_v30(
                    document_id,message_id,status,payload_json,response_json,
                    error_code,error_message,created_at,updated_at,confirmed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    storno_id, str(fiscal_result.get("message_id") or ""), fiscal_status,
                    json.dumps(fiscal_result.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(fiscal_result.get("response") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(fiscal_result.get("error_code") or ""),
                    str(fiscal_result.get("error_message") or "")[:1000],
                    now, now, now if fiscal_status == "CONFIRMED" else None,
                ),
            )
        c.execute(
            "UPDATE documents SET status='STORNIRANO',storno_reason=?,storno_at=?,"
            "storno_by=?,updated_at=? WHERE id=?",
            (reason,now,actor,now,int(document_id)),
        )
        c.execute(
            "INSERT INTO pos_audit_v30(document_id,event_type,detail,operator_code,actor_username,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                int(document_id),"INVOICE_STORNIRANO",
                f"storno_document={storno_id}; storno_no={storno_no}; "
                f"fiscal_no={storno_fiscal_no}; "
                f"furs={(fiscal_result or {}).get('status','NOT_REQUIRED')}; reason={reason}",
                operator_code,actor,now,
            ),
        )
        c.commit()
        return storno_id, storno_no
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _register_pos_pdf_fonts() -> tuple[str, str]:
    """Registrira Unicode pisavo, da č/š/ž ostanejo vidni tudi na termičnem izpisu."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (
            Path(r"C:\Windows\Fonts\segoeui.ttf"),
            Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    ]
    for regular_path, bold_path in candidates:
        if not regular_path.is_file():
            continue
        try:
            regular_name = "MikroFIXPos"
            bold_name = "MikroFIXPosBold"
            registered = set(pdfmetrics.getRegisteredFontNames())
            if regular_name not in registered:
                pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
            if bold_name not in registered:
                pdfmetrics.registerFont(
                    TTFont(bold_name, str(bold_path if bold_path.is_file() else regular_path))
                )
            pdfmetrics.registerFontFamily(
                regular_name,
                normal=regular_name,
                bold=bold_name,
                italic=regular_name,
                boldItalic=bold_name,
            )
            return regular_name, bold_name
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold"


def generate_central_invoice_pdfs(
    *, invoice: dict[str, Any], output_dir: Path | str, company: dict[str, Any],
    paper_width_mm: int = 80, test_mode: bool = True,
) -> tuple[Path, Path]:
    """Render an authoritative central invoice without a local accounting copy."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    snapshot = dict(invoice.get("snapshot") or {})
    invoice_no = str(invoice.get("invoice_number") or "").strip()
    invoice_id = str(invoice.get("invoice_id") or "").strip()
    lines = list(snapshot.get("lines") or [])
    total = dec(invoice.get("total_amount") or snapshot.get("total_amount"))
    if not invoice_no or not invoice_id or not lines:
        raise PosError("Centralni račun nima popolnega nespremenljivega posnetka.")
    line_total = sum((dec(line.get("line_total")) for line in lines), Decimal("0"))
    if line_total != total:
        raise PosError("Vrstice centralnega računa se ne ujemajo s skupnim zneskom.")

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    safe_no = re.sub(r"[^0-9A-Za-z_.-]", "_", invoice_no)
    a4_path = output / f"{safe_no}.pdf"
    receipt_path = output / f"{safe_no}_{int(paper_width_mm)}mm.pdf"
    regular_font, bold_font = _register_pos_pdf_fonts()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CentralInvoiceBody", parent=styles["BodyText"], fontName=regular_font,
        fontSize=9, leading=12,
    )
    small = ParagraphStyle(
        "CentralInvoiceSmall", parent=body, fontSize=7.5, leading=9,
    )
    title = ParagraphStyle(
        "CentralInvoiceTitle", parent=styles["Title"], fontName=bold_font,
        fontSize=18, leading=21,
    )
    customer = dict(snapshot.get("billing_customer") or {})
    payments = list(snapshot.get("payments") or [])
    payment_text = ", ".join(
        f"{payment_method_label(payment.get('method'))}: "
        f"{dec(payment.get('amount')):.2f} EUR"
        for payment in payments
    ) or "–"
    issued_at = str(snapshot.get("issued_at") or invoice.get("issued_at") or "-").replace(
        "T", " ", 1
    )
    heading = "TEST – RAČUN" if test_mode else "RAČUN"
    rows = [["Postavka", "Kol.", "Cena", "Rabat", "Znesek"]]
    for line in lines:
        description = str(line.get("description") or "Postavka")
        if line.get("line_kind") == "SERVICE" and line.get("billable_minutes"):
            description += f" ({line['billable_minutes']} min)"
        rows.append([
            Paragraph(escape(description), small),
            str(line.get("quantity") or "1"),
            f"{dec(line.get('base_unit_price')):.2f}",
            f"{dec(line.get('applied_discount_percent')):.2f} %",
            f"{dec(line.get('line_total')):.2f}",
        ])
    info = (
        f"<b>{escape(str(company.get('legal_name') or 'MikroFix'))}</b><br/>"
        f"{escape(str(company.get('street') or ''))}, "
        f"{escape(str(company.get('city') or ''))}<br/>"
        f"Davčna: {escape(str(company.get('tax_number') or '-'))}<br/><br/>"
        f"Številka: <b>{escape(invoice_no)}</b><br/>"
        f"Izdano: {escape(issued_at)}<br/>"
        f"Prostor/naprava: {escape(str(snapshot.get('premise_id') or '-'))}/"
        f"{escape(str(snapshot.get('electronic_device_id') or '-'))}<br/>"
        f"Kupec: {escape(str(customer.get('name') or 'Končni kupec'))}<br/>"
        f"Naslov: {escape(str(customer.get('address') or '-'))}"
    )
    table = Table(rows, colWidths=[90*mm, 18*mm, 24*mm, 24*mm, 28*mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF1")),
        ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#CDD4DA")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story = [Paragraph(heading, title), Paragraph(info, body), Spacer(1, 5*mm), table]
    story.extend([
        Spacer(1, 4*mm),
        Paragraph(f"<b>SKUPAJ: {total:.2f} EUR</b>", body),
        Paragraph(f"Plačilo: {escape(payment_text)}", body),
        Paragraph(escape(str(snapshot.get("tax_note") or "")), small),
    ])
    if invoice.get("furs_eor"):
        story.append(Paragraph(f"EOR: {escape(str(invoice['furs_eor']))}", small))
    SimpleDocTemplate(
        str(a4_path), pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    ).build(story)

    width = int(paper_width_mm) * mm
    receipt_rows = [["Postavka", "Kol.", "Znesek"]] + [
        [Paragraph(escape(str(line.get("description") or "Postavka")), small),
         str(line.get("quantity") or "1"), f"{dec(line.get('line_total')):.2f}"]
        for line in lines
    ]
    receipt_table = Table(receipt_rows, colWidths=[48*mm, 10*mm, 14*mm])
    receipt_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("LINEBELOW", (0, 0), (-1, 0), .4, colors.black),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]))
    receipt_story = [
        Paragraph(heading, ParagraphStyle(
            "CentralReceiptTitle", parent=title, fontSize=13, leading=15,
            alignment=1,
        )),
        Paragraph(f"<b>{escape(invoice_no)}</b><br/>{escape(issued_at)}", small),
        Spacer(1, 2*mm), receipt_table, Spacer(1, 2*mm),
        Paragraph(f"<b>SKUPAJ: {total:.2f} EUR</b>", body),
        Paragraph(f"Plačilo: {escape(payment_text)}", small),
        Paragraph(escape(str(snapshot.get("tax_note") or "")), small),
    ]
    height = max(150, 92 + 14 * len(lines)) * mm
    SimpleDocTemplate(
        str(receipt_path), pagesize=(width, height), leftMargin=4*mm,
        rightMargin=4*mm, topMargin=4*mm, bottomMargin=4*mm,
    ).build(receipt_story)
    return a4_path, receipt_path


def generate_pos_pdfs(
    *, office_db: Path | str, document_id: int, output_dir: Path | str,
    company: dict, tax_note: str, premise: str, device: str, paper_width_mm: int = 80,
    copy_no: int = 0,
    test_mode: bool | None = None,
) -> tuple[Path, Path]:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    doc, lines, payments = invoice_snapshot(office_db, document_id)
    regular_font, bold_font = _register_pos_pdf_fonts()
    public_invoice_no = str(doc.get("fiscal_invoice_no") or doc["doc_no"])
    is_storno = str(doc.get("doc_type") or "") == "STORNO RAČUNA"
    document_title = "STORNO RAČUNA" if is_storno else "RAČUN"
    premise = str(doc.get("fiscal_premise") or premise or "-")
    device = str(doc.get("fiscal_device") or device or "-")
    operator_code = str(doc.get("operator_code_snapshot") or "-")
    issued_at = str(doc.get("fiscal_issued_at") or doc.get("date") or "-").replace("T", " ", 1)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    suffix = f"_KOPIJA_{copy_no}" if copy_no else ""
    a4_path = output / f"{public_invoice_no}{suffix}.pdf"
    receipt_path = output / f"{public_invoice_no}_{paper_width_mm}mm{suffix}.pdf"
    styles = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontName=regular_font, fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=body, fontSize=7.5, leading=9)
    title = ParagraphStyle(
        "InvoiceTitle", parent=styles["Title"], fontName=bold_font,
        fontSize=18, leading=22,
    )
    heading = ParagraphStyle(
        "InvoiceHeading", parent=styles["Heading2"], fontName=bold_font,
        fontSize=14, leading=17,
    )
    copy_label = f"KOPIJA {copy_no}" if copy_no else "ORIGINAL"
    furs_zoi = str(doc.get("furs_zoi") or "")
    furs_eor = str(doc.get("furs_eor") or "")
    fiscal_qr = str(doc.get("furs_qr_payload") or "")
    fiscal_status = str(doc.get("fiscal_status") or "NOT_REQUIRED")
    if test_mode is None:
        test_mode = fiscal_status == "TEST_DISABLED"
    qr_paths: list[Path] = []
    fiscal_qr_path: Path | None = None
    upn_qr_path: Path | None = None

    def make_qr_png(payload: str, label: str, *, upn: bool = False) -> Path:
        import segno
        path = output / f".{public_invoice_no}_{label}.png"
        if upn:
            qr = segno.make_qr(
                payload, version=15, error="m", mode="byte",
                encoding="iso-8859-2", eci=True, boost_error=False,
            )
        else:
            qr = segno.make_qr(payload, error="m", boost_error=False)
        qr.save(str(path), kind="png", scale=8, border=4, dark="black", light="white")
        qr_paths.append(path)
        return path

    story = [
        Paragraph(f"<b>{escape(str(company.get('legal_name') or 'MikroFix'))}</b>", body),
        Paragraph(escape(str(company.get('address') or 'Paradiž 73, 2282 Cirkulane')), body),
        Paragraph(f"Davčna: {escape(str(company.get('tax_number') or '-'))} &nbsp;&nbsp; Matična: {escape(str(company.get('registration_number') or '-'))}", small),
        Spacer(1, 7 * mm),
        Paragraph(f"<b>{document_title} {escape(public_invoice_no)}</b> &nbsp; {copy_label}", title),
        Paragraph(
            f"Čas izdaje: {escape(issued_at)}<br/>"
            f"Poslovni prostor / naprava: {escape(premise)}/{escape(device)}<br/>"
            f"Operater: {escape(operator_code)}<br/>"
            f"Kupec: {escape(str(doc['partner_name_snapshot'] or 'Končni kupec'))}<br/>"
            f"{escape(str(doc['partner_address_snapshot'] or ''))}"
            + (
                f"<br/>Davčna kupca: {escape(str(doc['partner_tax_snapshot']))}"
                if str(doc.get("partner_tax_snapshot") or "").strip() else ""
            ),
            body,
        ),
        Spacer(1, 5 * mm),
    ]
    if doc.get("replacement_of_invoice_id"):
        story.extend([
            Paragraph(
                "Nadomestni račun za stornirani račun "
                f"<b>{escape(str(doc.get('replacement_original_no') or '-'))}</b> "
                f"(storno {escape(str(doc.get('replacement_storno_no') or '-'))}).",
                body,
            ),
            Spacer(1, 3 * mm),
        ])
    rows = [["Postavka", "Kol.", "Cena", "Rabat", "Znesek"]]
    for line in lines:
        rows.append([
            Paragraph(escape(service_line_public_name(line)), small),
            service_line_public_quantity(line), f"{float(line['unit_price']):.2f}",
            f"{float(line['discount_pct']):.2f} %", f"{float(line['line_total']):.2f}",
        ])
    table = Table(rows, colWidths=[88*mm, 18*mm, 25*mm, 23*mm, 27*mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), regular_font),
        ("FONTNAME", (0,0), (-1,0), bold_font),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EDF1")),
        ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#CDD4DA")),
        ("VALIGN", (0,0), (-1,-1), "TOP"), ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("FONTSIZE", (0,0), (-1,-1), 8), ("PADDING", (0,0), (-1,-1), 4),
    ]))
    story += [table, Spacer(1, 5*mm)]
    pay_text = ", ".join(
        f"{payment_method_label(p['method'])}: {float(p['amount']):.2f} EUR"
        for p in payments
    )
    story += [
        Paragraph(f"<b>SKUPAJ: {float(doc['invoice_total']):.2f} EUR</b>", heading),
        Paragraph(
            f"Plačilo: {escape(pay_text)} &nbsp; "
            f"Status: {escape(payment_status_label(doc['payment_status']))}",
            body,
        ),
        Paragraph(escape(tax_note), small),
        Spacer(1, 4*mm),
    ]
    if furs_zoi:
        story.extend([
            Paragraph(f"ZOI: {escape(furs_zoi)}", small),
            Paragraph(f"EOR: {escape(furs_eor or 'BO PRIDOBLJEN NAKNADNO')}", small),
        ])
        if fiscal_qr:
            fiscal_qr_path = make_qr_png(fiscal_qr, "FURS")
            story.extend([Image(str(fiscal_qr_path), width=22*mm, height=22*mm), Spacer(1, 2*mm)])
    bank_amount = sum(
        (dec(p["amount"]) for p in payments if str(p["method"]) == "BANK_TRANSFER"),
        Decimal("0.00"),
    )
    if bank_amount > 0:
        from mikrofix_upnqr import build_upnqr_payload
        reference = str(doc.get("payment_reference") or "")
        upn_payload = build_upnqr_payload(
            amount=bank_amount, recipient_iban=str(company.get("iban") or ""),
            recipient_reference=reference,
            recipient_name=str(company.get("legal_name") or "MikroFix"),
            recipient_street=str(company.get("street") or "Paradiž 73"),
            recipient_city=str(company.get("city") or "2282 Cirkulane"),
            payer_name=str(doc.get("partner_name_snapshot") or ""),
            payer_street=str(doc.get("partner_address_snapshot") or ""),
            purpose=f"Plačilo računa {public_invoice_no}",
        ).decode("iso-8859-2")
        upn_qr_path = make_qr_png(upn_payload, "UPN", upn=True)
        story.extend([
            Paragraph(f"<b>UPN QR – plačilo {float(bank_amount):.2f} EUR</b>", body),
            Image(str(upn_qr_path), width=42*mm, height=42*mm),
            Paragraph(
                "Odpri mobilno banko: Plačila → Slikaj UPN QR. "
                "Navadna kamera kode ne zna uporabiti za plačilo. "
                "UPN QR računa samodejno ne označi kot plačanega.", small,
            ),
        ])
    if test_mode:
        story.append(Paragraph(
            "TESTNI DOKUMENT – FURS PRODUKCIJA JE IZKLOPLJENA",
            ParagraphStyle("Warning", parent=body, textColor=colors.red),
        ))
    elif fiscal_status == "PENDING_RECONCILIATION":
        story.append(Paragraph(
            "RAČUN IMA ZOI. EOR BO PRIDOBLJEN NAKNADNO OB OBNOVITVI POVEZAVE.",
            ParagraphStyle("WarningPending", parent=body, textColor=colors.red),
        ))
    SimpleDocTemplate(str(a4_path), pagesize=A4, leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm).build(story)

    width = paper_width_mm * mm
    receipt_body = ParagraphStyle(
        "ReceiptBodyV30", parent=body, fontSize=8.8, leading=10.8,
    )
    receipt_small = ParagraphStyle(
        "ReceiptSmallV30", parent=receipt_body, fontSize=7.8, leading=9.5,
    )
    receipt_brand = str(company.get("short_name") or "MikroFix").strip()
    receipt_legal = str(company.get("legal_name") or receipt_brand).strip()
    rstory = [
        Paragraph(f"<b>{escape(receipt_brand)}</b>", ParagraphStyle("RC", parent=receipt_body, alignment=1, fontName=bold_font, fontSize=15, leading=17)),
        Paragraph(escape(receipt_legal), ParagraphStyle("RLegal", parent=receipt_small, alignment=1, fontSize=7.4, leading=8.8)),
        Paragraph(escape(str(company.get('address') or 'Paradiž 73, 2282 Cirkulane')), ParagraphStyle("RC2", parent=receipt_small, alignment=1)),
        Paragraph(
            f"Davčna: {escape(str(company.get('tax_number') or '-'))}",
            ParagraphStyle("RTax", parent=receipt_small, alignment=1),
        ),
        Spacer(1, 1.5*mm),
        HRFlowable(width="100%", thickness=1, color=colors.black, spaceBefore=0, spaceAfter=2*mm),
        Paragraph(
            f"<b>{'TEST – ' if test_mode else ''}{'STORNO RAČUNA' if is_storno else ('DAVČNO POTRJEN RAČUN' if furs_eor else 'RAČUN')}</b>",
            ParagraphStyle("RW", parent=receipt_body, alignment=1, fontName=bold_font, fontSize=10.5, leading=12.5, textColor=colors.red if test_mode else colors.black),
        ),
        Paragraph(
            f"<b>{escape(public_invoice_no)}</b><br/>{copy_label}<br/>"
            f"{escape(issued_at)}<br/>{escape(premise)}/{escape(device)} • {escape(operator_code)}",
            ParagraphStyle("RH", parent=receipt_small, alignment=1),
        ),
        Paragraph(
            f"<b>Kupec: {escape(str(doc.get('partner_name_snapshot') or 'Končni kupec'))}</b><br/>"
            f"{escape(str(doc.get('partner_address_snapshot') or ''))}"
            + (
                f"<br/>Davčna kupca: {escape(str(doc.get('partner_tax_snapshot') or ''))}"
                if str(doc.get("partner_tax_snapshot") or "").strip() else ""
            ),
            ParagraphStyle("RBuyer", parent=receipt_small, alignment=0),
        ),
        Spacer(1, 2*mm),
    ]
    if doc.get("replacement_of_invoice_id"):
        rstory.extend([
            Paragraph(
                "Nadomestni za: "
                f"{escape(str(doc.get('replacement_original_no') or '-'))}<br/>"
                f"Storno: {escape(str(doc.get('replacement_storno_no') or '-'))}",
                ParagraphStyle("RReplacement", parent=receipt_small, alignment=1),
            ),
            Spacer(1, 2*mm),
        ])
    rrows = [["Postavka", "Kol.", "Rabat", "Znesek"]]
    for line in lines:
        rrows.append([
            Paragraph(escape(service_line_public_name(line)), receipt_small),
            service_line_public_quantity(line),
            f"{float(line['discount_pct']):.2f} %",
            f"{float(line['line_total']):.2f}",
        ])
    usable_width_mm = max(50, paper_width_mm - 8)
    amount_width_mm = 17 if paper_width_mm >= 80 else 14
    quantity_width_mm = 8 if paper_width_mm >= 80 else 7
    discount_width_mm = 11 if paper_width_mm >= 80 else 9
    name_width_mm = (
        usable_width_mm - quantity_width_mm - discount_width_mm - amount_width_mm
    )
    rt = Table(
        rrows,
        colWidths=[
            name_width_mm * mm, quantity_width_mm * mm,
            discount_width_mm * mm, amount_width_mm * mm,
        ],
    )
    rt.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),regular_font),
        ("FONTNAME",(0,0),(-1,0),bold_font),
        ("FONTSIZE",(0,0),(-1,-1),7.5),
        ("LINEBELOW",(0,0),(-1,0),.5,colors.black),
        ("ALIGN",(1,0),(-1,-1),"RIGHT"),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("PADDING",(0,0),(-1,-1),2.5),
    ]))
    rstory += [
        rt,
        Spacer(1,2*mm),
        Paragraph(
            f"<b>SKUPAJ {float(doc['invoice_total']):.2f} EUR</b>",
            ParagraphStyle("RT", parent=receipt_body, alignment=2, fontSize=10.5, leading=12.5),
        ),
        Paragraph(f"Plačilo: {escape(pay_text)}", receipt_small),
        Paragraph(escape(tax_note), receipt_small),
    ]
    if furs_zoi:
        rstory.extend([
            Paragraph(f"ZOI: {escape(furs_zoi)}", ParagraphStyle("RZOI", parent=receipt_small, alignment=1, fontSize=6.8, leading=8.2)),
            Paragraph(f"EOR: {escape(furs_eor or 'BO PRIDOBLJEN NAKNADNO')}", ParagraphStyle("REOR", parent=receipt_small, alignment=1, fontSize=6.8, leading=8.2)),
        ])
        if fiscal_qr and fiscal_qr_path:
            rstory.append(Image(str(fiscal_qr_path), width=22*mm, height=22*mm, hAlign="CENTER"))
    elif test_mode:
        rstory.append(Paragraph(
            f"TEST {premise}/{device} • ZOI/EOR ni ustvarjen",
            ParagraphStyle("RF", parent=receipt_small, alignment=1),
        ))
    if bank_amount > 0 and upn_qr_path:
        thermal_qr_mm = 50 if paper_width_mm >= 80 else 46
        rstory.extend([
            Spacer(1, 3*mm),
            Paragraph(
                f"<b>SKENIRAJ ZA PLAČILO {float(bank_amount):.2f} EUR</b>",
                ParagraphStyle("RUPN", parent=receipt_small, alignment=1, fontSize=9, leading=11),
            ),
            Image(
                str(upn_qr_path), width=thermal_qr_mm*mm,
                height=thermal_qr_mm*mm, hAlign="CENTER",
            ),
            Paragraph(
                f"IBAN {escape(str(company.get('iban') or ''))}<br/>"
                f"Sklic {escape(str(doc.get('payment_reference') or ''))}<br/>"
                "Skeniraj v mobilni banki, ne z navadno kamero.",
                ParagraphStyle("RUPNData", parent=receipt_small, alignment=1, fontSize=7.2, leading=8.8),
            ),
        ])
    # Dolgi nazivi se na termičnem računu prelomijo v več vrstic. Višino zato
    # ocenimo iz dejanskega besedila, ne samo iz števila postavk.
    chars_per_row = 42 if paper_width_mm >= 80 else 28
    wrapped_rows = sum(
        max(1, (len(str(line["article_name"])) + chars_per_row - 1) // chars_per_row)
        for line in lines
    )
    upn_extra_height_mm = 68 if bank_amount > 0 and upn_qr_path else 0
    height = max(165 + upn_extra_height_mm, 132 + wrapped_rows * 12 + upn_extra_height_mm) * mm
    SimpleDocTemplate(str(receipt_path), pagesize=(width, height), leftMargin=4*mm, rightMargin=4*mm, topMargin=4*mm, bottomMargin=4*mm).build(rstory)
    for qr_path in qr_paths:
        try:
            qr_path.unlink()
        except OSError:
            pass
    return a4_path, receipt_path


def generate_pos_preview_pdfs(
    *, lines: Iterable[PosLine], payments: dict[str, object], customer: dict[str, str],
    labor_floor: object, output_dir: Path | str, company: dict,
    premise: str, device: str, token: str, paper_width_mm: int = 80,
    replacement_of_invoice_no: str = "",
) -> tuple[Path, Path]:
    """Izdela jasno označen predogled brez številke, knjiženja ali zalogovnega učinka."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    calculated = [calculate_line(line, labor_floor) for line in lines]
    if not calculated:
        raise PosError("Račun nima nobene postavke.")
    total = sum((line.line_total for line in calculated), Decimal("0.00")).quantize(CENT)
    normalized = {str(key): dec(value) for key, value in payments.items() if dec(value) > 0}
    if sum(normalized.values(), Decimal("0.00")) != total:
        raise PosError("Seštevek plačil mora biti natančno enak znesku predogleda.")

    regular_font, bold_font = _register_pos_pdf_fonts()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    safe_token = re.sub(r"[^0-9A-Za-z-]", "", str(token))[:36] or "PREDOGLED"
    a4_path = output / f"PREDOGLED_RACUNA_{safe_token}.pdf"
    receipt_path = output / f"PREDOGLED_RACUNA_{safe_token}_{paper_width_mm}mm.pdf"
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "PreviewBody", parent=styles["BodyText"], fontName=regular_font,
        fontSize=9, leading=12,
    )
    small = ParagraphStyle("PreviewSmall", parent=body, fontSize=7.5, leading=9)
    warning = ParagraphStyle(
        "PreviewWarning", parent=body, fontName=bold_font,
        fontSize=13, leading=16, alignment=1, textColor=colors.red,
    )
    payment_text = ", ".join(
        f"{payment_method_label(method)}: {amount:.2f} EUR"
        for method, amount in normalized.items()
    )
    rows = [["Izvor", "Postavka", "Kol.", "Cena", "Rabat", "Znesek"]]
    for item in calculated:
        source = "SERVIS" if item.source.source_locked else "PRODAJA"
        rows.append([
            source, Paragraph(escape(service_line_public_name(item.source)), small),
            service_line_public_quantity(item.source), f"{dec(item.source.base_unit_price):.2f}",
            f"{item.applied_discount_pct:.2f} %", f"{item.line_total:.2f}",
        ])
    story = [
        Paragraph("PREDOGLED – NI RAČUN", warning),
        Paragraph(
            "Predogled nima številke, ne spremeni zaloge in ni poslan na FURS.", body
        ),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>{escape(str(company.get('legal_name') or 'MikroFix'))}</b>", body),
        Paragraph(
            f"Kupec: {escape(str(customer.get('name') or 'Končni kupec'))}<br/>"
            f"Naslov: {escape(str(customer.get('address') or '-'))}<br/>"
            f"Telefon: {escape(str(customer.get('phone') or '-'))}<br/>"
            f"E-pošta: {escape(str(customer.get('email') or '-'))}<br/>"
            f"Prostor / naprava: {escape(str(premise))}/{escape(str(device))}",
            body,
        ),
        Spacer(1, 4 * mm),
    ]
    if str(replacement_of_invoice_no or "").strip():
        story.extend([
            Paragraph(
                "Po izdaji bo to nadomestni račun za stornirani račun "
                f"<b>{escape(str(replacement_of_invoice_no).strip())}</b>.",
                body,
            ),
            Spacer(1, 3 * mm),
        ])
    table = Table(
        rows, colWidths=[24*mm, 66*mm, 15*mm, 22*mm, 22*mm, 25*mm], repeatRows=1
    )
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF1")),
        ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#CDD4DA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [
        table, Spacer(1, 4*mm),
        Paragraph(f"<b>SKUPAJ: {total:.2f} EUR</b>", body),
        Paragraph(f"Plačilo: {escape(payment_text)}", body),
        Paragraph("PREDOGLED – NI RAČUN", warning),
    ]
    SimpleDocTemplate(
        str(a4_path), pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    ).build(story)

    width = paper_width_mm * mm
    preview_receipt_body = ParagraphStyle(
        "PreviewReceiptBody", parent=body, fontSize=8.8, leading=10.8,
    )
    preview_receipt_small = ParagraphStyle(
        "PreviewReceiptSmall", parent=preview_receipt_body, fontSize=7.8, leading=9.5,
    )
    preview_receipt_warning = ParagraphStyle(
        "PreviewReceiptWarning", parent=preview_receipt_body, fontName=bold_font,
        fontSize=10, leading=12, alignment=1, textColor=colors.red,
    )
    preview_brand = str(company.get("short_name") or "MikroFix").strip()
    preview_legal = str(company.get("legal_name") or preview_brand).strip()
    receipt_story = [
        Paragraph(
            f"<b>{escape(preview_brand)}</b>",
            ParagraphStyle("PreviewBrand", parent=preview_receipt_body, fontName=bold_font, fontSize=15, leading=17, alignment=1),
        ),
        Paragraph(
            escape(preview_legal),
            ParagraphStyle("PreviewLegal", parent=preview_receipt_small, alignment=1),
        ),
        Spacer(1, 1.5*mm),
        HRFlowable(width="100%", thickness=1, color=colors.black, spaceBefore=0, spaceAfter=2*mm),
        Paragraph("<b>PREDOGLED – NI RAČUN</b>", preview_receipt_warning),
        Paragraph(
            f"{escape(str(premise))}/{escape(str(device))}<br/>"
            f"Kupec: {escape(str(customer.get('name') or 'Končni kupec'))}",
            ParagraphStyle("PreviewReceiptHead", parent=preview_receipt_small, alignment=1),
        ),
        Spacer(1, 2*mm),
    ]
    if str(replacement_of_invoice_no or "").strip():
        receipt_story.extend([
            Paragraph(
                "Nadomestni za: " + escape(str(replacement_of_invoice_no).strip()),
                ParagraphStyle("PreviewReplacement", parent=preview_receipt_small, alignment=1),
            ),
            Spacer(1, 2*mm),
        ])
    receipt_rows = [["Postavka", "Kol.", "Rabat", "Znesek"]]
    for item in calculated:
        receipt_rows.append([
            Paragraph(escape(service_line_public_name(item.source)), preview_receipt_small),
            service_line_public_quantity(item.source),
            f"{item.applied_discount_pct:.2f}%", f"{item.line_total:.2f}",
        ])
    usable = max(50, paper_width_mm - 8)
    receipt_table = Table(
        receipt_rows,
        colWidths=[(usable-38)*mm, 8*mm, 12*mm, 18*mm],
    )
    receipt_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LINEBELOW", (0, 0), (-1, 0), .5, colors.black),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 2.5),
    ]))
    receipt_story += [
        receipt_table, Spacer(1, 2*mm),
        Paragraph(f"<b>SKUPAJ {total:.2f} EUR</b>", ParagraphStyle("PRTotal", parent=preview_receipt_body, alignment=2, fontSize=10.5, leading=12.5)),
        Paragraph(f"Plačilo: {escape(payment_text)}", preview_receipt_small),
        Paragraph("PREDOGLED – NI RAČUN", ParagraphStyle("PRFoot", parent=preview_receipt_small, alignment=1, textColor=colors.red)),
    ]
    wrapped_rows = sum(max(1, (len(str(item.source.name)) + 41) // 42) for item in calculated)
    height = max(165, 130 + wrapped_rows * 12) * mm
    SimpleDocTemplate(
        str(receipt_path), pagesize=(width, height), leftMargin=4*mm,
        rightMargin=4*mm, topMargin=4*mm, bottomMargin=4*mm,
    ).build(receipt_story)
    return a4_path, receipt_path
