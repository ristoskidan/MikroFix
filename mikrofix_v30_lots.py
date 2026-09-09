"""LOT/serijska sledljivost količin v skupni zalogi MikroFIX v30."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal


FOUR = Decimal("0.0001")
ALLOWED_SCHEMAS = {"main", "inv", "inventorydb"}


class LotError(RuntimeError):
    pass


def _schema(name: str) -> str:
    value = str(name or "main")
    if value not in ALLOWED_SCHEMAS:
        raise ValueError("Neveljavna podatkovna shema za LOT sledljivost.")
    return value


def _q(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(FOUR)


def ensure_lot_schema(connection: sqlite3.Connection, schema: str = "main") -> None:
    schema = _schema(schema)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.inventory_lots_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            source_line_id INTEGER,
            lot_serial TEXT NOT NULL,
            quantity_received REAL NOT NULL,
            quantity_available REAL NOT NULL,
            acquisition_cost REAL NOT NULL,
            sale_price REAL NOT NULL DEFAULT 0,
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            received_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {schema}.idx_inventory_lot_source_v30
        ON inventory_lots_v30(source_type, source_id, source_line_id)
        WHERE source_id IS NOT NULL AND source_line_id IS NOT NULL
        """
    )
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {schema}.idx_inventory_lot_available_v30
        ON inventory_lots_v30(article_id, status, received_at, id)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {schema}.inventory_lot_allocations_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            allocation_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            source_line_id INTEGER NOT NULL,
            lot_id INTEGER NOT NULL,
            lot_serial TEXT NOT NULL,
            quantity REAL NOT NULL,
            acquisition_cost REAL NOT NULL,
            created_at TEXT NOT NULL,
            reversed_at TEXT,
            UNIQUE(allocation_type, source_id, source_line_id, lot_id)
        )
        """
    )
    allocation_columns = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA {schema}.table_info(inventory_lot_allocations_v30)"
        )
    }
    if "reversed_at" not in allocation_columns:
        connection.execute(
            f"ALTER TABLE {schema}.inventory_lot_allocations_v30 ADD COLUMN reversed_at TEXT"
        )
    stamp = datetime.now().isoformat(timespec="seconds")
    # Obstoječo testno/staro zalogo enkrat uvedemo kot jasno označen začetni LOT.
    connection.execute(
        f"""
        INSERT INTO {schema}.inventory_lots_v30(
            article_id, source_type, source_id, source_line_id, lot_serial,
            quantity_received, quantity_available, acquisition_cost, sale_price,
            location, status, received_at, updated_at
        )
        SELECT i.id, 'LEGACY_MIGRATION', i.id, i.id,
               COALESCE(NULLIF(i.lot_serial,''), 'LEGACY-' || i.id),
               i.stock_qty, i.stock_qty, COALESCE(i.acquisition_cost,0),
               COALESCE(i.price,0), COALESCE(i.location,''), 'ACTIVE', ?, ?
        FROM {schema}.inventory_items i
        WHERE COALESCE(i.active,1)=1 AND COALESCE(i.stock_qty,0)>0
          AND NOT EXISTS(
              SELECT 1 FROM {schema}.inventory_lots_v30 l WHERE l.article_id=i.id
          )
        """,
        (stamp, stamp),
    )


def receive_lot(
    connection: sqlite3.Connection, *, schema: str, article_id: int,
    source_type: str, source_id: int, source_line_id: int, lot_serial: str,
    quantity, acquisition_cost, sale_price, location: str, received_at: str,
) -> int:
    schema = _schema(schema)
    ensure_lot_schema(connection, schema)
    existing = connection.execute(
        f"""
        SELECT id FROM {schema}.inventory_lots_v30
        WHERE source_type=? AND source_id=? AND source_line_id=?
        """,
        (source_type, int(source_id), int(source_line_id)),
    ).fetchone()
    if existing:
        return int(existing[0])
    qty = _q(quantity)
    if qty <= 0:
        raise LotError("Količina LOT-a mora biti večja od nič.")
    cursor = connection.execute(
        f"""
        INSERT INTO {schema}.inventory_lots_v30(
            article_id, source_type, source_id, source_line_id, lot_serial,
            quantity_received, quantity_available, acquisition_cost, sale_price,
            location, status, received_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)
        """,
        (
            int(article_id), source_type, int(source_id), int(source_line_id),
            str(lot_serial or ""), float(qty), float(qty), float(_q(acquisition_cost)),
            float(_q(sale_price)), str(location or ""), received_at, received_at,
        ),
    )
    return int(cursor.lastrowid)


def allocate_fifo(
    connection: sqlite3.Connection, *, schema: str, article_id: int, quantity,
    allocation_type: str, source_id: int, source_line_id: int, created_at: str,
    requested_lot_serial: str = "",
) -> list[dict]:
    schema = _schema(schema)
    ensure_lot_schema(connection, schema)
    needed = _q(quantity)
    if needed <= 0:
        return []
    requested = str(requested_lot_serial or "").strip()
    lot_filter = " AND lot_serial=?" if requested else ""
    parameters = (int(article_id), requested) if requested else (int(article_id),)
    rows = connection.execute(
        f"""
        SELECT id, lot_serial, quantity_available, acquisition_cost
        FROM {schema}.inventory_lots_v30
        WHERE article_id=? AND status='ACTIVE' AND quantity_available>0
          {lot_filter}
        ORDER BY received_at, id
        """,
        parameters,
    ).fetchall()
    available = sum((_q(row[2]) for row in rows), Decimal("0"))
    if available < needed:
        raise LotError(
            f"LOT sledljivost nima dovolj razpoložljive količine za artikel ID {article_id} "
            f"(LOT {available}, zahtevano {needed})."
        )
    allocations: list[dict] = []
    remaining = needed
    for lot_id, lot_serial, raw_available, raw_cost in rows:
        if remaining <= 0:
            break
        lot_available = _q(raw_available)
        used = min(lot_available, remaining)
        new_available = (lot_available - used).quantize(FOUR)
        status = "DEPLETED" if new_available == 0 else "ACTIVE"
        connection.execute(
            f"""
            UPDATE {schema}.inventory_lots_v30
            SET quantity_available=?, status=?, updated_at=? WHERE id=?
            """,
            (float(new_available), status, created_at, int(lot_id)),
        )
        connection.execute(
            f"""
            INSERT INTO {schema}.inventory_lot_allocations_v30(
                allocation_type, source_id, source_line_id, lot_id, lot_serial,
                quantity, acquisition_cost, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                allocation_type, int(source_id), int(source_line_id), int(lot_id),
                str(lot_serial or ""), float(used), float(_q(raw_cost)), created_at,
            ),
        )
        allocations.append({
            "lot_id": int(lot_id), "lot_serial": str(lot_serial or ""),
            "quantity": used, "acquisition_cost": _q(raw_cost),
        })
        remaining -= used
    return allocations


def reverse_unconsumed_receipt_lot(
    connection: sqlite3.Connection, *, schema: str, source_id: int,
    source_line_id: int, updated_at: str,
) -> int:
    schema = _schema(schema)
    ensure_lot_schema(connection, schema)
    row = connection.execute(
        f"""
        SELECT id, quantity_received, quantity_available
        FROM {schema}.inventory_lots_v30
        WHERE source_type='RECEIPT_V30' AND source_id=? AND source_line_id=?
        """,
        (int(source_id), int(source_line_id)),
    ).fetchone()
    if not row:
        raise LotError("LOT prvotnega prejema ni najden.")
    if _q(row[1]) != _q(row[2]):
        raise LotError(
            "Storno ni dovoljen, ker je bil iz tega konkretnega LOT-a že porabljen ali prodan del zaloge."
        )
    connection.execute(
        f"""
        UPDATE {schema}.inventory_lots_v30
        SET quantity_available=0, status='STORNIRANO', updated_at=? WHERE id=?
        """,
        (updated_at, int(row[0])),
    )
    return int(row[0])


def maximum_available_cost(
    connection: sqlite3.Connection, *, schema: str, article_id: int,
) -> Decimal | None:
    schema = _schema(schema)
    row = connection.execute(
        f"""
        SELECT MAX(acquisition_cost) FROM {schema}.inventory_lots_v30
        WHERE article_id=? AND status='ACTIVE' AND quantity_available>0
        """,
        (int(article_id),),
    ).fetchone()
    return None if not row or row[0] is None else _q(row[0])


def restore_allocations(
    connection: sqlite3.Connection, *, schema: str, allocation_type: str,
    source_id: int, source_line_id: int, reversed_at: str,
) -> int:
    """Pri stornu vrne količino v iste LOT-e; ponovitev ne spremeni stanja."""
    schema = _schema(schema)
    ensure_lot_schema(connection, schema)
    rows = connection.execute(
        f"""
        SELECT a.id, a.lot_id, a.quantity, l.quantity_received, l.quantity_available
        FROM {schema}.inventory_lot_allocations_v30 a
        JOIN {schema}.inventory_lots_v30 l ON l.id=a.lot_id
        WHERE a.allocation_type=? AND a.source_id=? AND a.source_line_id=?
          AND a.reversed_at IS NULL
        ORDER BY a.id
        """,
        (allocation_type, int(source_id), int(source_line_id)),
    ).fetchall()
    for allocation_id, lot_id, raw_qty, raw_received, raw_available in rows:
        restored = (_q(raw_available) + _q(raw_qty)).quantize(FOUR)
        if restored > _q(raw_received):
            raise LotError("Storno bi količino konkretnega LOT-a povečal nad prejeto količino.")
        connection.execute(
            f"""
            UPDATE {schema}.inventory_lots_v30
            SET quantity_available=?, status='ACTIVE', updated_at=? WHERE id=?
            """,
            (float(restored), reversed_at, int(lot_id)),
        )
        connection.execute(
            f"""
            UPDATE {schema}.inventory_lot_allocations_v30
            SET reversed_at=? WHERE id=? AND reversed_at IS NULL
            """,
            (reversed_at, int(allocation_id)),
        )
    return len(rows)
