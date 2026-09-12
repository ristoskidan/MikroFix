"""Atomsko knjiženje dejansko uporabljenih delov ob tehničnem zaključku."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from mikrofix_v30_lots import allocate_fifo
from mikrofix_service_inventory import service_inventory_by_barcode


def consume_service_parts(
    connection,
    *,
    inventory_schema: str,
    service_id: int,
    service_no: str,
    parts: Iterable[tuple],
    operator_code: str,
    consumed_at: str | None = None,
) -> list[int]:
    """V isti transakciji shrani vrstice in zalogo zmanjša natanko enkrat.

    ``parts`` vsebuje (naziv, interna koda, črtna koda, article_id, LOT, cena).
    Artikel brez ``article_id`` je nezalogovna sledljiva vrstica in zaloge ne
    spremeni. Funkcija sama nikoli ne potrdi transakcije.
    """
    schema = str(inventory_schema)
    stamp = consumed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_line_ids: list[int] = []
    central_used_in_call: dict[str, int] = {}
    for part_name, part_code, barcode, article_id, requested_lot, price in parts:
        cursor = connection.execute(
            """
            INSERT INTO service_parts(
                service_id,part_name,part_id,barcode,article_id,lot_serial,unit_price
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                int(service_id), str(part_name or ""), str(part_code or ""),
                str(barcode or ""),
                int(article_id) if article_id is not None else None,
                str(requested_lot or ""), float(price or 0),
            ),
        )
        line_id = int(cursor.lastrowid)
        created_line_ids.append(line_id)
        if article_id is None:
            continue

        if int(article_id) < 0:
            scanned = str(barcode or "").strip()
            central = service_inventory_by_barcode(connection, scanned)
            used = central_used_in_call.get(scanned, 0) + 1
            if (
                central is None
                or central.local_article_id != int(article_id)
                or central.blocked
                or central.quantity_available < used
            ):
                available = central.quantity_available if central else 0
                raise RuntimeError(
                    f"Premalo centralnega posnetka zaloge za artikel: "
                    f"{part_name} (na voljo {available})."
                )
            central_used_in_call[scanned] = used
            # The durable service line and completion outbox are the local
            # physical-use trace. PostgreSQL books the authoritative FIFO lot;
            # no legacy SQLite stock movement may be invented for this key.
            continue

        stock = connection.execute(
            f"SELECT stock_qty,COALESCE(reserved_qty,0),article_name,location "
            f"FROM {schema}.inventory_items WHERE id=? AND COALESCE(active,1)=1",
            (int(article_id),),
        ).fetchone()
        if not stock:
            raise RuntimeError("Eden od uporabljenih artiklov ne obstaja v zalogi.")
        available = float(stock[0] or 0) - float(stock[1] or 0)
        if available < 1:
            raise RuntimeError(
                f"Premalo zaloge za artikel: {stock[2]} (na voljo {available})."
            )
        allocations = allocate_fifo(
            connection,
            schema=schema,
            article_id=int(article_id),
            quantity=1,
            allocation_type="SERVICE_CONSUMPTION",
            source_id=int(service_id),
            source_line_id=line_id,
            created_at=stamp,
            requested_lot_serial=str(requested_lot or "") or None,
        )
        exact_lot = ", ".join(
            str(entry["lot_serial"])
            for entry in allocations if entry["lot_serial"]
        )
        connection.execute(
            "UPDATE service_parts SET lot_serial=? WHERE id=?",
            (exact_lot or str(requested_lot or ""), line_id),
        )
        balance = float(stock[0] or 0) - 1
        connection.execute(
            f"UPDATE {schema}.inventory_items SET stock_qty=?,updated_at=? WHERE id=?",
            (balance, stamp, int(article_id)),
        )
        connection.execute(
            f"""
            INSERT INTO {schema}.stock_movements(
                movement_no,created_at,article_id,quantity_delta,balance_after,
                movement_type,source_type,source_id,source_line_id,lot_serial,
                location_from,location_to,reason,operator_code,reversal_of_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                f"POR-SRV-{int(service_id):06d}-{line_id}", stamp,
                int(article_id), -1, balance, "SERVICE_USE_OUT", "SERVICE",
                int(service_id), line_id, exact_lot or str(requested_lot or ""),
                str(stock[3] or ""), "SERVIS",
                f"Poraba pri servisu {service_no}", str(operator_code or ""),
            ),
        )
    return created_line_ids
