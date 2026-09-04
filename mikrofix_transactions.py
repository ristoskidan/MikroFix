"""Atomske transakcije, ki segajo čez servisno, pisarniško in zalogovno bazo."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from mikrofix_operations import (
    complete_operation,
    ensure_operation_schema,
    fail_operation,
    prepare_operation,
    start_operation,
)


CENT = Decimal("0.01")


class AtomicTransactionError(RuntimeError):
    pass


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, ROUND_HALF_UP)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA {schema}.table_info({table})")
    }


def _require_columns(
    connection: sqlite3.Connection, schema: str, table: str, columns: set[str]
) -> None:
    existing = _columns(connection, schema, table)
    missing = sorted(columns - existing)
    if missing:
        raise AtomicTransactionError(
            f"Baza {schema}.{table} ni pripravljena; manjkajo: {', '.join(missing)}."
        )


def _ensure_cash_disbursement_links(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "main", "cash_disbursements")
    for name, definition in {
        "cash_session_id": "INTEGER",
        "cash_entry_id": "INTEGER",
        "booked_at": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE cash_disbursements ADD COLUMN {name} {definition}"
            )


def _next_prefixed_number(
    connection: sqlite3.Connection,
    *,
    qualified_table: str,
    column: str,
    prefix: str,
) -> str:
    year = datetime.now().year
    base = f"{prefix}-{year}-"
    highest = 0
    for (raw,) in connection.execute(
        f"SELECT {column} FROM {qualified_table} WHERE {column} LIKE ?",
        (f"{base}%",),
    ):
        try:
            highest = max(highest, int(str(raw).rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            continue
    return f"{base}{highest + 1:04d}"


def _operation_result(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(str(record.get("result_json") or "{}"))
    except json.JSONDecodeError:
        return {}


def complete_buyback_atomic(
    *,
    service_db: Path | str,
    office_db: Path | str,
    inventory_db: Path | str,
    buyback_id: int,
    purpose: str,
    buyer: dict[str, str],
    premise_id: str,
    device_id: str,
    operator_code: str = "DR",
    operation_id: str | None = None,
) -> dict[str, Any]:
    """V enem commit-u potrdi odkup, denarni dogodek in karanteno naprave.

    Če kateri korak odpove, se ne spremeni nobena izmed treh baz. Ponovitev z
    istim ID operacije je varna in vrne že dosežen rezultat.
    """
    service_db = Path(service_db).resolve()
    office_db = Path(office_db).resolve()
    inventory_db = Path(inventory_db).resolve()
    op_id = str(operation_id or f"BUYBACK-COMPLETE-{int(buyback_id)}")
    normalized_purpose = str(purpose or "UNDECIDED").upper()
    if normalized_purpose not in {"REFURBISH", "PARTS", "UNDECIDED"}:
        raise AtomicTransactionError("Neveljaven namen odkupljene naprave.")
    payload = {
        "buyback_id": int(buyback_id),
        "purpose": normalized_purpose,
        "buyer": {
            key: str(buyer.get(key, ""))
            for key in (
                "legal_name", "tax_number", "registration_number", "address",
                "representative",
            )
        },
        "premise_id": str(premise_id),
        "device_id": str(device_id),
    }

    connection = sqlite3.connect(str(service_db), timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=20000")
    try:
        ensure_operation_schema(connection)
        prepared = prepare_operation(
            connection,
            operation_type="BUYBACK_COMPLETE",
            aggregate_type="buyback",
            aggregate_id=int(buyback_id),
            payload=payload,
            operator_code=operator_code,
            operation_id=op_id,
        )
        connection.commit()
        if prepared["status"] == "COMPLETED":
            return _operation_result(prepared)

        connection.execute("ATTACH DATABASE ? AS office", (str(office_db),))
        connection.execute("ATTACH DATABASE ? AS inv", (str(inventory_db),))
        _ensure_cash_disbursement_links(connection)
        connection.commit()
        _require_columns(
            connection, "main", "buybacks",
            {"id", "buyback_no", "status", "payment_status", "purchase_price",
             "payout_method", "device_model", "device_serial", "device_imei",
             "device_inventory_id"},
        )
        _require_columns(
            connection, "inv", "used_devices",
            {"id", "inventory_code", "buyback_id", "buyback_no", "status"},
        )
        connection.execute("BEGIN IMMEDIATE")
        start_operation(connection, op_id)

        record = connection.execute(
            "SELECT * FROM buybacks WHERE id=?", (int(buyback_id),)
        ).fetchone()
        if not record:
            raise AtomicTransactionError("Odkupa ni mogoče najti.")
        record = dict(record)
        if record["status"] not in {"CONTRACT_READY", "COMPLETED"}:
            raise AtomicTransactionError(
                "Zaključiti je mogoče samo pripravljen odkupni dokument."
            )
        if record["status"] == "COMPLETED" and record.get("device_inventory_id"):
            existing = connection.execute(
                "SELECT id,inventory_code FROM inv.used_devices WHERE buyback_id=?",
                (int(buyback_id),),
            ).fetchone()
            if existing:
                result = {
                    "buyback_id": int(buyback_id),
                    "buyback_no": str(record["buyback_no"]),
                    "inventory_device_id": int(existing[0]),
                    "inventory_code": str(existing[1]),
                    "cash_entry_id": None,
                    "recovered_existing": True,
                }
                complete_operation(connection, op_id, result=result)
                connection.commit()
                return result

        now = _stamp()
        cash_entry_id: int | None = None
        source_order_id = (
            int(record["source_service_order_id"])
            if record.get("source_service_order_id") else None
        )
        source_service_id = (
            int(record["source_service_id"])
            if record.get("source_service_id") else None
        )
        if source_order_id:
            order = connection.execute(
                "SELECT status,service_id FROM service_orders WHERE id=?",
                (source_order_id,),
            ).fetchone()
            if not order:
                raise AtomicTransactionError("Izvirnega servisnega naročila ni mogoče najti.")
            if str(order[0]) not in {"ODKUP_V_POGOVORU", "PREUSMERJENO_V_ODKUP"}:
                raise AtomicTransactionError(
                    "Izvirni servis ni v pravilnem stanju za dokončanje odkupa."
                )
            source_service_id = source_service_id or (
                int(order[1]) if order[1] is not None else None
            )
            if source_service_id:
                service_record = connection.execute(
                    "SELECT invoice_no,office_invoice_id FROM service WHERE id=?",
                    (source_service_id,),
                ).fetchone()
                if not service_record:
                    raise AtomicTransactionError("Izvirnega servisnega zapisa ni mogoče najti.")
                if str(service_record[0] or "").strip() or service_record[1] is not None:
                    raise AtomicTransactionError(
                        "Servis z izdanim računom zahteva celoten storno pred odkupom."
                    )
        if str(record["payout_method"]).upper() == "CASH":
            _require_columns(
                connection, "office", "cash_sessions_v30",
                {"id", "premise_id", "device_id", "status", "opening_cash"},
            )
            _require_columns(
                connection, "office", "cash_journal_entries_v30",
                {"id", "session_id", "entry_no", "amount", "balance_after",
                 "reference_type", "reference_id", "entry_type"},
            )
            voucher = connection.execute(
                "SELECT voucher_no,buyback_no FROM cash_disbursements WHERE buyback_id=?",
                (int(buyback_id),),
            ).fetchone()
            if not voucher:
                raise AtomicTransactionError("Odkup nima povezanega blagajniškega izdatka.")
            existing_cash = connection.execute(
                """
                SELECT id,session_id,amount,reference_no
                FROM office.cash_journal_entries_v30
                WHERE reference_type='BUYBACK' AND reference_id=?
                  AND entry_type='CASH_BUYBACK'
                """,
                (int(buyback_id),),
            ).fetchone()
            if existing_cash:
                expected_references = {
                    str(voucher[0] or "").strip(), str(voucher[1] or "").strip()
                }
                if (
                    _money(existing_cash[2]) != -_money(record["purchase_price"])
                    or str(existing_cash[3] or "").strip() not in expected_references
                ):
                    raise AtomicTransactionError(
                        "Obstoječi blagajniški izdatek odkupa se ne ujema z listino."
                    )
                cash_entry_id = int(existing_cash[0])
                cash_session_id = int(existing_cash[1])
            else:
                session = connection.execute(
                    """
                    SELECT id,opening_cash FROM office.cash_sessions_v30
                    WHERE premise_id=? AND device_id=? AND status='OPEN'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(premise_id), str(device_id)),
                ).fetchone()
                if not session:
                    raise AtomicTransactionError(
                        "Gotovinskega odkupa ni mogoče zaključiti brez odprte blagajne."
                    )
                totals = connection.execute(
                    """
                    SELECT COALESCE(MAX(entry_no),0),COALESCE(SUM(amount),0)
                    FROM office.cash_journal_entries_v30 WHERE session_id=?
                    """,
                    (int(session[0]),),
                ).fetchone()
                amount = -_money(record["purchase_price"])
                balance = _money(session[1]) + _money(totals[1]) + amount
                if balance < 0:
                    raise AtomicTransactionError(
                        "Blagajniški izdatek bi ustvaril negativno stanje gotovine."
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO office.cash_journal_entries_v30(
                        session_id,entry_no,occurred_at,entry_type,amount,balance_after,
                        reference_type,reference_id,reference_no,description,operator_code
                    ) VALUES(?,?,?,'CASH_BUYBACK',?,?,'BUYBACK',?,?,?,?)
                    """,
                    (
                        int(session[0]), int(totals[0]) + 1, now, float(amount),
                        float(balance), int(buyback_id),
                        str(voucher[0]),
                        f"Gotovinski odkup {record['buyback_no']}",
                        str(operator_code),
                    ),
                )
                cash_entry_id = int(cursor.lastrowid)
                cash_session_id = int(session[0])
            updated = connection.execute(
                """
                UPDATE cash_disbursements
                SET status='BOOKED',confirmed_at=COALESCE(confirmed_at,?),
                    cash_session_id=?,cash_entry_id=?,booked_at=COALESCE(booked_at,?)
                WHERE buyback_id=? AND status IN ('PREPARED','CONFIRMED','BOOKED')
                  AND (cash_entry_id IS NULL OR cash_entry_id=?)
                """,
                (
                    now, cash_session_id, cash_entry_id, now,
                    int(buyback_id), cash_entry_id,
                ),
            )
            if updated.rowcount != 1:
                raise AtomicTransactionError("Neveljaven blagajniški izdatek odkupa.")

        existing_device = connection.execute(
            "SELECT id,inventory_code FROM inv.used_devices WHERE buyback_id=?",
            (int(buyback_id),),
        ).fetchone()
        if existing_device:
            inventory_device_id = int(existing_device[0])
            inventory_code = str(existing_device[1])
        else:
            inventory_code = _next_prefixed_number(
                connection,
                qualified_table="inv.used_devices",
                column="inventory_code",
                prefix="NAP",
            )
            cursor = connection.execute(
                """
                INSERT INTO inv.used_devices(
                    inventory_code,buyback_id,buyback_no,model,serial,imei,storage,
                    condition_text,purpose,status,acquisition_cost,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'QUARANTINE',?,?,?)
                """,
                (
                    inventory_code, int(buyback_id), record["buyback_no"],
                    record["device_model"], record["device_serial"], record["device_imei"],
                    str(record.get("device_storage") or ""),
                    str(record.get("condition_text") or ""), normalized_purpose,
                    float(_money(record["purchase_price"])), now, now,
                ),
            )
            inventory_device_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            UPDATE buybacks
            SET status='COMPLETED',payment_status='PAID',purpose=?,
                buyer_legal_name=?,buyer_tax_number=?,buyer_registration_number=?,
                buyer_address=?,buyer_representative=?,device_inventory_id=?,
                handover_at=COALESCE(handover_at,?)
            WHERE id=? AND status IN ('CONTRACT_READY','COMPLETED')
            """,
            (
                normalized_purpose, buyer.get("legal_name", ""),
                buyer.get("tax_number", ""), buyer.get("registration_number", ""),
                buyer.get("address", ""), buyer.get("representative", "Daniel Ristoski"),
                inventory_device_id, now, int(buyback_id),
            ),
        )
        if cursor.rowcount != 1:
            raise AtomicTransactionError("Odkupa ni bilo mogoče dokončno potrditi.")
        if source_order_id:
            converted = connection.execute(
                """
                UPDATE service_orders
                SET status='PREUSMERJENO_V_ODKUP',buyback_id=?,
                    conversion_operation_id=?,converted_at=?,
                    completed_at=COALESCE(completed_at,?)
                WHERE id=? AND status IN ('ODKUP_V_POGOVORU','PREUSMERJENO_V_ODKUP')
                """,
                (int(buyback_id), op_id, now, now, source_order_id),
            )
            if converted.rowcount != 1:
                raise AtomicTransactionError("Izvirnega servisa ni bilo mogoče zaključiti z odkupom.")
            if source_service_id:
                service_columns = _columns(connection, "main", "service")
                if "resolution_status" not in service_columns:
                    connection.execute(
                        "ALTER TABLE service ADD COLUMN resolution_status TEXT NOT NULL DEFAULT 'AWAITING_INVOICE'"
                    )
                if "converted_buyback_id" not in service_columns:
                    connection.execute(
                        "ALTER TABLE service ADD COLUMN converted_buyback_id INTEGER"
                    )
                connection.execute(
                    """
                    UPDATE service
                    SET resolution_status='CONVERTED_TO_BUYBACK',converted_buyback_id=?
                    WHERE id=?
                    """,
                    (int(buyback_id), source_service_id),
                )
        result = {
            "buyback_id": int(buyback_id),
            "buyback_no": str(record["buyback_no"]),
            "inventory_device_id": inventory_device_id,
            "inventory_code": inventory_code,
            "cash_entry_id": cash_entry_id,
            "recovered_existing": False,
        }
        complete_operation(
            connection,
            op_id,
            result=result,
            event_type="BUYBACK_COMPLETED_ATOMIC",
        )
        connection.commit()
        return result
    except Exception as exc:
        connection.rollback()
        try:
            fail_operation(connection, op_id, exc)
            connection.commit()
        except Exception:
            connection.rollback()
        raise
    finally:
        connection.close()

