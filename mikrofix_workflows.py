"""Servisni sprejem in odkup rabljenih naprav za MikroFIX v28 TEST."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from mikrofix_operations import (
    complete_operation,
    ensure_operation_schema,
    prepare_operation,
    start_operation,
)


SERVICE_ORDER_VERSION = "MF-SN-2.3-TEST"
BUYBACK_VERSION = "MF-ODK-1.2-TEST"

CONTRACT_CONTEXT_LABELS = {
    "MFD": "V poslovnem prostoru MFD",
    "MFT_TEREN": "Na terenu MFT",
    "NA_DALJAVO": "Na daljavo",
}
DELIVERY_LABELS = {
    "EMAIL_PDF": "PDF po e-pošti",
    "SMS_PDF": "SMS-povezava do PDF",
    "PAPER": "Papirni izvod",
}
ACQUISITION_LABELS = {
    "KUPLJENA_NOVA": "Kupljena nova",
    "KUPLJENA_RABLJENA_PODJETJE": "Kupljena rabljena od podjetja",
    "KUPLJENA_RABLJENA_FIZICNA_OSEBA": "Kupljena rabljena od fizične osebe",
    "DARILO_DEDOVANJE": "Darilo ali dedovanje",
    "DRUGO": "Drugo",
}
EVIDENCE_LABELS = {
    "NI_NA_VOLJO": "Ni na voljo",
    "ORIGINALNI_RAČUN": "Originalni račun",
    "PREJŠNJA_POGODBA": "Prejšnja pogodba",
    "DARILNA_IZJAVA": "Darilna izjava",
    "DRUGO": "Drugo",
}
IMEI_RESULT_LABELS = {
    "TEST_CLEAR": "TEST - čisto (simulacija)",
    "CLEAR": "Čisto",
    "BLOCKED": "Blokirano",
    "UNKNOWN": "Neznano",
}
RISK_LABELS = {"GREEN": "Zeleno", "ORANGE": "Oranžno", "RED": "Rdeče"}
PAYOUT_LABELS = {
    "BANK_TRANSFER": "Bančno nakazilo (priporočeno)",
    "CASH": "Gotovina (največ 420,00 EUR)",
}
CASH_BUYBACK_LIMIT_EUR = 420.0


def _label(mapping: dict[str, str], value: Any) -> str:
    raw = str(value or "").strip()
    return mapping.get(raw, raw.replace("_", " ").capitalize())


class WorkflowError(ValueError):
    """Vnos ni dovolj popoln ali varen za izdajo dokumenta."""


def ensure_workflow_schema(connection) -> None:
    ensure_operation_schema(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS service_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            customer_name TEXT NOT NULL,
            customer_address TEXT,
            customer_phone TEXT,
            customer_email TEXT,
            device_model TEXT NOT NULL,
            device_serial TEXT NOT NULL,
            device_imei TEXT,
            device_os TEXT,
            device_storage TEXT,
            device_type TEXT NOT NULL DEFAULT 'APPLE_MOBILE',
            identity_method TEXT NOT NULL DEFAULT 'MICRODETECT',
            customer_request TEXT NOT NULL,
            intake_condition TEXT,
            accessories TEXT,
            quote_eur REAL NOT NULL DEFAULT 0,
            logistics_eur REAL NOT NULL DEFAULT 0,
            expected_due TEXT,
            contract_context TEXT NOT NULL,
            immediate_start_requested INTEGER NOT NULL DEFAULT 0,
            withdrawal_loss_ack INTEGER NOT NULL DEFAULT 0,
            delivery_channel TEXT NOT NULL,
            microdetect_confirmed INTEGER NOT NULL DEFAULT 1,
            microdetect_at TEXT NOT NULL,
            service_id INTEGER,
            operator_code TEXT NOT NULL,
            notes TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_service_orders_device
        ON service_orders(device_serial, device_imei, status)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS buybacks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyback_no TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            seller_name TEXT NOT NULL,
            seller_tax_number TEXT NOT NULL,
            seller_birth_date TEXT NOT NULL,
            seller_address TEXT NOT NULL,
            seller_contact TEXT,
            seller_iban TEXT NOT NULL,
            identity_checked INTEGER NOT NULL,
            device_model TEXT NOT NULL,
            device_serial TEXT NOT NULL,
            device_imei TEXT NOT NULL,
            device_os TEXT,
            device_storage TEXT,
            color_capacity TEXT,
            accessories TEXT,
            condition_text TEXT NOT NULL,
            acquisition_method TEXT NOT NULL,
            acquisition_details TEXT,
            evidence_type TEXT,
            imei_check_provider TEXT NOT NULL,
            imei_check_result TEXT NOT NULL,
            imei_check_reference TEXT NOT NULL,
            imei_checked_at TEXT NOT NULL,
            account_removed INTEGER NOT NULL,
            activation_lock_removed INTEGER NOT NULL,
            mdm_clear INTEGER NOT NULL,
            ownership_declaration INTEGER NOT NULL,
            privacy_notice_received INTEGER NOT NULL,
            risk_level TEXT NOT NULL,
            risk_reason TEXT,
            purchase_price REAL NOT NULL,
            payout_method TEXT NOT NULL,
            operator_code TEXT NOT NULL,
            microdetect_confirmed INTEGER NOT NULL,
            microdetect_at TEXT NOT NULL,
            notes TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_buybacks_device
        ON buybacks(device_serial, device_imei, created_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cash_disbursements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT NOT NULL UNIQUE,
            buyback_id INTEGER NOT NULL UNIQUE,
            buyback_no TEXT NOT NULL,
            created_at TEXT NOT NULL,
            confirmed_at TEXT,
            status TEXT NOT NULL DEFAULT 'PREPARED',
            recipient_name TEXT NOT NULL,
            recipient_address TEXT NOT NULL,
            purpose TEXT NOT NULL,
            amount REAL NOT NULL,
            operator_code TEXT NOT NULL,
            cash_session_id INTEGER,
            cash_entry_id INTEGER,
            booked_at TEXT,
            FOREIGN KEY(buyback_id) REFERENCES buybacks(id)
        )
        """
    )
    cash_disbursement_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cash_disbursements)")
    }
    for name, definition in {
        "cash_session_id": "INTEGER",
        "cash_entry_id": "INTEGER",
        "booked_at": "TEXT",
    }.items():
        if name not in cash_disbursement_columns:
            connection.execute(
                f"ALTER TABLE cash_disbursements ADD COLUMN {name} {definition}"
            )
    for table in ("service_orders", "buybacks"):
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if "device_storage" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN device_storage TEXT"
            )
    service_order_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(service_orders)")
    }
    if "device_type" not in service_order_columns:
        connection.execute(
            "ALTER TABLE service_orders ADD COLUMN device_type TEXT NOT NULL DEFAULT 'APPLE_MOBILE'"
        )
    if "identity_method" not in service_order_columns:
        connection.execute(
            "ALTER TABLE service_orders ADD COLUMN identity_method TEXT NOT NULL DEFAULT 'MICRODETECT'"
        )
    for name, definition in {
        "previous_status": "TEXT",
        "buyback_id": "INTEGER",
        "conversion_operation_id": "TEXT",
        "conversion_started_at": "TEXT",
        "converted_at": "TEXT",
    }.items():
        if name not in service_order_columns:
            connection.execute(f"ALTER TABLE service_orders ADD COLUMN {name} {definition}")
    buyback_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(buybacks)")
    }
    if "payment_status" not in buyback_columns:
        connection.execute(
            "ALTER TABLE buybacks ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'PENDING'"
        )
    if "handover_at" not in buyback_columns:
        connection.execute("ALTER TABLE buybacks ADD COLUMN handover_at TEXT")
    if "purpose" not in buyback_columns:
        connection.execute(
            "ALTER TABLE buybacks ADD COLUMN purpose TEXT NOT NULL DEFAULT 'UNDECIDED'"
        )
    if "seller_tax_number" not in buyback_columns:
        connection.execute(
            "ALTER TABLE buybacks ADD COLUMN seller_tax_number TEXT NOT NULL DEFAULT ''"
        )
    for name, definition in {
        "source_service_order_id": "INTEGER",
        "source_service_id": "INTEGER",
        "conversion_operation_id": "TEXT",
        "installed_parts_cost": "REAL NOT NULL DEFAULT 0",
        "commercial_internal_labor_eur": "REAL NOT NULL DEFAULT 0",
        "accounting_owner_labor_eur": "REAL NOT NULL DEFAULT 0",
    }.items():
        if name not in buyback_columns:
            connection.execute(f"ALTER TABLE buybacks ADD COLUMN {name} {definition}")
    connection.commit()


def _required(data: dict[str, Any], key: str, label: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise WorkflowError(f"Manjka obvezni podatek: {label}.")
    return value


def _money(data: dict[str, Any], key: str, label: str) -> float:
    raw = str(data.get(key, "0") or "0").strip().replace(",", ".")
    try:
        value = round(float(raw), 2)
    except ValueError as exc:
        raise WorkflowError(f"{label} mora biti številka.") from exc
    if value < 0:
        raise WorkflowError(f"{label} ne sme biti negativna.")
    return value


def _next_number(connection, table: str, column: str, prefix: str) -> str:
    year = datetime.now().year
    base = f"{prefix}-{year}-"
    rows = connection.execute(
        f"SELECT {column} FROM {table} WHERE {column} LIKE ?",
        (f"{base}%",),
    ).fetchall()
    highest = 0
    for (value,) in rows:
        try:
            highest = max(highest, int(str(value).rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            continue
    return f"{base}{highest + 1:04d}"


def validate_service_order(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    cleaned["customer_name"] = _required(data, "customer_name", "ime stranke")
    cleaned["device_model"] = _required(data, "device_model", "model naprave")
    cleaned["device_serial"] = _required(data, "device_serial", "serijska številka")
    cleaned["customer_request"] = _required(data, "customer_request", "zahteva stranke")
    phone = str(data.get("customer_phone") or "").strip()
    email = str(data.get("customer_email") or "").strip()
    if not phone and not email:
        raise WorkflowError("Vpiši vsaj telefon ali e-pošto stranke.")
    identity_method = str(data.get("identity_method") or "MICRODETECT").strip().upper()
    if identity_method not in {"MICRODETECT", "MANUAL_OTHER"}:
        raise WorkflowError("Neveljaven način identifikacije naprave.")
    if identity_method == "MICRODETECT" and not bool(data.get("microdetect_confirmed")):
        raise WorkflowError("Apple mobilna naprava zahteva uspešen MicroDETECT.")
    if identity_method == "MANUAL_OTHER" and not bool(data.get("manual_identity_confirmed")):
        raise WorkflowError("Potrdi fizični prepis identifikacije druge naprave.")
    cleaned["identity_method"] = identity_method
    cleaned["device_type"] = str(data.get("device_type") or "DRUGO").strip().upper()
    cleaned["quote_eur"] = _money(data, "quote_eur", "Potrjena ponudba")
    cleaned["logistics_eur"] = _money(data, "logistics_eur", "Logistika")
    context = _required(data, "contract_context", "kraj oziroma način sklenitve")
    if context not in {"MFD", "MFT_TEREN", "NA_DALJAVO"}:
        raise WorkflowError("Neveljaven način sklenitve servisnega naročila.")
    delivery = _required(data, "delivery_channel", "način izročitve PDF")
    if delivery not in {"EMAIL_PDF", "SMS_PDF", "PAPER"}:
        raise WorkflowError("Neveljaven način izročitve dokumenta.")
    if delivery == "EMAIL_PDF" and not email:
        raise WorkflowError("Za izročitev po e-pošti manjka e-poštni naslov.")
    if delivery == "SMS_PDF" and not phone:
        raise WorkflowError("Za SMS povezavo manjka telefonska številka.")
    cleaned["contract_context"] = context
    cleaned["delivery_channel"] = delivery
    return cleaned


def create_service_order(
    connection, data: dict[str, Any], *, commit: bool = True
) -> tuple[int, str]:
    data = validate_service_order(data)
    existing = connection.execute(
        """
        SELECT order_no FROM service_orders
        WHERE status='OPEN' AND (
            device_serial=? OR (?<>'' AND device_imei=?)
        )
        ORDER BY id DESC LIMIT 1
        """,
        (
            data["device_serial"],
            str(data.get("device_imei") or "").strip(),
            str(data.get("device_imei") or "").strip(),
        ),
    ).fetchone()
    if existing:
        raise WorkflowError(
            f"Za to napravo že obstaja odprto servisno naročilo {existing[0]}."
        )
    order_no = _next_number(connection, "service_orders", "order_no", "MF-SN")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = connection.execute(
        """
        INSERT INTO service_orders(
            order_no, created_at, status,
            customer_name, customer_address, customer_phone, customer_email,
            device_model, device_serial, device_imei, device_os, device_storage,
            device_type, identity_method,
            customer_request, intake_condition, accessories,
            quote_eur, logistics_eur, expected_due, contract_context,
            immediate_start_requested, withdrawal_loss_ack, delivery_channel,
            microdetect_confirmed, microdetect_at, operator_code, notes
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            order_no, now, "OPEN",
            data["customer_name"], str(data.get("customer_address") or "").strip(),
            str(data.get("customer_phone") or "").strip(),
            str(data.get("customer_email") or "").strip(),
            data["device_model"], data["device_serial"],
            str(data.get("device_imei") or "").strip(),
            str(data.get("device_os") or "").strip(),
            str(data.get("device_storage") or "").strip(),
            data["device_type"], data["identity_method"],
            data["customer_request"], str(data.get("intake_condition") or "").strip(),
            str(data.get("accessories") or "").strip(),
            data["quote_eur"], data["logistics_eur"],
            str(data.get("expected_due") or "").strip(), data["contract_context"],
            int(bool(data.get("immediate_start_requested"))),
            int(bool(data.get("withdrawal_loss_ack"))), data["delivery_channel"],
            int(bool(data.get("microdetect_confirmed"))),
            str(data.get("microdetect_at") or now),
            str(data.get("operator_code") or "DR").strip(),
            str(data.get("notes") or "").strip(),
        ),
    )
    if commit:
        connection.commit()
    return int(cursor.lastrowid), order_no


def get_service_order(connection, order_id: int):
    row = connection.execute(
        "SELECT * FROM service_orders WHERE id=?", (order_id,)
    ).fetchone()
    if not row:
        raise WorkflowError("Servisnega naročila ni mogoče najti.")
    columns = [item[0] for item in connection.execute(
        "SELECT * FROM service_orders LIMIT 0"
    ).description]
    return dict(zip(columns, row))


def latest_open_service_order(connection, serial: str, imei: str = ""):
    row = connection.execute(
        """
        SELECT id FROM service_orders
        WHERE status='OPEN' AND (
            device_serial=? OR (?<>'' AND device_imei=?)
        )
        ORDER BY id DESC LIMIT 1
        """,
        (serial, imei, imei),
    ).fetchone()
    return int(row[0]) if row else None


def complete_service_order(
    connection, order_id: int, service_id: int, *, commit: bool = True
) -> None:
    cursor = connection.execute(
        """
        UPDATE service_orders
        SET status='COMPLETED', completed_at=?, service_id=?
        WHERE id=? AND status='OPEN'
        """,
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), service_id, order_id),
    )
    if cursor.rowcount != 1:
        raise WorkflowError("Odprtega servisnega naročila ni mogoče zaključiti.")
    if commit:
        connection.commit()


def link_service_result(
    connection, order_id: int, service_id: int, *, commit: bool = True
) -> None:
    """Poveže izvedeni servis, vendar nalog do izdanega računa ostane v obdelavi."""
    cursor = connection.execute(
        """
        UPDATE service_orders SET service_id=?
        WHERE id=? AND status='OPEN'
        """,
        (int(service_id), int(order_id)),
    )
    if cursor.rowcount != 1:
        raise WorkflowError("Servisnega naročila ni mogoče povezati z izvedenim servisom.")
    if commit:
        connection.commit()


def begin_service_buyback_offer(
    connection, order_id: int, *, operator_code: str = "DR", commit: bool = True
) -> None:
    """Začasno zaklene odprt servis, dokler stranka odloča o odkupu."""
    ensure_workflow_schema(connection)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cursor = connection.execute(
        """
        UPDATE service_orders
        SET previous_status=status,status='ODKUP_V_POGOVORU',conversion_started_at=?
        WHERE id=? AND status='OPEN' AND buyback_id IS NULL
        """,
        (now, int(order_id)),
    )
    if cursor.rowcount != 1:
        existing = connection.execute(
            "SELECT status FROM service_orders WHERE id=?", (int(order_id),)
        ).fetchone()
        if not existing or existing[0] != "ODKUP_V_POGOVORU":
            raise WorkflowError("Samo odprt servis je mogoče ponuditi za odkup.")
    if commit:
        connection.commit()


def cancel_service_buyback_offer(
    connection, order_id: int, *, commit: bool = True
) -> None:
    """Zavrnjena ponudba vrne servis v prejšnje stanje brez knjiženja."""
    ensure_workflow_schema(connection)
    cursor = connection.execute(
        """
        UPDATE service_orders
        SET status=COALESCE(NULLIF(previous_status,''),'OPEN'),previous_status=NULL,
            conversion_started_at=NULL
        WHERE id=? AND status='ODKUP_V_POGOVORU' AND buyback_id IS NULL
        """,
        (int(order_id),),
    )
    if cursor.rowcount != 1:
        raise WorkflowError("Ponudbe za odkup ni mogoče preklicati.")
    if commit:
        connection.commit()


def convert_service_order_to_buyback(
    connection,
    order_id: int,
    buyback_data: dict[str, Any],
    *,
    test_mode: bool,
    operator_code: str = "DR",
    installed_parts_cost: float = 0,
    commercial_internal_labor_eur: float = 0,
    operation_id: str | None = None,
    commit: bool = True,
) -> tuple[int, str]:
    """Podpisano ponudbo v eni transakciji spremeni v odkup.

    Že vgrajeni deli ostanejo porabljeni in povezani z izvirnim servisom. Njihov
    strošek se samo zapiše v interno komercialno sled odkupa. Lastnikovo delo
    se računovodsko ne kapitalizira samodejno.
    """
    ensure_workflow_schema(connection)
    order = connection.execute(
        "SELECT * FROM service_orders WHERE id=?", (int(order_id),)
    ).fetchone()
    if not order:
        raise WorkflowError("Servisnega naročila ni mogoče najti.")
    names = [item[0] for item in connection.execute(
        "SELECT * FROM service_orders LIMIT 0"
    ).description]
    order = dict(zip(names, order))
    if order["status"] == "PREUSMERJENO_V_ODKUP" and order.get("buyback_id"):
        existing = connection.execute(
            "SELECT buyback_no FROM buybacks WHERE id=?", (int(order["buyback_id"]),)
        ).fetchone()
        if existing:
            return int(order["buyback_id"]), str(existing[0])
    if order["status"] != "ODKUP_V_POGOVORU":
        raise WorkflowError("Servis mora biti najprej označen kot odkup v pogovoru.")

    service_id = int(order["service_id"]) if order.get("service_id") else None
    if service_id:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(service)")}
        if "invoice_no" in columns and "office_invoice_id" in columns:
            invoiced = connection.execute(
                "SELECT invoice_no,office_invoice_id FROM service WHERE id=?",
                (service_id,),
            ).fetchone()
            if not invoiced:
                raise WorkflowError("Povezanega servisnega zapisa ni mogoče najti.")
            if str(invoiced[0] or "").strip() or invoiced[1] is not None:
                raise WorkflowError(
                    "Servis z izdanim računom ni mogoče neposredno spremeniti v odkup. "
                    "Najprej je potreben celoten storno računa."
                )

    op_id = str(operation_id or f"SERVICE-TO-BUYBACK-{int(order_id)}")
    payload = {
        "service_order_id": int(order_id),
        "service_id": service_id,
        "seller_tax_number": str(buyback_data.get("seller_tax_number") or ""),
        "device_serial": str(buyback_data.get("device_serial") or ""),
        "device_imei": str(buyback_data.get("device_imei") or ""),
        "purchase_price": str(buyback_data.get("purchase_price") or ""),
        "payout_method": str(buyback_data.get("payout_method") or ""),
        "installed_parts_cost": round(float(installed_parts_cost or 0), 2),
        "commercial_internal_labor_eur": round(
            float(commercial_internal_labor_eur or 0), 2
        ),
    }
    prepared = prepare_operation(
        connection,
        operation_type="SERVICE_TO_BUYBACK",
        aggregate_type="service_order",
        aggregate_id=int(order_id),
        payload=payload,
        operator_code=operator_code,
        operation_id=op_id,
    )
    if prepared["status"] == "COMPLETED":
        result = json.loads(str(prepared["result_json"] or "{}"))
        return int(result["buyback_id"]), str(result["buyback_no"])
    start_operation(connection, op_id)
    buyback_id, buyback_no = create_buyback(
        connection, buyback_data, test_mode=test_mode, commit=False
    )
    connection.execute(
        """
        UPDATE buybacks
        SET source_service_order_id=?,source_service_id=?,conversion_operation_id=?,
            installed_parts_cost=?,commercial_internal_labor_eur=?,
            accounting_owner_labor_eur=0
        WHERE id=?
        """,
        (
            int(order_id), service_id, op_id, round(float(installed_parts_cost or 0), 2),
            round(float(commercial_internal_labor_eur or 0), 2), int(buyback_id),
        ),
    )
    cursor = connection.execute(
        """
        UPDATE service_orders
        SET status='PREUSMERJENO_V_ODKUP',buyback_id=?,conversion_operation_id=?,
            converted_at=?,completed_at=COALESCE(completed_at,?)
        WHERE id=? AND status='ODKUP_V_POGOVORU'
        """,
        (
            int(buyback_id), op_id,
            datetime.now().astimezone().isoformat(timespec="seconds"),
            datetime.now().astimezone().isoformat(timespec="seconds"), int(order_id),
        ),
    )
    if cursor.rowcount != 1:
        raise WorkflowError("Servisa ni bilo mogoče enolično preusmeriti v odkup.")
    if service_id:
        service_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(service)")
        }
        if "resolution_status" not in service_columns:
            connection.execute(
                "ALTER TABLE service ADD COLUMN resolution_status TEXT NOT NULL DEFAULT 'AWAITING_INVOICE'"
            )
        if "converted_buyback_id" not in service_columns:
            connection.execute("ALTER TABLE service ADD COLUMN converted_buyback_id INTEGER")
        connection.execute(
            """
            UPDATE service SET resolution_status='CONVERTED_TO_BUYBACK',converted_buyback_id=?
            WHERE id=?
            """,
            (int(buyback_id), service_id),
        )
    complete_operation(
        connection,
        op_id,
        result={"buyback_id": int(buyback_id), "buyback_no": buyback_no},
        event_type="SERVICE_CONVERTED_TO_BUYBACK",
    )
    if commit:
        connection.commit()
    return buyback_id, buyback_no


def validate_buyback(data: dict[str, Any], *, test_mode: bool) -> dict[str, Any]:
    cleaned = dict(data)
    for key, label in (
        ("seller_name", "ime prodajalca"),
        ("seller_tax_number", "davčna številka prodajalca"),
        ("seller_address", "naslov prodajalca"),
        ("device_model", "model naprave"),
        ("device_serial", "serijska številka"),
        ("device_imei", "IMEI"),
        ("condition_text", "opis stanja"),
        ("acquisition_method", "način pridobitve"),
    ):
        cleaned[key] = _required(data, key, label)
    seller_tax_number = cleaned["seller_tax_number"].replace(" ", "").upper()
    if seller_tax_number.startswith("SI"):
        seller_tax_number = seller_tax_number[2:]
    if len(seller_tax_number) != 8 or not seller_tax_number.isdigit():
        raise WorkflowError("Davčna številka prodajalca mora imeti 8 številk.")
    cleaned["seller_tax_number"] = seller_tax_number
    for key, label in (
        ("microdetect_confirmed", "MicroDETECT"),
        ("identity_checked", "vpogled v osebni dokument"),
        ("account_removed", "odstranjen uporabniški račun"),
        ("activation_lock_removed", "odstranjen aktivacijski zaklep"),
        ("mdm_clear", "odsotnost MDM"),
        ("ownership_declaration", "izjava o lastništvu"),
        ("privacy_notice_received", "obvestilo o zasebnosti"),
    ):
        if not bool(data.get(key)):
            raise WorkflowError(f"Pred odkupom mora biti potrjeno: {label}.")
    risk = _required(data, "risk_level", "stopnja tveganja").upper()
    if risk not in {"GREEN", "ORANGE", "RED"}:
        raise WorkflowError("Neveljavna stopnja tveganja odkupa.")
    if risk != "GREEN":
        raise WorkflowError("Odkup se lahko zaključi samo z zeleno odločitvijo.")
    payout = _required(data, "payout_method", "način izplačila").upper()
    if payout not in PAYOUT_LABELS:
        raise WorkflowError("Neveljaven način izplačila odkupa.")
    purchase_price = _money(data, "purchase_price", "Odkupna cena")
    if purchase_price <= 0:
        raise WorkflowError("Odkupna cena mora biti večja od 0,00 EUR.")
    seller_iban = str(data.get("seller_iban") or "").replace(" ", "").upper()
    if payout == "BANK_TRANSFER" and not seller_iban:
        raise WorkflowError("Pri bančnem nakazilu manjka IBAN prodajalca.")
    if payout == "CASH" and purchase_price > CASH_BUYBACK_LIMIT_EUR:
        raise WorkflowError(
            "Gotovinski odkup je dovoljen samo do vključno 420,00 EUR. "
            "Za višji znesek izberi bančno nakazilo; delitev enega odkupa ni dovoljena."
        )
    cleaned["seller_iban"] = seller_iban
    cleaned["purchase_price"] = purchase_price
    cleaned["risk_level"] = risk
    cleaned["imei_check_provider"] = ""
    cleaned["imei_check_result"] = "NOT_REQUIRED"
    cleaned["imei_check_reference"] = ""
    cleaned["payout_method"] = payout
    return cleaned


def create_buyback(
    connection, data: dict[str, Any], *, test_mode: bool, commit: bool = True
) -> tuple[int, str]:
    data = validate_buyback(data, test_mode=test_mode)
    buyback_no = _next_number(connection, "buybacks", "buyback_no", "MF-ODK")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = connection.execute(
        """
        INSERT INTO buybacks(
            buyback_no, created_at, status,
            seller_name, seller_tax_number, seller_birth_date,
            seller_address, seller_contact, seller_iban,
            identity_checked, device_model, device_serial, device_imei, device_os,
            device_storage,
            color_capacity, accessories, condition_text,
            acquisition_method, acquisition_details, evidence_type,
            imei_check_provider, imei_check_result, imei_check_reference, imei_checked_at,
            account_removed, activation_lock_removed, mdm_clear,
            ownership_declaration, privacy_notice_received,
            risk_level, risk_reason, purchase_price, payout_method,
            operator_code, microdetect_confirmed, microdetect_at, notes
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            buyback_no, now, "CONTRACT_READY",
            data["seller_name"], data["seller_tax_number"], "", data["seller_address"],
            str(data.get("seller_contact") or "").strip(), data["seller_iban"],
            1, data["device_model"], data["device_serial"], data["device_imei"],
            str(data.get("device_os") or "").strip(),
            str(data.get("device_storage") or "").strip(),
            str(data.get("color_capacity") or "").strip(),
            str(data.get("accessories") or "").strip(), data["condition_text"],
            data["acquisition_method"], str(data.get("acquisition_details") or "").strip(),
            str(data.get("evidence_type") or "").strip(),
            data["imei_check_provider"], data["imei_check_result"],
            data["imei_check_reference"], str(data.get("imei_checked_at") or now),
            1, 1, 1, 1, 1, data["risk_level"],
            str(data.get("risk_reason") or "").strip(), data["purchase_price"],
            data["payout_method"], str(data.get("operator_code") or "DR").strip(),
            1, str(data.get("microdetect_at") or now),
            str(data.get("notes") or "").strip(),
        ),
    )
    buyback_id = int(cursor.lastrowid)
    if data["payout_method"] == "CASH":
        voucher_no = _next_number(
            connection, "cash_disbursements", "voucher_no", "BI"
        )
        connection.execute(
            """
            INSERT INTO cash_disbursements(
                voucher_no, buyback_id, buyback_no, created_at, status,
                recipient_name, recipient_address, purpose, amount, operator_code
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                voucher_no, buyback_id, buyback_no, now, "PREPARED",
                data["seller_name"], data["seller_address"],
                f"Gotovinski odkup rabljene naprave po pogodbi {buyback_no}",
                data["purchase_price"],
                str(data.get("operator_code") or "DR").strip(),
            ),
        )
    if commit:
        connection.commit()
    return buyback_id, buyback_no


def get_buyback(connection, buyback_id: int):
    row = connection.execute("SELECT * FROM buybacks WHERE id=?", (buyback_id,)).fetchone()
    if not row:
        raise WorkflowError("Odkupnega zapisa ni mogoče najti.")
    columns = [item[0] for item in connection.execute(
        "SELECT * FROM buybacks LIMIT 0"
    ).description]
    record = dict(zip(columns, row))
    voucher = connection.execute(
        """
        SELECT id, voucher_no, created_at, confirmed_at, status, amount
        FROM cash_disbursements WHERE buyback_id=?
        """,
        (buyback_id,),
    ).fetchone()
    if voucher:
        for key, value in zip(
            (
                "cash_disbursement_id", "cash_disbursement_no",
                "cash_disbursement_created_at", "cash_disbursement_confirmed_at",
                "cash_disbursement_status", "cash_disbursement_amount",
            ),
            voucher,
        ):
            record[key] = value
    return record


def complete_buyback(
    connection, buyback_id: int, *, commit: bool = True
) -> dict[str, Any]:
    """Atomsko potrdi podpis in izplačilo; šele nato je odkup dokončan."""
    record = get_buyback(connection, buyback_id)
    if record["status"] == "COMPLETED":
        return record
    if record["status"] != "CONTRACT_READY":
        raise WorkflowError("Zaključiti je mogoče samo pripravljen odkupni dokument.")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if record["payout_method"] == "CASH":
        if not record.get("cash_disbursement_id"):
            raise WorkflowError("Gotovinskemu odkupu manjka blagajniški izdatek.")
        connection.execute(
            """
            UPDATE cash_disbursements
            SET status='CONFIRMED', confirmed_at=?
            WHERE buyback_id=? AND status='PREPARED'
            """,
            (now, buyback_id),
        )
    cursor = connection.execute(
        """
        UPDATE buybacks
        SET status='COMPLETED', payment_status='PAID', handover_at=?
        WHERE id=? AND status='CONTRACT_READY'
        """,
        (now, buyback_id),
    )
    if cursor.rowcount != 1:
        raise WorkflowError("Odkupa ni bilo mogoče dokončno potrditi.")
    if commit:
        connection.commit()
    return get_buyback(connection, buyback_id)


def _pdf_text(value: Any) -> str:
    return escape(str(value or "-")).replace("\n", "<br/>")


def _register_pdf_fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pairs = (
        (Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\segoeuib.ttf")),
        (Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    )
    for regular, bold in pairs:
        if regular.is_file() and bold.is_file():
            try:
                pdfmetrics.registerFont(TTFont("MFWorkflow", str(regular)))
                pdfmetrics.registerFont(TTFont("MFWorkflowBold", str(bold)))
                return "MFWorkflow", "MFWorkflowBold"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold"


def _pdf_context():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    regular_font, bold_font = _register_pdf_fonts()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "MFBody", parent=styles["BodyText"], fontName=regular_font,
        fontSize=8.6, leading=11.3, textColor=colors.HexColor("#2E3439"),
        spaceAfter=4,
    )
    title = ParagraphStyle(
        "MFTitle", parent=body, fontName=bold_font, fontSize=16,
        leading=19, alignment=1, textColor=colors.HexColor("#23679F"),
        spaceAfter=8,
    )
    h2 = ParagraphStyle(
        "MFH2", parent=body, fontName=bold_font, fontSize=10.5,
        leading=13, textColor=colors.HexColor("#2E3439"), spaceBefore=6,
        spaceAfter=4,
    )
    small = ParagraphStyle(
        "MFSmall", parent=body, fontSize=7.4, leading=9.5,
        textColor=colors.HexColor("#606A73"),
    )
    warning = ParagraphStyle(
        "MFWarning", parent=body, fontName=bold_font, fontSize=9,
        leading=11, alignment=1, textColor=colors.HexColor("#A63A3A"),
    )
    return body, title, h2, small, warning


def _header(story, title_text: str, number: str, version: str, test_mode: bool):
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    body, title, _h2, small, _warning = _pdf_context()
    visible_title = f"TEST - {title_text}" if test_mode else title_text
    blocks = [
        Paragraph("<b>MikroFIX</b> - popravilo elektronskih in telekomunikacijskih naprav", small),
        Paragraph(_pdf_text(visible_title), title),
        Paragraph(f"<b>{_pdf_text(number)}</b> | različica {_pdf_text(version)}", body),
    ]
    blocks.append(Spacer(1, 3 * mm))
    story.append(KeepTogether(blocks))


def _kv_table(rows, body):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph(f"<b>{_pdf_text(k)}</b>", body), Paragraph(_pdf_text(v), body)] for k, v in rows]
    table = Table(data, colWidths=[48 * mm, 119 * mm], repeatRows=0)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8DEE3")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F4F6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
    ]))
    return table


def _signature_table(body, left: str, right: str):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    table = Table([
        [Paragraph(f"<b>{_pdf_text(left)}</b>", body), Paragraph(f"<b>{_pdf_text(right)}</b>", body)],
        [Paragraph("Podpis: ____________________________", body), Paragraph("Podpis: ____________________________", body)],
        [Paragraph("Datum in ura: ______________________", body), Paragraph("Datum in ura: ______________________", body)],
    ], colWidths=[83.5 * mm, 83.5 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _footer(canvas, doc, number: str, version: str):
    from reportlab.lib import colors
    from reportlab.lib.units import mm

    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D8DEE3"))
    canvas.line(16 * mm, 12 * mm, 194 * mm, 12 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#606A73"))
    canvas.drawString(16 * mm, 8 * mm, f"MikroFIX | {number} | {version}")
    canvas.drawRightString(194 * mm, 8 * mm, f"Stran {doc.page}")
    canvas.restoreState()


def generate_service_order_pdf(
    record: dict[str, Any], output_dir: Path, *, test_mode: bool
) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record['order_no']}_servisno_narocilo_TEST.pdf"
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=17 * mm,
    )
    body, _title, h2, small, _warning = _pdf_context()
    story = []
    _header(story, "SERVISNO NAROČILO IN PREVZEM NAPRAVE", record["order_no"], SERVICE_ORDER_VERSION, test_mode)
    story.append(Paragraph("Izvajalec", h2))
    story.append(Paragraph(
        "MikroFix, popravilo elektronskih in telekomunikacijskih naprav, Ristoski Daniel s.p.<br/>"
        "Paradiž 73, 2282 Cirkulane | matična 7565178000 | davčna 84514841<br/>"
        "+386 40 353 402 | mikrofixsi@gmail.com | mikrofix.si", body,
    ))
    story.append(Paragraph("Stranka in način sklenitve", h2))
    story.append(_kv_table([
        ("Datum in ura", record["created_at"]),
        ("Ime in priimek", record["customer_name"]),
        ("Naslov", record.get("customer_address")),
        ("Telefon", record.get("customer_phone")),
        ("E-pošta", record.get("customer_email")),
        ("Način sklenitve", _label(CONTRACT_CONTEXT_LABELS, record["contract_context"])),
        ("Izročitev dokumenta", _label(DELIVERY_LABELS, record["delivery_channel"])),
    ], body))
    identity_is_microdetect = record.get("identity_method") == "MICRODETECT"
    story.append(Paragraph(
        "Naprava - podatki neposredno iz MicroDETECT"
        if identity_is_microdetect
        else "Naprava - preverjeni ali interno označeni podatki",
        h2,
    ))
    device_rows = [
        ("Vrsta naprave", _label({
            "APPLE_MOBILE": "Apple mobilna naprava",
            "TELEFON": "Telefon",
            "TABLICA": "Tablica",
            "PRENOSNIK": "Prenosnik",
            "NAMIZNI_PC": "Namizni računalnik",
            "KONZOLA": "Igralna konzola",
            "DRUGO": "Druga elektronska naprava",
        }, record.get("device_type"))),
        ("Model", record["device_model"]),
        ("Serijska številka", record["device_serial"]),
    ]
    for label, value in (
        ("IMEI", record.get("device_imei")),
        ("Operacijski sistem", record.get("device_os")),
        ("Pomnilnik", record.get("device_storage")),
    ):
        if str(value or "").strip():
            device_rows.append((label, value))
    device_rows.extend([
        (
            "Identifikacija",
            f"MicroDETECT - neposredno USB branje ob {record['microdetect_at']}"
            if identity_is_microdetect
            else f"Fizični prepis ali interna prevzemna oznaka ob {record['microdetect_at']}",
        ),
        ("Priložena oprema", record.get("accessories")),
    ])
    story.append(_kv_table(device_rows, body))
    story.append(Paragraph("Zahteva in vhodno stanje", h2))
    story.append(_kv_table([
        ("Zahteva stranke", record["customer_request"]),
        ("Vidno stanje", record.get("intake_condition")),
        ("Opombe", record.get("notes")),
    ], body))
    story.append(Paragraph("Cena in izvedba", h2))
    story.append(_kv_table([
        ("Potrjena ponudba / limit", f"{float(record['quote_eur']):.2f} EUR"),
        ("Logistika", f"{float(record['logistics_eur']):.2f} EUR"),
        ("Predvideni rok", record.get("expected_due")),
        ("Diagnostika", "30.00 EUR, če popravilo ni izvedeno; ob izvedbi se všteje"),
    ], body))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Brez novega pisnega ali elektronskega soglasja MikroFIX potrjenega cenovnega limita ne sme preseči. "
        "PIN, geslo in biometrični podatki se ne zbirajo. Napravo odklene stranka sama samo v potrebnem obsegu.", body,
    ))
    story.append(Paragraph(
        f"[{'X' if record.get('immediate_start_requested') else ' '}] Izrecna zahteva za začetek pred iztekom odstopnega roka.   "
        f"[{'X' if record.get('withdrawal_loss_ack') else ' '}] Seznanitev s prenehanjem pravice po popolni izvedbi, kadar so izpolnjeni pogoji.", body,
    ))
    story.append(Spacer(1, 4 * mm))
    story.append(_signature_table(body, "Stranka", "MikroFIX / Daniel Ristoski"))

    story.append(PageBreak())
    _header(story, "POGOJI SERVISA IN IZROČITEV", record["order_no"], SERVICE_ORDER_VERSION, test_mode)
    for heading, text in (
        ("Odpiranje, tesnilo in vodoodpornost", "Tesnilo se praviloma zamenja ob vsakem odpiranju naprave. Brez dokumentiranega tlačnega testa MikroFIX ne zagotavlja vodoodpornosti."),
        ("Vgrajeni deli", "Na evidentirane dele velja 3-mesečna prostovoljna tržna garancija MikroFIX. Za del, ki ga zagotovi stranka, MikroFIX ne daje garancije na del; odgovornost za pravilnost svojega dela ostane."),
        ("Garancijska rešitev", "Upravičen zahtevek se reši s popravilom ali zamenjavo; če to ni mogoče, se vrne cena dela. Za zamenjani del začne teči nov 3-mesečni rok. Zakonske pravice ostanejo nedotaknjene."),
        ("Hramba", "Neprevzeta naprava se brezplačno hrani 60 dni. Po dokumentiranem opozorilu se zaračuna 1.00 EUR na dan. Lastništvo nikoli ne preide samodejno na MikroFIX."),
        ("Elektronski dokument", "Celoten PDF se izroči po e-pošti. Če e-pošte ni, je SMS dovoljen samo kot povezava do nespremenljivega PDF, ki ga je mogoče prenesti in shraniti; sicer se izroči papir."),
        ("Varstvo podatkov", "MikroFIX dostopa samo do podatkov, ki so nujni za identifikacijo, diagnostiko, popravilo in dogovorjeno testiranje. Zasebne vsebine se brez nujnega razloga ne pregledujejo."),
    ):
        story.append(Paragraph(heading, h2))
        story.append(Paragraph(text, body))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph("Zaključek servisa se evidentira na ločenem končnem servisnem listu, povezanem s tem naročilom.", small))

    story.append(PageBreak())
    _header(story, "POUK O ODSTOPU - STORITEV", record["order_no"], SERVICE_ORDER_VERSION, test_mode)
    story.append(Paragraph("Kdaj se uporabi", h2))
    story.append(Paragraph(
        "Pri pogodbi, sklenjeni na daljavo ali zunaj poslovnih prostorov. Pri storitvi začne 14-dnevni rok teči z dnem sklenitve. Pri pogodbi v poslovnih prostorih se ta stran uporabi samo, če posebno pravilo to zahteva.", body,
    ))
    story.append(Paragraph("Začetek pred potekom roka", h2))
    story.append(Paragraph(
        "Če potrošnik izrecno zahteva začetek storitve in nato odstopi, plača sorazmeren del že opravljene storitve. Po popolni izvedbi storitve pravica do odstopa preneha le ob izpolnjenih zakonskih pogojih in predhodni seznanitvi.", body,
    ))
    story.append(Paragraph("Vzorčni obrazec", h2))
    story.append(Paragraph(
        "Prejemnik: MikroFIX, Paradiž 73, 2282 Cirkulane | mikrofixsi@gmail.com<br/><br/>"
        "Obveščam vas, da odstopam od pogodbe za naslednjo storitev:<br/><br/>"
        "________________________________________________________________________________<br/><br/>"
        "Ime in priimek: ______________________________________<br/><br/>"
        "Naslov: ______________________________________________<br/><br/>"
        "Datum: ____________________   Podpis pri papirnem obrazcu: __________________________", body,
    ))
    doc.build(
        story,
        onFirstPage=lambda c, d: _footer(c, d, record["order_no"], SERVICE_ORDER_VERSION),
        onLaterPages=lambda c, d: _footer(c, d, record["order_no"], SERVICE_ORDER_VERSION),
    )
    return path


def generate_buyback_pdf(
    record: dict[str, Any], output_dir: Path, *, test_mode: bool
) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record['buyback_no']}_odkupna_pogodba_TEST.pdf"
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=17 * mm,
    )
    body, _title, h2, small, _warning = _pdf_context()
    story = []
    _header(story, "ODKUPNA POGODBA ZA RABLJENO NAPRAVO", record["buyback_no"], BUYBACK_VERSION, test_mode)
    story.append(Paragraph("Kupec", h2))
    story.append(Paragraph(
        "MikroFix, popravilo elektronskih in telekomunikacijskih naprav, Ristoski Daniel s.p.<br/>"
        "Paradiž 73, 2282 Cirkulane | matična 7565178000 | davčna 84514841", body,
    ))
    story.append(Paragraph("Prodajalec", h2))
    seller_rows = [
        ("Ime in priimek", record["seller_name"]),
        ("Davčna številka", record.get("seller_tax_number")),
        ("Naslov", record["seller_address"]),
        ("Kontakt", record.get("seller_contact")),
    ]
    if record["payout_method"] == "BANK_TRANSFER":
        seller_rows.append(("IBAN", record["seller_iban"]))
    seller_rows.append(
        ("Identiteta", "Preverjena z vpogledom v veljaven dokument; kopija ni izdelana")
    )
    story.append(_kv_table(seller_rows, body))
    story.append(Paragraph("Naprava - samodejno iz MicroDETECT", h2))
    story.append(_kv_table([
        ("Model", record["device_model"]),
        ("Serijska številka", record["device_serial"]),
        ("IMEI", record["device_imei"]),
        ("Operacijski sistem", record.get("device_os")),
        ("Pomnilnik", record.get("device_storage")),
        ("Barva", record.get("color_capacity")),
        ("Oprema", record.get("accessories")),
        ("Stanje", record["condition_text"]),
    ], body))
    story.append(Paragraph("Izvor, cena in plačilo", h2))
    story.append(_kv_table([
        ("Način pridobitve", _label(ACQUISITION_LABELS, record["acquisition_method"])),
        ("Podrobnosti", record.get("acquisition_details")),
        ("Podporno dokazilo", _label(EVIDENCE_LABELS, record.get("evidence_type"))),
        ("Kupnina", f"{float(record['purchase_price']):.2f} EUR"),
        ("Plačilo", _label(PAYOUT_LABELS, record["payout_method"])),
        ("Stanje izplačila", "POTRJENO" if record.get("payment_status") == "PAID" else "ČAKA NA PODPIS IN POTRDITEV IZPLAČILA"),
    ], body))
    story.append(Paragraph("Izjava prodajalca", h2))
    story.append(Paragraph(
        "Prodajalec izjavlja, da je lastnik naprave in sme z njo razpolagati; da naprava ni ukradena, izgubljena, najeta, službena, zastavljena ali obremenjena s pravicami tretjih; da so podatki resnični; da so uporabniški račun, aktivacijski zaklep in MDM odstranjeni. Neresnična izjava ne ustvari lastništva in povzroči odgovornost po zakonu.", body,
    ))
    story.append(Spacer(1, 4 * mm))
    story.append(_signature_table(body, "Prodajalec", "MikroFIX / Daniel Ristoski"))

    story.append(PageBreak())
    _header(story, "ZAPISNIK PREGLEDA IN PREVZEMA", record["buyback_no"], BUYBACK_VERSION, test_mode)
    story.append(_kv_table([
        ("MicroDETECT", f"USPEŠEN ob {record['microdetect_at']}"),
        ("Uporabniški račun", "ODSTRANJEN"),
        ("Aktivacijski zaklep", "ODSTRANJEN"),
        ("MDM", "NI PRISOTEN"),
        ("Stopnja tveganja", _label(RISK_LABELS, record["risk_level"])),
        ("Razlog / opombe", record.get("risk_reason") or record.get("notes")),
    ], body))
    story.append(Paragraph("Minimalni funkcionalni pregled", h2))
    story.append(Paragraph(
        "[ ] naprava se vključi in zaključi osnovni zagon<br/>"
        "[ ] zaslon in dotik delujeta<br/>"
        "[ ] polnjenje in podatkovni priključek sta preverjena<br/>"
        "[ ] kamere, mikrofon in zvočniki so preverjeni<br/>"
        "[ ] brezžične povezave so preverjene<br/>"
        "[ ] biometrične funkcije so preverjene, kjer je mogoče<br/>"
        "[ ] stanje baterije in vidne poškodbe so zapisani", body,
    ))
    story.append(Paragraph("Odločitev", h2))
    story.append(Paragraph(
        "ZELENO pomeni, da so identiteta naprave, odstranjene zaščite in izjava prodajalca skladni. Zunanje komercialno preverjanje IMEI ni izvedeno in rezultat uspešnega zagona ali klica ni dokaz lastništva. ORANŽNO zahteva Danielovo dokumentirano odobritev. RDEČE pomeni zavrnitev. Ta pogodba je ustvarjena samo za zeleno odločitev.", body,
    ))
    story.append(Spacer(1, 5 * mm))
    story.append(_signature_table(body, "Prodajalec potrjuje izročitev", "MikroFIX potrjuje prevzem"))

    if record["payout_method"] == "CASH":
        story.append(PageBreak())
        voucher_no = record.get("cash_disbursement_no") or "MANJKA"
        _header(
            story, "BLAGAJNIŠKI IZDATEK", voucher_no,
            BUYBACK_VERSION, test_mode,
        )
        story.append(_kv_table([
            ("Izplačevalec", "MikroFIX, Ristoski Daniel s.p., davčna 84514841"),
            ("Prejemnik", record["seller_name"]),
            ("Naslov prejemnika", record["seller_address"]),
            ("Povezana pogodba", record["buyback_no"]),
            ("Namen", f"Gotovinski odkup rabljene naprave {record['device_model']}"),
            ("Znesek", f"{float(record['purchase_price']):.2f} EUR"),
            ("Datum priprave", record.get("cash_disbursement_created_at")),
        ], body))
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph(
            "Prejemnik s podpisom potrjuje, da je navedeni znesek prejel v gotovini v celoti. Ta izdatek je neločljivo povezan z navedeno odkupno pogodbo in se ne sme uporabiti za razdelitev istega odkupa na več izplačil.",
            body,
        ))
        story.append(Spacer(1, 8 * mm))
        story.append(_signature_table(body, "Prejemnik gotovine", "Izplačal MikroFIX"))

    story.append(PageBreak())
    _header(story, "OBVESTILO O OBDELAVI OSEBNIH PODATKOV", record["buyback_no"], BUYBACK_VERSION, test_mode)
    for heading, text in (
        ("Upravljavec", "MikroFIX, Paradiž 73, 2282 Cirkulane; mikrofixsi@gmail.com; +386 40 353 402."),
        ("Podatki", "Ime, davčna številka, naslov, kontakt, po potrebi IBAN, podpis, izvor naprave, model, IMEI, serijska številka, stanje, pogodba, plačilo in preveritvena sled. Datuma rojstva, številke ali kopije osebnega dokumenta ne hranimo."),
        ("Namen in podlaga", "Sklenitev in izvedba odkupa, računovodstvo in davčne obveznosti, evidenca blaga, preprečevanje zlorab ter pravni zahtevki. Podlage so pogodba, pravne obveznosti in zakoniti interesi glede na namen."),
        ("Prejemniki", "Pooblaščene osebe MikroFIX, računovodstvo, banka, pogodbeni ponudniki podpore ter pristojni organi, kadar obstaja veljavna podlaga. Podatkov ne prodajamo."),
        ("Hramba", "Pogodbeni, plačilni in knjigovodski dokumenti se hranijo po obveznih rokih. Podatki neuspelega odkupa se praviloma izbrišejo v 30 dneh, razen ob dokumentiranem incidentu ali zahtevku."),
        ("Pravice", "Prodajalec lahko pod zakonskimi pogoji zahteva dostop, popravek, izbris, omejitev, prenosljivost ali ugovarja obdelavi. Pritožba je mogoča pri Informacijskem pooblaščencu Republike Slovenije."),
    ):
        story.append(Paragraph(heading, h2))
        story.append(Paragraph(text, body))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Prodajalec s podpisom pogodbe potrjuje, da mu je bilo to obvestilo dostopno pred podpisom. Potrditev ni privolitev in ne pomeni odpovedi pravicam.", small,
    ))
    doc.build(
        story,
        onFirstPage=lambda c, d: _footer(c, d, record["buyback_no"], BUYBACK_VERSION),
        onLaterPages=lambda c, d: _footer(c, d, record["buyback_no"], BUYBACK_VERSION),
    )
    return path

