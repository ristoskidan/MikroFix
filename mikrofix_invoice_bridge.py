"""Preverljiv most med servisnim zapisom in osnutkom računa v Office."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class ServiceInvoiceSeed:
    service_id: int
    service_no: str
    customer: str
    customer_address: str
    customer_phone: str
    customer_email: str
    model: str
    serial: str
    work: str
    labor_price: float
    manual_pricing: bool
    manual_lines: tuple[tuple, ...]
    parts: tuple[tuple, ...]
    manual_line_ids: tuple[int, ...]
    part_line_ids: tuple[int, ...]


def ensure_service_invoice_schema(connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(service)")}
    if "labor_price" not in columns:
        connection.execute(
            "ALTER TABLE service ADD COLUMN labor_price REAL NOT NULL DEFAULT 0"
        )
    if "office_invoice_id" not in columns:
        connection.execute("ALTER TABLE service ADD COLUMN office_invoice_id INTEGER")
    if "pricing_mode" not in columns:
        connection.execute(
            "ALTER TABLE service ADD COLUMN pricing_mode TEXT NOT NULL DEFAULT 'LEGACY_LABOR'"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS service_manual_lines(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            line_kind TEXT NOT NULL,
            description TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            service_tier TEXT,
            billable_minutes INTEGER NOT NULL DEFAULT 0,
            tariff_version TEXT,
            first_rate REAL NOT NULL DEFAULT 0,
            extra_half_hour_rate REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
        )
        """
    )
    manual_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(service_manual_lines)")
    }
    for name, ddl in {
        "service_tier": "TEXT",
        "billable_minutes": "INTEGER NOT NULL DEFAULT 0",
        "tariff_version": "TEXT",
        "first_rate": "REAL NOT NULL DEFAULT 0",
        "extra_half_hour_rate": "REAL NOT NULL DEFAULT 0",
        "line_uuid": "TEXT",
    }.items():
        if name not in manual_columns:
            connection.execute(f"ALTER TABLE service_manual_lines ADD COLUMN {name} {ddl}")
    for legacy_line_id, in connection.execute(
        "SELECT id FROM service_manual_lines WHERE line_uuid IS NULL OR line_uuid=''"
    ).fetchall():
        connection.execute(
            "UPDATE service_manual_lines SET line_uuid=? WHERE id=?",
            (str(uuid4()), int(legacy_line_id)),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_service_manual_line_uuid "
        "ON service_manual_lines(line_uuid)"
    )
    connection.commit()


def load_service_invoice_seed(
    connection, service_id: int, *, labor_floor_eur: float
) -> ServiceInvoiceSeed:
    ensure_service_invoice_schema(connection)
    row = connection.execute(
        """
        SELECT s.service_no, s.customer, s.customer_address, s.customer_phone,
               s.model, s.serial, s.work, COALESCE(s.labor_price, 0),
               COALESCE(s.pricing_mode, 'LEGACY_LABOR'),
               s.service_order_id, COALESCE(s.invoice_no, ''), s.office_invoice_id
        FROM service s WHERE s.id=?
        """,
        (int(service_id),),
    ).fetchone()
    if not row:
        raise ValueError("Servisnega zapisa ni mogoče najti.")
    if str(row[10] or "").strip() or row[11] is not None:
        raise ValueError("Ta servis že ima izdan račun. Ponovna izdaja ni dovoljena.")
    service_columns = {
        item[1] for item in connection.execute("PRAGMA table_info(service)")
    }
    if "resolution_status" in service_columns:
        resolution = connection.execute(
            "SELECT resolution_status FROM service WHERE id=?", (int(service_id),)
        ).fetchone()
        if resolution and str(resolution[0] or "AWAITING_INVOICE") != "AWAITING_INVOICE":
            raise ValueError("Ta servis je bil preusmerjen v odkup in ga ni dovoljeno zaračunati.")
    order_columns = {
        item[1] for item in connection.execute("PRAGMA table_info(service_orders)")
    }
    if row[9] and "status" in order_columns:
        order_status = connection.execute(
            "SELECT status FROM service_orders WHERE id=?", (int(row[9]),)
        ).fetchone()
        if order_status and str(order_status[0]) != "OPEN":
            raise ValueError("Servisno naročilo ni odprto za izdajo računa.")

    email = ""
    if row[9]:
        order = connection.execute(
            "SELECT customer_email FROM service_orders WHERE id=?",
            (int(row[9]),),
        ).fetchone()
        email = str(order[0] or "") if order else ""

    part_rows = connection.execute(
        """
        SELECT id, article_id, barcode, part_name, part_id, unit_price
        FROM service_parts WHERE service_id=? ORDER BY id
        """,
        (int(service_id),),
    ).fetchall()
    manual_rows = connection.execute(
        """
        SELECT id, line_kind, description, quantity, unit_price,
               COALESCE(service_tier,''), COALESCE(billable_minutes,0),
               COALESCE(tariff_version,''), COALESCE(first_rate,0),
               COALESCE(extra_half_hour_rate,0)
        FROM service_manual_lines WHERE service_id=? ORDER BY id
        """,
        (int(service_id),),
    ).fetchall()

    return ServiceInvoiceSeed(
        service_id=int(service_id),
        service_no=str(row[0] or ""),
        customer=str(row[1] or ""),
        customer_address=str(row[2] or ""),
        customer_phone=str(row[3] or ""),
        customer_email=email,
        model=str(row[4] or ""),
        serial=str(row[5] or ""),
        work=str(row[6] or ""),
        labor_price=(
            float(row[7] or 0)
            if str(row[8]) == "MANUAL_LINES_V29"
            else max(float(labor_floor_eur), float(row[7] or 0))
        ),
        manual_pricing=str(row[8]) == "MANUAL_LINES_V29",
        manual_lines=tuple(tuple(row[1:]) for row in manual_rows),
        parts=tuple(tuple(row[1:]) for row in part_rows),
        manual_line_ids=tuple(int(row[0]) for row in manual_rows),
        part_line_ids=tuple(int(row[0]) for row in part_rows),
    )


def mark_service_invoiced(
    connection, service_id: int, invoice_no: str, office_invoice_id: int
) -> None:
    ensure_service_invoice_schema(connection)
    cursor = connection.execute(
        """
        UPDATE service
        SET invoice_no=?, office_invoice_id=?
        WHERE id=?
        """,
        (str(invoice_no), int(office_invoice_id), int(service_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError("Računa ni bilo mogoče povezati s servisom.")
    order = connection.execute(
        "SELECT service_order_id FROM service WHERE id=?", (int(service_id),)
    ).fetchone()
    order_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(service_orders)")
    }
    lifecycle_columns = {"id", "status", "completed_at", "service_id"}
    if order and order[0] and lifecycle_columns.issubset(order_columns):
        order_cursor = connection.execute(
            """
            UPDATE service_orders
            SET status='COMPLETED', completed_at=CURRENT_TIMESTAMP, service_id=?
            WHERE id=? AND status='OPEN'
            """,
            (int(service_id), int(order[0])),
        )
        if order_cursor.rowcount != 1:
            existing = connection.execute(
                "SELECT status, service_id FROM service_orders WHERE id=?",
                (int(order[0]),),
            ).fetchone()
            if not existing or existing[0] != "COMPLETED" or int(existing[1] or 0) != int(service_id):
                raise ValueError("Servisnega naročila ni bilo mogoče zaključiti z računom.")
    connection.commit()
