"""V30 varovalke za dokumentne premike zaloge."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence

from mikrofix_v29_core import (
    InventoryExceptionLine,
    V29Error,
    _insert_movement,
    _next_number,
)
from mikrofix_v30_lots import LotError, allocate_fifo, ensure_lot_schema, restore_allocations


def _connect(path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=20)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_inventory_exception(
    *, inventory_db: Path | str, document_type: str, reason: str,
    lines: Sequence[InventoryExceptionLine], operator_code: str = "DR",
):
    """V30 dovoljuje tu samo dokumentirano zmanjšanje, nikoli popravka/inventure."""
    kind = str(document_type or "").strip().upper()
    if kind not in {"ODPIS", "INTERNA_PORABA"}:
        raise V29Error(
            "Neposredni popravek ali knjiženje inventure ni dovoljeno. "
            "Uporabi namenski dokumentni postopek."
        )
    if any(float(line.quantity_delta) >= 0 for line in lines):
        raise V29Error("Odpis in interna poraba lahko zalogo samo zmanjšata.")
    if not reason.strip() or not lines:
        raise V29Error("Razlog in vsaj ena postavka sta obvezna.")
    connection = _connect(inventory_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_lot_schema(connection, "main")
        stamp = _stamp()
        prefix = "ODP" if kind == "ODPIS" else "INP"
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
            (document_no, kind, reason.strip(), operator_code, stamp, stamp),
        )
        document_id = int(cursor.lastrowid)
        wrote_line = False
        for item in lines:
            delta = round(float(item.quantity_delta), 4)
            if abs(delta) < 1e-9:
                continue
            if delta >= 0:
                raise V29Error("Odpis in interna poraba lahko zalogo samo zmanjšata.")
            wrote_line = True
            line_cursor = connection.execute(
                """
                INSERT INTO inventory_exception_lines(
                    document_id, article_id, quantity, direction, lot_serial
                ) VALUES(?,?,?,-1,'')
                """,
                (document_id, int(item.article_id), abs(delta)),
            )
            line_id = int(line_cursor.lastrowid)
            try:
                allocations = allocate_fifo(
                    connection,
                    schema="main",
                    article_id=int(item.article_id),
                    quantity=abs(delta),
                    allocation_type="INVENTORY_EXCEPTION",
                    source_id=document_id,
                    source_line_id=line_id,
                    created_at=stamp,
                )
            except LotError as exc:
                raise V29Error(str(exc)) from exc
            exact_lots = ", ".join(
                str(entry["lot_serial"])
                for entry in allocations
                if entry["lot_serial"]
            )
            movement_id = _insert_movement(
                connection,
                article_id=int(item.article_id),
                quantity_delta=delta,
                movement_type=kind,
                source_type="INVENTORY_EXCEPTION",
                source_id=document_id,
                source_line_id=line_id,
                lot_serial=exact_lots,
                reason=reason.strip(),
                operator_code=operator_code,
            )
            connection.execute(
                """
                UPDATE inventory_exception_lines
                SET stock_movement_id=?, lot_serial=? WHERE id=?
                """,
                (movement_id, exact_lots, line_id),
            )
        if not wrote_line:
            raise V29Error("Dokument nima veljavne postavke.")
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
    connection = _connect(inventory_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_lot_schema(connection, "main")
        document = connection.execute(
            "SELECT document_no, status FROM inventory_exception_documents WHERE id=?",
            (int(document_id),),
        ).fetchone()
        if not document or document[1] != "BOOKED":
            raise V29Error("Stornirati je mogoče samo knjižen izredni dokument.")
        lines = connection.execute(
            """
            SELECT l.id, l.article_id, l.quantity, l.lot_serial,
                   l.stock_movement_id
            FROM inventory_exception_lines l
            WHERE l.document_id=? ORDER BY l.id
            """,
            (int(document_id),),
        ).fetchall()
        stamp = _stamp()
        for line_id, article_id, quantity, lot_serial, movement_id in lines:
            try:
                restore_allocations(
                    connection,
                    schema="main",
                    allocation_type="INVENTORY_EXCEPTION",
                    source_id=int(document_id),
                    source_line_id=int(line_id),
                    reversed_at=stamp,
                )
            except LotError as exc:
                raise V29Error(str(exc)) from exc
            _insert_movement(
                connection,
                article_id=int(article_id),
                quantity_delta=float(quantity),
                movement_type="INVENTORY_EXCEPTION_STORNO",
                source_type="INVENTORY_EXCEPTION_STORNO",
                source_id=int(document_id),
                source_line_id=int(line_id),
                lot_serial=str(lot_serial or ""),
                reason=reason.strip(),
                operator_code=operator_code,
                reversal_of_id=int(movement_id) if movement_id is not None else None,
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


__all__ = [
    "InventoryExceptionLine", "V29Error", "create_inventory_exception",
    "storno_inventory_exception",
]
