import os
import sys
import json
import shutil
import sqlite3
import subprocess
import threading
import queue
import runpy
import time
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from datetime import datetime, timedelta

from mikrofix_tk_bootstrap import maximize_work_window, prepare_tcl_tk

prepare_tcl_tk()

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from mikrofix_auth import (
    AuthStore,
    PERMISSION_BUYBACK,
    PERMISSION_CASHIER,
    PERMISSION_MANAGE_USERS,
    PERMISSION_OFFICE,
    PERMISSION_SERVICE,
)
from mikrofix_user_ui import SessionAuthUI
from mikrofix_runtime import APP_VERSION, load_runtime
from mikrofix_policy import (
    WARRANTY_SCOPE_INSTALLED_PARTS,
    service_parts_warranty,
)
from mikrofix_supplier import SupplierCatalogStore
from mikrofix_device import (
    DeviceDetectionError,
    extract_device_identity,
    problem as device_problem,
    read_usb_iphone,
)
from mikrofix_workflows import (
    ACQUISITION_LABELS,
    CASH_BUYBACK_LIMIT_EUR,
    CONTRACT_CONTEXT_LABELS,
    DELIVERY_LABELS,
    EVIDENCE_LABELS,
    IMEI_RESULT_LABELS,
    PAYOUT_LABELS,
    RISK_LABELS,
    WorkflowError,
    create_buyback,
    create_service_order,
    begin_service_buyback_offer,
    cancel_service_buyback_offer,
    complete_buyback,
    ensure_workflow_schema,
    generate_buyback_pdf,
    generate_service_order_pdf,
    generate_service_tag_pdf,
    get_buyback,
    get_service_order,
    finish_service_work_timer,
    latest_open_service_order,
    list_running_service_work_sessions,
    link_service_result,
    load_service_order_parts,
    pause_service_work_timer,
    save_service_order_parts,
    service_work_timer_snapshot,
    start_service_work_timer,
)
from mikrofix_backup import backup_sqlite_file
from mikrofix_printing import (
    default_printer,
    list_printers,
    send_pdf_to_printer,
)
from mikrofix_platform import open_local_file, protect_private_path
from mikrofix_v29_core import (
    initialize_v29,
    register_buyback_device,
    sync_completed_buybacks_to_inventory,
)
from mikrofix_v30_lots import allocate_fifo, ensure_lot_schema
from mikrofix_v30_cash import (
    cash_session_snapshot,record_cash_entry,require_open_cash_session,
)
from mikrofix_invoice_bridge import load_service_invoice_seed
from mikrofix_ajpes import AjpesError, find_partner_by_tax_number
from mikrofix_v30_pos import (
    customer_partner_by_id, list_customer_partners, save_customer_partner,
    service_labor_from_minutes, service_tariff_snapshot,
)
from mikrofix_v30_pos_ui import open_pos
from mikrofix_transactions import complete_buyback_atomic
from mikrofix_operations import (
    list_recovery_operations,
    mark_stale_operations,
    verify_outbox_chain,
)
from mikrofix_backup_runtime import (
    BackupRuntimeError,
    create_due_external_backup,
    external_backup_status,
    save_backup_passphrase,
    update_backup_settings,
)
from mikrofix_data_encryption import DataEncryptionError, unmount_encrypted_data
from mikrofix_attachments import (
    ServiceImageError,
    archive_service_image,
    load_service_image,
    resolve_service_image_path,
)

APP_DIR = Path(__file__).resolve().parent
RUNTIME = load_runtime(APP_DIR)
DB = RUNTIME.db_path("service")
AUTH_DB = RUNTIME.db_path("auth")
INVENTORY_DB = RUNTIME.db_path("inventory")
OFFICE_DB = RUNTIME.db_path("office")
DATA_DIR = RUNTIME.service_data_dir
PHOTOS_DIR = DATA_DIR / "photos"
PDF_DIR = DATA_DIR / "pdf"
BACKUP_DIR = DATA_DIR / "backups"

for d in (DATA_DIR, PHOTOS_DIR, PDF_DIR, BACKUP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# MikroFix colors
BG = "#F2F4F6"
PANEL = "#FFFFFF"
BLUE = "#2F80C7"
BLUE_DARK = "#23679F"
GRAY = "#606A73"
DARK = "#2E3439"
LIGHT_GRAY = "#D8DEE3"
SUCCESS = "#2C7A4B"
WARNING = "#A26916"
DANGER = "#A63A3A"

ACTIVE_SCREEN = "LOGIN"
main_menu_frame = None
IDLE_TIMEOUT_SECONDS = 10 * 60
_last_user_activity = time.monotonic()
_idle_lock_running = False
external_backup_thread = None
external_backup_last_message = ""
external_backup_attempted = False

BUYBACK_PURPOSE_LABELS = {
    "UNDECIDED": "Namen še ni določen",
    "REFURBISH": "Obnova in prodaja",
    "PARTS": "Za rezervne dele",
}

# Podatki podjetja za servisne in reklamacijske izpise.
COMPANY_NAME = "MikroFIX"
COMPANY_ADDRESS = "Paradiž 73, 2282 Cirkulane"
COMPANY_PHONE = "+386 40 353 402"
COMPANY_EMAIL = "mikrofixsi@gmail.com"
COMPANY_WEB = "mikrofix.si"
FURS_ENABLED = bool(RUNTIME.settings["furs"].get("enabled", False))
ENVIRONMENT_NOTICE = (
    f"SANDBOX • NI PRODUKCIJA • FURS PRODUKCIJA IZKLOPLJENA • "
    f"{RUNTIME.active_premise}/{RUNTIME.electronic_device}"
    if RUNTIME.is_test else
    f"PRODUKCIJA • FURS AKTIVEN • {RUNTIME.active_premise}/{RUNTIME.electronic_device}"
)
ENVIRONMENT_NOTICE_COLOR = DANGER if RUNTIME.is_test else SUCCESS

TEST_ITEMS = [
    "Face ID / Touch ID",
    "Zaslon",
    "Touch",
    "Sprednja kamera",
    "Zadnja kamera",
    "Earpiece / slušalka",
    "Loudspeaker",
    "Mikrofon",
    "Polnjenje",
    "Vibracija / Taptic",
    "Gumbi",
    "Proximity senzor",
    "True Tone",
    "Wi-Fi / Bluetooth",
]

TEST_STATUSES = ["OK", "NE DELA", "NI TESTIRANO", "N/A"]


def fit_dialog_to_screen(window, desired_width, desired_height, *, margin=55):
    """Prilagodi pojavno okno tudi Windows zaslonu z večjim merilom."""
    window.update_idletasks()
    screen_w = max(480, window.winfo_screenwidth())
    screen_h = max(420, window.winfo_screenheight())
    width = max(440, min(int(desired_width), screen_w - margin))
    height = max(340, min(int(desired_height), screen_h - margin))
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _display_value(mapping, code):
    return mapping.get(code, code)


def _stored_value(mapping, displayed):
    displayed = str(displayed or "").strip()
    if displayed in mapping:
        return displayed
    reverse = {label: code for code, label in mapping.items()}
    return reverse.get(displayed, displayed)

pending_photos = []  # [{"path": "...", "stage": "PRED"}]
active_service_order_id = None
device_identity_method = ""
device_type_code = ""
last_saved_service_id = None
service_timer_after_id = None

DEVICE_TYPE_LABELS = {
    "TELEFON": "Telefon",
    "TABLICA": "Tablica",
    "PRENOSNIK": "Prenosnik",
    "NAMIZNI_PC": "Namizni računalnik",
    "KONZOLA": "Igralna konzola",
    "DRUGO": "Druga elektronska naprava",
}

SERVICE_LINE_KIND_LABELS = {
    "T1": "T1 – Delo v delavnici",
    "T2": "T2 – Delo na terenu",
}
LABOR_FLOOR_EUR = float(RUNTIME.settings.get("pricing", {}).get("l0_eur", 22.0))



def protect_file_permissions(path):
    """Omeji produkcijsko datoteko na trenutnega uporabnika."""
    try:
        p = Path(path)
        # TEST izdaja vsebuje samo umetne podatke. Stroga produkcijska ACL bi
        # otežila razvojne preglede, selitev testne mape in obnovitvene teste.
        if RUNTIME.is_test:
            return
        if os.environ.get("MIKROFIX_SKIP_FILE_PROTECTION") == "1":
            return
        protect_private_path(p)
    except Exception:
        # ACL je dodatna zaščita; napaka ne sme ustaviti servisa.
        pass


_AUTH_STORE = None


def auth_store():
    global _AUTH_STORE
    if _AUTH_STORE is None:
        _AUTH_STORE = AuthStore(AUTH_DB, protect_file_permissions)
    return _AUTH_STORE


AUTH_SESSION = SessionAuthUI(
    auth_store,
    version_label=RUNTIME.version_label,
    login_context="servisni sistem",
    bg=BG,
    panel=PANEL,
    blue=BLUE,
    gray=GRAY,
    dark=DARK,
    light_gray=LIGHT_GRAY,
    danger=DANGER,
)


def init_auth_db():
    auth_store().init()


def auth_has_pin(username=None):
    return auth_store().has_pin(username or AUTH_SESSION.username)


def set_local_pin(pin, event_type="PIN_SET", username=None):
    target = username or AUTH_SESSION.username
    auth_store().set_pin(
        pin,
        target,
        event_type=event_type,
        changed_by=AUTH_SESSION.username,
    )


def verify_local_pin(pin, username=None):
    return auth_store().verify_pin(
        pin, username or AUTH_SESSION.username
    ).ok


def audit_event(event_type, entity_type=None, entity_id=None, details=None):
    try:
        auth_store().record_event(
            event_type,
            username=AUTH_SESSION.username,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
        return True
    except sqlite3.Error:
        return False


def run_pin_login(parent, username_only=None):
    return AUTH_SESSION.login(parent, username_only=username_only)


def change_pin_dialog(parent):
    AUTH_SESSION.change_pin(parent)


def manage_users_dialog(parent):
    AUTH_SESSION.manage_users(parent)


def logout_and_lock():
    global ACTIVE_SCREEN
    audit_event("LOGOUT", "user", AUTH_SESSION.username)
    ACTIVE_SCREEN = "LOGIN"
    root.withdraw()
    if run_pin_login(root):
        root.deiconify()
        try:
            root.state("zoomed")
        except Exception:
            pass
        show_start_menu()
    else:
        root.destroy()


def note_user_activity(_event=None):
    global _last_user_activity
    _last_user_activity = time.monotonic()


def lock_for_inactivity():
    """Zaklene sejo, ne da bi zaprl ali počistil odprto delo."""
    global ACTIVE_SCREEN, _idle_lock_running
    if _idle_lock_running or ACTIVE_SCREEN in {"LOGIN", "CLOSED"}:
        return
    _idle_lock_running = True
    previous_screen = ACTIVE_SCREEN
    required_username = AUTH_SESSION.username
    visible_windows = []
    grabbed_window = None
    try:
        try:
            grabbed_window = root.grab_current()
            if grabbed_window is not None:
                grabbed_window.grab_release()
        except tk.TclError:
            grabbed_window = None
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel) and child.winfo_viewable():
                visible_windows.append(child)
                child.withdraw()
        audit_event("AUTO_LOCK", "user", required_username)
        ACTIVE_SCREEN = "LOGIN"
        root.withdraw()
        if not run_pin_login(root, username_only=required_username):
            ACTIVE_SCREEN = "CLOSED"
            root.destroy()
            return
        ACTIVE_SCREEN = previous_screen
        if previous_screen == "OFFICE":
            root.withdraw()
        else:
            root.deiconify()
            try:
                root.state("zoomed")
            except Exception:
                pass
        for child in visible_windows:
            if child.winfo_exists():
                child.deiconify()
                child.lift()
        if grabbed_window is not None and grabbed_window.winfo_exists():
            try:
                grabbed_window.grab_set()
            except tk.TclError:
                pass
    finally:
        note_user_activity()
        _idle_lock_running = False


def check_idle_timeout():
    if (
        ACTIVE_SCREEN not in {"LOGIN", "CLOSED"}
        and not _idle_lock_running
        and time.monotonic() - _last_user_activity >= IDLE_TIMEOUT_SECONDS
    ):
        lock_for_inactivity()
    if ACTIVE_SCREEN != "CLOSED" and root.winfo_exists():
        root.after(5000, check_idle_timeout)


def connect():
    return RUNTIME.connect_sqlite("service", foreign_keys=True)


def inventory_connect():
    return RUNTIME.connect_sqlite("inventory", foreign_keys=True)


def init_inventory_db():
    c = inventory_connect()
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL UNIQUE,
            article_name TEXT NOT NULL,
            internal_code TEXT,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            lot_serial TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_barcode
        ON inventory_items(barcode)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_inventory_name
        ON inventory_items(article_name COLLATE NOCASE)
    """)
    c.commit()
    c.close()
    protect_file_permissions(INVENTORY_DB)


def migrate_inventory_from_main_db():
    """Enkratno prenesi stare artikle iz mikrofix.db v mikrofix_inventory.db."""
    c = connect()
    exists = c.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='inventory_items'
    """).fetchone()

    if not exists:
        c.close()
        return

    old_columns = {
        row[1] for row in c.execute("PRAGMA table_info(inventory_items)")
    }
    required = {
        "id", "barcode", "article_name", "internal_code",
        "stock_qty", "price", "lot_serial", "created_at", "updated_at"
    }

    if not required.issubset(old_columns):
        c.close()
        return

    rows = c.execute("""
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial, created_at, updated_at
        FROM inventory_items
        ORDER BY id
    """).fetchall()

    inv = inventory_connect()
    try:
        inv.execute("BEGIN")
        for row in rows:
            inv.execute("""
                INSERT INTO inventory_items(
                    id, barcode, article_name, internal_code,
                    stock_qty, price, lot_serial, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(barcode) DO UPDATE SET
                    article_name=excluded.article_name,
                    internal_code=excluded.internal_code,
                    stock_qty=excluded.stock_qty,
                    price=excluded.price,
                    lot_serial=excluded.lot_serial,
                    updated_at=excluded.updated_at
            """, row)
        inv.commit()
    except Exception:
        inv.rollback()
        inv.close()
        c.close()
        raise
    inv.close()

    # Šele po uspešnem prenosu odstranimo staro tabelo iz servisne baze.
    c.execute("DROP INDEX IF EXISTS idx_inventory_barcode")
    c.execute("DROP TABLE IF EXISTS inventory_items")
    c.commit()
    c.close()
    protect_file_permissions(DB)
    protect_file_permissions(INVENTORY_DB)


def init_db():
    c = connect()

    c.execute("""CREATE TABLE IF NOT EXISTS service(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_no TEXT UNIQUE,
        date TEXT,
        customer TEXT,
        customer_address TEXT,
        customer_phone TEXT,
        model TEXT,
        serial TEXT,
        imei TEXT,
        ios TEXT,
        work TEXT,
        part_id TEXT,
        invoice_no TEXT,
        warranty_until TEXT,
        warranty_scope TEXT,
        warranty_months INTEGER
    )""")

    columns = {row[1] for row in c.execute("PRAGMA table_info(service)")}
    wanted = {
        "customer": "TEXT",
        "customer_address": "TEXT",
        "customer_phone": "TEXT",
        "model": "TEXT",
        "serial": "TEXT",
        "imei": "TEXT",
        "ios": "TEXT",
        "work": "TEXT",
        "part_id": "TEXT",
        "invoice_no": "TEXT",
        "warranty_until": "TEXT",
        "warranty_scope": "TEXT",
        "warranty_months": "INTEGER",
        "service_order_id": "INTEGER",
        "device_type": "TEXT",
        "identity_method": "TEXT",
        "labor_price": "REAL NOT NULL DEFAULT 0",
        "pricing_mode": "TEXT NOT NULL DEFAULT 'LEGACY_LABOR'",
        "office_invoice_id": "INTEGER",
        "resolution_status": "TEXT NOT NULL DEFAULT 'AWAITING_INVOICE'",
        "converted_buyback_id": "INTEGER",
        "ready_for_pickup_at": "TEXT",
        "picked_up_at": "TEXT",
        "final_notice_recorded_at": "TEXT",
    }
    for col, typ in wanted.items():
        if col not in columns:
            c.execute(f"ALTER TABLE service ADD COLUMN {col} {typ}")

    c.execute("""CREATE TABLE IF NOT EXISTS service_parts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_id INTEGER NOT NULL,
        part_name TEXT,
        part_id TEXT,
        barcode TEXT,
        article_id INTEGER,
        lot_serial TEXT,
        unit_price REAL,
        FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS service_manual_lines(
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
    )""")

    manual_columns = {row[1] for row in c.execute("PRAGMA table_info(service_manual_lines)")}
    for col, typ in {
        "service_tier": "TEXT",
        "billable_minutes": "INTEGER NOT NULL DEFAULT 0",
        "tariff_version": "TEXT",
        "first_rate": "REAL NOT NULL DEFAULT 0",
        "extra_half_hour_rate": "REAL NOT NULL DEFAULT 0",
    }.items():
        if col not in manual_columns:
            c.execute(f"ALTER TABLE service_manual_lines ADD COLUMN {col} {typ}")

    part_columns = {row[1] for row in c.execute("PRAGMA table_info(service_parts)")}
    if "barcode" not in part_columns:
        c.execute("ALTER TABLE service_parts ADD COLUMN barcode TEXT")
    if "article_id" not in part_columns:
        c.execute("ALTER TABLE service_parts ADD COLUMN article_id INTEGER")
    if "lot_serial" not in part_columns:
        c.execute("ALTER TABLE service_parts ADD COLUMN lot_serial TEXT")
    if "unit_price" not in part_columns:
        c.execute("ALTER TABLE service_parts ADD COLUMN unit_price REAL")

    c.execute("""CREATE TABLE IF NOT EXISTS service_tests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_id INTEGER NOT NULL,
        stage TEXT NOT NULL,
        test_name TEXT NOT NULL,
        status TEXT NOT NULL,
        FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS service_photos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_id INTEGER NOT NULL,
        stage TEXT,
        rel_path TEXT NOT NULL,
        FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_id INTEGER NOT NULL,
        claim_no TEXT,
        date TEXT NOT NULL,
        description TEXT,
        intake_condition TEXT,
        customer_request TEXT,
        finding TEXT,
        resolution TEXT,
        status TEXT,
        FOREIGN KEY(service_id) REFERENCES service(id) ON DELETE CASCADE
    )""")

    claim_columns = {row[1] for row in c.execute("PRAGMA table_info(claims)")}
    claim_wanted = {
        "claim_no": "TEXT",
        "intake_condition": "TEXT",
        "customer_request": "TEXT",
        "finding": "TEXT",
    }
    for col, typ in claim_wanted.items():
        if col not in claim_columns:
            c.execute(f"ALTER TABLE claims ADD COLUMN {col} {typ}")

    c.execute("CREATE INDEX IF NOT EXISTS idx_service_serial ON service(serial)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_service_imei ON service(imei)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claims_service ON claims(service_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_service_order_id ON service(service_order_id)")

    ensure_workflow_schema(c)

    c.commit()
    c.close()
    protect_file_permissions(DB)


def backup_db(reason="manual"):
    if not DB.exists():
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    main_dest = BACKUP_DIR / f"mikrofix_{stamp}_{reason}.db"
    inventory_dest = BACKUP_DIR / f"mikrofix_inventory_{stamp}_{reason}.db"

    try:
        backup_sqlite_file(DB, main_dest)
        protect_file_permissions(main_dest)

        if INVENTORY_DB.exists():
            backup_sqlite_file(INVENTORY_DB, inventory_dest)
            protect_file_permissions(inventory_dest)

        main_backups = sorted(
            BACKUP_DIR.glob("mikrofix_[0-9]*_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        inventory_backups = sorted(
            BACKUP_DIR.glob("mikrofix_inventory_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        for old in main_backups[100:]:
            try:
                old.unlink()
            except OSError:
                pass
        for old in inventory_backups[100:]:
            try:
                old.unlink()
            except OSError:
                pass

        return main_dest
    except OSError:
        return None



def pdf_safe(value):
    """Varno vstavi uporabniško besedilo v ReportLab Paragraph."""
    return xml_escape(str(value or "-")).replace("\n", "<br/>")


def safe_float(value, default=0.0):
    """Pretvori uporabniški decimalni vnos (sprejme vejico ali piko)."""
    try:
        return float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def mask_identifier(value):
    """Za javni prikaz skrij vse razen zadnjih 4 znakov."""
    value = str(value or "").strip()
    if not value:
        return "-"
    if len(value) <= 4:
        return value
    return ("X" * (len(value) - 4)) + value[-4:]


def normalize_claim_status(value):
    value = str(value or "").strip().upper()
    legacy = {
        "V OBRAVNAVI": "V OBDELAVI",
        "REŠENA": "ZAKLJUČENA",
    }
    return legacy.get(value, value or "ODPRTA")


def claim_status_is_terminal(value):
    return normalize_claim_status(value) in {"ZAKLJUČENA", "ZAVRNJENA"}


def set_quick_tests_enabled(enabled):
    state = "normal" if enabled else "disabled"
    for widget in globals().get("quick_test_widgets", []):
        try:
            widget.configure(state=state)
        except Exception:
            pass


def device_is_identified():
    """Veljavna identifikacija je USB branje ali fizični prepis druge naprave."""
    return bool(
        serial.get().strip()
        and model.get().strip()
        and connected_state.get() == "POVEZAN"
        and device_identity_method in {"MICRODETECT", "MANUAL_OTHER"}
    )


def device_is_microdetected():
    return device_is_identified() and device_identity_method == "MICRODETECT"


def require_identified_device(action_name="to funkcijo"):
    if device_is_identified():
        return True

    messagebox.showwarning(
        "MikroFix",
        f"Najprej identificiraj napravo.\n\n"
        f"Za podprto Apple mobilno napravo uporabi MicroDETECT. Za računalnik, "
        f"konzolo ali drugo nepodprto napravo uporabi »DRUGA NAPRAVA«.\n\n"
        f"Brez identifikacije ni mogoče uporabiti: {action_name}."
    )
    try:
        top_nb.select(tab_device_top)
    except Exception:
        pass
    return False


def update_workflow_lock():
    """Osebni podatki so dostopni šele po dokumentirani identifikaciji.

    Apple mobilne naprave uporablja MicroDETECT. Pri drugi elektroniki se
    identiteta fizično prepiše z nalepke ali ohišja. Odkup ostaja odklenjen
    samo za neposredno prebrane mobilne naprave.
    """
    if "top_nb" not in globals():
        return

    identified = device_is_identified()

    try:
        if ACTIVE_SCREEN == "SERVICE":
            top_nb.tab(tab_intake_top, state="normal" if identified else "disabled")
            # Pregled odprtih sprejemov mora biti dosegljiv tudi brez na novo
            # priključene naprave. Izbrani nalog sam naloži svojo shranjeno
            # identiteto, šele nato pa odpre delovni prostor in časovnik.
            top_nb.tab(tab_service_top, state="normal")
            top_nb.tab(tab_claims_top, state="normal" if identified else "disabled")
            if "tab_buyback_top" in globals():
                top_nb.tab(tab_buyback_top, state="hidden")
        elif ACTIVE_SCREEN == "BUYBACK":
            top_nb.tab(tab_intake_top, state="hidden")
            top_nb.tab(tab_service_top, state="hidden")
            top_nb.tab(tab_claims_top, state="hidden")
            top_nb.tab(
                tab_buyback_top,
                state="normal" if device_is_microdetected() else "disabled",
            )
    except Exception:
        pass

    if not identified:
        try:
            protected_tabs = {
                str(tab_intake_top), str(tab_claims_top)
            }
            if "tab_buyback_top" in globals():
                protected_tabs.add(str(tab_buyback_top))
            if top_nb.select() in protected_tabs:
                top_nb.select(tab_device_top)
        except Exception:
            pass


detect_in_progress = False
detect_result_queue = queue.Queue()
detect_previous_serial = ""
detect_previous_imei = ""
start_workflow_intent = "SERVICE"


def _finish_detect_ui():
    global detect_in_progress
    detect_in_progress = False
    try:
        detect_button.configure(state="normal", text="MicroDETECT")
        if "service_detect_button" in globals():
            service_detect_button.configure(state="normal", text="MicroDETECT")
        detect_progress.stop()
    except Exception:
        pass


def _detect_success(data):
    global active_service_order_id, device_identity_method, device_type_code
    try:
        identity = extract_device_identity(data)
    except DeviceDetectionError as exc:
        _detect_error(exc)
        return

    serial_value = identity["serial"]
    imei_value = identity["imei"]
    if detect_previous_serial and serial_value:
        device_changed = detect_previous_serial != serial_value
    else:
        device_changed = bool(
            detect_previous_imei and imei_value and detect_previous_imei != imei_value
        )
    model.set(identity["model"])
    ios.set(identity["ios"])
    storage.set(identity.get("storage") or "Ni mogoče prebrati")
    name.set(identity["name"])
    serial.set(serial_value)
    imei.set(imei_value)
    serial_display.set(mask_identifier(serial_value))
    imei_display.set(mask_identifier(imei_value))
    device_identity_method = "MICRODETECT"
    device_type_code = "APPLE_MOBILE"
    set_service_test_tabs_for_device(True)

    # Osebni podatki in odprto naročilo nikoli ne smejo ostati pripeti na
    # drugo napravo. Pri ponovni zaznavi iste naprave vnos ostane nedotaknjen.
    if device_changed:
        clear_intake_form()
        reset_buyback_form()
        clear_form(keep_device=True)
        active_service_order_id = None
        audit_event(
            "DEVICE_CONTEXT_SWITCHED",
            "device",
            serial_value[-4:],
            {"previous_serial": mask_identifier(detect_previous_serial)},
        )

    connected_state.set("POVEZAN")
    connected_label.configure(fg=SUCCESS)
    header_connected_label.configure(fg=SUCCESS)
    set_quick_tests_enabled(True)
    status.set(
        "MicroDETECT uspešen. Zdaj je dovoljen vnos osebnih podatkov za servis ali odkup."
    )
    audit_event(
        "DEVICE_DETECTED",
        "device",
        serial_value[-4:],
        {
            "model": identity["model"],
            "ios": identity["ios"],
            "storage": identity.get("storage") or "",
            "has_imei": bool(imei_value),
        },
    )

    refresh_device_history_count()
    update_workflow_lock()
    refresh_service_tab_history()
    refresh_claims_tab()
    if "intake_device_summary" in globals():
        intake_device_summary.set(
            f"{identity['model']} | S/N {mask_identifier(serial_value)} | "
            f"IMEI {mask_identifier(imei_value)} | {identity.get('storage') or 'pomnilnik ni prebran'} | "
            f"iOS {identity['ios'] or '-'}"
        )
    if "buyback_device_summary" in globals():
        buyback_device_summary.set(
            f"{identity['model']} | S/N {mask_identifier(serial_value)} | "
            f"IMEI {mask_identifier(imei_value)} | {identity.get('storage') or 'pomnilnik ni prebran'} | "
            f"iOS {identity['ios'] or '-'}"
        )
    if start_workflow_intent == "BUYBACK":
        try:
            top_nb.select(tab_buyback_top)
        except Exception:
            pass
    _finish_detect_ui()


def _detect_error(error, timeout=False):
    if timeout:
        issue = device_problem("TIMEOUT")
    elif isinstance(error, DeviceDetectionError):
        issue = error
    else:
        issue = device_problem("UNKNOWN")
    connected_state.set("NI POVEZAVE")
    connected_label.configure(fg=GRAY)
    header_connected_label.configure(fg=GRAY)
    status.set(f"MicroDETECT: {issue.user_message}")
    set_quick_tests_enabled(False)
    update_workflow_lock()
    _finish_detect_ui()
    audit_event(
        "DEVICE_DETECT_FAILED",
        "device",
        None,
        {"reason": issue.code},
    )

    messagebox.showwarning("MicroDETECT", issue.user_message)


def _poll_detect_queue():
    """Edino glavni Tkinter thread spreminja GUI."""
    try:
        while True:
            event, payload = detect_result_queue.get_nowait()

            if event == "status":
                status.set(str(payload))
            elif event == "success":
                _detect_success(payload)
            elif event == "timeout":
                _detect_error("", timeout=True)
            elif event == "error":
                _detect_error(payload)
    except queue.Empty:
        pass

    if detect_in_progress:
        root.after(80, _poll_detect_queue)


def _detect_worker():
    try:
        detect_result_queue.put((
            "status", "Preverjam Apple USB povezavo in iščem iPhone..."
        ))
        data = read_usb_iphone(
            lambda message: detect_result_queue.put(("status", message))
        )
        detect_result_queue.put(("success", data))

    except DeviceDetectionError as exc:
        detect_result_queue.put(("error", exc))
    except Exception:
        detect_result_queue.put(("error", device_problem("UNKNOWN")))


def detect():
    global detect_in_progress, detect_previous_serial, detect_previous_imei
    global device_identity_method, device_type_code
    if detect_in_progress:
        return

    # Počisti morebitne stare rezultate prejšnjega poskusa.
    try:
        while True:
            detect_result_queue.get_nowait()
    except queue.Empty:
        pass

    detect_in_progress = True

    detect_previous_serial = serial.get().strip()
    detect_previous_imei = imei.get().strip()

    # Nova zaznava vedno začne iz čistega stanja, da med branjem
    # ne ostanejo odklenjeni podatki prejšnje naprave.
    name.set("")
    model.set("")
    ios.set("")
    storage.set("")
    serial.set("")
    imei.set("")
    serial_display.set("")
    imei_display.set("")
    device_identity_method = ""
    device_type_code = ""
    device_history_count.set("")
    for var in pre_test_vars.values():
        var.set("NI TESTIRANO")

    status.set("Preverjam iPhone prek USB...")
    connected_state.set("ZAZNAVAM...")
    connected_label.configure(fg=BLUE_DARK)
    header_connected_label.configure(fg=BLUE_DARK)
    set_quick_tests_enabled(False)
    update_workflow_lock()
    refresh_service_tab_history()
    refresh_claims_tab()

    try:
        detect_button.configure(state="disabled", text="ZAZNAVAM...")
        if "service_detect_button" in globals():
            service_detect_button.configure(state="disabled", text="ZAZNAVAM...")
        detect_progress.start(12)
    except Exception:
        pass

    threading.Thread(
        target=_detect_worker,
        name="MikroFIX-USB-Detect",
        daemon=True
    ).start()

    root.after(80, _poll_detect_queue)


def identify_other_device():
    """Ročna identifikacija naprav, ki jih MicroDETECT tehnično ne podpira."""
    global active_service_order_id, device_identity_method, device_type_code

    win = tk.Toplevel(root)
    win.title("MikroFIX - Druga naprava")
    maximize_work_window(win, "580x430")
    win.minsize(480, 370)
    win.resizable(True, True)
    win.configure(bg=BG)
    win.transient(root)
    win.grab_set()

    card = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY)
    card.pack(fill="both", expand=True, padx=14, pady=14)
    tk.Label(
        card, text="Fizični prepis oznake naprave", bg=PANEL, fg=DARK,
        font=("Segoe UI", 15, "bold"),
    ).pack(anchor="w", padx=14, pady=(12, 3))
    tk.Label(
        card,
        text=(
            "Za računalnik, prenosnik, konzolo ali drugo napravo, ki je MicroDETECT "
            "ne podpira. Proizvajalčevo oznako prepiši z naprave. Če je ni, "
            "obdrži ustvarjeno interno prevzemno oznako."
        ),
        bg=PANEL, fg=GRAY, font=("Segoe UI", 8), justify="left", wraplength=500,
    ).pack(anchor="w", padx=14, pady=(0, 8))

    type_var = tk.StringVar(value="Namizni računalnik")
    model_var = tk.StringVar()
    serial_var = tk.StringVar(
        value=datetime.now().strftime("MF-DEV-%Y%m%d-%H%M%S")
    )
    imei_var = tk.StringVar()
    confirm_var = tk.BooleanVar(value=False)

    form = tk.Frame(card, bg=PANEL)
    form.pack(fill="x", padx=14)
    for row, (label, widget) in enumerate([
        ("Vrsta", ttk.Combobox(
            form, textvariable=type_var, values=list(DEVICE_TYPE_LABELS.values()),
            state="readonly", width=38,
        )),
        ("Proizvajalec in model", ttk.Entry(form, textvariable=model_var, width=42)),
        ("Serijska / interna oznaka", ttk.Entry(form, textvariable=serial_var, width=42)),
        ("IMEI (samo če obstaja)", ttk.Entry(form, textvariable=imei_var, width=42)),
    ]):
        tk.Label(
            form, text=label, bg=PANEL, fg=GRAY,
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        widget.grid(row=row, column=1, sticky="ew", pady=4)
    form.grid_columnconfigure(1, weight=1)

    tk.Checkbutton(
        card,
        text=(
            "Potrjujem fizični prepis proizvajalčeve oznake oziroma dodelitev "
            "prikazane interne prevzemne oznake."
        ),
        variable=confirm_var, bg=PANEL, fg=DARK, activebackground=PANEL,
        font=("Segoe UI", 8), justify="left", wraplength=500,
    ).pack(anchor="w", padx=14, pady=(8, 5))

    def confirm():
        global active_service_order_id, device_identity_method, device_type_code
        model_v = model_var.get().strip()
        serial_v = serial_var.get().strip()
        if not model_v or not serial_v:
            messagebox.showwarning(
                "MikroFIX", "Vpiši model in oznako, ki je dejansko na napravi.", parent=win
            )
            return
        if not confirm_var.get():
            messagebox.showwarning(
                "MikroFIX", "Potrdi fizični prepis oznake naprave.", parent=win
            )
            return

        previous_serial = serial.get().strip()
        if previous_serial and previous_serial != serial_v:
            clear_intake_form()
            reset_buyback_form()
            clear_form(keep_device=True)
            active_service_order_id = None

        reverse_types = {label: code for code, label in DEVICE_TYPE_LABELS.items()}
        device_type_code = reverse_types.get(type_var.get(), "DRUGO")
        device_identity_method = "MANUAL_OTHER"
        set_service_test_tabs_for_device(False)
        name.set(model_v)
        model.set(model_v)
        serial.set(serial_v)
        imei.set(imei_var.get().strip())
        ios.set("")
        storage.set("")
        serial_display.set(mask_identifier(serial_v))
        imei_display.set(mask_identifier(imei.get()))
        connected_state.set("POVEZAN")
        connected_label.configure(fg=SUCCESS)
        header_connected_label.configure(fg=SUCCESS)
        set_quick_tests_enabled(False)
        status.set(
            "Druga naprava je identificirana s fizičnim prepisom. Odkup ni odklenjen."
        )
        intake_device_summary.set(
            f"{DEVICE_TYPE_LABELS.get(device_type_code, 'Druga naprava')} | "
            f"{model_v} | oznaka {mask_identifier(serial_v)}"
        )
        buyback_device_summary.set("Odkup je na voljo samo po uspešnem MicroDETECT.")
        audit_event(
            "DEVICE_IDENTIFIED_MANUALLY", "device", serial_v[-4:],
            {"model": model_v, "device_type": device_type_code},
        )
        update_workflow_lock()
        refresh_device_history_count()
        refresh_service_tab_history()
        refresh_claims_tab()
        win.destroy()

    actions = tk.Frame(card, bg=PANEL)
    actions.pack(side="bottom", fill="x", padx=14, pady=(7, 10))
    ttk.Button(actions, text="PREKLIČI", command=win.destroy).pack(side="left")
    ttk.Button(
        actions, text="POTRDI NAPRAVO", command=confirm, style="Blue.TButton"
    ).pack(side="right")



def autofill_customer_for_connected_device():
    """Namenski priklic stare stranke; nikoli se ne izvede samodejno.

    Uspešen MicroDETECT potrdi povezavo z napravo, ne pa identitete trenutne
    osebe. Zato se stari osebni podatki ne smejo prikazati samo zaradi priklopa.
    """
    sn = serial.get().strip()
    im = imei.get().strip()

    if not sn and not im:
        return False

    c = connect()
    row = c.execute("""
        SELECT customer, customer_address, customer_phone, service_no, date
        FROM service
        WHERE serial = ? OR (imei <> '' AND imei = ?)
        ORDER BY id DESC
        LIMIT 1
    """, (sn, im)).fetchone()
    c.close()

    if not row:
        return False

    old_customer, old_address, old_phone, old_service_no, old_date = row

    if old_customer:
        customer.set(old_customer)
    if old_address:
        customer_address.set(old_address)
    if old_phone:
        customer_phone.set(old_phone)

    status.set(
        f"Naprava je že bila servisirana ({old_service_no}). "
        "Podatki stranke so samodejno izpolnjeni."
    )
    return True

def next_no(c):
    year = datetime.now().year
    rows = c.execute(
        "SELECT service_no FROM service WHERE service_no LIKE ?",
        (f"MF-{year}-%",)
    ).fetchall()
    max_n = 0
    for (no,) in rows:
        try:
            max_n = max(max_n, int(str(no).split("-")[-1]))
        except Exception:
            pass
    return f"MF-{year}-{max_n + 1:04d}"


def get_test_values(stage):
    src = pre_test_vars if stage == "PRED" else post_test_vars
    return {name: var.get() for name, var in src.items()}


def collect_parts_from_tree():
    rows = []
    for iid in parts_tree.get_children():
        values = parts_tree.item(iid, "values")
        if len(values) >= 6:
            try:
                article_id_v = int(values[0])
            except Exception:
                article_id_v = None

            part_name_v = str(values[1]).strip()
            internal_code_v = str(values[2]).strip()
            barcode_v = str(values[3]).strip()
            lot_serial_v = str(values[4]).strip()

            try:
                unit_price_v = float(str(values[5]).replace(",", "."))
            except Exception:
                unit_price_v = 0.0

            rows.append((
                part_name_v,
                internal_code_v,
                barcode_v,
                article_id_v,
                lot_serial_v,
                unit_price_v
            ))
    return rows


def collect_manual_service_lines():
    rows = []
    tree = globals().get("service_lines_tree")
    if tree is None:
        return rows
    for iid in tree.get_children():
        values = tree.item(iid, "values")
        if len(values) < 9:
            continue
        shown_kind = str(values[0]).strip()
        reverse_kinds = {label: code for code, label in SERVICE_LINE_KIND_LABELS.items()}
        rows.append((
            reverse_kinds.get(shown_kind, shown_kind.upper()),
            str(values[1]).strip(),
            float(str(values[2]).replace(",", ".")),
            float(str(values[3]).replace(",", ".")),
            float(str(values[4]).replace(",", ".")),
            str(values[5]),
            int(values[6]),
            float(str(values[7]).replace(",", ".")),
            float(str(values[8]).replace(",", ".")),
        ))
    return rows


def refresh_service_lines_total():
    total = 0.0
    tree = globals().get("service_lines_tree")
    if tree is not None:
        for iid in tree.get_children():
            values = tree.item(iid, "values")
            if len(values) >= 5:
                total += float(str(values[4]).replace(",", "."))
    if "service_lines_total_var" in globals():
        service_lines_total_var.set(f"Skupaj storitve: {total:.2f} €")


def refresh_service_tariff_preview(*_args):
    try:
        reverse = {label: code for code, label in SERVICE_LINE_KIND_LABELS.items()}
        tier = reverse.get(service_line_kind_var.get(), "T1")
        extra = int(service_line_quantity_var.get().strip())
        tariff = service_tariff_snapshot(tier, max(0, extra), RUNTIME.settings)
        service_line_price_var.set(f"{float(tariff['price']):.2f}")
    except (ValueError, TypeError):
        service_line_price_var.set("—")


def add_manual_service_line():
    description = service_line_description_var.get().strip()
    if not description:
        messagebox.showwarning("MikroFIX", "Vpiši opis servisne postavke.")
        return
    try:
        extra_half_hours = int(service_line_quantity_var.get().strip())
    except ValueError:
        messagebox.showwarning("MikroFIX", "Število dodatnih 30-minutnih enot mora biti celo število.")
        return
    if extra_half_hours < 0:
        messagebox.showwarning("MikroFIX", "Število dodatnih enot ne sme biti negativno.")
        return
    reverse_kinds = {label: code for code, label in SERVICE_LINE_KIND_LABELS.items()}
    tier = reverse_kinds.get(service_line_kind_var.get(), "T1")
    tariff = service_tariff_snapshot(tier, extra_half_hours, RUNTIME.settings)
    total = float(tariff["price"])
    service_lines_tree.insert(
        "", "end",
        values=(
            service_line_kind_var.get(), description,
            "1.00", f"{total:.2f}", f"{total:.2f}",
            tariff["tariff_version"], tariff["billable_minutes"],
            f"{tariff['first_rate']:.2f}", f"{tariff['extra_half_hour_rate']:.2f}",
        ),
    )
    service_line_description_var.set("")
    service_line_quantity_var.set("0")
    service_line_price_var.set(f"{float(service_tariff_snapshot('T1', 0, RUNTIME.settings)['price']):.2f}")
    refresh_service_lines_total()
    service_line_description_entry.focus_set()


def remove_manual_service_line():
    for iid in service_lines_tree.selection():
        service_lines_tree.delete(iid)
    refresh_service_lines_total()


def get_article_by_barcode(barcode):
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    c = inventory_connect()
    row = c.execute("""
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial
        FROM inventory_items
        WHERE barcode = ? AND COALESCE(active, 1)=1
    """, (barcode,)).fetchone()
    c.close()
    return row


def get_article(article_id):
    c = inventory_connect()
    row = c.execute("""
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial
        FROM inventory_items
        WHERE id = ? AND COALESCE(active, 1)=1
    """, (article_id,)).fetchone()
    c.close()
    return row


def format_price(value):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "0.00"


def pending_article_count(article_id):
    count = 0
    for iid in parts_tree.get_children():
        values = parts_tree.item(iid, "values")
        if not values:
            continue
        try:
            if int(values[0]) == int(article_id):
                count += 1
        except Exception:
            pass
    return count


def add_article_to_service(article):
    if not article:
        return False

    article_id_v, barcode_v, name_v, code_v, stock_v, price_v, lot_v = article
    already_pending = pending_article_count(article_id_v)

    if int(stock_v or 0) - already_pending <= 0:
        messagebox.showwarning(
            "MikroFix",
            f"Artikel nima več razpoložljive zaloge.\n\n"
            f"{name_v}\nBarkoda: {barcode_v}\n"
            f"Zaloga: {stock_v}\nŽe dodano na ta servis: {already_pending}"
        )
        return False

    inserted_iid = parts_tree.insert(
        "", "end",
        values=(
            article_id_v,
            name_v or "",
            code_v or "",
            barcode_v or "",
            lot_v or "",
            format_price(price_v)
        )
    )
    if not persist_active_service_parts():
        parts_tree.delete(inserted_iid)
        return False
    status.set(f"Dodan artikel: {name_v} ({barcode_v})")
    return True



def search_service_articles(*_args):
    """Live iskanje artiklov za trenutni servis po nazivu in/ali barkodi."""
    if "service_article_search_tree" not in globals():
        return

    name_q = (
        service_article_name_var.get().strip()
        if "service_article_name_var" in globals()
        else ""
    )
    barcode_q = (
        service_article_barcode_var.get().strip()
        if "service_article_barcode_var" in globals()
        else ""
    )

    for iid in service_article_search_tree.get_children():
        service_article_search_tree.delete(iid)

    sql = """
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial
        FROM inventory_items
    """
    where = ["COALESCE(active, 1)=1"]
    params = []

    if name_q:
        where.append("article_name LIKE ?")
        params.append(f"%{name_q}%")
    if barcode_q:
        where.append("barcode LIKE ?")
        params.append(f"%{barcode_q}%")

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY article_name COLLATE NOCASE LIMIT 100"

    c = inventory_connect()
    rows = c.execute(sql, params).fetchall()
    c.close()

    for row in rows:
        values = list(row)
        values[5] = f"{float(values[5] or 0):.2f} €"
        service_article_search_tree.insert("", "end", values=values)

    if "service_article_result_count" in globals():
        service_article_result_count.set(f"{len(rows)} zadetkov")


def selected_service_article():
    if "service_article_search_tree" not in globals():
        return None
    sel = service_article_search_tree.selection()
    if not sel:
        return None

    values = service_article_search_tree.item(sel[0], "values")
    if not values:
        return None

    try:
        article_id_v = int(values[0])
    except Exception:
        return None

    return get_article(article_id_v)


def add_selected_service_article(_event=None):
    article = selected_service_article()
    if not article:
        messagebox.showwarning(
            "MikroFIX",
            "Izberi artikel, ki ga želiš dodati v servis."
        )
        return

    if add_article_to_service(article):
        # Po dodajanju ohranimo iskalni niz, da lahko hitro dodamo več kosov.
        search_service_articles()


def service_barcode_enter(_event=None):
    """Enter v barkodi: če obstaja točen zadetek, ga takoj dodaj."""
    bc = service_article_barcode_var.get().strip()
    if not bc:
        return

    article = get_article_by_barcode(bc)
    if article:
        if add_article_to_service(article):
            service_article_barcode_var.set("")
            service_article_barcode_entry.focus_set()
            search_service_articles()
        return

    # Če ni točnega zadetka, pusti live rezultate za delno barkodo.
    search_service_articles()



def process_barcode_scan(_event=None):
    """Združljivost s starejšim klicem; uporablja novo iskanje barkode."""
    return service_barcode_enter(_event)



def open_inventory_editor(parent=None, article_id=None, on_saved=None):
    existing = get_article(article_id) if article_id else None
    editor_state = {"article_id": existing[0] if existing else None}

    win = tk.Toplevel(parent or root)
    win.title("MikroFIX - Artikel")
    maximize_work_window(win, "760x660")
    win.minsize(620, 520)
    win.resizable(True, True)
    win.configure(bg=BG)
    win.transient(parent or root)
    win.grab_set()

    outer_editor = tk.Frame(win, bg=BG)
    outer_editor.pack(fill="both", expand=True, padx=20, pady=18)

    editor_title = tk.StringVar(
        value="Uredi artikel" if existing else "Nov artikel"
    )
    tk.Label(
        outer_editor,
        textvariable=editor_title,
        bg=BG, fg=DARK, font=("Segoe UI", 16, "bold")
    ).pack(anchor="w", pady=(0, 10))

    barcode_var = tk.StringVar(value=existing[1] if existing else "")
    name_var = tk.StringVar(value=existing[2] if existing else "")
    code_var = tk.StringVar(value=existing[3] if existing else "")
    stock_var = tk.StringVar(value=str(existing[4]) if existing else "0")
    price_var = tk.StringVar(value=format_price(existing[5]) if existing else "0.00")
    lot_var = tk.StringVar(value=existing[6] if existing and existing[6] else "")

    search_box = tk.Frame(
        outer_editor, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    search_box.pack(fill="x", pady=(0,10))

    tk.Label(
        search_box,
        text="Pred vnosom lahko poiščeš obstoječi artikel",
        bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
    ).grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(9,5))

    find_name_var = tk.StringVar()
    find_barcode_var = tk.StringVar()

    tk.Label(search_box, text="Naziv", bg=PANEL, fg=GRAY).grid(
        row=1, column=0, sticky="w", padx=(12,5), pady=(0,10)
    )
    ttk.Entry(search_box, textvariable=find_name_var, width=24).grid(
        row=1, column=1, sticky="ew", padx=(0,12), pady=(0,10)
    )
    tk.Label(search_box, text="Barkoda", bg=PANEL, fg=GRAY).grid(
        row=1, column=2, sticky="w", padx=(0,5), pady=(0,10)
    )
    ttk.Entry(search_box, textvariable=find_barcode_var, width=22).grid(
        row=1, column=3, sticky="ew", padx=(0,12), pady=(0,10)
    )
    search_box.grid_columnconfigure(1, weight=1)
    search_box.grid_columnconfigure(3, weight=1)

    form = tk.Frame(
        outer_editor, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    form.pack(fill="x")

    fields = [
        ("Barkoda", barcode_var),
        ("Naziv artikla", name_var),
        ("Interna koda", code_var),
        ("Zaloga", stock_var),
        ("Cena (€)", price_var),
        ("LOT / serijska", lot_var),
    ]

    entries = []
    for row_i, (label_v, var_v) in enumerate(fields):
        tk.Label(
            form, text=label_v, bg=PANEL, fg=GRAY,
            font=("Segoe UI", 9, "bold"), width=19, anchor="w"
        ).grid(row=row_i, column=0, sticky="w", padx=(14, 8), pady=8)
        ent = ttk.Entry(form, textvariable=var_v, width=44)
        ent.grid(row=row_i, column=1, sticky="ew", padx=(0, 14), pady=8)
        entries.append(ent)
    form.grid_columnconfigure(1, weight=1)
    entries[3].configure(state="readonly")
    entries[4].configure(state="readonly")
    tk.Label(
        outer_editor,
        text=(
            "Zaloga se spreminja samo s prejemom, porabo, prodajo ali sledljivim "
            "izrednim dokumentom. Cena se potrdi v Office ob prejemu blaga."
        ),
        bg=BG, fg=WARNING, font=("Segoe UI", 9, "bold"),
        justify="left", wraplength=690,
    ).pack(anchor="w", pady=(8, 0))

    def load_editor_row(row):
        if not row:
            return
        editor_state["article_id"] = int(row[0])
        barcode_var.set(row[1] or "")
        name_var.set(row[2] or "")
        code_var.set(row[3] or "")
        stock_var.set(str(row[4] or 0))
        price_var.set(format_price(row[5]))
        lot_var.set(row[6] or "")
        editor_title.set("Uredi artikel")

    def show_search_results(rows):
        if not rows:
            messagebox.showinfo(
                "MikroFIX",
                "Ni najdenega artikla.",
                parent=win
            )
            return
        if len(rows) == 1:
            load_editor_row(rows[0])
            return

        sw = tk.Toplevel(win)
        sw.title("MikroFIX - Izberi artikel")
        sw.geometry("720x360")
        sw.configure(bg=BG)
        sw.transient(win)
        sw.grab_set()

        frame = tk.Frame(sw, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=14, pady=14)

        tree = ttk.Treeview(
            frame,
            columns=("id","barcode","name","code","stock"),
            show="headings"
        )
        tree.heading("id", text="ID")
        tree.heading("barcode", text="Barkoda")
        tree.heading("name", text="Naziv")
        tree.heading("code", text="Koda")
        tree.heading("stock", text="Zaloga")
        tree.column("id", width=0, minwidth=0, stretch=False)
        tree.column("barcode", width=180)
        tree.column("name", width=280)
        tree.column("code", width=150)
        tree.column("stock", width=70)

        row_map = {}
        for row in rows:
            iid = tree.insert(
                "", "end",
                values=(row[0], row[1], row[2], row[3], row[4])
            )
            row_map[iid] = row
        tree.pack(fill="both", expand=True)

        def choose(_event=None):
            sel = tree.selection()
            if not sel:
                return
            load_editor_row(row_map[sel[0]])
            sw.destroy()

        tree.bind("<Double-1>", choose)
        ttk.Button(
            sw, text="IZBERI", command=choose, style="Blue.TButton"
        ).pack(pady=(0,12))

    def search_existing():
        nq = find_name_var.get().strip()
        bq = find_barcode_var.get().strip()

        if not nq and not bq:
            messagebox.showwarning(
                "MikroFIX",
                "Vpiši naziv ali barkodo za iskanje.",
                parent=win
            )
            return

        sql = """
            SELECT id, barcode, article_name, internal_code,
                   stock_qty, price, lot_serial
            FROM inventory_items
        """
        where = ["COALESCE(active, 1)=1"]
        params = []
        if nq:
            where.append("article_name LIKE ?")
            params.append(f"%{nq}%")
        if bq:
            where.append("barcode LIKE ?")
            params.append(f"%{bq}%")
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY article_name COLLATE NOCASE"

        c = inventory_connect()
        rows = c.execute(sql, params).fetchall()
        c.close()
        show_search_results(rows)

    ttk.Button(
        search_box, text="POIŠČI",
        command=search_existing, style="Blue.TButton"
    ).grid(row=2, column=0, columnspan=4, sticky="e", padx=12, pady=(0,10))

    def do_save():
        bc = barcode_var.get().strip()
        nm = name_var.get().strip()
        code = code_var.get().strip()
        lot = lot_var.get().strip()

        if not bc:
            messagebox.showwarning("MikroFIX", "Vpiši ali poskeniraj barkodo.", parent=win)
            return
        if not nm:
            messagebox.showwarning("MikroFIX", "Vpiši naziv artikla.", parent=win)
            return

        c = inventory_connect()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inventory_event = "INVENTORY_CREATED"
        try:
            current_id = editor_state["article_id"]
            if current_id:
                inventory_event = "INVENTORY_UPDATED"
                c.execute("""
                    UPDATE inventory_items
                    SET barcode=?, article_name=?, internal_code=?,
                        lot_serial=?, updated_at=?
                    WHERE id=?
                """, (bc, nm, code, lot, now, current_id))
                saved_id = current_id
            else:
                cur = c.execute("""
                    INSERT INTO inventory_items(
                        barcode, article_name, internal_code,
                        stock_qty, price, lot_serial,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                """, (bc, nm, code, 0, 0, lot, now, now))
                saved_id = cur.lastrowid
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            messagebox.showerror(
                "MikroFIX",
                "Ta barkoda že obstaja v zalogi. Uporabi iskanje in uredi obstoječi artikel.",
                parent=win
            )
            c.close()
            return
        c.close()

        audit_event(
            inventory_event,
            "inventory_item",
            saved_id,
            {"barcode": bc, "stock_change": "NONE"},
        )

        win.destroy()
        if on_saved:
            on_saved(get_article(saved_id))

    buttons = tk.Frame(outer_editor, bg=BG)
    buttons.pack(fill="x", pady=(16, 0))
    ttk.Button(
        buttons, text="SHRANI ARTIKEL",
        command=do_save, style="Blue.TButton"
    ).pack(side="left")
    ttk.Button(buttons, text="PREKLIČI", command=win.destroy).pack(side="right")

    if existing:
        entries[1].focus_set()
    else:
        find_name_var.set("")
        find_barcode_var.set("")
        entries[0].focus_set()



def refresh_inventory_table(*_args):
    if "inventory_tree" not in globals():
        return

    name_q = (
        inventory_name_search_var.get().strip()
        if "inventory_name_search_var" in globals()
        else ""
    )
    barcode_q = (
        inventory_barcode_search_var.get().strip()
        if "inventory_barcode_search_var" in globals()
        else ""
    )

    for iid in inventory_tree.get_children():
        inventory_tree.delete(iid)

    sql = """
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial
        FROM inventory_items
    """
    where = ["COALESCE(active, 1)=1"]
    params = []

    if name_q:
        where.append("article_name LIKE ?")
        params.append(f"%{name_q}%")
    if barcode_q:
        where.append("barcode LIKE ?")
        params.append(f"%{barcode_q}%")

    sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY article_name COLLATE NOCASE"

    c = inventory_connect()
    rows = c.execute(sql, params).fetchall()
    c.close()

    for row in rows:
        values = list(row)
        values[5] = f"{float(values[5] or 0):.2f} €"
        inventory_tree.insert("", "end", values=values)

    if "inventory_count_var" in globals():
        inventory_count_var.set(f"{len(rows)} artiklov")


def refresh_stock_from_database(show_message=True):
    """Ponovno prebere skupno mikrofix_inventory.db brez spreminjanja zaloge."""
    try:
        refresh_inventory_table()
        if "service_article_search_tree" in globals():
            search_service_articles()
        if "supplier_tree" in globals():
            refresh_supplier_catalog_table()
        if "status" in globals():
            status.set("Zaloga ponovno prebrana iz baze.")
        if show_message:
            messagebox.showinfo(
                "MikroFIX",
                "Zaloga in cene so ponovno prebrane iz mikrofix_inventory.db."
            )
    except Exception as e:
        messagebox.showerror(
            "MikroFIX",
            f"Zaloge ni bilo mogoče osvežiti:\n{e}"
        )


def refresh_supplier_catalog_table(*_args):
    if "supplier_tree" not in globals():
        return
    search = (
        supplier_search_var.get().strip()
        if "supplier_search_var" in globals()
        else ""
    )
    for iid in supplier_tree.get_children():
        supplier_tree.delete(iid)
    rows = SUPPLIER_STORE.list_catalog(search)
    for row in rows:
        fixed = row["offer_status"] == "OBJAVI"
        pickup = f"{float(row['pickup_price']):.0f} €" if fixed else "PO PONUDBI"
        onsite = f"{float(row['onsite_price']):.0f} €" if fixed else "PO PONUDBI"
        public_price = (
            f"{float(row['public_price']):.2f} €"
            if row["public_price"] is not None else "—"
        )
        target = f"{row['inventory_tier']} • {int(row['target_vehicle'])}+{int(row['target_home'])}"
        checked = str(row.get("last_success") or "")[:16].replace("T", " ") or "—"
        tags = []
        if row["offer_status"] != "OBJAVI":
            tags.append("quote")
        if not str(row["local_status"]).startswith("NA ZALOGI:"):
            tags.append("no_local")
        supplier_tree.insert(
            "", "end",
            values=(
                row["id"], row["model"], row["repair_label"], row["quality"],
                public_price, pickup, onsite, row["local_status"],
                row["supplier_status"], target, checked,
            ),
            tags=tuple(tags),
        )
    if "supplier_count_var" in globals():
        publishable = sum(row["offer_status"] == "OBJAVI" for row in rows)
        supplier_count_var.set(f"{len(rows)} delov • {publishable} s fiksno ceno")


def selected_supplier_catalog_id():
    if "supplier_tree" not in globals():
        return None
    selected = supplier_tree.selection()
    if not selected:
        return None
    values = supplier_tree.item(selected[0], "values")
    return int(values[0]) if values else None


def supplier_add_to_local_inventory():
    catalog_id = selected_supplier_catalog_id()
    if catalog_id is None:
        messagebox.showwarning(
            "MikroFIX", "Izberi dobaviteljski del, ki ga želiš dodati v lokalno bazo."
        )
        return
    try:
        inventory_id = SUPPLIER_STORE.ensure_inventory_item(catalog_id)
        audit_event(
            "SUPPLIER_ITEM_LINKED",
            "inventory_item",
            inventory_id,
            {"supplier_catalog_id": catalog_id},
        )
        refresh_inventory_table()
        refresh_supplier_catalog_table()
        supplier_status_var.set(
            "Del je v lokalni bazi s količino 0. Količino vpiši šele po dejanskem prevzemu."
        )
    except Exception as exc:
        messagebox.showerror("MikroFIX", f"Dela ni bilo mogoče dodati v lokalno bazo:\n{exc}")


def supplier_export_site_prices():
    destination = filedialog.asksaveasfilename(
        parent=root,
        title="Izvozi cenik za spletni kalkulator",
        initialfile="mikrofix-prices.json",
        defaultextension=".json",
        filetypes=[("JSON cenik", "*.json")],
    )
    if not destination:
        return
    try:
        payload = SUPPLIER_STORE.export_site_prices(Path(destination))
        audit_event(
            "SUPPLIER_SITE_EXPORT",
            "supplier",
            None,
            {"models": len(payload["models"])},
        )
        supplier_status_var.set(
            f"Spletni cenik je izvožen: {len(payload['models'])} modelov."
        )
        messagebox.showinfo(
            "MikroFIX",
            "Datoteka mikrofix-prices.json je pripravljena za spletni kalkulator.\n"
            "Dobaviteljske cene in povezave niso vključene.",
        )
    except Exception as exc:
        messagebox.showerror("MikroFIX", f"Spletnega cenika ni bilo mogoče izvoziti:\n{exc}")


def start_supplier_refresh(force=False):
    global supplier_refresh_thread
    if not RUNTIME.settings["supplier"]["fixshop"].get("enabled", True):
        supplier_status_var.set("FixShop osveževanje je izključeno v nastavitvah.")
        return
    if supplier_refresh_thread and supplier_refresh_thread.is_alive():
        supplier_status_var.set("Osveževanje FixShop cen že poteka v ozadju.")
        return
    supplier_status_var.set("Preverjam, ali je 24-urno osveževanje FixShop cen potrebno …")

    def post_to_ui(callback):
        try:
            root.after(0, callback)
            return True
        except (RuntimeError, tk.TclError):
            return False

    def progress(done, total):
        post_to_ui(lambda: supplier_status_var.set(
                f"Osveževanje FixShop: {done}/{total} delov …"
            ))

    def finished(result, error=None):
        if error:
            supplier_status_var.set(
                "Osveževanje ni uspelo. Zadnje uspešne cene ostajajo shranjene in označene."
            )
            if force:
                messagebox.showerror("MikroFIX", f"FixShop osveževanje ni uspelo:\n{error}")
            return
        refresh_supplier_catalog_table()
        if result["status"] == "fresh":
            supplier_status_var.set("FixShop cene so še sveže; novo osveževanje ni potrebno.")
        else:
            supplier_status_var.set(
                f"FixShop končano: {result['succeeded']} novih, "
                f"{result['unchanged']} nespremenjenih, {result['failed']} napak."
            )
        audit_event("SUPPLIER_REFRESH", "supplier", None, result)

    def worker():
        try:
            result = SUPPLIER_STORE.refresh_due(force=force, progress=progress)
            post_to_ui(lambda: finished(result))
        except Exception as exc:
            post_to_ui(lambda error=str(exc): finished({}, error))

    supplier_refresh_thread = threading.Thread(
        target=worker, name="MikroFIX-FixShop", daemon=True
    )
    supplier_refresh_thread.start()


def inventory_add():
    open_inventory_editor(
        root,
        on_saved=lambda _x: refresh_inventory_table()
    )


def inventory_edit(_event=None):
    if "inventory_tree" not in globals():
        return
    sel = inventory_tree.selection()
    if not sel:
        return
    values = inventory_tree.item(sel[0], "values")
    if not values:
        return
    article_id_v = int(values[0])
    open_inventory_editor(
        root,
        article_id=article_id_v,
        on_saved=lambda _x: refresh_inventory_table()
    )


def inventory_delete():
    if "inventory_tree" not in globals():
        return
    sel = inventory_tree.selection()
    if not sel:
        messagebox.showwarning("MikroFix", "Izberi artikel za deaktivacijo.")
        return

    values = inventory_tree.item(sel[0], "values")
    if not values:
        return

    article_id_v = int(values[0])
    article_name_v = values[2]
    barcode_v = values[1]

    if not messagebox.askyesno(
        "Deaktiviraj artikel",
        f"Artikel bo skrit iz novih izbir, zgodovina pa ostane nespremenjena.\n\n"
        f"{article_name_v}\nBarkoda: {barcode_v}"
    ):
        return

    c = inventory_connect()
    row = c.execute(
        "SELECT stock_qty, COALESCE(reserved_qty, 0) FROM inventory_items WHERE id=?",
        (article_id_v,),
    ).fetchone()
    if row and (float(row[0] or 0) > 0 or float(row[1] or 0) > 0):
        c.close()
        messagebox.showwarning(
            "MikroFix",
            "Artikla z zalogo ali rezervirano količino ni mogoče deaktivirati.",
        )
        return
    c.execute(
        "UPDATE inventory_items SET active=0, updated_at=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), article_id_v),
    )
    c.commit()
    c.close()
    audit_event(
        "INVENTORY_DEACTIVATED",
        "inventory_item",
        article_id_v,
        {"barcode": barcode_v},
    )
    refresh_inventory_table()


def remove_selected_part():
    removed = []
    for iid in parts_tree.selection():
        removed.append((parts_tree.index(iid), parts_tree.item(iid, "values")))
        parts_tree.delete(iid)
    if removed and not persist_active_service_parts():
        for index, values in sorted(removed, key=lambda row: row[0]):
            parts_tree.insert("", index, values=values)


def persist_active_service_parts(*, quiet=False):
    """Shrani dele odprtega naloga; zaloga se tu še ne odpisuje."""
    if not active_service_order_id:
        return True
    c = connect()
    try:
        save_service_order_parts(
            c, int(active_service_order_id), collect_parts_from_tree()
        )
    except Exception as exc:
        c.close()
        if not quiet:
            messagebox.showerror(
                "MikroFix – shranjevanje artiklov",
                f"Seznama uporabljenih artiklov ni bilo mogoče shraniti:\n{exc}",
                parent=root,
            )
        return False
    c.close()
    return True


def restore_active_service_parts():
    """Napolni prikaz iz trajnega osnutka izbranega odprtega naloga."""
    if not active_service_order_id:
        return True
    c = connect()
    try:
        rows = load_service_order_parts(c, int(active_service_order_id))
    except Exception as exc:
        c.close()
        messagebox.showerror(
            "MikroFix – nalaganje artiklov",
            f"Shranjeni uporabljeni artikli niso bili naloženi:\n{exc}",
            parent=root,
        )
        return False
    c.close()
    for part_name, part_id, barcode_v, article_id_v, lot_v, price_v in rows:
        parts_tree.insert(
            "", "end",
            values=(
                article_id_v if article_id_v is not None else "",
                part_name or "", part_id or "", barcode_v or "",
                lot_v or "", format_price(price_v),
            ),
        )
    return True

def add_photos():
    paths = filedialog.askopenfilenames(
        title="Dodaj fotografije naprave",
        filetypes=[
            ("Slike", "*.jpg *.jpeg *.png *.webp *.bmp"),
        ]
    )
    if not paths:
        return
    stage = photo_stage.get()
    for p in paths:
        pending_photos.append({"path": p, "stage": stage})
    refresh_photo_list()


def add_approval_evidence():
    files = filedialog.askopenfilenames(
        title="Izberi fotografijo podpisa, e-pošte ali SMS odobritve",
        filetypes=[("Slike", "*.jpg *.jpeg *.png *.webp *.bmp")],
    )
    for path in files:
        pending_photos.append({"path": path, "stage": "ODOBRITEV"})
    refresh_photo_list()


def refresh_photo_list():
    photos_list.delete(0, "end")
    for i, item in enumerate(pending_photos, 1):
        photos_list.insert("end", f"{i}. [{item['stage']}] {Path(item['path']).name}")
    photo_count.set(f"{len(pending_photos)} fotografij")


def remove_selected_photo():
    sel = list(photos_list.curselection())
    for idx in reversed(sel):
        try:
            pending_photos.pop(idx)
        except IndexError:
            pass
    refresh_photo_list()


def copy_photos_for_service(service_id, service_no, c):
    if not pending_photos:
        return []
    target_dir = PHOTOS_DIR / service_no
    target_dir.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for idx, item in enumerate(pending_photos, 1):
            dest = archive_service_image(
                Path(item["path"]),
                target_dir,
                f"{idx:02d}_{item['stage']}",
            )
            created.append(dest)
            rel = dest.relative_to(RUNTIME.data_dir)
            c.execute(
                "INSERT INTO service_photos(service_id, stage, rel_path) VALUES(?,?,?)",
                (service_id, item["stage"], str(rel)),
            )
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return created


service_save_in_progress = False


def _save_service():
    global active_service_order_id, last_saved_service_id, service_save_in_progress

    if service_save_in_progress:
        messagebox.showinfo(
            "MikroFix – zaključek servisa",
            "Zaključevanje servisa že poteka. Počakaj na potrditev ali prikaz napake.",
        )
        return

    if not active_service_order_id:
        messagebox.showwarning(
            "MikroFix - servisno naročilo",
            "Najprej shrani servisno naročilo ob prevzemu naprave. "
            "Končnega servisnega lista brez povezanega naročila ni mogoče izdati."
        )
        try:
            top_nb.select(tab_intake_top)
        except Exception:
            pass
        return

    # Zaključek zahteva dokumentirano identifikacijo. Apple mobilne naprave
    # uporablja MicroDETECT, druga elektronika fizični prepis oznake.
    if not device_is_identified():
        status.set("Zaključek čaka na identifikacijo naprave.")
        messagebox.showwarning(
            "MikroFix - zaključek servisa",
            "Servisnega lista še ni mogoče zaključiti.\n\n"
            "Za podprto Apple mobilno napravo izvedi MicroDETECT. Za računalnik, "
            "konzolo ali drugo nepodprto napravo uporabi »DRUGA NAPRAVA« in "
            "fizično prepiši oznako z naprave."
        )
        try:
            service_detect_button.focus_set()
        except Exception:
            pass
        return

    parts = collect_parts_from_tree()
    service_end_v = datetime.now()
    c = connect()
    try:
        order = get_service_order(c, int(active_service_order_id))
        elapsed_minutes_v = finish_service_work_timer(
            c, int(active_service_order_id), ended_at=service_end_v,
            operator_code=RUNTIME.operator_code, commit=False
        )
        labor = service_labor_from_minutes(
            order.get("contract_context"), elapsed_minutes_v,
            RUNTIME.settings, description="Delo",
        )
        c.rollback()
    except Exception as exc:
        c.rollback()
        c.close()
        messagebox.showerror(
            "MikroFix - zaključek servisa",
            f"Časa dela ni bilo mogoče izračunati:\n{exc}",
        )
        return
    c.close()
    manual_lines = [(
        labor["tier"], labor["description"], 1.0, float(labor["price"]),
        float(labor["price"]), labor["tariff_version"],
        int(labor["billable_minutes"]), float(labor["first_rate"]),
        float(labor["extra_half_hour_rate"]),
    )]
    labor_price_v = float(labor["price"])
    quoted_total_v = float(order.get("quote_eur") or 0)
    prepared_total_v = labor_price_v + sum(float(row[5] or 0) for row in parts)
    approval_required_v = quoted_total_v > 0 and prepared_total_v > quoted_total_v + 0.009
    approved_limit_v = safe_float(service_approved_limit_var.get(), 0)
    approval_channel_v = service_approval_channel_var.get().strip()
    approval_evidence_v = any(
        item.get("stage") == "ODOBRITEV" for item in pending_photos
    )
    if approval_required_v and (
        approved_limit_v + 0.009 < prepared_total_v
        or approval_channel_v not in {"PODPIS", "E-POŠTA", "SMS"}
        or not approval_evidence_v
    ):
        messagebox.showwarning(
            "MikroFix – odobritev dodatnega dela",
            f"Prvotna ponudba je {quoted_total_v:.2f} EUR, pripravljene postavke pa "
            f"{prepared_total_v:.2f} EUR.\n\nVpiši odobreni limit, izberi kanal in "
            "pripni fotografijo podpisa, e-pošte ali SMS odobritve.",
        )
        try:
            service_nb.select(tab_approval)
        except Exception:
            pass
        return
    service_save_in_progress = True
    if "service_save_button" in globals():
        service_save_button.configure(state="disabled", text="SHRANJUJEM...")
    try:
        backup_db("before_save")
    except Exception as exc:
        service_save_in_progress = False
        if "service_save_button" in globals():
            service_save_button.configure(
                state="normal", text="ZAKLJUČI SERVIS IN PRIPRAVI RAČUN"
            )
        messagebox.showerror(
            "MikroFix – varnostna kopija",
            f"Servis ni bil zaključen, ker predhodna varnostna kopija ni uspela:\n{exc}",
        )
        return

    c = connect()
    created_photo_paths = []
    try:
        c.execute("ATTACH DATABASE ? AS inventorydb", (str(INVENTORY_DB),))
        ensure_lot_schema(c, "inventorydb")
        elapsed_minutes_v = finish_service_work_timer(
            c, int(active_service_order_id), ended_at=service_end_v,
            operator_code=RUNTIME.operator_code, commit=False
        )
        no = next_no(c)
        now = datetime.now()
        (
            warranty_scope_v,
            warranty_months_v,
            warranty_until_v,
        ) = service_parts_warranty(
            now,
            bool(parts),
            RUNTIME.settings["warranty"]["installed_parts_months"],
        )
        legacy_part_id = ", ".join([pid for _, pid, _, _, _, _ in parts if pid])
        c.execute(
            """
            UPDATE service_orders
            SET authorized_limit_eur=?,approval_status=?
            WHERE id=?
            """,
            (
                approved_limit_v if approval_required_v else quoted_total_v,
                "APPROVED" if approval_required_v else "NOT_REQUIRED",
                int(active_service_order_id),
            ),
        )

        cur = c.execute("""
            INSERT INTO service(
                service_no, date, customer, customer_address, customer_phone,
                model, serial, imei, ios,
                work, part_id, invoice_no, warranty_until,
                warranty_scope, warranty_months, service_order_id,
                device_type, identity_method, labor_price
                , pricing_mode
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            no,
            now.strftime("%Y-%m-%d %H:%M"),
            customer.get().strip(),
            customer_address.get().strip(),
            customer_phone.get().strip(),
            model.get().strip(),
            serial.get().strip(),
            imei.get().strip(),
            ios.get().strip(),
            work.get("1.0", "end").strip(),
            legacy_part_id,
            invoice_no.get().strip(),
            warranty_until_v,
            warranty_scope_v,
            warranty_months_v,
            int(active_service_order_id),
            device_type_code or "DRUGO",
            device_identity_method or "MANUAL_OTHER",
            labor_price_v,
            "MANUAL_LINES_V29",
        ))
        service_id = cur.lastrowid

        for line_kind_v, description_v, qty_v, unit_price_v, _total_v, version_v, minutes_v, first_v, extra_v in manual_lines:
            c.execute("""
                INSERT INTO service_manual_lines(
                    service_id, line_kind, description, quantity, unit_price,
                    service_tier, billable_minutes, tariff_version, first_rate,
                    extra_half_hour_rate
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                service_id, line_kind_v, description_v, qty_v, unit_price_v,
                line_kind_v, minutes_v, version_v, first_v, extra_v,
            ))

        # Ob pripravi servisa se deli rezervirajo. Fizični odpis nastane šele
        # v isti transakciji kot povezani račun.
        article_counts = {}
        for _pn, _pid, _barcode, article_id_v, _lot, _price in parts:
            if article_id_v is not None:
                article_counts[article_id_v] = article_counts.get(article_id_v, 0) + 1

        for article_id_v, qty_used in article_counts.items():
            cur_stock = c.execute(
                """
                SELECT stock_qty, article_name, COALESCE(reserved_qty,0)
                FROM inventorydb.inventory_items WHERE id=?
                """,
                (article_id_v,)
            ).fetchone()
            if not cur_stock:
                raise RuntimeError(
                    "Eden od skeniranih artiklov ne obstaja več v zalogi."
                )
            available_stock = float(cur_stock[0] or 0) - float(cur_stock[2] or 0)
            if available_stock < qty_used:
                raise RuntimeError(
                    f"Premalo zaloge za artikel: {cur_stock[1]} "
                    f"(na voljo {available_stock}, zahtevano {qty_used})."
                )

        for pn, pid, barcode_v, article_id_v, lot_v, price_v in parts:
            part_cursor = c.execute("""
                INSERT INTO service_parts(
                    service_id, part_name, part_id, barcode, article_id,
                    lot_serial, unit_price
                ) VALUES(?,?,?,?,?,?,?)
            """, (
                service_id, pn, pid, barcode_v, article_id_v,
                lot_v, price_v
            ))
            service_part_id = int(part_cursor.lastrowid)
            if article_id_v is None:
                continue
            reservation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            allocations = allocate_fifo(
                c,
                schema="inventorydb",
                article_id=int(article_id_v),
                quantity=1,
                allocation_type="SERVICE_RESERVATION",
                source_id=int(service_id),
                source_line_id=service_part_id,
                created_at=reservation_time,
            )
            exact_lot = ", ".join(
                str(entry["lot_serial"])
                for entry in allocations if entry["lot_serial"]
            )
            c.execute(
                "UPDATE service_parts SET lot_serial=? WHERE id=?",
                (exact_lot or lot_v, service_part_id),
            )
            stock_row = c.execute(
                "SELECT stock_qty,COALESCE(reserved_qty,0),location "
                "FROM inventorydb.inventory_items WHERE id=?",
                (int(article_id_v),),
            ).fetchone()
            c.execute(
                "UPDATE inventorydb.inventory_items "
                "SET reserved_qty=COALESCE(reserved_qty,0)+1,updated_at=? WHERE id=?",
                (reservation_time, int(article_id_v)),
            )
            c.execute("""
                INSERT INTO inventorydb.stock_movements(
                    movement_no, created_at, article_id, quantity_delta,
                    balance_after, movement_type, source_type, source_id,
                    source_line_id, lot_serial, location_from, location_to,
                    reason, operator_code, reversal_of_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """, (
                f"REZ-SRV-{service_id:06d}-{service_part_id}", reservation_time,
                int(article_id_v), 0, float(stock_row[0] or 0),
                "RESERVE_SERVICE", "SERVICE", int(service_id), service_part_id,
                exact_lot or lot_v or "", stock_row[2] or "", "SERVIS",
                f"Rezervacija za servis {no}", "DR",
            ))

        if device_type_code == "APPLE_MOBILE":
            for stage in ("PRED", "PO"):
                vals = get_test_values(stage)
                for test_name, test_status in vals.items():
                    c.execute("""
                        INSERT INTO service_tests(service_id, stage, test_name, status)
                        VALUES(?,?,?,?)
                    """, (service_id, stage, test_name, test_status))

        created_photo_paths = copy_photos_for_service(service_id, no, c)
        link_service_result(
            c, int(active_service_order_id), int(service_id), commit=False
        )
        c.execute(
            "DELETE FROM service_order_parts WHERE service_order_id=?",
            (int(active_service_order_id),),
        )
        c.commit()
    except Exception as exc:
        c.rollback()
        c.close()
        for photo_path in created_photo_paths:
            photo_path.unlink(missing_ok=True)
        service_save_in_progress = False
        if "service_save_button" in globals():
            service_save_button.configure(
                state="normal", text="ZAKLJUČI SERVIS IN PRIPRAVI RAČUN"
            )
        messagebox.showerror("MikroFIX", f"Servis ni bil shranjen:\n{exc}")
        return
    c.close()

    # Podatki so od te točke že potrjeni v bazi. Gumb zato vedno sprostimo,
    # tudi če kasnejši audit, PDF ali osvežitev uporabniškega vmesnika odpove.
    last_saved_service_id = int(service_id)
    service_save_in_progress = False
    if "service_save_button" in globals():
        service_save_button.configure(
            state="normal", text="ZAKLJUČI SERVIS IN PRIPRAVI RAČUN"
        )

    post_save_warnings = []
    try:
        audit_event(
            "SERVICE_CREATED",
            "service",
            service_id,
            {
                "service_no": no,
                "prepared_total_eur": prepared_total_v,
                "approval_required": approval_required_v,
                "approval_channel": approval_channel_v if approval_required_v else "",
                "parts_count": len(parts),
                "elapsed_minutes": elapsed_minutes_v,
                "billable_minutes": labor["billable_minutes"],
                "service_tier": labor["tier"],
            },
        )
    except Exception as exc:
        post_save_warnings.append(f"audit: {exc}")
    try:
        backup_db("after_save")
    except Exception as exc:
        post_save_warnings.append(f"varnostna kopija: {exc}")
    try:
        refresh_inventory_table()
    except Exception as exc:
        post_save_warnings.append(f"osvežitev zaloge: {exc}")

    pdf_msg = ""
    try:
        pdf_path = generate_pdf(service_id)
        pdf_msg = f"\nPDF: {pdf_path.name}"
    except Exception as e:
        pdf_msg = f"\nPDF ni bil ustvarjen: {e}"

    status.set(f"Servisni list shranjen: {no}")
    warranty_message = (
        f"Tržna garancija na vgrajene dele: {warranty_months_v} mesece, "
        f"do {format_print_date(warranty_until_v)}."
        if warranty_scope_v == WARRANTY_SCOPE_INSTALLED_PARTS
        else "Vgrajen del ni evidentiran; dodatna tržna garancija na dele ni določena."
    )
    open_cashier_now = messagebox.askyesno(
        "MikroFix – servis je pripravljen za račun",
        f"Servisni list {no} je shranjen.\n{warranty_message}{pdf_msg}"
        + (f"\nOpozorilo: {'; '.join(post_save_warnings)}" if post_save_warnings else "")
        + "\n\n"
        "Servis še ni dokončno zaključen. Zaključen bo šele, ko blagajna izda "
        "povezani račun.\n\nOdprim blagajno s prenesenimi postavkami zdaj?"
    )
    clear_form(keep_device=True)
    active_service_order_id = None
    refresh_device_history_count()
    refresh_service_tab_history()
    show_service_history()
    if open_cashier_now:
        root.after(100, lambda sid=int(service_id): prepare_invoice_for_service(sid))


def save():
    """GUI meja: nobena napaka zaključka servisa ne sme ostati samo v terminalu."""
    global service_save_in_progress
    try:
        return _save_service()
    except Exception as exc:
        service_save_in_progress = False
        if "service_save_button" in globals():
            try:
                service_save_button.configure(
                    state="normal", text="ZAKLJUČI SERVIS IN PRIPRAVI RAČUN"
                )
            except Exception:
                pass
        try:
            status.set("Zaključek servisa ni uspel.")
        except Exception:
            pass
        messagebox.showerror(
            "MikroFix – zaključek servisa",
            f"Servis ni bil zaključen. Podatki odprtega servisa so ostali shranjeni.\n\n{exc}",
        )
        return None


def clear_form(keep_device=True):
    global device_identity_method, device_type_code
    customer.set("")
    customer_address.set("")
    customer_phone.set("")
    invoice_no.set("")
    work.delete("1.0", "end")
    if "service_lines_tree" in globals():
        for iid in service_lines_tree.get_children():
            service_lines_tree.delete(iid)
    if "service_lines_total_var" in globals():
        service_lines_total_var.set("Skupaj storitve: 0,00 €")
    for iid in parts_tree.get_children():
        parts_tree.delete(iid)
    if "service_article_name_var" in globals():
        service_article_name_var.set("")
    if "service_article_barcode_var" in globals():
        service_article_barcode_var.set("")
    pending_photos.clear()
    refresh_photo_list()
    if "service_approved_limit_var" in globals():
        service_approved_limit_var.set("")
    if "service_approval_channel_var" in globals():
        service_approval_channel_var.set("")
    for var in list(pre_test_vars.values()) + list(post_test_vars.values()):
        var.set("NI TESTIRANO")
    if not keep_device:
        device_identity_method = ""
        device_type_code = ""
        name.set("")
        model.set("")
        ios.set("")
        storage.set("")
        serial.set("")
        imei.set("")
        serial_display.set("")
        imei_display.set("")
        connected_state.set("NI POVEZAVE")
        connected_label.configure(fg=GRAY)
        header_connected_label.configure(fg=GRAY)
        set_quick_tests_enabled(False)
        update_workflow_lock()
        refresh_claims_tab()
    status.set("Pripravljen za nov servisni zapis.")


def prepare_invoice_for_service(service_id=None):
    """Odpri blagajno iste prijavljene seje z računom iz servisa."""
    target_id = service_id or last_saved_service_id or selected_service_history_id()
    if not target_id:
        messagebox.showwarning(
            "MikroFIX",
            "Najprej zaključi servis ali v zgodovini označi servis za račun.",
        )
        return
    if not AUTH_SESSION.has_permission(PERMISSION_CASHIER):
        audit_event("PERMISSION_DENIED", "module", "CASHIER")
        messagebox.showwarning(
            "MikroFIX", "Za pripravo računa nimaš dovoljenja za Blagajno."
        )
        return
    status.set("Blagajna se odpira s predizpolnjenim osnutkom računa.")
    launch_office("--new-invoice", "--service-id", str(int(target_id)))


last_intake_pdf_path = None
last_intake_tag_path = None
last_buyback_pdf_path = None
buyback_save_in_progress = False
prepared_buyback_id = None
prepared_buyback_no = ""
buyback_source_service_order_id = None


def _text_value(widget):
    return widget.get("1.0", "end").strip()


def service_installed_parts_cost(service_id):
    """Vrne sledljivo nabavno vrednost že vgrajenih zalogovnih delov."""
    if not service_id:
        return 0.0
    c = connect()
    try:
        article_ids = [
            int(row[0]) for row in c.execute(
                """
                SELECT article_id FROM service_parts
                WHERE service_id=? AND article_id IS NOT NULL
                """,
                (int(service_id),),
            ).fetchall()
        ]
    finally:
        c.close()
    if not article_ids:
        return 0.0
    inv = inventory_connect()
    try:
        total = 0.0
        for article_id in article_ids:
            row = inv.execute(
                "SELECT acquisition_cost FROM inventory_items WHERE id=?",
                (article_id,),
            ).fetchone()
            if row:
                total += float(row[0] or 0)
        return round(total, 2)
    finally:
        inv.close()


def start_service_buyback_offer():
    """Prenese odprti servis v predizpolnjeno ponudbo za odkup."""
    global buyback_source_service_order_id
    target_order_id = active_service_order_id
    if not target_order_id and last_saved_service_id:
        lookup = connect()
        try:
            row = lookup.execute(
                "SELECT service_order_id FROM service WHERE id=?",
                (int(last_saved_service_id),),
            ).fetchone()
            target_order_id = int(row[0]) if row and row[0] else None
        finally:
            lookup.close()
    if not target_order_id:
        messagebox.showwarning(
            "MikroFix - ponudba za odkup",
            "Najprej odpri ali ustvari servisno naročilo.",
        )
        return
    c = connect()
    try:
        record = get_service_order(c, int(target_order_id))
        begin_service_buyback_offer(
            c, int(target_order_id), operator_code=RUNTIME.operator_code,
            commit=False,
        )
        c.commit()
    except Exception as exc:
        c.rollback()
        messagebox.showerror("MikroFix - ponudba za odkup", str(exc))
        return
    finally:
        c.close()

    reset_buyback_form(
        status_message=(
            f"Servisno naročilo {record['order_no']} je začasno zaklenjeno, "
            "dokler stranka odloča o odkupu."
        )
    )
    buyback_source_service_order_id = int(target_order_id)
    buyback_seller_name.set(str(record.get("customer_name") or ""))
    buyback_address.set(str(record.get("customer_address") or ""))
    buyback_contact.set(
        str(record.get("customer_email") or record.get("customer_phone") or "")
    )
    buyback_accessories.set(str(record.get("accessories") or ""))
    buyback_condition_text.delete("1.0", "end")
    buyback_condition_text.insert("1.0", str(record.get("intake_condition") or ""))
    buyback_notes.set(f"Ponudba iz servisnega naročila {record['order_no']}")
    top_nb.tab(tab_buyback_top, state="normal", text="Ponudba za odkup")
    top_nb.select(tab_buyback_top)


def cancel_service_buyback_offer_ui():
    global buyback_source_service_order_id
    if not buyback_source_service_order_id:
        messagebox.showinfo("MikroFix", "Ta odkup ni nastal iz odprtega servisa.")
        return
    if prepared_buyback_id:
        cancel_prepared_buyback()
        return
    if not messagebox.askyesno(
        "MikroFix - ponudba za odkup",
        "Stranka je ponudbo zavrnila? Servis se bo vrnil v odprto stanje.",
    ):
        return
    c = connect()
    try:
        cancel_service_buyback_offer(
            c, int(buyback_source_service_order_id), commit=False
        )
        c.commit()
    except Exception as exc:
        c.rollback()
        messagebox.showerror("MikroFix", str(exc))
        return
    finally:
        c.close()
    reset_buyback_form()
    top_nb.tab(tab_buyback_top, state="hidden", text="Odkup")
    top_nb.select(tab_service_top)


def clear_buyback_form_safely():
    if buyback_source_service_order_id:
        cancel_service_buyback_offer_ui()
    else:
        reset_buyback_form()


def save_intake_from_tab():
    """Shrani prevzem in PDF brez zagona obračunljivega servisnega časa."""
    global active_service_order_id, last_intake_pdf_path, last_intake_tag_path
    global intake_customer_partner_id

    if not require_identified_device("servisno naročilo in osebne podatke"):
        return

    # Če je za isto napravo sprejem že odprt, gumb ne sme ustvariti dvojnika
    # in predvsem ne sme samodejno zagnati njegovega časovnika.
    c = connect()
    try:
        existing_order_id = active_service_order_id or latest_open_service_order(
            c, serial.get().strip(), imei.get().strip()
        )
    finally:
        c.close()
    if existing_order_id:
        active_service_order_id = int(existing_order_id)
        messagebox.showinfo(
            "MikroFix – sprejem že obstaja",
            "Za to napravo je sprejem že shranjen. Čas ni bil zagnan. "
            "Za dejansko delo uporabi »ODPRI / NADALJUJ ODPRTI SERVIS«.",
            parent=root,
        )
        return

    payload = {
        "customer_partner_id": intake_customer_partner_id,
        "customer_name": intake_customer_name.get(),
        "customer_address": intake_customer_address.get(),
        "customer_phone": intake_customer_phone.get(),
        "customer_email": intake_customer_email.get(),
        "device_model": model.get(),
        "device_serial": serial.get(),
        "device_imei": imei.get(),
        "device_os": ios.get(),
        "device_storage": storage.get(),
        "device_type": device_type_code or "DRUGO",
        "identity_method": device_identity_method or "MICRODETECT",
        "customer_request": _text_value(intake_request_text),
        "intake_condition": _text_value(intake_condition_text),
        "accessories": intake_accessories.get(),
        "quote_eur": intake_quote.get(),
        "logistics_eur": intake_logistics.get(),
        "expected_due": intake_due.get(),
        "contract_context": _stored_value(
            CONTRACT_CONTEXT_LABELS, intake_context.get()
        ),
        "immediate_start_requested": intake_immediate_start.get(),
        "withdrawal_loss_ack": intake_withdrawal_ack.get(),
        "delivery_channel": _stored_value(
            DELIVERY_LABELS, intake_delivery.get()
        ),
        "microdetect_confirmed": device_is_microdetected(),
        "manual_identity_confirmed": device_identity_method == "MANUAL_OTHER",
        "microdetect_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "operator_code": RUNTIME.operator_code,
        "notes": _text_value(intake_notes_text),
    }

    try:
        backup_db("before_service_order")
        c = connect()
        try:
            order_id, order_no = create_service_order(c, payload, commit=False)
            record = get_service_order(c, order_id)
            last_intake_pdf_path = generate_service_order_pdf(
                record, PDF_DIR, test_mode=RUNTIME.is_test
            )
            last_intake_tag_path = generate_service_tag_pdf(
                record, PDF_DIR, test_mode=RUNTIME.is_test
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        active_service_order_id = order_id
        audit_event(
            "SERVICE_ORDER_CREATED", "service_order", order_id,
            {"order_no": order_no, "device": serial.get().strip()[-4:]},
        )
        backup_db("after_service_order")
    except Exception as exc:
        messagebox.showerror("MikroFix - servisno naročilo", str(exc))
        return

    intake_order_status.set(
        f"Odprto naročilo {order_no} | PDF + 80-mm oznaka pripravljena"
    )
    tag_print_ok = False
    tag_print_notice = ""
    tag_printer = str(
        RUNTIME.settings.get("printer", {}).get("printer_name")
        or RUNTIME.settings.get("printer", {}).get("windows_printer_name")
        or ""
    ).strip()
    try:
        if not tag_printer:
            raise RuntimeError("80-mm tiskalnik ni nastavljen.")
        print_result = send_pdf_to_printer(last_intake_tag_path, tag_printer)
        tag_print_ok = bool(print_result.completed)
        tag_print_notice = (
            f"80-mm QR oznaka je natisnjena na {tag_printer}."
            if tag_print_ok
            else f"80-mm QR oznaka je poslana na {tag_printer}: {print_result.detail}"
        )
        audit_event(
            "SERVICE_ORDER_TAG_PRINTED", "service_order", order_id,
            {"order_no": order_no, "printer": tag_printer, "confirmed": tag_print_ok},
        )
    except Exception as exc:
        tag_print_notice = (
            f"Samodejni tisk 80-mm oznake ni uspel: {exc} "
            "Sprejem je varno shranjen; izpis lahko ponoviš z gumbom."
        )
        audit_event(
            "SERVICE_ORDER_TAG_PRINT_FAILED", "service_order", order_id,
            {"order_no": order_no, "printer": tag_printer, "error": str(exc)},
        )
    saved_order_id = int(order_id)
    active_service_order_id = None
    clear_intake_form()
    clear_form(keep_device=False)
    top_nb.select(tab_device_top)
    status.set(
        f"Sprejem {order_no} je shranjen. Pripravljeno za naslednjo napravo."
    )
    show_intake_completion(
        saved_order_id, order_no, tag_print_ok=tag_print_ok,
        tag_print_notice=tag_print_notice,
    )


def open_last_intake_pdf():
    if last_intake_pdf_path and Path(last_intake_pdf_path).exists():
        show_pdf_actions(last_intake_pdf_path, "Servisno naročilo", root)
    else:
        messagebox.showinfo("MikroFix", "V tej seji še ni ustvarjenega servisnega naročila PDF.")


def open_last_intake_tag():
    if last_intake_tag_path and Path(last_intake_tag_path).exists():
        show_pdf_actions(last_intake_tag_path, "80-mm servisna oznaka", root)
    else:
        messagebox.showinfo("MikroFix", "V tej seji še ni ustvarjene 80-mm servisne oznake.")


def open_service_order_intake_output(order_id, output_kind, parent=None):
    """Ponovno pripravi in odpre sprejemni dokument shranjenega naloga."""
    parent = parent or root
    c = connect()
    try:
        record = get_service_order(c, int(order_id))
    except Exception as exc:
        c.close()
        messagebox.showerror(
            "MikroFix – dokument sprejema", str(exc), parent=parent
        )
        return
    c.close()
    try:
        if output_kind == "A4":
            path = generate_service_order_pdf(
                record, PDF_DIR, test_mode=RUNTIME.is_test
            )
            label = f"servisno naročilo {record['order_no']}"
        elif output_kind == "TAG":
            path = generate_service_tag_pdf(
                record, PDF_DIR, test_mode=RUNTIME.is_test
            )
            label = f"80-mm servisna oznaka {record['order_no']}"
        else:
            raise ValueError("Neznana vrsta sprejemnega dokumenta.")
    except Exception as exc:
        messagebox.showerror(
            "MikroFix – dokument sprejema",
            f"Dokumenta ni bilo mogoče pripraviti:\n{exc}",
            parent=parent,
        )
        return
    show_pdf_actions(path, label, parent)


def show_intake_completion(
    order_id, order_no, *, tag_print_ok=False, tag_print_notice=""
):
    """Po sprejemu ohrani dostop do obeh izpisov in čakalne vrste."""
    win = tk.Toplevel(root)
    win.title("MikroFix – sprejem je shranjen")
    win.geometry("600x430")
    win.resizable(False, False)
    win.configure(bg=BG)
    win.transient(root)
    win.grab_set()

    card = tk.Frame(
        win, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True, padx=22, pady=22)
    tk.Label(
        card, text=f"Sprejem {order_no} je ustvarjen",
        bg=PANEL, fg=DARK, font=("Segoe UI", 16, "bold"),
    ).pack(anchor="w", padx=18, pady=(18, 5))
    tk.Label(
        card,
        text=(
            "A4 servisno naročilo in 80-mm Epson oznaka sta pripravljena. "
            "Servisni čas še ne teče; začne se šele ob izbiri naloga."
        ),
        bg=PANEL, fg=GRAY, font=("Segoe UI", 10),
        justify="left", wraplength=520,
    ).pack(anchor="w", padx=18, pady=(0, 16))
    if tag_print_notice:
        tk.Label(
            card, text=tag_print_notice,
            bg=PANEL, fg=SUCCESS if tag_print_ok else DANGER,
            font=("Segoe UI", 9, "bold"), justify="left", wraplength=520,
        ).pack(anchor="w", padx=18, pady=(0, 10))
    ttk.Button(
        card, text="A4 PDF / TISK",
        command=lambda: open_service_order_intake_output(order_id, "A4", win),
        style="Blue.TButton",
    ).pack(fill="x", padx=18, pady=5)
    ttk.Button(
        card, text="80-mm OZNAKA / TISK",
        command=lambda: open_service_order_intake_output(order_id, "TAG", win),
    ).pack(fill="x", padx=18, pady=5)

    def open_queue():
        win.destroy()
        open_workflow_module(tab_service_top, "SERVICE")
        show_service_history()
        choose_open_service_order()

    ttk.Button(
        card, text="PREGLEJ ODPRTE SPREJEME",
        command=open_queue,
    ).pack(fill="x", padx=18, pady=5)
    ttk.Button(card, text="ZAPRI", command=win.destroy).pack(
        fill="x", padx=18, pady=(12, 5)
    )


def clear_intake_form():
    """Počisti osebne in pogodbene podatke, identiteto naprave pa pusti pri miru."""
    global intake_customer_partner_id
    intake_customer_partner_id = None
    for variable in (
        intake_customer_name, intake_customer_address, intake_customer_phone,
        intake_customer_email, intake_accessories,
    ):
        variable.set("")
    intake_quote.set("0.00")
    intake_logistics.set("0.00")
    intake_due.set("1-2 delovna dneva")
    intake_context.set(_display_value(CONTRACT_CONTEXT_LABELS, "MFT_TEREN"))
    intake_delivery.set(_display_value(DELIVERY_LABELS, "EMAIL_PDF"))
    intake_immediate_start.set(False)
    intake_withdrawal_ack.set(False)
    for widget in (intake_request_text, intake_condition_text, intake_notes_text):
        widget.delete("1.0", "end")
    intake_order_status.set("Novo servisno naročilo.")


def choose_intake_customer():
    """Izbere obstoječega kupca ali ga ločeno pridobi iz AJPES/ročnega vnosa."""
    global intake_customer_partner_id
    dialog = tk.Toplevel(root)
    dialog.title("MikroFix – stranka servisnega naročila")
    dialog.transient(root)
    dialog.grab_set()
    dialog.geometry("760x520")
    dialog.minsize(680, 470)
    partners = list_customer_partners(OFFICE_DB)
    by_display = {}
    displays = []
    for partner in partners:
        display = (
            f"{partner['name']} | {partner['tax_number'] or partner['address'] or '-'} "
            f"| ID {partner['id']}"
        )
        displays.append(display)
        by_display[display] = partner
    partner_var = tk.StringVar()
    selected_id = {"value": None}
    local = {
        "name": tk.StringVar(value=intake_customer_name.get()),
        "address": tk.StringVar(value=intake_customer_address.get()),
        "phone": tk.StringVar(value=intake_customer_phone.get()),
        "email": tk.StringVar(value=intake_customer_email.get()),
        "tax_number": tk.StringVar(),
    }
    frame = ttk.Frame(dialog, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Obstoječa stranka").grid(row=0, column=0, sticky="w", pady=5)
    combo = ttk.Combobox(frame, textvariable=partner_var, values=displays, state="readonly")
    combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=5)

    def fill(customer, partner_id=None):
        for key, variable in local.items():
            variable.set(str(customer.get(key) or ""))
        selected_id["value"] = partner_id

    def choose_existing(_event=None):
        partner = by_display.get(partner_var.get())
        if partner:
            fill(partner, partner["id"])

    entries = []
    for row, (label, key) in enumerate((
        ("Naziv / ime", "name"), ("Naslov", "address"),
        ("Telefon", "phone"), ("E-pošta", "email"),
        ("Davčna številka", "tax_number"),
    ), start=1):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(frame, textvariable=local[key])
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 6), pady=4)
        entries.append(entry)

    def manual_mode():
        partner_var.set("")
        fill({key: "" for key in local})
        entries[0].focus_set()

    def ajpes_mode():
        tax = "".join(ch for ch in local["tax_number"].get() if ch.isdigit())
        if not tax:
            tax = simpledialog.askstring(
                "AJPES – stranka", "Vpiši davčno številko:", parent=dialog
            ) or ""
        try:
            found = find_partner_by_tax_number(
                tax, test_mode=RUNTIME.is_test, timeout=15
            )
        except AjpesError as exc:
            messagebox.showwarning(
                "AJPES – stranka", f"{exc}\n\nRočni vnos ostaja omogočen.",
                parent=dialog,
            )
            return
        fill({
            "name": found.name,
            "address": ", ".join(x for x in (found.address, found.post_city) if x),
            "phone": local["phone"].get(), "email": local["email"].get(),
            "tax_number": found.tax_number,
        })

    ttk.Button(frame, text="NOV / ROČNI VNOS", command=manual_mode).grid(row=1, column=2, padx=(4, 0))
    ttk.Button(frame, text="AJPES PO DAVČNI", command=ajpes_mode).grid(row=5, column=2, padx=(4, 0))
    combo.bind("<<ComboboxSelected>>", choose_existing)
    frame.columnconfigure(1, weight=1)

    def save_selection():
        global intake_customer_partner_id
        customer_data = {key: variable.get().strip() for key, variable in local.items()}
        try:
            partner_id = save_customer_partner(
                OFFICE_DB, customer_data, preferred_partner_id=selected_id["value"]
            )
            saved = customer_partner_by_id(OFFICE_DB, partner_id)
            if not saved:
                raise WorkflowError("Stranke po shranjevanju ni mogoče prebrati.")
        except Exception as exc:
            messagebox.showerror("Stranka", str(exc), parent=dialog)
            return
        intake_customer_partner_id = partner_id
        intake_customer_name.set(saved["name"])
        intake_customer_address.set(saved["address"])
        intake_customer_phone.set(saved["phone"])
        intake_customer_email.set(saved["email"])
        intake_order_status.set(f"Izbrana stranka: {saved['name']} (ID {partner_id})")
        dialog.destroy()

    actions = ttk.Frame(dialog, padding=(14, 0, 14, 14))
    actions.pack(fill="x")
    ttk.Button(actions, text="PREKLIČI", command=dialog.destroy).pack(side="left")
    ttk.Button(actions, text="SHRANI IN IZBERI", command=save_selection).pack(side="right")
    (combo if displays else entries[0]).focus_set()


def reset_buyback_form(*, enable_save=True, status_message=None):
    global buyback_save_in_progress, prepared_buyback_id, prepared_buyback_no
    global buyback_source_service_order_id
    for variable in (
        buyback_seller_name, buyback_tax_number, buyback_address,
        buyback_contact, buyback_iban, buyback_color_capacity,
        buyback_accessories, buyback_acquisition_details,
        buyback_evidence, buyback_imei_reference, buyback_risk_reason,
        buyback_price, buyback_notes,
    ):
        variable.set("")
    buyback_identity_checked.set(False)
    buyback_acquisition_method.set(
        _display_value(ACQUISITION_LABELS, "KUPLJENA_RABLJENA_FIZICNA_OSEBA")
    )
    buyback_evidence.set(_display_value(EVIDENCE_LABELS, "NI_NA_VOLJO"))
    buyback_price.set("0.00")
    buyback_payout.set(_display_value(PAYOUT_LABELS, "BANK_TRANSFER"))
    buyback_imei_provider.set("TESTNI SIMULATOR" if RUNTIME.is_test else "")
    buyback_imei_result.set(
        _display_value(IMEI_RESULT_LABELS, "TEST_CLEAR") if RUNTIME.is_test else ""
    )
    buyback_account_removed.set(False)
    buyback_activation_removed.set(False)
    buyback_mdm_clear.set(False)
    buyback_ownership.set(False)
    buyback_privacy.set(False)
    buyback_risk.set(_display_value(RISK_LABELS, "GREEN"))
    buyback_purpose.set(_display_value(BUYBACK_PURPOSE_LABELS, "UNDECIDED"))
    buyback_condition_text.delete("1.0", "end")
    buyback_save_in_progress = False
    prepared_buyback_id = None
    prepared_buyback_no = ""
    buyback_source_service_order_id = None
    if "buyback_save_button" in globals():
        buyback_save_button.configure(state="normal" if enable_save else "disabled")
    if "buyback_complete_button" in globals():
        buyback_complete_button.configure(state="disabled")
    if "buyback_cancel_button" in globals():
        buyback_cancel_button.configure(state="disabled")
    buyback_status_text.set(
        status_message or "Nov odkup - osebni podatki še niso shranjeni."
    )


def _find_pending_buyback():
    c = connect()
    try:
        row = c.execute(
            """
            SELECT id, buyback_no FROM buybacks
            WHERE status='CONTRACT_READY'
              AND COALESCE(payment_status, 'PENDING')='PENDING'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return row
    finally:
        c.close()


def resume_pending_buyback():
    global prepared_buyback_id, prepared_buyback_no, last_buyback_pdf_path
    global buyback_save_in_progress, buyback_source_service_order_id
    pending = _find_pending_buyback()
    if not pending:
        return False
    prepared_buyback_id, prepared_buyback_no = int(pending[0]), str(pending[1])
    c = connect()
    try:
        source = c.execute(
            "SELECT source_service_order_id FROM buybacks WHERE id=?",
            (prepared_buyback_id,),
        ).fetchone()
        buyback_source_service_order_id = int(source[0]) if source and source[0] else None
    finally:
        c.close()
    candidate = PDF_DIR / f"{prepared_buyback_no}_odkupna_pogodba_TEST.pdf"
    if candidate.exists():
        last_buyback_pdf_path = candidate
    buyback_save_in_progress = False
    buyback_status_text.set(
        f"{prepared_buyback_no} čaka na podpis in potrditev izplačila. "
        "Do takrat naprava ni zaloga MikroFIX."
    )
    if "buyback_save_button" in globals():
        buyback_save_button.configure(state="disabled")
    if "buyback_complete_button" in globals():
        buyback_complete_button.configure(state="normal")
    if "buyback_cancel_button" in globals():
        buyback_cancel_button.configure(state="normal")
    return True


def complete_prepared_buyback():
    global prepared_buyback_id, prepared_buyback_no
    if not prepared_buyback_id and not resume_pending_buyback():
        messagebox.showinfo("MikroFix - odkup", "Ni pripravljenega odkupa za potrditev.")
        return
    c = connect()
    try:
        record = get_buyback(c, prepared_buyback_id)
    finally:
        c.close()
    if record["payout_method"] == "CASH":
        cash_session,_cash_entries=cash_session_snapshot(
            OFFICE_DB,premise_id=RUNTIME.active_premise,
            device_id=RUNTIME.electronic_device,
        )
        if not cash_session:
            messagebox.showerror(
                "MikroFix - odkup",
                "Gotovinskega odkupa ni mogoče potrditi, dokler blagajna ni odprta. "
                "Odpri Blagajno in njen blagajniški dnevnik.",
            )
            return
        if float(cash_session["expected_cash"]) < float(record["purchase_price"]):
            messagebox.showerror(
                "MikroFix - odkup",
                "V odprti blagajni ni dovolj evidentirane gotovine za ta izdatek.",
            )
            return
        confirmation = (
            f"Potrdi samo, če je prodajalec podpisal pogodbo in blagajniški izdatek "
            f"{record.get('cash_disbursement_no') or '-'} ter je prejel točno "
            f"{float(record['purchase_price']):.2f} EUR v gotovini."
        )
    else:
        confirmation = (
            f"Potrdi samo, če je prodajalec podpisal pogodbo in je bilo nakazilo "
            f"{float(record['purchase_price']):.2f} EUR izvedeno na IBAN "
            f"{record.get('seller_iban') or '-'} ."
        )
    if not messagebox.askyesno(
        "MikroFix - dokončanje odkupa",
        confirmation + "\n\nPo potrditvi dokumenta ni več mogoče popravljati.",
    ):
        return
    try:
        backup_db("before_buyback_completion")
        purpose_code = str(record.get("purpose") or "UNDECIDED")
        business = RUNTIME.settings["business"]
        result = complete_buyback_atomic(
            service_db=DB,
            office_db=OFFICE_DB,
            inventory_db=INVENTORY_DB,
            buyback_id=prepared_buyback_id,
            purpose=purpose_code,
            buyer={
                "legal_name": business.get("legal_name", "MikroFIX"),
                "tax_number": business.get("tax_number", ""),
                "registration_number": business.get("registration_number", ""),
                "address": COMPANY_ADDRESS,
                "representative": "Daniel Ristoski",
            },
            premise_id=RUNTIME.active_premise,
            device_id=RUNTIME.electronic_device,
            operator_code=RUNTIME.operator_code,
        )
        inventory_code = str(result["inventory_code"])
        audit_event(
            "BUYBACK_COMPLETED", "buyback", prepared_buyback_id,
            {"buyback_no": prepared_buyback_no, "inventory_code": inventory_code},
        )
        backup_db("after_buyback_completion")
    except Exception as exc:
        messagebox.showerror("MikroFix - odkup", str(exc))
        return
    completed_no = prepared_buyback_no
    messagebox.showinfo(
        "MikroFix - odkup",
        f"Odkup {completed_no} je dokončno potrjen. Naprava je v karanteni "
        f"pod oznako {inventory_code}.",
    )
    reset_buyback_form()


def cancel_prepared_buyback():
    global prepared_buyback_id, prepared_buyback_no
    if not prepared_buyback_id and not resume_pending_buyback():
        return
    if not messagebox.askyesno(
        "MikroFix - preklic osnutka",
        f"Prekliči nepotrjeni osnutek {prepared_buyback_no}? Zapis bo ostal v zgodovini, "
        "zaloga in blagajna pa se ne bosta spremenili.",
    ):
        return
    c = connect()
    try:
        c.execute(
            """
            UPDATE buybacks SET status='CANCELLED'
            WHERE id=? AND status='CONTRACT_READY'
            """,
            (prepared_buyback_id,),
        )
        c.execute(
            """
            UPDATE cash_disbursements SET status='CANCELLED'
            WHERE buyback_id=? AND status='PREPARED'
            """,
            (prepared_buyback_id,),
        )
        if buyback_source_service_order_id:
            cancel_service_buyback_offer(
                c, int(buyback_source_service_order_id), commit=False
            )
        c.commit()
    finally:
        c.close()
    audit_event(
        "BUYBACK_DRAFT_CANCELLED", "buyback", prepared_buyback_id,
        {"buyback_no": prepared_buyback_no},
    )
    reset_buyback_form()


def save_buyback_from_tab():
    """Shrani odkup in izdela pogodbo; identifikatorji so samo iz MicroDETECT."""
    global last_buyback_pdf_path, buyback_save_in_progress
    global prepared_buyback_id, prepared_buyback_no, buyback_source_service_order_id

    if buyback_save_in_progress:
        return

    if _find_pending_buyback():
        resume_pending_buyback()
        messagebox.showwarning(
            "MikroFix - odkup",
            "Najprej dokončaj ali prekliči že pripravljen odkup. "
            "Nepotrjen dokument ne ustvari zaloge.",
        )
        return

    if not require_identified_device("odkup in podatke prodajalca"):
        return
    if not imei.get().strip():
        messagebox.showerror(
            "MikroFix - odkup",
            "MicroDETECT ni vrnil IMEI. Odkupa ni mogoče zaključiti."
        )
        return

    buyback_save_in_progress = True
    if "buyback_save_button" in globals():
        buyback_save_button.configure(state="disabled")

    payload = {
        "seller_name": buyback_seller_name.get(),
        "seller_tax_number": buyback_tax_number.get(),
        "seller_birth_date": "",
        "seller_address": buyback_address.get(),
        "seller_contact": buyback_contact.get(),
        "seller_iban": buyback_iban.get(),
        "identity_checked": buyback_identity_checked.get(),
        "device_model": model.get(),
        "device_serial": serial.get(),
        "device_imei": imei.get(),
        "device_os": ios.get(),
        "device_storage": storage.get(),
        "color_capacity": buyback_color_capacity.get(),
        "accessories": buyback_accessories.get(),
        "condition_text": _text_value(buyback_condition_text),
        "acquisition_method": _stored_value(
            ACQUISITION_LABELS, buyback_acquisition_method.get()
        ),
        "acquisition_details": buyback_acquisition_details.get(),
        "evidence_type": _stored_value(EVIDENCE_LABELS, buyback_evidence.get()),
        "imei_check_provider": "",
        "imei_check_result": "NOT_REQUIRED",
        "imei_check_reference": "",
        "imei_checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account_removed": buyback_account_removed.get(),
        "activation_lock_removed": buyback_activation_removed.get(),
        "mdm_clear": buyback_mdm_clear.get(),
        "ownership_declaration": buyback_ownership.get(),
        "privacy_notice_received": buyback_privacy.get(),
        "risk_level": _stored_value(RISK_LABELS, buyback_risk.get()),
        "risk_reason": buyback_risk_reason.get(),
        "purchase_price": buyback_price.get(),
        "payout_method": _stored_value(PAYOUT_LABELS, buyback_payout.get()),
        "operator_code": RUNTIME.operator_code,
        "microdetect_confirmed": device_is_identified(),
        "microdetect_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": buyback_notes.get(),
    }

    try:
        backup_db("before_buyback")
        c = connect()
        try:
            buyback_id, buyback_no = create_buyback(
                c, payload, test_mode=RUNTIME.is_test, commit=False
            )
            purpose_code = _stored_value(
                BUYBACK_PURPOSE_LABELS, buyback_purpose.get()
            )
            source_service_id = None
            if buyback_source_service_order_id:
                source = c.execute(
                    "SELECT service_id FROM service_orders WHERE id=?",
                    (int(buyback_source_service_order_id),),
                ).fetchone()
                source_service_id = int(source[0]) if source and source[0] else None
            c.execute(
                """
                UPDATE buybacks
                SET purpose=?,source_service_order_id=?,source_service_id=?,
                    installed_parts_cost=?,commercial_internal_labor_eur=0,
                    accounting_owner_labor_eur=0
                WHERE id=?
                """,
                (
                    purpose_code,
                    int(buyback_source_service_order_id)
                    if buyback_source_service_order_id else None,
                    source_service_id,
                    service_installed_parts_cost(source_service_id),
                    buyback_id,
                ),
            )
            record = get_buyback(c, buyback_id)
            last_buyback_pdf_path = generate_buyback_pdf(
                record, PDF_DIR, test_mode=RUNTIME.is_test
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        audit_event(
            "BUYBACK_DOCUMENT_PREPARED", "buyback", buyback_id,
            {"buyback_no": buyback_no, "device": serial.get().strip()[-4:]},
        )
        backup_db("after_buyback")
    except Exception as exc:
        buyback_save_in_progress = False
        if "buyback_save_button" in globals():
            buyback_save_button.configure(state="normal")
        messagebox.showerror("MikroFix - odkup", str(exc))
        return

    prepared_buyback_id = buyback_id
    prepared_buyback_no = buyback_no
    buyback_status_text.set(
        f"{buyback_no} pripravljen. Natisni oziroma podpiši dokument, izvedi izplačilo "
        "in nato klikni POTRDI PODPIS IN IZPLAČILO. Zaloga še ni ustvarjena."
    )
    if "buyback_complete_button" in globals():
        buyback_complete_button.configure(state="normal")
    if "buyback_cancel_button" in globals():
        buyback_cancel_button.configure(state="normal")
    messagebox.showinfo(
        "MikroFix - odkup",
        f"Dokumenti za odkup {buyback_no} so pripravljeni.\n\n"
        "Odkup še ni dokončan in naprava še ni zaloga. Najprej pridobi podpis ter "
        "potrdi dejansko izplačilo. Zunanje komercialno IMEI preverjanje ni zahtevano."
    )
    show_pdf_actions(last_buyback_pdf_path, "Odkupna pogodba", root)


def open_last_buyback_pdf():
    if last_buyback_pdf_path and Path(last_buyback_pdf_path).exists():
        show_pdf_actions(last_buyback_pdf_path, "Odkupna pogodba", root)
    else:
        messagebox.showinfo("MikroFix", "V tej seji še ni ustvarjene odkupne pogodbe PDF.")


def fetch_all_services():
    c = connect()
    rows = c.execute("""
        SELECT id, service_no, date, customer, model, serial, imei,
               invoice_no, warranty_until, work
        FROM service
        ORDER BY id DESC
    """).fetchall()
    c.close()
    return rows


def fetch_device_services():
    if not require_identified_device("pregled servisov povezane naprave"):
        return None

    sn = serial.get().strip()
    im = imei.get().strip()
    if not sn and not im:
        messagebox.showwarning("Ni povezane naprave", "Najprej priklopi in zaznaj iPhone.")
        return None

    c = connect()
    rows = c.execute("""
        SELECT id, service_no, date, customer, model, serial, imei,
               invoice_no, warranty_until, work
        FROM service
        WHERE serial = ? OR (imei <> '' AND imei = ?)
        ORDER BY id DESC
    """, (sn, im)).fetchall()
    c.close()
    return rows


def refresh_device_history_count():
    sn = serial.get().strip()
    im = imei.get().strip()
    if not sn and not im:
        device_history_count.set("")
        return
    c = connect()
    n = c.execute("""
        SELECT COUNT(*) FROM service
        WHERE serial = ? OR (imei <> '' AND imei = ?)
    """, (sn, im)).fetchone()[0]
    c.close()
    device_history_count.set(f"Prejšnji servisi: {n}")



def set_claim_form_locked(locked, status_value=None):
    """Zaključenih/zavrnjenih reklamacij ni mogoče več spreminjati."""
    widgets = [
        claim_description,
        claim_intake_condition,
        claim_customer_request,
        claim_finding,
        claim_resolution,
    ]
    state = "disabled" if locked else "normal"
    for widget in widgets:
        try:
            widget.configure(state=state)
        except Exception:
            pass

    try:
        claim_status_combo.configure(
            state="disabled" if locked else "readonly"
        )
    except Exception:
        pass

    try:
        if locked:
            claim_save_button.configure(
                state="disabled",
                text="REKLAMACIJA ZAKLENJENA"
            )
        else:
            claim_save_button.configure(
                state="normal",
                text=(
                    "SHRANI SPREMEMBE"
                    if claim_selected_claim_id.get().strip()
                    else "SHRANI REKLAMACIJO"
                )
            )
    except Exception:
        pass

    if status_value is not None:
        claim_status.set(normalize_claim_status(status_value))


def _set_claim_text(widget, value):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value or "")


def clear_claim_form(clear_service=False):
    if clear_service:
        claim_selected_service_id.set("")
        claim_service_no.set("")
        claim_service_date.set("")
        claim_invoice.set("")
        claim_warranty.set("")
        claim_customer.set("")
        claim_address.set("")
        claim_phone.set("")
        claim_original_work.set("")
        claim_original_parts.set("")

    claim_selected_claim_id.set("")
    claim_form_title.set("NOVA REKLAMACIJA")
    claim_status.set("ODPRTA")

    for widget in [
        claim_description,
        claim_intake_condition,
        claim_customer_request,
        claim_finding,
        claim_resolution,
    ]:
        _set_claim_text(widget, "")

    try:
        claim_status_combo.configure(
            values=["ODPRTA", "V OBDELAVI", "ZAKLJUČENA", "ZAVRNJENA"]
        )
    except Exception:
        pass
    set_claim_form_locked(False, "ODPRTA")


def new_claim_for_selected_service():
    """Za izbrani servis pripravi popolnoma novo reklamacijo."""
    clear_claim_form(clear_service=False)
    claim_form_title.set("NOVA REKLAMACIJA")



def refresh_device_claim_history():
    """Prikaži vse reklamacije, vezane na katerikoli servis trenutno povezane naprave."""
    if "claim_history_tree" not in globals():
        return

    for iid in claim_history_tree.get_children():
        claim_history_tree.delete(iid)

    sn = serial.get().strip()
    im = imei.get().strip()
    if not sn and not im:
        if "claim_history_count" in globals():
            claim_history_count.set("Reklamacije: 0")
        return

    c = connect()
    rows = c.execute("""
        SELECT
            cl.id,
            cl.service_id,
            COALESCE(cl.claim_no, ''),
            cl.date,
            s.service_no,
            COALESCE(cl.status, ''),
            COALESCE(cl.description, ''),
            COALESCE(cl.finding, ''),
            COALESCE(cl.resolution, '')
        FROM claims cl
        JOIN service s ON s.id = cl.service_id
        WHERE s.serial = ? OR (s.imei <> '' AND s.imei = ?)
        ORDER BY cl.id DESC
    """, (sn, im)).fetchall()
    c.close()

    for (
        claim_id_v, service_id_v, claim_no_v, date_v, service_no_v,
        status_v, desc_v, finding_v, resolution_v
    ) in rows:
        result_v = resolution_v or finding_v or "-"
        claim_history_tree.insert(
            "", "end",
            values=(
                claim_id_v,
                service_id_v,
                claim_no_v or f"R-{claim_id_v}",
                date_v or "-",
                service_no_v or "-",
                normalize_claim_status(status_v),
                desc_v or "-",
                result_v
            )
        )

    claim_history_count.set(f"Reklamacije: {len(rows)}")



def load_claim_service(service_id_v, reset_claim=True):
    """Naloži podatke originalnega servisa v obrazec reklamacije."""
    row, parts, _tests, _photos, _claims = load_service(service_id_v)
    if not row:
        return False

    (
        service_no_v, date_v, customer_v, address_v, phone_v,
        model_v, serial_v, imei_v, ios_v,
        work_v, legacy_part_id, invoice_v, warranty_v
    ) = row

    claim_selected_service_id.set(str(service_id_v))
    claim_service_no.set(service_no_v or "-")
    claim_service_date.set(date_v or "-")
    claim_invoice.set(invoice_v or "-")
    claim_warranty.set(warranty_text(warranty_v))
    claim_customer.set(customer_v or "-")
    claim_address.set(address_v or "-")
    claim_phone.set(phone_v or "-")
    claim_original_work.set(work_v or "-")

    if parts:
        lines = []
        for pn, pid, barcode_v, lot_v, price_v in parts:
            extra = []
            if pid:
                extra.append(f"koda {pid}")
            if barcode_v:
                extra.append(f"barkoda {barcode_v}")
            if lot_v:
                extra.append(f"LOT/SN {lot_v}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"{pn or 'Artikel'}{suffix}")
        claim_original_parts.set("; ".join(lines))
    else:
        claim_original_parts.set(legacy_part_id or "-")

    if reset_claim:
        new_claim_for_selected_service()

    return True


def open_claim_history_service(_event=None):
    """Odpri konkretno reklamacijo in prikaži njen originalni servis."""
    sel = claim_history_tree.selection()
    if not sel:
        return

    values = claim_history_tree.item(sel[0], "values")
    if len(values) < 2:
        return

    try:
        claim_id_v = int(values[0])
        service_id_v = int(values[1])
    except Exception:
        return

    # Nastavi servisni izbirnik na servis reklamacije.
    for display, sid in claim_service_map.items():
        if sid == service_id_v:
            claim_service_choice.set(display)
            break
    load_claim_service(service_id_v, reset_claim=False)

    c = connect()
    row = c.execute("""
        SELECT
            id, claim_no, date, description,
            intake_condition, customer_request, finding,
            resolution, status
        FROM claims
        WHERE id = ?
    """, (claim_id_v,)).fetchone()
    c.close()

    if not row:
        return

    (
        _claim_id, claim_no_v, claim_date_v, description_v,
        intake_v, request_v, finding_v, resolution_v, status_v
    ) = row

    status_v = normalize_claim_status(status_v)
    claim_selected_claim_id.set(str(claim_id_v))
    claim_form_title.set(
        f"REKLAMACIJA {claim_no_v or claim_id_v}  |  {claim_date_v or '-'}"
    )

    _set_claim_text(claim_description, description_v)
    _set_claim_text(claim_intake_condition, intake_v)
    _set_claim_text(claim_customer_request, request_v)
    _set_claim_text(claim_finding, finding_v)
    _set_claim_text(claim_resolution, resolution_v)

    claim_status.set(status_v)
    claim_status_combo.configure(
        values=["ODPRTA", "V OBDELAVI", "ZAKLJUČENA", "ZAVRNJENA"]
    )
    set_claim_form_locked(claim_status_is_terminal(status_v), status_v)



def refresh_claims_tab(preferred_service_id=None):
    """Reklamacije: samo servisni izbirnik + zgodovina reklamacij."""
    if "claim_history_tree" not in globals():
        return

    sn = serial.get().strip()
    im = imei.get().strip()

    refresh_device_claim_history()

    if not sn and not im:
        claim_device_summary.set("Ni povezane naprave.")
        claim_device_detail.set("Priklopi iPhone in ga zaznaj v zavihku Naprava.")
        claim_service_combo["values"] = []
        claim_service_choice.set("")
        clear_claim_form(clear_service=True)
        return

    device_title = name.get().strip() or model.get().strip() or "iPhone"
    claim_device_summary.set(device_title)
    claim_device_detail.set(
        f"Model: {model.get().strip() or '-'}   |   "
        f"S/N: {mask_identifier(sn)}   |   IMEI: {mask_identifier(im)}"
    )

    c = connect()
    rows = c.execute("""
        SELECT id, service_no, date
        FROM service
        WHERE serial = ? OR (imei <> '' AND imei = ?)
        ORDER BY id DESC
    """, (sn, im)).fetchall()
    c.close()

    claim_service_map.clear()
    display_values = []
    for service_id_v, service_no_v, date_v in rows:
        display = f"{service_no_v}  |  {date_v}"
        claim_service_map[display] = service_id_v
        display_values.append(display)

    claim_service_combo["values"] = display_values
    claim_service_count.set(f"Servisi naprave: {len(rows)}")

    target_id = preferred_service_id
    if target_id is None and claim_selected_service_id.get().strip():
        try:
            target_id = int(claim_selected_service_id.get())
        except Exception:
            target_id = None
    if target_id is None and rows:
        target_id = rows[0][0]

    selected_display = ""
    if target_id is not None:
        for display, sid in claim_service_map.items():
            if sid == target_id:
                selected_display = display
                break

    if selected_display:
        claim_service_choice.set(selected_display)
        load_claim_service(target_id, reset_claim=False)
    elif rows:
        claim_service_choice.set(display_values[0])
        load_claim_service(rows[0][0], reset_claim=False)
    else:
        claim_service_choice.set("")
        clear_claim_form(clear_service=True)


def on_claim_service_combo(_event=None):
    display = claim_service_choice.get().strip()
    service_id_v = claim_service_map.get(display)
    if service_id_v is None:
        return
    load_claim_service(service_id_v, reset_claim=True)



def next_claim_no(service_id):
    c = connect()
    row = c.execute(
        "SELECT service_no FROM service WHERE id=?",
        (service_id,)
    ).fetchone()
    n = c.execute(
        "SELECT COUNT(*) FROM claims WHERE service_id=?",
        (service_id,)
    ).fetchone()[0] + 1
    c.close()

    service_no_v = row[0] if row else f"S{service_id}"
    return f"R-{service_no_v}-{n:02d}"


def save_claim_from_tab():
    if not require_identified_device("reklamacije"):
        return

    sid = claim_selected_service_id.get().strip()
    if not sid:
        messagebox.showwarning(
            "MikroFix",
            "Izberi servis, na katerega se reklamacija nanaša."
        )
        return

    description_v = claim_description.get("1.0", "end").strip()
    if not description_v:
        messagebox.showwarning(
            "MikroFix",
            "Vpiši opis reklamacije oziroma težavo, ki jo stranka prijavlja."
        )
        return

    try:
        service_id_v = int(sid)
    except ValueError:
        messagebox.showerror("MikroFix", "Neveljavna povezava s servisom.")
        return

    desired_status = normalize_claim_status(claim_status.get())
    if desired_status not in {"ODPRTA", "V OBDELAVI", "ZAKLJUČENA", "ZAVRNJENA"}:
        desired_status = "ODPRTA"

    existing_claim_id = claim_selected_claim_id.get().strip()

    backup_db("before_claim")
    c = connect()

    if existing_claim_id:
        try:
            claim_id_v = int(existing_claim_id)
        except ValueError:
            c.close()
            messagebox.showerror("MikroFix", "Neveljavna reklamacija.")
            return

        current = c.execute(
            "SELECT status, claim_no FROM claims WHERE id=?",
            (claim_id_v,)
        ).fetchone()

        if not current:
            c.close()
            messagebox.showerror("MikroFix", "Reklamacije ni mogoče najti.")
            return

        current_status = normalize_claim_status(current[0])
        claim_no_v = current[1] or f"R-{claim_id_v}"

        # Terminalni status je nepovraten in zapis je samo za branje.
        if claim_status_is_terminal(current_status):
            c.close()
            set_claim_form_locked(True, current_status)
            messagebox.showwarning(
                "MikroFix",
                "Zaključene ali zavrnjene reklamacije ni več mogoče spreminjati."
            )
            return

        c.execute("""
            UPDATE claims
            SET description=?,
                intake_condition=?,
                customer_request=?,
                finding=?,
                resolution=?,
                status=?
            WHERE id=?
        """, (
            description_v,
            claim_intake_condition.get("1.0", "end").strip(),
            claim_customer_request.get("1.0", "end").strip(),
            claim_finding.get("1.0", "end").strip(),
            claim_resolution.get("1.0", "end").strip(),
            desired_status,
            claim_id_v
        ))
        c.commit()
        c.close()
        audit_event(
            "CLAIM_UPDATED",
            "claim",
            claim_id_v,
            {"claim_no": claim_no_v, "status": desired_status},
        )
        backup_db("after_claim")

        messagebox.showinfo(
            "MikroFix",
            f"Reklamacija {claim_no_v} je posodobljena."
        )
        refresh_device_claim_history()

        # Če je bila pravkar zaključena/zavrnjena, takoj zakleni.
        set_claim_form_locked(claim_status_is_terminal(desired_status), desired_status)
        return

    # Nova reklamacija.
    claim_no_v = next_claim_no(service_id_v)
    now_v = datetime.now().strftime("%Y-%m-%d %H:%M")

    c.execute("""
        INSERT INTO claims(
            service_id, claim_no, date, description,
            intake_condition, customer_request, finding,
            resolution, status
        ) VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        service_id_v,
        claim_no_v,
        now_v,
        description_v,
        claim_intake_condition.get("1.0", "end").strip(),
        claim_customer_request.get("1.0", "end").strip(),
        claim_finding.get("1.0", "end").strip(),
        claim_resolution.get("1.0", "end").strip(),
        desired_status
    ))
    new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    c.close()
    audit_event(
        "CLAIM_CREATED",
        "claim",
        new_id,
        {"claim_no": claim_no_v, "service_id": service_id_v},
    )
    backup_db("after_claim")

    claim_selected_claim_id.set(str(new_id))
    claim_form_title.set(f"REKLAMACIJA {claim_no_v}  |  {now_v}")

    messagebox.showinfo(
        "MikroFix",
        f"Reklamacija {claim_no_v} je shranjena."
    )
    refresh_device_claim_history()
    set_claim_form_locked(claim_status_is_terminal(desired_status), desired_status)




def refresh_service_tab_history():
    if "service_history_tree" not in globals():
        return

    for iid in service_history_tree.get_children():
        service_history_tree.delete(iid)

    if not device_is_identified():
        service_history_device.set(
            "Najprej odkleni napravo, potrdi povezavo in izvedi MicroDETECT. "
            "Pred tem osebnih podatkov ni mogoče vnašati."
        )
        service_history_count.set("Servisi: –")
        return

    sn = serial.get().strip()
    im = imei.get().strip()
    service_history_device.set(
        f"{name.get().strip() or model.get().strip() or 'iPhone'}   |   "
        f"S/N: {mask_identifier(sn)}   |   IMEI: {mask_identifier(im)}"
    )

    c = connect()
    rows = c.execute("""
        SELECT id, service_no, date, customer, invoice_no, warranty_until, work,
               ready_for_pickup_at, picked_up_at, final_notice_recorded_at
        FROM service
        WHERE serial = ? OR (imei <> '' AND imei = ?)
        ORDER BY id DESC
    """, (sn, im)).fetchall()
    c.close()

    for row in rows:
        service_history_tree.insert(
            "", "end",
            values=(
                row[0], row[1], row[2], row[3] or "-",
                row[4] or "-", warranty_text(row[5]),
                unclaimed_device_status(row[7], row[8], row[9]), row[6] or "-"
            )
        )

    service_history_count.set(f"Servisi: {len(rows)}")


def selected_service_history_id():
    if "service_history_tree" not in globals():
        return None
    sel = service_history_tree.selection()
    if not sel:
        return None
    values = service_history_tree.item(sel[0], "values")
    if not values:
        return None
    try:
        return int(values[0])
    except Exception:
        return None


def open_service_from_history(_event=None):
    service_id_v = selected_service_history_id()
    if service_id_v is not None:
        open_service_detail(service_id_v)


def prefill_new_service_customer(service_id_v=None):
    """Prenesi podatke stranke iz izbranega ali zadnjega servisa iste naprave."""
    sn = serial.get().strip()
    im = imei.get().strip()

    c = connect()
    if service_id_v is not None:
        row = c.execute("""
            SELECT customer, customer_address, customer_phone
            FROM service
            WHERE id = ?
        """, (service_id_v,)).fetchone()
    else:
        row = c.execute("""
            SELECT customer, customer_address, customer_phone
            FROM service
            WHERE serial = ? OR (imei <> '' AND imei = ?)
            ORDER BY id DESC
            LIMIT 1
        """, (sn, im)).fetchone()
    c.close()

    if row:
        customer.set(row[0] or "")
        customer_address.set(row[1] or "")
        customer_phone.set(row[2] or "")


def show_service_history():
    global service_editor_active
    if "service_home" not in globals():
        return
    if not persist_active_service_parts():
        return
    pause_active_service_timer(reason="LEAVE_WORKSPACE", quiet=True)
    service_editor_active = False
    service_editor.pack_forget()
    service_home.pack(fill="both", expand=True)
    refresh_service_open_orders()
    refresh_service_tab_history()


def refresh_service_open_orders(*_args):
    """Napolni Service čakalno vrsto iz iste baze, ki jo prikazuje Office."""
    if "service_open_order_tree" not in globals():
        return
    for iid in service_open_order_tree.get_children():
        service_open_order_tree.delete(iid)
    c = connect()
    try:
        rows = c.execute(
            """
            SELECT id,order_no,created_at,customer_name,device_model,
                   device_serial,work_started_at,customer_request
            FROM service_orders
            WHERE status='OPEN'
            ORDER BY id DESC
            LIMIT 500
            """
        ).fetchall()
    except Exception as exc:
        c.close()
        service_open_order_status.set(f"Napaka: {exc}")
        return
    c.close()
    for row in rows:
        service_open_order_tree.insert(
            "", "end", iid=str(row[0]),
            values=(
                row[0], row[1] or "-", str(row[2] or "").replace("T", " "),
                row[3] or "-", row[4] or "-", mask_identifier(row[5]),
                row[6] or "NI ZAČET", (row[7] or "")[:180],
            ),
        )
    service_open_order_status.set(f"{len(rows)} odprtih sprejemov")


def selected_service_open_order_id():
    selected = service_open_order_tree.selection()
    if not selected:
        messagebox.showinfo(
            "MikroFix – odprti sprejemi", "Izberi odprti sprejem.", parent=root
        )
        return None
    try:
        return int(service_open_order_tree.item(selected[0], "values")[0])
    except (IndexError, TypeError, ValueError):
        messagebox.showerror(
            "MikroFix – odprti sprejemi",
            "Izbranega sprejema ni mogoče prebrati.", parent=root,
        )
        return None


def start_selected_service_open_order(_event=None):
    order_id = selected_service_open_order_id()
    if order_id is not None:
        open_service_order_for_work(order_id)


def open_selected_service_intake_output(output_kind):
    order_id = selected_service_open_order_id()
    if order_id is not None:
        open_service_order_intake_output(order_id, output_kind, root)


def _format_elapsed_seconds(value):
    seconds = max(0, int(value or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def refresh_service_timer_display():
    """Osveži namenski prikaz ure in dejanskega intervalnega časa."""
    global service_timer_after_id
    if service_timer_after_id is not None:
        try:
            root.after_cancel(service_timer_after_id)
        except Exception:
            pass
        service_timer_after_id = None
    if not active_service_order_id or not service_editor_active:
        return
    c = connect()
    try:
        snapshot = service_work_timer_snapshot(c, int(active_service_order_id))
    except Exception as exc:
        c.close()
        service_timer_state_var.set(f"NAPAKA ČASOVNIKA: {exc}")
        return
    c.close()
    service_clock_var.set(datetime.now().strftime("%H:%M:%S"))
    first = snapshot.get("first_started_at")
    service_timer_started_var.set(
        first.strftime("%d. %m. %Y %H:%M:%S") if first else "—"
    )
    service_timer_elapsed_var.set(
        _format_elapsed_seconds(snapshot.get("total_seconds"))
    )
    if snapshot.get("running"):
        service_timer_state_var.set("ČAS TEČE")
        service_timer_toggle_button.configure(
            text="USTAVI ČAS", command=pause_active_service_timer,
            style="Blue.TButton",
        )
    else:
        service_timer_state_var.set("ČAS JE USTAVLJEN")
        service_timer_toggle_button.configure(
            text="NADALJUJ ČAS", command=resume_active_service_timer,
            style="Blue.TButton",
        )
    service_timer_after_id = root.after(1000, refresh_service_timer_display)


def resume_active_service_timer():
    if not active_service_order_id:
        return
    c = connect()
    try:
        start_service_work_timer(
            c, int(active_service_order_id), operator_code=RUNTIME.operator_code
        )
    except Exception as exc:
        c.close()
        messagebox.showerror("MikroFix – čas servisa", str(exc), parent=root)
        return
    c.close()
    audit_event("SERVICE_TIMER_STARTED", "service_order", active_service_order_id)
    refresh_service_timer_display()


def pause_active_service_timer(*, reason="MANUAL_PAUSE", quiet=False):
    if not active_service_order_id:
        return 0
    c = connect()
    try:
        before = service_work_timer_snapshot(c, int(active_service_order_id))
        minutes = pause_service_work_timer(
            c, int(active_service_order_id), operator_code=RUNTIME.operator_code,
            reason=reason,
        )
    except Exception as exc:
        c.close()
        if not quiet:
            messagebox.showerror("MikroFix – čas servisa", str(exc), parent=root)
        return 0
    c.close()
    if before.get("running"):
        audit_event(
            "SERVICE_TIMER_PAUSED", "service_order", active_service_order_id,
            {"minutes": minutes, "reason": reason},
        )
    refresh_service_timer_display()
    return minutes


def open_service_order_for_work(order_id):
    """Odpri isti servisni nalog in začni nov nadzorovani delovni interval."""
    global service_editor_active, active_service_order_id
    global device_identity_method, device_type_code

    c = connect()
    try:
        order = get_service_order(c, int(order_id))
        started_at_v = start_service_work_timer(
            c, order_id, operator_code=RUNTIME.operator_code
        )
    except WorkflowError as exc:
        c.close()
        messagebox.showerror("MikroFix - začetek servisa", str(exc))
        return False
    c.close()

    device_type_code = str(order.get("device_type") or "DRUGO")
    device_identity_method = str(order.get("identity_method") or "MANUAL_OTHER")
    model.set(order.get("device_model") or "")
    name.set(order.get("device_model") or "")
    serial.set(order.get("device_serial") or "")
    imei.set(order.get("device_imei") or "")
    ios.set(order.get("device_os") or "")
    storage.set(order.get("device_storage") or "")
    serial_display.set(mask_identifier(serial.get()))
    imei_display.set(mask_identifier(imei.get()))
    connected_state.set("POVEZAN")
    try:
        connected_label.configure(fg=SUCCESS)
        header_connected_label.configure(fg=SUCCESS)
    except Exception:
        pass
    clear_form(keep_device=True)
    if order.get("authorized_limit_eur") is not None:
        service_approved_limit_var.set(
            f"{float(order.get('authorized_limit_eur') or 0):.2f}"
        )
    is_iphone = device_type_code == "APPLE_MOBILE"
    set_service_test_tabs_for_device(is_iphone)
    set_quick_tests_enabled(is_iphone)
    active_service_order_id = int(order_id)
    customer.set(order.get("customer_name") or "")
    customer_address.set(order.get("customer_address") or "")
    customer_phone.set(order.get("customer_phone") or "")
    work.insert("1.0", order.get("customer_request") or "")
    if not restore_active_service_parts():
        pause_active_service_timer(reason="LOAD_PARTS_FAILED", quiet=True)
        active_service_order_id = None
        return False

    service_home.pack_forget()
    service_editor.pack(fill="both", expand=True)
    service_editor_active = True
    status.set(
        "Čas servisa teče od "
        + started_at_v.strftime("%d. %m. %Y %H:%M:%S")
    )
    refresh_service_timer_display()
    try:
        service_nb.select(tab_parts)
    except Exception:
        pass
    update_workflow_lock()
    return True


def open_service_order_from_office(order_id):
    """Po izboru v Office odpri enak servisni delovni prostor kot gumb v Servisu."""
    if not AUTH_SESSION.has_permission(PERMISSION_SERVICE):
        audit_event("PERMISSION_DENIED", "module", "SERVICE")
        messagebox.showwarning(
            "MikroFIX", "Za začetek servisa nimaš dovoljenja.", parent=root
        )
        show_start_menu()
        return False
    open_workflow_module(tab_service_top, "SERVICE")
    if not open_service_order_for_work(int(order_id)):
        show_start_menu()
        return False
    return True


def start_new_service():
    if not device_is_identified():
        choose_open_service_order()
        return

    c = connect()
    order_id = latest_open_service_order(
        c, serial.get().strip(), imei.get().strip()
    )
    c.close()
    if order_id is None:
        top_nb.select(tab_intake_top)
        messagebox.showinfo(
            "MikroFix",
            "Najprej izpolni in shrani servisno naročilo ob prevzemu. "
            "Podatki se bodo nato prenesli v končni servisni list."
        )
        return
    open_service_order_for_work(order_id)


def choose_open_service_order():
    """Izbira odprtega naloga ali varen priklic s servisno QR oznako."""
    c = connect()
    try:
        rows = c.execute(
            "SELECT id,order_no,created_at,customer_name,device_model,device_serial,device_imei,"
            "customer_request FROM service_orders WHERE status='OPEN' ORDER BY id DESC"
        ).fetchall()
    finally:
        c.close()
    if not rows:
        messagebox.showinfo("MikroFix – odprti nalogi", "Ni odprtih servisnih naročil.", parent=root)
        return
    dialog = tk.Toplevel(root)
    dialog.title("MikroFix – izberi nalog in začni servis")
    dialog.transient(root)
    maximize_work_window(dialog, "1120x680")
    query = tk.StringVar()
    ttk.Label(
        dialog,
        text="Skeniraj 80-mm servisno QR oznako ali išči po nalogu, stranki oziroma napravi.",
    ).pack(anchor="w", padx=14, pady=(14, 3))
    search = ttk.Entry(dialog, textvariable=query, font=("Segoe UI", 12))
    search.pack(fill="x", padx=14, pady=(0, 8))
    cols=("no","date","customer","device","serial","request")
    tree=ttk.Treeview(dialog,columns=cols,show="headings",selectmode="browse")
    for key,label,width in (
        ("no","Nalog",130),("date","Sprejet",145),("customer","Stranka",180),
        ("device","Naprava",170),("serial","S/N",110),("request","Zahteva",350),
    ):
        tree.heading(key,text=label);tree.column(key,width=width,stretch=key=="request")
    tree.pack(fill="both",expand=True,padx=14,pady=(0,8))
    row_map={str(row[0]):row for row in rows}

    def qr_order(value):
        parts=str(value or "").strip().split("|")
        return parts[1] if len(parts)>=2 and parts[0]=="MIKROFIX-SERVICE" else ""

    def fill(*_args):
        raw=query.get().strip();term=(qr_order(raw) or raw).casefold()
        for iid in tree.get_children():tree.delete(iid)
        for row in rows:
            haystack=" ".join(str(value or "") for value in row[1:]).casefold()
            if term and term not in haystack:continue
            tree.insert("","end",iid=str(row[0]),values=(
                row[1],str(row[2]).replace("T"," "),row[3],row[4],mask_identifier(row[5]),row[7],
            ))
        children=tree.get_children()
        if len(children)==1:
            tree.selection_set(children[0]);tree.focus(children[0])

    def start_selected(_event=None):
        selected=tree.selection()
        if not selected:
            messagebox.showinfo("MikroFix – začetek servisa","Izberi odprti nalog.",parent=dialog);return
        row=row_map[selected[0]]
        parts=query.get().strip().split("|")
        if len(parts)>=4 and parts[0]=="MIKROFIX-SERVICE":
            if str(parts[1])!=str(row[1]) or str(parts[2])!=str(row[5] or "") or str(parts[3])!=str(row[6] or ""):
                messagebox.showerror(
                    "MikroFix – napačna oznaka",
                    "QR oznaka se ne ujema s shranjeno serijsko številko/IMEI. Servis ni zagnan.",
                    parent=dialog,
                );return
        order_id=int(row[0]);dialog.destroy();open_service_order_for_work(order_id)

    def selected_order_id():
        selected = tree.selection()
        if not selected:
            messagebox.showinfo(
                "MikroFix – odprti sprejemi", "Izberi odprti nalog.", parent=dialog
            )
            return None
        return int(row_map[selected[0]][0])

    def open_selected_output(output_kind):
        order_id = selected_order_id()
        if order_id is not None:
            open_service_order_intake_output(order_id, output_kind, dialog)

    query.trace_add("write",fill)
    actions=ttk.Frame(dialog,padding=(14,0,14,14));actions.pack(fill="x")
    ttk.Button(actions,text="PREKLIČI",command=dialog.destroy).pack(side="left")
    ttk.Button(
        actions, text="A4 PDF / TISK",
        command=lambda: open_selected_output("A4"),
    ).pack(side="left", padx=(8, 0))
    ttk.Button(
        actions, text="80-mm OZNAKA / TISK",
        command=lambda: open_selected_output("TAG"),
    ).pack(side="left", padx=(8, 0))
    ttk.Button(actions,text="ZAČNI / NADALJUJ SERVIS",command=start_selected,style="Blue.TButton").pack(side="right")
    tree.bind("<Double-1>",start_selected);search.bind("<Return>",start_selected)
    fill();search.focus_set()


def open_claim_for_service_from_history():
    service_id_v = selected_service_history_id()
    if service_id_v is None:
        messagebox.showwarning(
            "MikroFIX",
            "Izberi servis, za katerega želiš odpreti reklamacijo."
        )
        return

    claim_selected_service_id.set(str(service_id_v))
    top_nb.select(tab_claims_top)
    refresh_claims_tab(preferred_service_id=service_id_v)
    new_claim_for_selected_service()



def warranty_text(date_value):
    if not date_value:
        return "-"
    try:
        d = datetime.strptime(date_value, "%Y-%m-%d").date()
        today = datetime.now().date()
        if d >= today:
            return f"do {d.strftime('%d.%m.%Y')}"
        return f"potekla {d.strftime('%d.%m.%Y')}"
    except Exception:
        return str(date_value)


def unclaimed_device_status(ready_at, picked_up_at=None, final_notice_at=None, now=None):
    """Vrni lokalni prikaz hrambe; funkcija ne pošilja nobenega obvestila."""
    if picked_up_at:
        return f"PREVZETO {str(picked_up_at)[:10]}"
    if not ready_at:
        return "ČAKA NA RAČUN"
    try:
        started = datetime.fromisoformat(str(ready_at))
    except ValueError:
        return "PRI MIKROFIX – datum ni veljaven"
    days = max(0, ((now or datetime.now()).date() - started.date()).days)
    policy = RUNTIME.settings.get("service_policy", {})
    free_days = int(policy.get("unclaimed_free_days", 30))
    block_price = float(policy.get("unclaimed_storage_eur_per_30_days", 10))
    notice_days = int(policy.get("unclaimed_final_notice_days", 90))
    charged_days = max(0, days - free_days)
    blocks = int(math.ceil(charged_days / 30.0)) if charged_days else 0
    fee = blocks * block_price
    notice = " • KONČNI POZIV EVIDENTIRAJ" if days >= notice_days and not final_notice_at else ""
    if final_notice_at:
        notice = f" • poziv {str(final_notice_at)[:10]}"
    return f"PRI MIKROFIX {days} dni • hramba {fee:.2f} EUR{notice}"


def mark_selected_service_picked_up():
    service_id_v = selected_service_history_id()
    if service_id_v is None:
        messagebox.showwarning("MikroFix", "Izberi servis.")
        return
    if not messagebox.askyesno(
        "MikroFix – prevzem naprave",
        "Potrdi, da je bila naprava dejansko izročena stranki.",
    ):
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    c = connect()
    updated = c.execute(
        "UPDATE service SET picked_up_at=? WHERE id=? "
        "AND ready_for_pickup_at IS NOT NULL AND picked_up_at IS NULL",
        (stamp, int(service_id_v)),
    )
    c.commit()
    c.close()
    if updated.rowcount != 1:
        messagebox.showwarning(
            "MikroFix", "Naprava še nima izdanega računa ali je prevzem že evidentiran."
        )
        return
    audit_event("SERVICE_DEVICE_PICKED_UP", "service", service_id_v)
    refresh_service_tab_history()


def record_selected_final_notice():
    service_id_v = selected_service_history_id()
    if service_id_v is None:
        messagebox.showwarning("MikroFix", "Izberi servis.")
        return
    c = connect()
    row = c.execute(
        "SELECT ready_for_pickup_at,picked_up_at,final_notice_recorded_at "
        "FROM service WHERE id=?", (int(service_id_v),),
    ).fetchone()
    if not row:
        c.close()
        return
    notice_days = int(
        RUNTIME.settings.get("service_policy", {}).get("unclaimed_final_notice_days", 90)
    )
    try:
        elapsed = (datetime.now().date() - datetime.fromisoformat(str(row[0])).date()).days
    except (TypeError, ValueError):
        elapsed = -1
    if row[1] or row[2] or elapsed < notice_days:
        status_v = unclaimed_device_status(row[0], row[1], row[2])
        c.close()
        messagebox.showwarning(
            "MikroFix", f"Končnega poziva še ni mogoče evidentirati.\n\n{status_v}"
        )
        return
    if not messagebox.askyesno(
        "MikroFix – končni poziv",
        "Potrdi samo, če je bil končni poziv dejansko izročen ali poslan. "
        "Program ga ne pošlje samodejno.",
    ):
        c.close()
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    c.execute(
        "UPDATE service SET final_notice_recorded_at=? WHERE id=?",
        (stamp, int(service_id_v)),
    )
    c.commit()
    c.close()
    audit_event("SERVICE_FINAL_NOTICE_RECORDED", "service", service_id_v)
    refresh_service_tab_history()


def load_service(service_id):
    c = connect()
    row = c.execute("""
        SELECT service_no, date, customer, customer_address, customer_phone,
               model, serial, imei, ios,
               work, part_id, invoice_no, warranty_until
        FROM service WHERE id = ?
    """, (service_id,)).fetchone()

    parts = c.execute("""
        SELECT part_name, part_id, barcode, lot_serial, unit_price
        FROM service_parts
        WHERE service_id = ? ORDER BY id
    """, (service_id,)).fetchall()

    tests = c.execute("""
        SELECT stage, test_name, status FROM service_tests
        WHERE service_id = ? ORDER BY id
    """, (service_id,)).fetchall()

    photos = c.execute("""
        SELECT stage, rel_path FROM service_photos
        WHERE service_id = ? ORDER BY id
    """, (service_id,)).fetchall()

    claims = c.execute("""
        SELECT id, date, description, resolution, status FROM claims
        WHERE service_id = ? ORDER BY id DESC
    """, (service_id,)).fetchall()

    c.close()
    return row, parts, tests, photos, claims


def load_service_warranty_meta(service_id):
    c = connect()
    row = c.execute("""
        SELECT warranty_scope, warranty_months
        FROM service WHERE id = ?
    """, (service_id,)).fetchone()
    c.close()
    if not row:
        return "", 0
    return str(row[0] or ""), int(row[1] or 0)


def add_claim(service_id, parent=None):
    win = tk.Toplevel(parent or root)
    win.title("MikroFIX - Nova reklamacija")
    maximize_work_window(win, "620x500")
    win.configure(bg=BG)
    win.transient(parent or root)

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=18, pady=16)

    tk.Label(outer, text="Nova reklamacija", bg=BG, fg=DARK,
             font=("Segoe UI", 15, "bold")).pack(anchor="w")

    tk.Label(outer, text="Opis reklamacije", bg=BG, fg=GRAY,
             font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(14, 4))
    desc = tk.Text(outer, height=7, font=("Segoe UI", 10), wrap="word")
    desc.pack(fill="x")

    tk.Label(outer, text="Rešitev / ugotovitev", bg=BG, fg=GRAY,
             font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 4))
    resolution = tk.Text(outer, height=7, font=("Segoe UI", 10), wrap="word")
    resolution.pack(fill="x")

    status_var = tk.StringVar(value="ODPRTA")
    rowf = tk.Frame(outer, bg=BG)
    rowf.pack(fill="x", pady=(12, 0))
    tk.Label(rowf, text="Status", bg=BG, fg=GRAY,
             font=("Segoe UI", 9, "bold")).pack(side="left")
    ttk.Combobox(rowf, textvariable=status_var,
                 values=["ODPRTA", "V OBDELAVI", "ZAKLJUČENA", "ZAVRNJENA"],
                 state="readonly", width=16).pack(side="left", padx=10)

    def do_save():
        d = desc.get("1.0", "end").strip()
        r = resolution.get("1.0", "end").strip()
        if not d:
            messagebox.showwarning("MikroFix", "Vpiši opis reklamacije.", parent=win)
            return
        backup_db("before_claim")
        c = connect()
        c.execute("""
            INSERT INTO claims(service_id, date, description, resolution, status)
            VALUES(?,?,?,?,?)
        """, (
            service_id,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            d, r, status_var.get()
        ))
        c.commit()
        c.close()
        backup_db("after_claim")
        messagebox.showinfo("MikroFix", "Reklamacija je shranjena.", parent=win)
        win.destroy()

    buttons = tk.Frame(outer, bg=BG)
    buttons.pack(fill="x", pady=(16, 0))
    ttk.Button(buttons, text="SHRANI REKLAMACIJO",
               command=do_save, style="Blue.TButton").pack(side="left")
    ttk.Button(buttons, text="PREKLIČI",
               command=win.destroy).pack(side="right")


def open_photo(rel_path):
    try:
        p = resolve_service_image_path(
            rel_path,
            data_dir=RUNTIME.data_dir,
            app_dir=APP_DIR,
        )
        image = load_service_image(p)
        image.thumbnail((1100, 760))
        from PIL import ImageTk

        win = tk.Toplevel(root)
        win.title(f"Servisna fotografija - {p.name}")
        win.configure(bg=BG)
        preview = ImageTk.PhotoImage(image)
        image.close()
        label = tk.Label(win, image=preview, bg=BG)
        label.image = preview
        label.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Button(win, text="ZAPRI", command=win.destroy).pack(pady=(0, 12))
    except ServiceImageError as exc:
        messagebox.showwarning("MikroFix", str(exc))


def register_pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                pdfmetrics.registerFont(TTFont("MikroFixFont", str(p)))
                return "MikroFixFont"
            except Exception:
                pass
    return "Helvetica"



def build_customer_document_header(
    story, font, title_style, body_style, h2_style,
    customer_name, customer_address, customer_phone,
    document_title, document_no=None
):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer

    company_lines = [
        Paragraph(f"<b>{pdf_safe(COMPANY_NAME)}</b>", body_style),
        Paragraph(pdf_safe(COMPANY_ADDRESS), body_style),
        Paragraph(pdf_safe(COMPANY_PHONE), body_style),
        Paragraph(pdf_safe(COMPANY_EMAIL), body_style),
        Paragraph(pdf_safe(COMPANY_WEB), body_style),
    ]

    customer_lines = [
        Paragraph(f"<b>{pdf_safe(customer_name or 'Stranka')}</b>", body_style),
        Paragraph(pdf_safe(customer_address or "-"), body_style),
        Paragraph(pdf_safe(customer_phone or "-"), body_style),
    ]

    header = Table(
        [[company_lines, customer_lines]],
        colWidths=[83*mm, 84*mm]
    )
    header.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), font),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ("LINEBELOW", (0,0), (-1,-1), 0.6, colors.HexColor("#C7D0D7")),
    ]))
    story.append(header)
    story.append(Spacer(1, 8*mm))

    heading = document_title.upper()
    if document_no:
        heading += f": {document_no}"
    story.append(Paragraph(heading, title_style))
    story.append(Spacer(1, 3*mm))





def format_print_date(value):
    value = str(value or "").strip()
    if not value:
        return "-"
    try:
        date_part = value[:10]
        return datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return value



def add_compact_document_meta(
    story, font, body_style,
    model_v, serial_v, imei_v,
    treatment_date_v, invoice_v, warranty_v
):
    """Dve kompaktni vrstici, enaki na vseh javnih izpisih."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer

    def cell(label, value):
        return Paragraph(
            f"<b>{pdf_safe(label)}:</b> {pdf_safe(value or '-')}",
            body_style
        )

    rows = [
        [
            cell("Model", model_v),
            cell("S/N", mask_identifier(serial_v)),
            cell("IMEI", mask_identifier(imei_v)),
        ],
        [
            cell("Datum obravnave", format_print_date(treatment_date_v)),
            cell("Št. računa", invoice_v),
            cell("Garancija na dele", format_print_date(warranty_v)),
        ],
    ]

    meta = Table(rows, colWidths=[56*mm, 56*mm, 55*mm])
    meta.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), font),
        ("FONTSIZE", (0,0), (-1,-1), 8.7),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F7F9FA")),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#D6DDE2")),
        ("INNERGRID", (0,0), (-1,-1), 0.3, colors.HexColor("#E1E6EA")),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
    ]))
    story.append(meta)
    story.append(Spacer(1, 5*mm))


def add_parts_to_document(story, parts, legacy_part_id, font, h2, body):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle, Paragraph

    if not parts and not legacy_part_id:
        return

    story.append(Paragraph("Uporabljeni artikli", h2))
    part_rows = [["Artikel", "Koda", "Barkoda", "LOT / serijska", "Cena"]]
    if parts:
        for pn, pid, barcode_v, lot_v, price_v in parts:
            part_rows.append([
                pn or "-",
                pid or "-",
                barcode_v or "-",
                lot_v or "-",
                f"{float(price_v or 0):.2f} €"
            ])
    else:
        part_rows.append(["-", legacy_part_id or "-", "-", "-", "-"])

    pt = Table(
        part_rows,
        colWidths=[48*mm, 27*mm, 37*mm, 35*mm, 20*mm]
    )
    pt.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), font),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EDF1")),
        ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#D3D9DE")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("PADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(pt)



def generate_pdf(service_id, print_mode="full"):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        )
    except ImportError:
        raise RuntimeError("Manjka ReportLab. Zaženi namesti_v28.bat.")

    print_mode = "short" if print_mode == "short" else "full"

    row, parts, tests, _photos, claims = load_service(service_id)
    if not row:
        raise RuntimeError("Servisnega zapisa ni mogoče najti.")

    (
        service_no_v, date_v, customer_v, customer_address_v, customer_phone_v,
        model_v, serial_v, imei_v, ios_v,
        work_v, legacy_part_id, invoice_v, warranty_v
    ) = row
    warranty_scope_v, warranty_months_v = load_service_warranty_meta(service_id)

    font = register_pdf_font()
    is_final = bool(str(invoice_v or "").strip())
    suffix = (
        "skrajsano" if print_mode == "short" and is_final
        else "celotno" if is_final
        else "osnutek"
    )
    path = PDF_DIR / f"{service_no_v}_{suffix}.pdf"

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        rightMargin=16*mm, leftMargin=16*mm,
        topMargin=15*mm, bottomMargin=15*mm
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "MFTitle", parent=styles["Title"], fontName=font,
        fontSize=17, leading=21, textColor=colors.HexColor(BLUE_DARK),
        alignment=1, spaceAfter=8
    )
    h2 = ParagraphStyle(
        "MFH2", parent=styles["Heading2"], fontName=font,
        fontSize=11, leading=14, textColor=colors.HexColor(DARK),
        spaceBefore=8, spaceAfter=5
    )
    body = ParagraphStyle(
        "MFBody", parent=styles["BodyText"], fontName=font,
        fontSize=9, leading=12, textColor=colors.HexColor(DARK)
    )

    story = []
    build_customer_document_header(
        story, font, title, body, h2,
        customer_v, customer_address_v, customer_phone_v,
        "Servisni list št", service_no_v
    )
    if not is_final:
        story.append(Paragraph(
            "<b>PREDOGLED – NI KONČNI SERVISNI LIST</b><br/>"
            "Končni servisni list nastane šele ob izdaji povezanega računa.",
            ParagraphStyle(
                "ServiceDraftNotice", parent=body, alignment=1,
                textColor=colors.HexColor(DANGER), fontSize=9, leading=12,
            ),
        ))
        story.append(Spacer(1, 2*mm))
    if RUNTIME.is_test or not FURS_ENABLED:
        story.append(Paragraph(
            "<b>TESTNI IZPIS – NI ZA IZDAJO KOT FISKALNI RAČUN</b>",
            ParagraphStyle(
                "ServiceTestNotice", parent=body, alignment=1,
                textColor=colors.HexColor(DANGER), fontSize=9, leading=12,
            ),
        ))
        story.append(Spacer(1, 2*mm))

    add_compact_document_meta(
        story, font, body,
        model_v, serial_v, imei_v,
        date_v, invoice_v, warranty_v
    )

    # Skrajšan izpis: bistvo za stranko.
    story.append(Paragraph("Opravljeno delo", h2))
    story.append(Paragraph(pdf_safe(work_v), body))
    add_parts_to_document(
        story, parts, legacy_part_id, font, h2, body
    )

    if warranty_scope_v == WARRANTY_SCOPE_INSTALLED_PARTS:
        warranty_notice = (
            f"<b>Prostovoljna tržna garancija MikroFIX na vgrajene dele:</b> "
            f"{warranty_months_v} mesece, do {pdf_safe(format_print_date(warranty_v))}. "
            "Velja za na tem listu evidentirane vgrajene dele pod pogoji garancijskega "
            "dokumenta. Ne omejuje zakonskih pravic potrošnika zaradi nepravilnosti pri "
            "opravljeni storitvi ali neskladnosti dobavljenega blaga."
        )
    elif warranty_v:
        warranty_notice = (
            "<b>Starejši servisni zapis:</b> prikazan je ob shranitvi evidentiran rok. "
            "Zakonske pravice potrošnika ostanejo nespremenjene."
        )
    else:
        warranty_notice = (
            "V tem servisnem zapisu ni evidentiran vgrajen del, zato dodatna tržna "
            "garancija na dele ni navedena. Zakonske pravice potrošnika zaradi "
            "nepravilnosti pri opravljeni storitvi ostanejo nespremenjene."
        )
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(warranty_notice, body))

    if print_mode == "full":
        if tests:
            by_stage = {"PRED": [], "PO": []}
            for stage, test_name, st in tests:
                by_stage.setdefault(stage, []).append((test_name, st))

            for stage, title_txt in [
                ("PRED", "Test pred servisom"),
                ("PO", "Test po servisu")
            ]:
                rows = by_stage.get(stage, [])
                if rows:
                    story.append(Paragraph(title_txt, h2))
                    tr = [["Test", "Stanje"]] + [[n, s] for n, s in rows]
                    tt = Table(tr, colWidths=[120*mm, 47*mm])
                    tt.setStyle(TableStyle([
                        ("FONTNAME", (0,0), (-1,-1), font),
                        ("FONTSIZE", (0,0), (-1,-1), 8.5),
                        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EDF1")),
                        ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#D3D9DE")),
                        ("PADDING", (0,0), (-1,-1), 4),
                    ]))
                    story.append(tt)

        if claims:
            story.append(Paragraph("Povezane reklamacije", h2))
            cr = [["Datum", "Status", "Opis / rešitev"]]
            for _cid, cdate, desc, res, cst in claims:
                text = desc or ""
                if res:
                    text += "\nRešitev: " + res
                cr.append([
                    cdate or "-",
                    normalize_claim_status(cst),
                    text or "-"
                ])
            ct = Table(cr, colWidths=[34*mm, 30*mm, 103*mm])
            ct.setStyle(TableStyle([
                ("FONTNAME", (0,0), (-1,-1), font),
                ("FONTSIZE", (0,0), (-1,-1), 8),
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EDF1")),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#D3D9DE")),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("PADDING", (0,0), (-1,-1), 4),
            ]))
            story.append(ct)

    doc.build(story)
    return path



def generate_claim_pdf(claim_id, print_mode="full"):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        )
    except ImportError:
        raise RuntimeError("Manjka ReportLab. Zaženi namesti_v28.bat.")

    print_mode = "short" if print_mode == "short" else "full"

    c = connect()
    claim = c.execute("""
        SELECT
            cl.id, cl.service_id, cl.claim_no, cl.date,
            cl.description, cl.intake_condition, cl.customer_request,
            cl.finding, cl.resolution, cl.status
        FROM claims cl
        WHERE cl.id = ?
    """, (claim_id,)).fetchone()
    c.close()

    if not claim:
        raise RuntimeError("Reklamacije ni mogoče najti.")

    (
        _claim_id, service_id_v, claim_no_v, claim_date_v,
        description_v, intake_v, request_v,
        finding_v, resolution_v, claim_status_v
    ) = claim

    row, parts, tests, _photos, _claims = load_service(service_id_v)
    if not row:
        raise RuntimeError("Originalnega servisnega zapisa ni mogoče najti.")

    (
        service_no_v, service_date_v, customer_v, address_v, phone_v,
        model_v, serial_v, imei_v, ios_v,
        work_v, legacy_part_id, invoice_v, warranty_v
    ) = row

    font = register_pdf_font()
    safe_claim_no = claim_no_v or f"R-{claim_id}"
    suffix = "skrajsano" if print_mode == "short" else "celotno"
    path = PDF_DIR / f"{safe_claim_no}_{suffix}.pdf"

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        rightMargin=16*mm, leftMargin=16*mm,
        topMargin=15*mm, bottomMargin=15*mm
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ClaimTitle", parent=styles["Title"], fontName=font,
        fontSize=17, leading=21, textColor=colors.HexColor(BLUE_DARK),
        alignment=1, spaceAfter=8
    )
    h2 = ParagraphStyle(
        "ClaimH2", parent=styles["Heading2"], fontName=font,
        fontSize=11, leading=14, textColor=colors.HexColor(DARK),
        spaceBefore=8, spaceAfter=5
    )
    body = ParagraphStyle(
        "ClaimBody", parent=styles["BodyText"], fontName=font,
        fontSize=9, leading=12, textColor=colors.HexColor(DARK)
    )

    story = []
    build_customer_document_header(
        story, font, title, body, h2,
        customer_v, address_v, phone_v,
        "Reklamacija št", safe_claim_no
    )
    if RUNTIME.is_test or not FURS_ENABLED:
        story.append(Paragraph(
            "<b>TESTNI IZPIS – NI ZA IZDAJO KOT FISKALNI RAČUN</b>",
            ParagraphStyle(
                "ClaimTestNotice", parent=body, alignment=1,
                textColor=colors.HexColor(DANGER), fontSize=9, leading=12,
            ),
        ))
        story.append(Spacer(1, 2*mm))

    add_compact_document_meta(
        story, font, body,
        model_v, serial_v, imei_v,
        claim_date_v, invoice_v, warranty_v
    )

    # Skrajšana reklamacija: samo bistvo.
    story.append(Paragraph("Prijavljena težava", h2))
    story.append(Paragraph(pdf_safe(description_v), body))

    story.append(Paragraph("Ugotovitev / rešitev", h2))
    compact_result = resolution_v or finding_v or "-"
    story.append(Paragraph(pdf_safe(compact_result), body))

    if print_mode == "full":
        story.append(Paragraph("Status reklamacije", h2))
        story.append(
            Paragraph(pdf_safe(normalize_claim_status(claim_status_v)), body)
        )

        sections = [
            ("Stanje ob sprejemu reklamacije", intake_v),
            ("Zahteva stranke", request_v),
            ("Ugotovitev servisa", finding_v),
            ("Rešitev", resolution_v),
            ("Originalni servis", service_no_v),
            ("Prvotno opravljeno delo", work_v),
        ]
        for heading, value in sections:
            story.append(Paragraph(heading, h2))
            story.append(Paragraph(pdf_safe(value), body))

        add_parts_to_document(
            story, parts, legacy_part_id, font, h2, body
        )

        if tests:
            story.append(Paragraph("Testi originalnega servisa", h2))
            test_rows = [["Faza", "Modul", "Stanje"]]
            test_rows += [
                [stage, test_name, st]
                for stage, test_name, st in tests
            ]
            tt = Table(
                test_rows,
                colWidths=[28*mm, 92*mm, 47*mm]
            )
            tt.setStyle(TableStyle([
                ("FONTNAME", (0,0), (-1,-1), font),
                ("FONTSIZE", (0,0), (-1,-1), 8),
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8EDF1")),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#D3D9DE")),
                ("PADDING", (0,0), (-1,-1), 4),
            ]))
            story.append(tt)

    doc.build(story)
    return path



def selected_claim_history_id():
    if "claim_history_tree" not in globals():
        return None
    sel = claim_history_tree.selection()
    if not sel:
        return None
    values = claim_history_tree.item(sel[0], "values")
    if not values:
        return None
    try:
        return int(values[0])
    except Exception:
        return None


def save_pdf_as(source_path, suggested_name, parent=None):
    target = filedialog.asksaveasfilename(
        parent=parent or root,
        title="Shrani PDF",
        defaultextension=".pdf",
        initialfile=suggested_name,
        filetypes=[("PDF dokument", "*.pdf")]
    )
    if not target:
        return False

    try:
        same_file = Path(source_path).resolve() == Path(target).resolve()
    except Exception:
        same_file = False

    if not same_file:
        shutil.copy2(source_path, target)

    messagebox.showinfo(
        "MikroFIX",
        f"PDF je shranjen:\n{target}",
        parent=parent or root
    )
    return True


def print_pdf_file(pdf_path, parent=None):
    parent = parent or root
    printers = list_printers()
    if not printers:
        if messagebox.askyesno(
            "MikroFIX - tiskalnik",
            "Operacijski sistem ni vrnil nobenega nameščenega tiskalnika.\n\n"
            "Želiš PDF odpreti in tiskanje izbrati v pregledovalniku?",
            parent=parent,
        ):
            open_local_file(pdf_path)
        return

    win = tk.Toplevel(parent)
    win.title("MikroFIX - Izberi tiskalnik")
    win.geometry("620x360")
    win.minsize(560, 330)
    win.configure(bg=BG)
    win.transient(parent)
    win.grab_set()

    card = tk.Frame(win, bg=PANEL)
    card.pack(fill="both", expand=True, padx=20, pady=20)
    tk.Label(
        card, text="Izberi nameščeni tiskalnik",
        bg=PANEL, fg=DARK, font=("Segoe UI",15,"bold")
    ).pack(anchor="w", padx=14, pady=(14,5))
    tk.Label(
        card,
        text="Program ne potrebuje privzetega tiskalnika. Izbereš ga za vsak izpis posebej.",
        bg=PANEL, fg=GRAY, font=("Segoe UI",9), wraplength=540, justify="left"
    ).pack(anchor="w", padx=14, pady=(0,12))

    configured = str(
        RUNTIME.settings.get("printer", {}).get("printer_name")
        or RUNTIME.settings.get("printer", {}).get("windows_printer_name")
        or ""
    ).strip()
    default_name = default_printer()
    names = [item.name for item in printers]
    selected_name = (
        configured if configured in names
        else default_name if default_name in names
        else names[0]
    )
    printer_var = tk.StringVar(value=selected_name)
    printer_info_var = tk.StringVar()
    combo = ttk.Combobox(
        card, textvariable=printer_var, values=names, state="readonly"
    )
    combo.pack(fill="x", padx=14, ipady=4)

    def refresh_printer_info(*_args):
        selected = next(
            (item for item in printers if item.name == printer_var.get()), None
        )
        if selected:
            kind = "navidezni" if selected.is_virtual else "fizični"
            printer_info_var.set(
                f"{kind.capitalize()} tiskalnik | vrata: {selected.port or '-'} | "
                f"gonilnik: {selected.driver or '-'}"
            )

    combo.bind("<<ComboboxSelected>>", refresh_printer_info)
    refresh_printer_info()
    tk.Label(
        card, textvariable=printer_info_var,
        bg=PANEL, fg=GRAY, font=("Segoe UI",8), wraplength=540, justify="left"
    ).pack(anchor="w", padx=14, pady=(7,16))

    def send_selected():
        try:
            send_pdf_to_printer(pdf_path, printer_var.get())
        except Exception as exc:
            messagebox.showerror(
                "MikroFIX - tiskanje",
                f"Neposredni ukaz za tiskanje ni uspel:\n{exc}\n\n"
                "Uporabi gumb ODPRI PDF IN NATISNI.",
                parent=win,
            )
            return
        messagebox.showinfo(
            "MikroFIX - tiskanje",
            f"Dokument je poslan tiskalniku:\n{printer_var.get()}",
            parent=win,
        )
        win.destroy()

    buttons = tk.Frame(card, bg=PANEL)
    buttons.pack(fill="x", padx=14, pady=(2,14))
    ttk.Button(
        buttons, text="POŠLJI IZBRANEMU TISKALNIKU",
        command=send_selected, style="Blue.TButton"
    ).pack(fill="x", pady=4)
    ttk.Button(
        buttons, text="ODPRI PDF IN NATISNI",
        command=lambda: open_local_file(pdf_path)
    ).pack(fill="x", pady=4)


def show_pdf_actions(pdf_path, label, parent=None):
    parent = parent or root

    win = tk.Toplevel(parent)
    win.title("MikroFIX - Izpis")
    win.geometry("480x315")
    win.resizable(False, False)
    win.configure(bg=BG)
    win.transient(parent)
    win.grab_set()

    card = tk.Frame(
        win, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True, padx=22, pady=22)

    tk.Label(
        card,
        text="Način izhoda",
        bg=PANEL, fg=DARK,
        font=("Segoe UI",16,"bold")
    ).pack(anchor="w", padx=18, pady=(18,5))

    tk.Label(
        card,
        text=f"Pripravljen je {label}.",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI",9)
    ).pack(anchor="w", padx=18, pady=(0,18))

    ttk.Button(
        card,
        text="SHRANI KOT PDF",
        command=lambda: save_pdf_as(
            pdf_path, Path(pdf_path).name, win
        ),
        style="Blue.TButton"
    ).pack(fill="x", padx=18, pady=5)

    ttk.Button(
        card,
        text="TISKALNIK",
        command=lambda: print_pdf_file(pdf_path, win)
    ).pack(fill="x", padx=18, pady=5)

    ttk.Button(
        card,
        text="ODPRI PDF",
        command=lambda: open_local_file(pdf_path)
    ).pack(fill="x", padx=18, pady=5)


def prepare_selected_output(kind, record_id, print_mode, parent=None):
    parent = parent or root

    try:
        if kind == "service":
            pdf_path = generate_pdf(record_id, print_mode=print_mode)
            doc_name = "servisni list"
        elif kind == "claim":
            pdf_path = generate_claim_pdf(record_id, print_mode=print_mode)
            doc_name = "reklamacija"
        else:
            raise RuntimeError("Neznan tip izpisa.")
    except Exception as e:
        messagebox.showerror(
            "MikroFIX",
            f"Izpisa ni mogoče pripraviti:\n{e}",
            parent=parent
        )
        return

    variant = "skrajšani" if print_mode == "short" else "celotni"
    show_pdf_actions(
        pdf_path,
        f"{variant} {doc_name}",
        parent=parent
    )


def show_output_choice(kind, record_id, parent=None):
    parent = parent or root

    win = tk.Toplevel(parent)
    win.title("MikroFIX - Vrsta izpisa")
    win.geometry("500x300")
    win.resizable(False, False)
    win.configure(bg=BG)
    win.transient(parent)
    win.grab_set()

    card = tk.Frame(
        win, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True, padx=22, pady=22)

    tk.Label(
        card,
        text="Izberi vrsto dokumenta",
        bg=PANEL, fg=DARK,
        font=("Segoe UI",16,"bold")
    ).pack(anchor="w", padx=18, pady=(18,5))

    tk.Label(
        card,
        text=(
            "Skrajšano = ključni podatki in bistvo posega.  "
            "Celotno = podrobna servisna oziroma reklamacijska evidenca."
        ),
        bg=PANEL, fg=GRAY,
        font=("Segoe UI",9),
        wraplength=410,
        justify="left"
    ).pack(anchor="w", padx=18, pady=(0,18))

    def choose(mode):
        win.destroy()
        prepare_selected_output(
            kind, record_id, mode, parent=parent
        )

    ttk.Button(
        card,
        text="NATISNI SKRAJŠANO",
        command=lambda: choose("short"),
        style="Blue.TButton"
    ).pack(fill="x", padx=18, pady=6)

    ttk.Button(
        card,
        text="NATISNI CELOTNO",
        command=lambda: choose("full")
    ).pack(fill="x", padx=18, pady=6)



def print_selected_service():
    service_id_v = selected_service_history_id()
    if service_id_v is None:
        messagebox.showwarning("MikroFIX", "Najprej označi servis za izpis.")
        return
    show_output_choice("service", service_id_v)


def print_selected_claim():
    claim_id_v = selected_claim_history_id()
    if claim_id_v is None:
        messagebox.showwarning("MikroFIX", "Najprej označi reklamacijo za izpis.")
        return
    show_output_choice("claim", claim_id_v)



def export_pdf_with_message(service_id, parent=None):
    try:
        p = generate_pdf(service_id)
        messagebox.showinfo("MikroFix", f"PDF ustvarjen:\n{p}", parent=parent or root)
    except Exception as e:
        messagebox.showerror("MikroFix", f"PDF ni bil ustvarjen:\n{e}", parent=parent or root)



def open_claim_for_service_id(service_id, parent=None):
    if not require_identified_device("reklamacije"):
        return

    c = connect()
    row = c.execute(
        "SELECT serial, imei FROM service WHERE id=?",
        (service_id,)
    ).fetchone()
    c.close()

    if not row:
        messagebox.showwarning("MikroFIX", "Servisnega zapisa ni mogoče najti.", parent=parent or root)
        return

    service_serial = str(row[0] or "").strip()
    service_imei = str(row[1] or "").strip()
    current_serial = serial.get().strip()
    current_imei = imei.get().strip()

    same_device = (
        (service_serial and service_serial == current_serial)
        or (service_imei and current_imei and service_imei == current_imei)
    )
    if not same_device:
        messagebox.showwarning(
            "MikroFIX",
            "Reklamacijo lahko odpreš samo za trenutno identificirano napravo.",
            parent=parent or root
        )
        return

    claim_selected_service_id.set(str(service_id))
    top_nb.select(tab_claims_top)
    refresh_claims_tab(preferred_service_id=service_id)
    new_claim_for_selected_service()

    if parent is not None:
        try:
            parent.destroy()
        except Exception:
            pass



def open_service_detail(service_id):
    row, parts, tests, photos, claims = load_service(service_id)
    if not row:
        messagebox.showwarning("MikroFix", "Servisnega zapisa ni bilo mogoče najti.")
        return

    (
        service_no_v, date_v, customer_v, customer_address_v, customer_phone_v,
        model_v, serial_v, imei_v, ios_v,
        work_v, legacy_part_id, invoice_no_v, warranty_v
    ) = row

    win = tk.Toplevel(root)
    win.title(f"MikroFix - {service_no_v}")
    maximize_work_window(win, "900x760")
    win.minsize(800, 650)
    win.configure(bg=BG)

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=18, pady=14)

    brand = tk.Frame(outer, bg=BG)
    brand.pack(fill="x")
    tk.Label(brand, text="Mikro", bg=BG, fg=GRAY,
             font=("Segoe UI", 22, "bold")).pack(side="left")
    tk.Label(brand, text="Fix", bg=BG, fg=BLUE,
             font=("Segoe UI", 22, "bold")).pack(side="left")
    tk.Label(brand, text=service_no_v, bg=BG, fg=DARK,
             font=("Segoe UI", 13, "bold")).pack(side="right", pady=(7, 0))

    notebook = ttk.Notebook(outer)
    notebook.pack(fill="both", expand=True, pady=(10, 8))

    # Osnovni podatki
    tab_info = tk.Frame(notebook, bg=PANEL)
    notebook.add(tab_info, text="Servis")
    fields = [
        ("Datum", date_v),
        ("Stranka", customer_v or "-"),
        ("Naslov", customer_address_v or "-"),
        ("Telefon", customer_phone_v or "-"),
        ("Model", model_v or "-"),
        ("Serial Number", mask_identifier(serial_v)),
        ("IMEI", mask_identifier(imei_v)),
        ("iOS", ios_v or "-"),
        ("Številka računa", invoice_no_v or "-"),
        ("Garancija", warranty_text(warranty_v)),
    ]
    for r, (label, value) in enumerate(fields):
        tk.Label(tab_info, text=label, bg=PANEL, fg=GRAY,
                 font=("Segoe UI", 9, "bold"), width=22, anchor="w").grid(
                     row=r, column=0, sticky="nw", padx=(16, 8), pady=6)
        tk.Label(tab_info, text=value, bg=PANEL, fg=DARK,
                 font=("Segoe UI", 10), anchor="w", justify="left").grid(
                     row=r, column=1, sticky="nw", padx=(0, 16), pady=6)

    tk.Label(tab_info, text="Opravljeno delo", bg=PANEL, fg=GRAY,
             font=("Segoe UI", 9, "bold"), width=22, anchor="nw").grid(
                 row=len(fields), column=0, sticky="nw", padx=(16, 8), pady=8)
    wb = tk.Text(tab_info, height=8, font=("Segoe UI", 10), wrap="word")
    wb.grid(row=len(fields), column=1, sticky="nsew", padx=(0,16), pady=8)
    wb.insert("1.0", work_v or "")
    wb.configure(state="disabled")
    tab_info.grid_columnconfigure(1, weight=1)
    tab_info.grid_rowconfigure(len(fields), weight=1)

    # Deli
    tab_parts = tk.Frame(notebook, bg=PANEL)
    notebook.add(tab_parts, text=f"Deli ({len(parts) if parts else (1 if legacy_part_id else 0)})")
    pt = ttk.Treeview(
        tab_parts,
        columns=("part","code","barcode","lot","price"),
        show="headings"
    )
    pt.heading("part", text="Artikel")
    pt.heading("code", text="Interna koda")
    pt.heading("barcode", text="Barkoda")
    pt.heading("lot", text="LOT / serijska")
    pt.heading("price", text="Cena")
    pt.column("part", width=250)
    pt.column("code", width=150)
    pt.column("barcode", width=200)
    pt.column("lot", width=190)
    pt.column("price", width=90)
    pt.pack(fill="both", expand=True, padx=12, pady=12)
    if parts:
        for pn, pid, barcode_v, lot_v, price_v in parts:
            pt.insert(
                "", "end",
                values=(
                    pn or "-",
                    pid or "-",
                    barcode_v or "-",
                    lot_v or "-",
                    f"{float(price_v or 0):.2f} €"
                )
            )
    elif legacy_part_id:
        pt.insert("", "end", values=("-", legacy_part_id, "-", "-", "-"))

    # Testi - primerjava PRED / MODUL / PO
    tab_tests = tk.Frame(notebook, bg=PANEL)
    notebook.add(tab_tests, text="Testi")

    # Pretvori zapise (faza, test, stanje) v primerjalno tabelo.
    test_map = {}
    test_order = list(TEST_ITEMS)
    for stage_v, test_name_v, status_v in tests:
        if test_name_v not in test_map:
            test_map[test_name_v] = {}
        test_map[test_name_v][stage_v.upper()] = status_v

        # Ohranimo tudi morebitne stare/ročno dodane module.
        if test_name_v not in test_order:
            test_order.append(test_name_v)

    table_wrap = tk.Frame(
        tab_tests, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    table_wrap.pack(fill="both", expand=True, padx=14, pady=14)

    # Glava
    headers = [("PRED", 0), ("MODUL", 1), ("PO", 2)]
    for text_v, col_v in headers:
        tk.Label(
            table_wrap,
            text=text_v,
            bg="#E8EDF1",
            fg=DARK,
            font=("Segoe UI", 10, "bold"),
            anchor="center",
            padx=8,
            pady=9,
            highlightthickness=1,
            highlightbackground=LIGHT_GRAY
        ).grid(row=0, column=col_v, sticky="nsew")

    table_wrap.grid_columnconfigure(0, weight=1, minsize=120)
    table_wrap.grid_columnconfigure(1, weight=3, minsize=320)
    table_wrap.grid_columnconfigure(2, weight=1, minsize=120)

    def test_status_visual(status_v):
        status_v = (status_v or "NI TESTIRANO").upper()
        if status_v == "OK":
            return "✓", SUCCESS, ("Segoe UI Symbol", 17, "bold")
        if status_v == "NE DELA":
            return "✕", DANGER, ("Segoe UI Symbol", 17, "bold")
        if status_v == "N/A":
            return "N/A", GRAY, ("Segoe UI", 9, "bold")
        return "—", GRAY, ("Segoe UI", 15, "bold")

    # Vrstice: PRED | MODUL | PO
    for row_i, test_name_v in enumerate(test_order, start=1):
        pre_status = test_map.get(test_name_v, {}).get("PRED", "NI TESTIRANO")
        post_status = test_map.get(test_name_v, {}).get("PO", "NI TESTIRANO")

        pre_text, pre_color, pre_font = test_status_visual(pre_status)
        post_text, post_color, post_font = test_status_visual(post_status)

        tk.Label(
            table_wrap,
            text=pre_text,
            bg=PANEL,
            fg=pre_color,
            font=pre_font,
            anchor="center",
            padx=8,
            pady=7,
            highlightthickness=1,
            highlightbackground=LIGHT_GRAY
        ).grid(row=row_i, column=0, sticky="nsew")

        tk.Label(
            table_wrap,
            text=test_name_v,
            bg=PANEL,
            fg=DARK,
            font=("Segoe UI", 10),
            anchor="w",
            padx=14,
            pady=7,
            highlightthickness=1,
            highlightbackground=LIGHT_GRAY
        ).grid(row=row_i, column=1, sticky="nsew")

        tk.Label(
            table_wrap,
            text=post_text,
            bg=PANEL,
            fg=post_color,
            font=post_font,
            anchor="center",
            padx=8,
            pady=7,
            highlightthickness=1,
            highlightbackground=LIGHT_GRAY
        ).grid(row=row_i, column=2, sticky="nsew")

    tk.Label(
        tab_tests,
        text="✓ deluje    ✕ ne deluje    — ni testirano",
        bg=PANEL,
        fg=GRAY,
        font=("Segoe UI", 9)
    ).pack(anchor="w", padx=16, pady=(0, 10))

    # Fotografije
    tab_photos = tk.Frame(notebook, bg=PANEL)
    notebook.add(tab_photos, text=f"Fotografije ({len(photos)})")
    ph_tree = ttk.Treeview(tab_photos, columns=("stage","file"), show="headings")
    ph_tree.heading("stage", text="Faza")
    ph_tree.heading("file", text="Datoteka")
    ph_tree.column("stage", width=100)
    ph_tree.column("file", width=580)
    ph_tree.pack(fill="both", expand=True, padx=12, pady=(12,6))
    photo_map = {}
    for idx, (stage, rel) in enumerate(photos):
        iid = ph_tree.insert("", "end", values=(stage, Path(rel).name))
        photo_map[iid] = rel
    def ph_open(_event=None):
        sel = ph_tree.selection()
        if sel:
            open_photo(photo_map[sel[0]])
    ph_tree.bind("<Double-1>", ph_open)
    tk.Label(tab_photos, text="Dvoklikni fotografijo za odpiranje.",
             bg=PANEL, fg=GRAY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(0,10))

    # Reklamacije
    tab_claims = tk.Frame(notebook, bg=PANEL)
    notebook.add(tab_claims, text=f"Reklamacije ({len(claims)})")
    cl = ttk.Treeview(tab_claims, columns=("date","status","desc","res"), show="headings")
    cl.heading("date", text="Datum")
    cl.heading("status", text="Status")
    cl.heading("desc", text="Opis")
    cl.heading("res", text="Rešitev")
    cl.column("date", width=130)
    cl.column("status", width=100)
    cl.column("desc", width=280)
    cl.column("res", width=280)
    cl.pack(fill="both", expand=True, padx=12, pady=12)
    for cid, cdate, desc, res, cst in claims:
        cl.insert(
            "", "end",
            values=(cdate, normalize_claim_status(cst), desc, res)
        )

    buttons = tk.Frame(outer, bg=BG)
    buttons.pack(fill="x")
    ttk.Button(
        buttons, text="REKLAMACIJA",
        command=lambda: open_claim_for_service_id(service_id, win)
    ).pack(side="left")
    ttk.Button(
        buttons, text="NATISNI / PDF",
        command=lambda: show_output_choice("service", service_id, win),
        style="Blue.TButton"
    ).pack(side="left", padx=8)
    ttk.Button(buttons, text="ZAPRI", command=win.destroy).pack(side="right")


def open_selected_service(event, tree):
    selected = tree.selection()
    if not selected:
        return
    values = tree.item(selected[0], "values")
    if not values:
        return
    try:
        service_id = int(values[0])
    except Exception:
        return
    open_service_detail(service_id)


def open_service_browser(mode="all"):
    if mode == "device":
        rows = fetch_device_services()
        if rows is None:
            return
        title = "Servisi povezane naprave"
        subtitle = (
            f"S/N: {mask_identifier(serial.get())}   "
            f"IMEI: {mask_identifier(imei.get())}"
        )
    else:
        rows = fetch_all_services()
        title = "Pregled servisne baze"
        subtitle = f"Skupaj servisnih zapisov: {len(rows)}"

    win = tk.Toplevel(root)
    win.title(f"MikroFix - {title}")
    maximize_work_window(win, "1320x650")
    win.configure(bg=BG)

    top = tk.Frame(win, bg=BG)
    top.pack(fill="x", padx=18, pady=(16,8))
    tk.Label(top, text=title, bg=BG, fg=DARK,
             font=("Segoe UI",16,"bold")).pack(anchor="w")
    tk.Label(top, text=subtitle, bg=BG, fg=GRAY,
             font=("Segoe UI",9)).pack(anchor="w", pady=(2,0))
    tk.Label(top, text="Dvoklikni servisni zapis za celoten servisni list.",
             bg=BG, fg=BLUE_DARK, font=("Segoe UI",9)).pack(anchor="w", pady=(4,0))

    frame = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY)
    frame.pack(fill="both", expand=True, padx=18, pady=(0,18))

    cols = ("id","no","date","customer","model","serial","imei","invoice","warranty","work")
    headings = ["ID","Servis","Datum","Stranka","Model","Serial Number","IMEI","Račun","Garancija","Opravljeno"]
    widths = [0,100,125,145,110,145,140,100,120,330]

    tree = ttk.Treeview(frame, columns=cols, show="headings")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    for c, h, w in zip(cols, headings, widths):
        tree.heading(c, text=h)
        if c == "id":
            tree.column(c, width=0, minwidth=0, stretch=False)
        else:
            tree.column(c, width=w, minwidth=80, anchor="w")

    for row in rows:
        values = list(row)
        # V programu nikoli javno ne prikazujemo celotnega S/N ali IMEI.
        values[5] = mask_identifier(values[5])
        values[6] = mask_identifier(values[6])
        values[8] = warranty_text(values[8])
        tree.insert("", "end", values=values)

    tree.bind("<Double-1>", lambda event: open_selected_service(event, tree))

    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)


def make_test_tab(parent, vars_dict):
    canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
    sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=PANEL)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0,0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)

    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    tk.Label(
        inner,
        text="Vsak modul začne kot NI TESTIRANO. Rezultat spremeni šele po dejanskem preizkusu.",
        bg=PANEL, fg=GRAY, font=("Segoe UI",9)
    ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12,8))

    for r, test_name in enumerate(TEST_ITEMS, start=1):
        tk.Label(inner, text=test_name, bg=PANEL, fg=DARK,
                 font=("Segoe UI",10), width=30, anchor="w").grid(
                     row=r, column=0, sticky="w", padx=12, pady=5)
        var = vars_dict.get(test_name)
        if var is None:
            var = tk.StringVar(value="NI TESTIRANO")
            vars_dict[test_name] = var
        cb = ttk.Combobox(inner, textvariable=var, values=TEST_STATUSES,
                          state="readonly", width=18)
        cb.grid(row=r, column=1, sticky="w", padx=8, pady=5)


def style_app():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure("TButton", font=("Segoe UI",10), padding=(10,7),
                    background=PANEL, foreground=DARK)
    style.map("TButton", background=[("active","#E7EDF2")])

    style.configure("Blue.TButton", font=("Segoe UI",10,"bold"), padding=(12,8),
                    background=BLUE, foreground="white",
                    bordercolor=BLUE, lightcolor=BLUE, darkcolor=BLUE)
    style.map("Blue.TButton",
              background=[("active",BLUE_DARK)],
              foreground=[("active","white")])

    style.configure(
        "Hero.TButton",
        font=("Segoe UI",13,"bold"),
        padding=(22,14),
        background=BLUE,
        foreground="white",
        bordercolor=BLUE,
        lightcolor=BLUE,
        darkcolor=BLUE
    )
    style.map(
        "Hero.TButton",
        background=[("active",BLUE_DARK)],
        foreground=[("active","white")]
    )

    style.configure("Treeview", rowheight=26, font=("Segoe UI",9),
                    background="white", fieldbackground="white", foreground=DARK)
    style.configure("Treeview.Heading", font=("Segoe UI",9,"bold"),
                    background="#E8EDF1", foreground=DARK)

    style.configure("TLabelframe", background=PANEL, bordercolor=LIGHT_GRAY)
    style.configure("TLabelframe.Label", background=PANEL, foreground=GRAY,
                    font=("Segoe UI",9,"bold"))
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", font=("Segoe UI",9,"bold"), padding=(12,7))


def on_close():
    c = connect()
    try:
        running_sessions = list_running_service_work_sessions(c)
    except Exception as exc:
        c.close()
        messagebox.showerror(
            "MikroFIX - preverjanje servisnega časa ni uspelo",
            "Programa ni mogoče varno zapreti, ker stanja servisnih sej ni "
            f"bilo mogoče preveriti:\n\n{exc}",
            parent=root,
        )
        return
    c.close()
    if running_sessions:
        details = []
        for session in running_sessions[:5]:
            description = " – ".join(
                value for value in (
                    session["order_no"], session["device_model"],
                    session["customer_name"],
                ) if value
            )
            details.append(f"• {description}")
        if len(running_sessions) > 5:
            details.append(f"• in še {len(running_sessions) - 5}")
        messagebox.showwarning(
            "MikroFIX - servisni čas še teče",
            "Programa ni mogoče zapreti, ker je aktivna servisna seja.\n\n"
            + "\n".join(details)
            + "\n\nNajprej odpri navedeni servis ter izberi USTAVI ČAS ali "
              "zaključi servis.",
            parent=root,
        )
        return
    if ACTIVE_SCREEN != "MENU":
        show_start_menu()
        return
    backup_db("close")
    if bool(RUNTIME.settings.get("backup", {}).get("enabled")):
        try:
            create_due_external_backup(RUNTIME)
        except Exception as exc:
            if not messagebox.askyesno(
                "MikroFIX - varnostna kopija ni uspela",
                f"Dnevne zunanje varnostne kopije ni bilo mogoče dokončati:\n\n{exc}"
                "\n\nAli kljub temu zaprem program?",
                parent=root,
            ):
                return
    if bool(RUNTIME.settings.get("data_encryption", {}).get("enabled")):
        try:
            unmount_encrypted_data(RUNTIME.data_dir)
        except DataEncryptionError as exc:
            messagebox.showerror(
                "MikroFIX - zaklep podatkov ni uspel",
                f"Program ostaja odprt, ker podatkovne mape ni bilo mogoče varno zakleniti:\n\n{exc}",
                parent=root,
            )
            return
    root.destroy()


def _restore_after_office(return_code=0, service_order_id=None):
    """Po zaprtju Office vrne isto prijavljeno sejo v začetni izbor."""
    root.deiconify()
    try:
        root.state("zoomed")
        root.lift()
        root.focus_force()
    except Exception:
        pass
    if return_code == 20:
        # ODJAVA v Office pomeni pravo odjavo tudi v glavnem programu.
        root.after(50, logout_and_lock)
    elif service_order_id is not None:
        root.after(80, lambda oid=int(service_order_id): open_service_order_from_office(oid))
    else:
        root.after(80, show_start_menu)


def launch_office(*arguments):
    """Odpre Office v istem procesu in uporabi že potrjeno sejo."""
    required_permission = (
        PERMISSION_CASHIER if "--new-invoice" in arguments else PERMISSION_OFFICE
    )
    if not AUTH_SESSION.has_permission(required_permission):
        audit_event(
            "PERMISSION_DENIED",
            "module",
            "CASHIER" if required_permission == PERMISSION_CASHIER else "OFFICE",
        )
        messagebox.showwarning(
            "MikroFIX", "Za izbrani del programa nimaš dovoljenja.", parent=root
        )
        return False
    return_code = 0
    service_order_id = None
    previous_handoff = os.environ.get("MIKROFIX_SESSION_HANDOFF")
    previous_argv = list(sys.argv)
    try:
        token = auth_store().issue_session_handoff(
            AUTH_SESSION.username,
            "OFFICE",
            ttl_seconds=60,
        )
        os.environ["MIKROFIX_SESSION_HANDOFF"] = token
        sys.argv = [str(APP_DIR / "mikrofix_office.py"), *arguments]
        root.withdraw()
        namespace = runpy.run_path(
            str(APP_DIR / "mikrofix_office.py"),
            init_globals={
                "MIKROFIX_PARENT": root,
                "MIKROFIX_FINALIZE_SERVICE_PDF": generate_pdf,
            },
        )
        return_code = int(namespace.get("OFFICE_EXIT_CODE") or 0)
        service_order_id = namespace.get("OFFICE_START_SERVICE_ORDER_ID")
    except SystemExit as exc:
        return_code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    except Exception as exc:
        if os.environ.get("MIKROFIX_SMOKE_TEST") == "1":
            # Pri avtomatskem zagonskem pregledu ni uporabnika, ki bi videl
            # grafično opozorilo. Celoten traceback mora zato v terminal, sicer
            # končni RuntimeError skrije pravi razlog napake v Office modulu.
            import traceback

            traceback.print_exc()
        else:
            messagebox.showerror("MikroFIX", f"Office ni bilo mogoče odpreti:\n{exc}")
        return False
    finally:
        sys.argv = previous_argv
        if previous_handoff is None:
            os.environ.pop("MIKROFIX_SESSION_HANDOFF", None)
        else:
            os.environ["MIKROFIX_SESSION_HANDOFF"] = previous_handoff
        _restore_after_office(return_code, service_order_id)
    return True


def launch_pos(service_id=None):
    """Odpre Blagajno neposredno iz glavnega programa, brez Office v ozadju."""
    global ACTIVE_SCREEN
    if not AUTH_SESSION.has_permission(PERMISSION_CASHIER):
        audit_event("PERMISSION_DENIED", "module", "CASHIER")
        messagebox.showwarning(
            "MikroFIX", "Za Blagajno nimaš dovoljenja.", parent=root
        )
        return False
    ACTIVE_SCREEN = "CASHIER"
    if main_menu_frame is not None and main_menu_frame.winfo_exists():
        main_menu_frame.pack_forget()

    def return_to_menu():
        root.deiconify()
        try:
            root.state("zoomed")
        except Exception:
            pass
        root.after(50, show_start_menu)

    try:
        open_pos(
            root,
            runtime=RUNTIME,
            office_db=OFFICE_DB,
            inventory_db=INVENTORY_DB,
            service_db=DB,
            pdf_dir=PDF_DIR,
            operator_code=RUNTIME.operator_code,
            authenticated_username=AUTH_SESSION.username,
            action_permissions=AUTH_SESSION.permissions,
            audit_event=audit_event,
            service_id=service_id,
            service_loader=load_service_invoice_seed,
            service_finalizer=generate_pdf,
            on_close=return_to_menu,
        )
    except Exception as exc:
        messagebox.showerror(
            "MikroFIX", f"Blagajne ni bilo mogoče odpreti:\n{exc}", parent=root
        )
        return_to_menu()
        return False
    return True


def show_recovery_center(parent):
    """Prikaže nedokončane operacije brez samodejnega posega v poslovne podatke."""
    dialog = tk.Toplevel(parent)
    dialog.title("MikroFIX - obnovitveni center")
    maximize_work_window(dialog, "900x500")
    dialog.minsize(760, 420)
    dialog.transient(parent)

    top = tk.Frame(dialog, bg=PANEL)
    top.pack(fill="x")
    tk.Label(
        top, text="Obnovitveni center", bg=PANEL, fg=DARK,
        font=("Segoe UI", 17, "bold"),
    ).pack(anchor="w", padx=18, pady=(15, 2))
    tk.Label(
        top,
        text=(
            "Tu so prikazane prekinjene ali neuspele operacije. Program jih ne "
            "bo skril ali samovoljno ponovil."
        ),
        bg=PANEL, fg=GRAY, font=("Segoe UI", 9),
    ).pack(anchor="w", padx=18, pady=(0, 14))

    body = tk.Frame(dialog, bg=BG)
    body.pack(fill="both", expand=True, padx=16, pady=14)
    columns = ("id", "type", "status", "time", "error")
    tree = ttk.Treeview(body, columns=columns, show="headings")
    for key, label, width in (
        ("id", "ID operacije", 215),
        ("type", "Vrsta", 150),
        ("status", "Stanje", 120),
        ("time", "Zadnja sprememba", 165),
        ("error", "Pojasnilo", 300),
    ):
        tree.heading(key, text=label)
        tree.column(key, width=width, minwidth=80, stretch=key == "error")
    scroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    footer = tk.Frame(dialog, bg=PANEL)
    footer.pack(fill="x", side="bottom")
    status_var = tk.StringVar(value="")
    tk.Label(
        footer, textvariable=status_var, bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9),
    ).pack(side="left", padx=16, pady=12)

    def refresh():
        tree.delete(*tree.get_children())
        connection = connect()
        try:
            mark_stale_operations(connection)
            rows = list_recovery_operations(connection)
            errors = verify_outbox_chain(connection)
            connection.commit()
        finally:
            connection.close()
        for row in rows:
            tree.insert(
                "", "end",
                values=(
                    row["operation_id"], row["operation_type"], row["status"],
                    row["updated_at"], row.get("error_text") or "-",
                ),
            )
        if errors:
            for error in errors:
                tree.insert("", "end", values=("DNEVNIK", "PREVERJANJE", "NAPAKA", "-", error))
        status_var.set(
            "Ni odprtih postopkov." if not rows and not errors
            else f"Za pregled: {len(rows) + len(errors)}"
        )

    ttk.Button(footer, text="OSVEŽI", command=refresh).pack(
        side="right", padx=(6, 16), pady=9
    )
    ttk.Button(footer, text="ZAPRI", command=dialog.destroy).pack(
        side="right", pady=9
    )
    refresh()


def run_external_backup(*, force=False, notify=False):
    """Zažene zunanjo kopijo v ozadju, da ne zamrzne uporabniškega vmesnika."""
    global external_backup_thread, external_backup_last_message, external_backup_attempted
    if external_backup_thread is not None and external_backup_thread.is_alive():
        if notify:
            messagebox.showinfo("MikroFIX - varnostna kopija", "Kopiranje že poteka.")
        return

    external_backup_attempted = True

    def schedule_if_open(callback):
        # Delovna nit ne sme klicati winfo_* na Tk objektu. Če se je aplikacija
        # med kopiranjem že zaprla, se pozni UI odziv varno opusti.
        try:
            root.after(0, callback)
            return True
        except (RuntimeError, tk.TclError):
            return False

    def worker():
        global external_backup_last_message
        try:
            path = create_due_external_backup(RUNTIME, force=force)
            external_backup_last_message = (
                f"Zunanja kopija je preverjena: {path}"
                if path else "Zunanja kopija je že veljavna in mlajša od 24 ur."
            )
            if notify:
                schedule_if_open(
                    lambda: messagebox.showinfo(
                        "MikroFIX - varnostna kopija", external_backup_last_message
                    ),
                )
        except Exception as exc:
            external_backup_last_message = str(exc)
            if notify:
                schedule_if_open(
                    lambda text=str(exc): messagebox.showerror(
                        "MikroFIX - varnostna kopija", text
                    ),
                )
        finally:
            schedule_if_open(
                lambda: show_start_menu() if ACTIVE_SCREEN == "MENU" else None
            )

    external_backup_thread = threading.Thread(
        target=worker, name="MikroFixExternalBackup", daemon=True
    )
    external_backup_thread.start()


def configure_external_backup(parent):
    if os.name == "nt":
        initial_dir = "E:\\" if Path("E:\\").exists() else str(Path.home())
    else:
        initial_dir = (
            "/media/mikrofix/KINGSTON"
            if Path("/media/mikrofix/KINGSTON").is_dir()
            else str(Path.home())
        )
    selected = filedialog.askdirectory(
        title="Izberi zunanji nosilec za šifrirane varnostne kopije",
        initialdir=initial_dir,
        mustexist=True,
        parent=parent,
    )
    if not selected:
        return
    selected_path = Path(selected).resolve()
    target_dir = (
        selected_path
        if selected_path.name == "MikroFix_Backup"
        else selected_path / "MikroFix_Backup"
    )
    first = simpledialog.askstring(
        "MikroFIX - obnovitveno geslo",
        "Določi obnovitveno geslo (najmanj 12 znakov).\n"
        "To geslo bo potrebno za obnovitev kopije na drugem računalniku:",
        show="*", parent=parent,
    )
    if first is None:
        return
    second = simpledialog.askstring(
        "MikroFIX - ponovi geslo", "Ponovno vpiši isto obnovitveno geslo:",
        show="*", parent=parent,
    )
    if second is None:
        return
    if first != second:
        messagebox.showerror("MikroFIX", "Gesli se ne ujemata.", parent=parent)
        return
    try:
        save_backup_passphrase(RUNTIME.data_dir, first)
        update_backup_settings(
            RUNTIME.settings_path,
            target_dir=str(target_dir),
            enabled=True,
            passphrase_configured=True,
        )
        RUNTIME.settings["backup"].update(
            {
                "enabled": True,
                "target_dir": str(target_dir),
                "retention_days": 30,
                "max_age_hours": 24,
                "require_distinct_volume": True,
                "passphrase_configured": True,
            }
        )
    except Exception as exc:
        messagebox.showerror("MikroFIX - varnostna kopija", str(exc), parent=parent)
        return
    run_external_backup(force=True, notify=True)


def open_workflow_module(tab, intent):
    """Odpri delovni modul iz menija ali po predaji iz Office."""
    global ACTIVE_SCREEN, start_workflow_intent
    ACTIVE_SCREEN = intent
    start_workflow_intent = intent
    if main_menu_frame is not None and main_menu_frame.winfo_exists():
        main_menu_frame.pack_forget()
    outer.pack(fill="both", expand=True, padx=22, pady=16)
    root.title(f"MikroFIX {RUNTIME.version_label} - {intent.title()}")
    all_tabs = (
        tab_device_top,
        tab_intake_top,
        tab_service_top,
        tab_buyback_top,
        tab_inventory_top,
        tab_supplier_top,
        tab_claims_top,
    )
    for known_tab in all_tabs:
        top_nb.tab(known_tab, state="hidden")
    visible_tabs = (
        (tab_device_top, tab_intake_top, tab_service_top, tab_claims_top)
        if intent == "SERVICE"
        else (tab_device_top, tab_buyback_top)
    )
    for visible_tab in visible_tabs:
        top_nb.tab(visible_tab, state="normal")
    update_workflow_lock()
    top_nb.select(tab)


def show_start_menu():
    """Prikaže glavni meni kot zaslon istega aplikacijskega okna."""
    global ACTIVE_SCREEN, main_menu_frame
    pause_active_service_timer(reason="MAIN_MENU", quiet=True)
    ACTIVE_SCREEN = "MENU"
    root.title(f"MikroFIX {RUNTIME.version_label} - Kaj želiš narediti?")
    root.deiconify()
    outer.pack_forget()
    if main_menu_frame is not None and main_menu_frame.winfo_exists():
        main_menu_frame.destroy()

    main_menu_frame = tk.Frame(root, bg=BG)
    main_menu_frame.pack(fill="both", expand=True)

    header = tk.Frame(main_menu_frame, bg=PANEL, height=72)
    header.pack(fill="x")
    header.pack_propagate(False)
    brand_box = tk.Frame(header, bg=PANEL)
    brand_box.pack(side="left", padx=24, pady=12)
    tk.Label(
        brand_box, text="Mikro", bg=PANEL, fg=GRAY,
        font=("Segoe UI", 22, "bold"),
    ).pack(side="left")
    tk.Label(
        brand_box, text="FIX", bg=PANEL, fg=BLUE,
        font=("Segoe UI", 22, "bold"),
    ).pack(side="left")
    tk.Label(
        brand_box, text="  v30.0", bg=PANEL, fg=BLUE_DARK,
        font=("Segoe UI", 9, "bold"),
    ).pack(side="left", pady=(8, 0))

    ttk.Button(header, text="ODJAVA", command=logout_and_lock).pack(
        side="right", padx=(8, 24), pady=18
    )
    ttk.Button(
        header, text="SPREMENI PIN", command=lambda: change_pin_dialog(root)
    ).pack(side="right", pady=18)
    if AUTH_SESSION.has_permission(PERMISSION_MANAGE_USERS):
        ttk.Button(
            header, text="UPORABNIKI", command=lambda: manage_users_dialog(root)
        ).pack(side="right", padx=(0, 8), pady=18)
    tk.Label(
        header, text=AUTH_SESSION.caption(), bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9, "bold"),
    ).pack(side="right", padx=16, pady=24)

    content = tk.Frame(main_menu_frame, bg=BG)
    content.pack(fill="both", expand=True, padx=36, pady=28)
    tk.Label(
        content, text="Kaj želiš narediti?", bg=BG, fg=DARK,
        font=("Segoe UI", 23, "bold"),
    ).pack(anchor="w")
    tk.Label(
        content,
        text="Izberi del programa. Prijava velja za celotno sejo.",
        bg=BG, fg=GRAY, font=("Segoe UI", 10),
    ).pack(anchor="w", pady=(3, 18))

    recovery_connection = connect()
    try:
        mark_stale_operations(recovery_connection)
        recovery_items = list_recovery_operations(recovery_connection)
        outbox_errors = verify_outbox_chain(recovery_connection)
        recovery_connection.commit()
    finally:
        recovery_connection.close()

    if recovery_items or outbox_errors:
        warning_box = tk.Frame(
            content, bg="#FFF4DF", highlightthickness=1,
            highlightbackground="#D8A447",
        )
        warning_box.pack(fill="x", pady=(0, 14))
        message = (
            f"Obnovitveni center: {len(recovery_items)} nedokončanih operacij"
            if recovery_items
            else "Obnovitveni center: preverjanje dnevnika je odkrilo nepravilnost"
        )
        tk.Label(
            warning_box, text=message, bg="#FFF4DF", fg=DARK,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=12, pady=9)
        ttk.Button(
            warning_box, text="PREGLEJ",
            command=lambda: show_recovery_center(root),
        ).pack(side="right", padx=10, pady=6)

    backup_state = external_backup_status(RUNTIME.settings, RUNTIME.data_dir)
    backup_color = "#EAF5EC" if backup_state.code == "OK" else "#FFF4DF"
    backup_border = "#6AA174" if backup_state.code == "OK" else "#D8A447"
    backup_box = tk.Frame(
        content, bg=backup_color, highlightthickness=1,
        highlightbackground=backup_border,
    )
    backup_box.pack(fill="x", pady=(0, 14))
    backup_text = backup_state.message
    if external_backup_last_message:
        backup_text = external_backup_last_message
    tk.Label(
        backup_box, text=f"Varnostna kopija: {backup_text}",
        bg=backup_color, fg=DARK, font=("Segoe UI", 9, "bold"),
    ).pack(side="left", padx=12, pady=9)
    if backup_state.code in {"SETUP_REQUIRED", "PASSWORD_REQUIRED", "TARGET_MISSING"}:
        ttk.Button(
            backup_box, text="NASTAVI",
            command=lambda: configure_external_backup(root),
        ).pack(side="right", padx=10, pady=6)
    else:
        ttk.Button(
            backup_box, text="KOPIRAJ ZDAJ",
            command=lambda: run_external_backup(force=True, notify=True),
        ).pack(side="right", padx=10, pady=6)
        if backup_state.due and not external_backup_attempted:
            root.after(200, run_external_backup)

    grid = tk.Frame(content, bg=BG)
    grid.pack(fill="both", expand=True)
    grid.grid_rowconfigure(0, weight=1, uniform="menu")
    for column in range(3):
        grid.grid_columnconfigure(column, weight=1, uniform="menu")

    def choose_service():
        # Servis se odpre na delovni čakalni vrsti. Nova naprava oziroma nov
        # sprejem sta še vedno dosegljiva z istega zaslona.
        open_workflow_module(tab_service_top, "SERVICE")
        show_service_history()

    def choose_office(*args):
        global ACTIVE_SCREEN
        ACTIVE_SCREEN = "OFFICE"
        main_menu_frame.pack_forget()
        if not launch_office(*args):
            root.after(100, show_start_menu)

    choices = [
        (
            PERMISSION_SERVICE, "SERVIS",
            "Sprejem naprave, MicroDETECT, servisne postavke in zaključek.",
            "Odprti servisi", choose_service,
        ),
        (
            PERMISSION_CASHIER, "BLAGAJNA",
            "Novi račun in računi, pripravljeni iz zaključenih servisov.",
            "Pripravljeni računi", launch_pos,
        ),
        (
            PERMISSION_OFFICE, "OFFICE",
            "Dokumenti, zaloga, odkup, obnova, cene in administracija.",
            "Nedokončani dokumenti", lambda: choose_office("--open-tab", "documents"),
        ),
    ]

    visible_choices = [
        choice for choice in choices if AUTH_SESSION.has_permission(choice[0])
    ]
    for index, (_permission, title, detail, counter_label, command) in enumerate(
        visible_choices
    ):
        row, column = 0, index
        card = tk.Frame(
            grid, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY,
        )
        card.grid(row=row, column=column, sticky="nsew", padx=8, pady=8)
        ttk.Button(
            card, text=title, command=command, style="Hero.TButton",
        ).pack(fill="x", padx=16, pady=(18, 10))
        tk.Label(
            card, text=detail, bg=PANEL, fg=GRAY, font=("Segoe UI", 10),
            justify="left", wraplength=420,
        ).pack(anchor="w", padx=18)
        tk.Label(
            card, text=f"{counter_label}: -", bg=PANEL, fg=BLUE,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=18, pady=(14, 16))

    if not visible_choices:
        tk.Label(
            grid,
            text="Ta uporabnik nima dovoljenja za noben modul. Obrni se na skrbnika.",
            bg=BG, fg=DANGER, font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="nsew", pady=30)


init_db()
init_inventory_db()
migrate_inventory_from_main_db()
initialize_v29(
    inventory_db=INVENTORY_DB,
    office_db=RUNTIME.db_path("office"),
    service_db=DB,
    pricing=RUNTIME.settings["pricing"],
)
sync_completed_buybacks_to_inventory(
    service_db=DB,
    inventory_db=INVENTORY_DB,
    buyer={
        "legal_name": RUNTIME.settings["business"]["legal_name"],
        "tax_number": RUNTIME.settings["business"]["tax_number"],
        "registration_number": RUNTIME.settings["business"]["registration_number"],
        "address": COMPANY_ADDRESS,
        "representative": "Daniel Ristoski",
    },
    operator_code=RUNTIME.operator_code,
)
SUPPLIER_STORE = SupplierCatalogStore(
    INVENTORY_DB,
    RUNTIME.settings["pricing"],
    RUNTIME.settings["supplier"]["fixshop"],
)
SUPPLIER_STORE.init()
SUPPLIER_STORE.seed_from_file(APP_DIR / "mikrofix_supplier_seed.json")
supplier_refresh_thread = None
backup_db("startup")

root = tk.Tk()
root.title(f"MikroFIX Service {RUNTIME.version_label}")
root.geometry("1180x760")
root.minsize(900, 620)
root.configure(bg=BG)
root.withdraw()

# Program se na Windows odpre maksimiran.
maximize_work_window(root, "1180x760")
style_app()
root.protocol("WM_DELETE_WINDOW", on_close)
for activity_event in ("<Any-KeyPress>", "<Any-Button>", "<MouseWheel>"):
    root.bind_all(activity_event, note_user_activity, add="+")
root.after(5000, check_idle_timeout)

outer = tk.Frame(root, bg=BG)
outer.pack(fill="both", expand=True, padx=22, pady=16)

# Header
brand = tk.Frame(outer, bg=BG)
brand.pack(fill="x", pady=(0,10))

brand_left = tk.Frame(brand, bg=BG)
brand_left.pack(side="left")

brand_logo = tk.Frame(brand_left, bg=BG)
brand_logo.pack(anchor="w")

tk.Label(
    brand_logo, text="Mikro", bg=BG, fg=GRAY,
    font=("Segoe UI",26,"bold")
).pack(side="left")
tk.Label(
    brand_logo, text="FIX", bg=BG, fg=BLUE,
    font=("Segoe UI",26,"bold")
).pack(side="left")

tk.Label(
    brand_left, text="SERVICE",
    bg=BG, fg=GRAY,
    font=("Segoe UI",8,"bold")
).pack(anchor="center", pady=(0,0))

tk.Label(
    brand, text=ENVIRONMENT_NOTICE,
    bg=BG, fg=ENVIRONMENT_NOTICE_COLOR, font=("Segoe UI", 9, "bold")
).pack(side="left", padx=(24, 0), pady=(12, 0))

connected_state = tk.StringVar(value="NI POVEZAVE")
header_connected_label = tk.Label(
    brand, textvariable=connected_state,
    bg=BG, fg=GRAY, font=("Segoe UI",9,"bold")
)
header_connected_label.pack(side="right", pady=(12,0))

ttk.Button(
    brand, text="ODJAVA",
    command=logout_and_lock
).pack(side="right", padx=(0,8), pady=(6,0))

ttk.Button(
    brand, text="ZAČETNI MENI",
    command=show_start_menu,
).pack(side="right", padx=(0,8), pady=(6,0))

ttk.Button(
    brand, text="SPREMENI PIN",
    command=lambda: change_pin_dialog(root)
).pack(side="right", padx=(0,8), pady=(6,0))

users_button = ttk.Button(
    brand, text="UPORABNIKI",
    command=lambda: manage_users_dialog(root)
)
users_button.pack(side="right", padx=(0,8), pady=(6,0))
AUTH_SESSION.bind_admin_widget(users_button)

current_user_var = tk.StringVar()
AUTH_SESSION.bind_caption_variable(current_user_var)
tk.Label(
    brand, textvariable=current_user_var,
    bg=BG, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="right", padx=(8,12), pady=(12,0))

# Zgornji zavihki: Naprava / Zaloga delov
top_nb = ttk.Notebook(outer)
top_nb.pack(fill="both", expand=True)

tab_device_top = tk.Frame(top_nb, bg=PANEL)
tab_intake_top = tk.Frame(top_nb, bg=PANEL)
tab_service_top = tk.Frame(top_nb, bg=PANEL)
tab_buyback_top = tk.Frame(top_nb, bg=PANEL)
tab_inventory_top = tk.Frame(top_nb, bg=PANEL)
tab_supplier_top = tk.Frame(top_nb, bg=PANEL)
tab_claims_top = tk.Frame(top_nb, bg=PANEL)

top_nb.add(tab_device_top, text="Naprava")
top_nb.add(tab_intake_top, text="Servisno naročilo", state="disabled")
top_nb.add(tab_service_top, text="Servisni list", state="disabled")
top_nb.add(tab_buyback_top, text="Odkup", state="disabled")
top_nb.add(tab_inventory_top, text="Zaloga delov")
top_nb.add(tab_supplier_top, text="Cenik in dobavitelj")
top_nb.add(tab_claims_top, text="Reklamacije", state="disabled")

# Naprava - vhodna servisna stran
model = tk.StringVar()
ios = tk.StringVar()
storage = tk.StringVar()
serial = tk.StringVar()       # polna vrednost - interno / baza
imei = tk.StringVar()         # polna vrednost - interno / baza
serial_display = tk.StringVar()
imei_display = tk.StringVar()
name = tk.StringVar()
customer = tk.StringVar()
customer_address = tk.StringVar()
customer_phone = tk.StringVar()
invoice_no = tk.StringVar()
labor_price = tk.StringVar(value=f"{LABOR_FLOOR_EUR:.2f}")
device_history_count = tk.StringVar(value="")

# Servisno naročilo - osebni podatki so dosegljivi šele po MicroDETECT.
intake_device_summary = tk.StringVar(value="Naprava še ni identificirana.")
intake_order_status = tk.StringVar(value="Novo servisno naročilo.")
intake_customer_name = tk.StringVar()
intake_customer_address = tk.StringVar()
intake_customer_phone = tk.StringVar()
intake_customer_email = tk.StringVar()
intake_customer_partner_id = None

def _intake_customer_edited(*_args):
    global intake_customer_partner_id
    if intake_customer_partner_id is not None:
        intake_customer_partner_id = None
        intake_order_status.set(
            "Podatki stranke so bili ročno spremenjeni; po potrebi jo ponovno shrani/izberi."
        )

for _customer_variable in (
    intake_customer_name, intake_customer_address,
    intake_customer_phone, intake_customer_email,
):
    _customer_variable.trace_add("write", _intake_customer_edited)
intake_accessories = tk.StringVar()
intake_quote = tk.StringVar(value="0.00")
intake_logistics = tk.StringVar(value="0.00")
intake_due = tk.StringVar(value="1-2 delovna dneva")
intake_context = tk.StringVar(
    value=_display_value(CONTRACT_CONTEXT_LABELS, "MFT_TEREN")
)
intake_delivery = tk.StringVar(
    value=_display_value(DELIVERY_LABELS, "EMAIL_PDF")
)
intake_immediate_start = tk.BooleanVar(value=False)
intake_withdrawal_ack = tk.BooleanVar(value=False)

# Odkup - model, OS, S/N in IMEI niso vnosna polja; prihajajo iz MicroDETECT.
buyback_device_summary = tk.StringVar(value="Naprava še ni identificirana.")
buyback_status_text = tk.StringVar(value="Nov odkup - osebni podatki še niso shranjeni.")
buyback_seller_name = tk.StringVar()
buyback_tax_number = tk.StringVar()
buyback_address = tk.StringVar()
buyback_contact = tk.StringVar()
buyback_iban = tk.StringVar()
buyback_identity_checked = tk.BooleanVar(value=False)
buyback_color_capacity = tk.StringVar()
buyback_accessories = tk.StringVar()
buyback_acquisition_method = tk.StringVar(
    value=_display_value(ACQUISITION_LABELS, "KUPLJENA_RABLJENA_FIZICNA_OSEBA")
)
buyback_acquisition_details = tk.StringVar()
buyback_evidence = tk.StringVar(
    value=_display_value(EVIDENCE_LABELS, "NI_NA_VOLJO")
)
buyback_imei_provider = tk.StringVar(value="TESTNI SIMULATOR" if RUNTIME.is_test else "")
buyback_imei_result = tk.StringVar(
    value=_display_value(IMEI_RESULT_LABELS, "TEST_CLEAR") if RUNTIME.is_test else ""
)
buyback_imei_reference = tk.StringVar()
buyback_account_removed = tk.BooleanVar(value=False)
buyback_activation_removed = tk.BooleanVar(value=False)
buyback_mdm_clear = tk.BooleanVar(value=False)
buyback_ownership = tk.BooleanVar(value=False)
buyback_privacy = tk.BooleanVar(value=False)
buyback_risk = tk.StringVar(value=_display_value(RISK_LABELS, "GREEN"))
buyback_risk_reason = tk.StringVar()
buyback_price = tk.StringVar(value="0.00")
buyback_payout = tk.StringVar(
    value=_display_value(PAYOUT_LABELS, "BANK_TRANSFER")
)
buyback_purpose = tk.StringVar(
    value=_display_value(BUYBACK_PURPOSE_LABELS, "UNDECIDED")
)
buyback_notes = tk.StringVar()

# Test PRED je skupen med vhodno stranjo in servisnim listom.
pre_test_vars = {
    test_name: tk.StringVar(value="NI TESTIRANO")
    for test_name in TEST_ITEMS
}
quick_test_widgets = []
quick_test_summary = tk.StringVar(value=f"0 OK • 0 napak • {len(TEST_ITEMS)} nepreverjenih")

device_page = tk.Frame(tab_device_top, bg=BG)
device_page.pack(fill="both", expand=True, padx=24, pady=14)

device_hero = tk.Frame(device_page, bg=BG)
device_hero.pack(fill="x", pady=(2,10))

tk.Label(
    device_hero,
    text="Poveži napravo",
    bg=BG, fg=DARK,
    font=("Segoe UI",21,"bold")
).pack(anchor="w")

tk.Label(
    device_hero,
    text=(
        "iPhone: lastnik odklene napravo in potrdi povezavo, nato MicroDETECT. "
        "Računalnik, konzola ali druga elektronika: uporabi DRUGA NAPRAVA."
    ),
    bg=BG, fg=GRAY,
    font=("Segoe UI",10)
).pack(anchor="w", pady=(4,0))

device_card = tk.Frame(
    device_page, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
device_card.pack(fill="x")

device_card_top = tk.Frame(device_card, bg=PANEL)
device_card_top.pack(fill="x", padx=18, pady=(12,7))

tk.Label(
    device_card_top,
    text="USB IDENTIFIKACIJA",
    bg=PANEL, fg=GRAY,
    font=("Segoe UI",9,"bold")
).pack(side="left")

connected_label = tk.Label(
    device_card_top,
    textvariable=connected_state,
    bg=PANEL, fg=GRAY,
    font=("Segoe UI",10,"bold")
)
connected_label.pack(side="right")

detect_action_row = tk.Frame(device_card, bg=PANEL)
detect_action_row.pack(fill="x", padx=22, pady=(0,12))

detect_button = ttk.Button(
    detect_action_row,
    text="MicroDETECT",
    command=detect,
    style="Blue.TButton"
)
detect_button.pack(side="left")

ttk.Button(
    detect_action_row,
    text="DRUGA NAPRAVA",
    command=identify_other_device,
).pack(side="left", padx=(8, 0))

detect_progress = ttk.Progressbar(
    detect_action_row,
    mode="indeterminate",
    length=160
)
detect_progress.pack(side="left", padx=(12,0))

device_info = tk.Frame(device_card, bg="#F7F9FA")
device_info.pack(fill="x", padx=18, pady=(0,12))

device_fields = [
    ("IME NAPRAVE", name),
    ("MODEL", model),
    ("POMNILNIK", storage),
    ("iOS", ios),
    ("SERIAL NUMBER", serial_display),
    ("IMEI", imei_display),
]

for col, (label_text, var) in enumerate(device_fields):
    box = tk.Frame(device_info, bg="#F7F9FA")
    box.grid(row=0, column=col, sticky="nsew", padx=1, pady=1)
    tk.Label(
        box, text=label_text,
        bg="#F7F9FA", fg=GRAY,
        font=("Segoe UI",8,"bold")
    ).pack(anchor="w", padx=12, pady=(10,2))
    tk.Label(
        box, textvariable=var,
        bg="#F7F9FA", fg=DARK,
        font=("Segoe UI",11,"bold"),
        anchor="w"
    ).pack(anchor="w", padx=12, pady=(0,10))
    device_info.grid_columnconfigure(col, weight=1)

# Hiter predservisni pregled
quick_test_card = tk.Frame(
    device_page, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
quick_test_card.pack(fill="both", expand=True, pady=(8,0))

quick_test_head = tk.Frame(quick_test_card, bg=PANEL)
quick_test_head.pack(fill="x", padx=16, pady=(9,4))

tk.Label(
    quick_test_head,
    text="HITER TEST PRED SERVISOM",
    bg=PANEL, fg=DARK,
    font=("Segoe UI",11,"bold")
).pack(side="left")

quick_test_summary_label = tk.Label(
    quick_test_head,
    textvariable=quick_test_summary,
    bg=PANEL, fg=SUCCESS,
    font=("Segoe UI",9,"bold")
)
quick_test_summary_label.pack(side="right")

tk.Label(
    quick_test_card,
    text="Vsak modul je najprej nepreverjen. Prvi klik ga označi OK, naslednji NE DELA; podroben izbor je v servisnem listu.",
    bg=PANEL, fg=GRAY,
    font=("Segoe UI",9)
).pack(anchor="w", padx=20, pady=(0,8))

quick_test_grid = tk.Frame(quick_test_card, bg=PANEL)
quick_test_grid.pack(fill="both", expand=True, padx=12, pady=(0,10))

def update_quick_test_summary(*_args):
    ok_count = sum(
        1 for var in pre_test_vars.values()
        if var.get() == "OK"
    )
    fail_count = sum(
        1 for var in pre_test_vars.values()
        if var.get() == "NE DELA"
    )
    unchecked_count = len(TEST_ITEMS) - ok_count - fail_count
    quick_test_summary.set(
        f"{ok_count} OK • {fail_count} napak • {unchecked_count} nepreverjenih"
    )
    quick_test_summary_label.configure(
        fg=DANGER if fail_count else (WARNING if unchecked_count else SUCCESS)
    )

for idx, test_name in enumerate(TEST_ITEMS):
    row_i = idx // 3
    col_i = idx % 3

    item = tk.Frame(quick_test_grid, bg="#F7F9FA")
    item.grid(
        row=row_i, column=col_i,
        sticky="nsew", padx=5, pady=4
    )
    quick_test_grid.grid_columnconfigure(col_i, weight=1)
    quick_test_grid.grid_rowconfigure(row_i, weight=1)

    cb = tk.Checkbutton(
        item,
        variable=pre_test_vars[test_name],
        onvalue="OK",
        offvalue="NE DELA",
        indicatoron=False,
        relief="flat",
        overrelief="flat",
        bd=0,
        font=("Segoe UI",10,"bold"),
        anchor="w",
        padx=12,
        pady=10,
        cursor="hand2",
        disabledforeground=GRAY,
        state="disabled"
    )
    cb.pack(fill="both", expand=True)

    def _style_tile(*_args, widget=cb, var=pre_test_vars[test_name], label=test_name):
        state_v = var.get()
        if state_v == "OK":
            widget.configure(
                text=f"✓  {label}",
                bg="#EAF5EE",
                fg=SUCCESS,
                activebackground="#DDEFE4",
                activeforeground=SUCCESS
            )
        elif state_v == "NE DELA":
            widget.configure(
                text=f"✕  {label}",
                bg="#FBECEC",
                fg=DANGER,
                activebackground="#F6DDDD",
                activeforeground=DANGER
            )
        else:
            widget.configure(
                text=f"—  {label}",
                bg="#F3F5F6",
                fg=GRAY,
                activebackground="#E9ECEE",
                activeforeground=GRAY
            )

    quick_test_widgets.append(cb)
    pre_test_vars[test_name].trace_add("write", update_quick_test_summary)
    pre_test_vars[test_name].trace_add("write", _style_tile)
    _style_tile()

update_quick_test_summary()

device_meta = tk.Frame(device_page, bg=BG)
device_meta.pack(fill="x", pady=(10,0))

tk.Label(
    device_meta,
    textvariable=device_history_count,
    bg=BG, fg=GRAY,
    font=("Segoe UI",9)
).pack(side="left")

status = tk.StringVar(value="Priklopi iPhone prek USB in klikni MicroDETECT.")
tk.Label(
    device_meta,
    textvariable=status,
    bg=BG, fg=GRAY,
    font=("Segoe UI",9)
).pack(side="right")

# Servisno naročilo - prvi dokument ob prevzemu
intake_page = tk.Frame(tab_intake_top, bg=BG)
intake_page.pack(fill="both", expand=True, padx=18, pady=12)

intake_head = tk.Frame(intake_page, bg=BG)
intake_head.pack(fill="x", pady=(0,8))
tk.Label(
    intake_head, text="Servisno naročilo in prevzem",
    bg=BG, fg=DARK, font=("Segoe UI",17,"bold")
).pack(side="left")
tk.Label(
    intake_head, textvariable=intake_order_status,
    bg=BG, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="right")

intake_identity = tk.Label(
    intake_page, textvariable=intake_device_summary,
    bg="#EAF5EE", fg=SUCCESS, font=("Segoe UI",10,"bold"),
    anchor="w", padx=12, pady=9
)
intake_identity.pack(fill="x", pady=(0,8))

intake_columns = tk.Frame(intake_page, bg=BG)
intake_columns.pack(fill="both", expand=True)
intake_left = tk.Frame(intake_columns, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY)
intake_right = tk.Frame(intake_columns, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY)
intake_left.grid(row=0, column=0, sticky="nsew", padx=(0,5))
intake_right.grid(row=0, column=1, sticky="nsew", padx=(5,0))
intake_columns.grid_columnconfigure(0, weight=1)
intake_columns.grid_columnconfigure(1, weight=1)
intake_columns.grid_rowconfigure(0, weight=1)

tk.Label(intake_left, text="STRANKA IN DOGOVOR", bg=PANEL, fg=DARK,
         font=("Segoe UI",11,"bold")).grid(row=0,column=0,columnspan=2,sticky="w",padx=12,pady=(10,6))
ttk.Button(
    intake_left, text="IZBERI / DODAJ STRANKO",
    command=choose_intake_customer,
).grid(row=0, column=1, sticky="e", padx=12, pady=(7, 4))
for row_i, (label_v, var_v) in enumerate([
    ("Ime in priimek", intake_customer_name),
    ("Naslov", intake_customer_address),
    ("Telefon", intake_customer_phone),
    ("E-pošta", intake_customer_email),
    ("Priložena oprema", intake_accessories),
    ("Potrjena ponudba EUR", intake_quote),
    ("Logistika EUR", intake_logistics),
    ("Predvideni rok", intake_due),
], start=1):
    tk.Label(intake_left, text=label_v, bg=PANEL, fg=GRAY,
             font=("Segoe UI",9,"bold"), width=22, anchor="w").grid(
                 row=row_i,column=0,sticky="w",padx=(12,6),pady=4)
    ttk.Entry(intake_left, textvariable=var_v).grid(
        row=row_i,column=1,sticky="ew",padx=(0,12),pady=4)
intake_left.grid_columnconfigure(1, weight=1)

tk.Label(intake_left, text="Način sklenitve", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold"), width=22, anchor="w").grid(
             row=9,column=0,sticky="w",padx=(12,6),pady=4)
ttk.Combobox(
    intake_left, textvariable=intake_context,
    values=list(CONTRACT_CONTEXT_LABELS.values()), state="readonly"
).grid(row=9,column=1,sticky="ew",padx=(0,12),pady=4)

tk.Label(intake_left, text="Izročitev dokumenta", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold"), width=22, anchor="w").grid(
             row=10,column=0,sticky="w",padx=(12,6),pady=4)
ttk.Combobox(
    intake_left, textvariable=intake_delivery,
    values=list(DELIVERY_LABELS.values()), state="readonly"
).grid(row=10,column=1,sticky="ew",padx=(0,12),pady=4)

ttk.Checkbutton(
    intake_left, text="Izrecen začetek pred iztekom odstopnega roka",
    variable=intake_immediate_start
).grid(row=11,column=0,columnspan=2,sticky="w",padx=12,pady=(8,3))
ttk.Checkbutton(
    intake_left, text="Seznanitev s prenehanjem pravice po popolni izvedbi",
    variable=intake_withdrawal_ack
).grid(row=12,column=0,columnspan=2,sticky="w",padx=12,pady=(0,10))

tk.Label(intake_right, text="ZAHTEVA IN VHODNO STANJE", bg=PANEL, fg=DARK,
         font=("Segoe UI",11,"bold")).pack(anchor="w",padx=12,pady=(10,6))
tk.Label(intake_right, text="Kaj stranka naroča / opis težave", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).pack(anchor="w",padx=12,pady=(2,3))
intake_request_text = tk.Text(intake_right, height=7, wrap="word", font=("Segoe UI",10), bd=1, relief="solid")
intake_request_text.pack(fill="x",padx=12,pady=(0,7))
tk.Label(intake_right, text="Vidno stanje in poškodbe ob prevzemu", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).pack(anchor="w",padx=12,pady=(2,3))
intake_condition_text = tk.Text(intake_right, height=6, wrap="word", font=("Segoe UI",10), bd=1, relief="solid")
intake_condition_text.pack(fill="x",padx=12,pady=(0,7))
tk.Label(intake_right, text="Dodatne opombe", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).pack(anchor="w",padx=12,pady=(2,3))
intake_notes_text = tk.Text(intake_right, height=4, wrap="word", font=("Segoe UI",10), bd=1, relief="solid")
intake_notes_text.pack(fill="both",expand=True,padx=12,pady=(0,10))

intake_actions = tk.Frame(intake_page, bg=BG)
intake_actions.pack(fill="x",pady=(10,0))
ttk.Button(
    intake_actions, text="USTVARI NOV SPREJEM IN PDF",
    command=save_intake_from_tab, style="Blue.TButton"
).pack(side="left")
ttk.Button(
    intake_actions, text="ODPRI / NADALJUJ ODPRTI SERVIS",
    command=start_new_service,
).pack(side="left", padx=(8,0))
ttk.Button(
    intake_actions, text="ODPRI ZADNJI PDF", command=open_last_intake_pdf
).pack(side="left",padx=(8,0))
ttk.Button(
    intake_actions, text="80-mm QR OZNAKA", command=open_last_intake_tag
).pack(side="left",padx=(8,0))

# Odkup rabljenih naprav - samodejni identifikatorji iz MicroDETECT
buyback_page = tk.Frame(tab_buyback_top, bg=BG)
buyback_page.pack(fill="both", expand=True, padx=18, pady=12)
buyback_head = tk.Frame(buyback_page, bg=BG)
buyback_head.pack(fill="x", pady=(0,8))
tk.Label(buyback_head, text="Odkup rabljene naprave", bg=BG, fg=DARK,
         font=("Segoe UI",17,"bold")).pack(side="left")
tk.Label(buyback_head, textvariable=buyback_status_text, bg=BG, fg=GRAY,
         font=("Segoe UI",9,"bold")).pack(side="right")
tk.Label(
    buyback_page, textvariable=buyback_device_summary,
    bg="#EAF5EE", fg=SUCCESS, font=("Segoe UI",10,"bold"),
    anchor="w", padx=12, pady=9
).pack(fill="x",pady=(0,8))

buyback_nb = ttk.Notebook(buyback_page)
buyback_nb.pack(fill="both", expand=True)
buyback_seller_tab = tk.Frame(buyback_nb, bg=PANEL)
buyback_review_tab = tk.Frame(buyback_nb, bg=PANEL)
buyback_decision_tab = tk.Frame(buyback_nb, bg=PANEL)
buyback_nb.add(buyback_seller_tab, text="1. Prodajalec")
buyback_nb.add(buyback_review_tab, text="2. Naprava in preverjanje")
buyback_nb.add(buyback_decision_tab, text="3. Odločitev in pogodba")

tk.Label(buyback_seller_tab, text="Osebnega dokumenta ne kopiramo; istovetnost preverimo samo z vpogledom.",
         bg=PANEL, fg=GRAY, font=("Segoe UI",9)).grid(row=0,column=0,columnspan=2,sticky="w",padx=16,pady=(14,8))
for row_i, (label_v, var_v) in enumerate([
    ("Ime in priimek", buyback_seller_name),
    ("Davčna številka prodajalca", buyback_tax_number),
    ("Naslov", buyback_address),
    ("Telefon / e-pošta", buyback_contact),
    ("IBAN (obvezen pri bančnem nakazilu)", buyback_iban),
], start=1):
    tk.Label(buyback_seller_tab, text=label_v, bg=PANEL, fg=GRAY,
             font=("Segoe UI",9,"bold"), width=24, anchor="w").grid(
                 row=row_i,column=0,sticky="w",padx=(16,8),pady=7)
    ttk.Entry(buyback_seller_tab, textvariable=var_v, width=60).grid(
        row=row_i,column=1,sticky="ew",padx=(0,16),pady=7)
buyback_seller_tab.grid_columnconfigure(1, weight=1)
ttk.Checkbutton(
    buyback_seller_tab,
    text="Istovetnost in polnoletnost preverjeni z vpogledom v veljaven osebni dokument",
    variable=buyback_identity_checked
).grid(row=6,column=0,columnspan=2,sticky="w",padx=16,pady=(12,6))

review_left = tk.Frame(buyback_review_tab, bg=PANEL)
review_right = tk.Frame(buyback_review_tab, bg=PANEL)
review_left.grid(row=0,column=0,sticky="nsew",padx=(12,6),pady=12)
review_right.grid(row=0,column=1,sticky="nsew",padx=(6,12),pady=12)
buyback_review_tab.grid_columnconfigure(0, weight=1)
buyback_review_tab.grid_columnconfigure(1, weight=1)
buyback_review_tab.grid_rowconfigure(0, weight=1)
tk.Label(review_left, text="Pomnilnik (MicroDETECT)", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).grid(row=0,column=0,sticky="w",pady=(3,2))
tk.Label(review_left, textvariable=storage, bg="#F7F9FA", fg=DARK,
         font=("Segoe UI",10,"bold"), anchor="w", padx=8, pady=5).grid(
             row=1,column=0,sticky="ew",pady=(0,7))
for row_i, (label_v, var_v) in enumerate([
    ("Barva (ročni podatek)", buyback_color_capacity),
    ("Priložena oprema", buyback_accessories),
    ("Podrobnosti pridobitve", buyback_acquisition_details),
], start=1):
    tk.Label(review_left, text=label_v, bg=PANEL, fg=GRAY,
             font=("Segoe UI",9,"bold")).grid(row=row_i*2,column=0,sticky="w",pady=(3,2))
    ttk.Entry(review_left, textvariable=var_v).grid(row=row_i*2+1,column=0,sticky="ew",pady=(0,7))
tk.Label(review_left, text="Podporno dokazilo", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).grid(row=8,column=0,sticky="w",pady=(3,2))
ttk.Combobox(
    review_left, textvariable=buyback_evidence,
    values=list(EVIDENCE_LABELS.values()), state="readonly"
).grid(row=9,column=0,sticky="ew",pady=(0,7))
tk.Label(review_left, text="Način pridobitve", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).grid(row=10,column=0,sticky="w",pady=(3,2))
ttk.Combobox(
    review_left, textvariable=buyback_acquisition_method,
    values=list(ACQUISITION_LABELS.values()),
    state="readonly"
).grid(row=11,column=0,sticky="ew",pady=(0,7))
review_left.grid_columnconfigure(0, weight=1)

tk.Label(review_left, text="Konkreten opis fizičnega in funkcionalnega stanja", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).grid(row=12,column=0,sticky="w",pady=(5,2))
buyback_condition_text = tk.Text(review_left, height=7, wrap="word", font=("Segoe UI",10), bd=1, relief="solid")
buyback_condition_text.grid(row=13,column=0,sticky="nsew")
review_left.grid_rowconfigure(13, weight=1)

review_right.grid_columnconfigure(0, weight=1)
tk.Label(
    review_right,
    text="Varnostne kontrole pred odkupom",
    bg=PANEL, fg=DARK, font=("Segoe UI",11,"bold")
).grid(row=0,column=0,sticky="w",pady=(3,10))
for row_i, (text_v, var_v) in enumerate([
    ("Uporabniški račun odstranjen", buyback_account_removed),
    ("Aktivacijski zaklep odstranjen", buyback_activation_removed),
    ("MDM / oddaljeno upravljanje ni prisotno", buyback_mdm_clear),
], start=1):
    ttk.Checkbutton(review_right, text=text_v, variable=var_v).grid(
        row=row_i,column=0,sticky="w",pady=5)
tk.Label(
    review_right,
    text=(
        "Zunanje komercialno preverjanje IMEI (GSMA Device Check) je iz začetnega "
        "postopka odstranjeno. MicroDETECT, uspešna aktivacija in klic ne dokazujejo "
        "lastništva; zato ostajata obvezna preverjena identiteta in podpisana izjava prodajalca."
    ),
    bg="#FFF4DF", fg=WARNING, font=("Segoe UI",9,"bold"),
    wraplength=410, justify="left", padx=10, pady=10
).grid(row=5,column=0,sticky="ew",pady=(14,0))

decision_form = tk.Frame(buyback_decision_tab, bg=PANEL)
decision_form.pack(fill="both",expand=True,padx=16,pady=14)
tk.Label(decision_form,text="Odkupna cena EUR",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=0,column=0,sticky="w",pady=6)
ttk.Entry(decision_form,textvariable=buyback_price).grid(row=0,column=1,sticky="ew",pady=6)
tk.Label(decision_form,text="Način izplačila",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=1,column=0,sticky="w",pady=6)
ttk.Combobox(
    decision_form,
    textvariable=buyback_payout,
    values=list(PAYOUT_LABELS.values()),
    state="readonly",
).grid(row=1,column=1,sticky="ew",pady=6)
decision_form.grid_columnconfigure(1,weight=1)
tk.Label(decision_form,text="Stopnja tveganja",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=2,column=0,sticky="w",pady=6)
ttk.Combobox(decision_form,textvariable=buyback_risk,
             values=list(RISK_LABELS.values()),state="readonly").grid(row=2,column=1,sticky="ew",pady=6)
tk.Label(decision_form,text="Razlog / opomba tveganja",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=3,column=0,sticky="w",pady=6)
ttk.Entry(decision_form,textvariable=buyback_risk_reason).grid(row=3,column=1,sticky="ew",pady=6)
tk.Label(decision_form,text="Druge opombe",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=4,column=0,sticky="w",pady=6)
ttk.Entry(decision_form,textvariable=buyback_notes).grid(row=4,column=1,sticky="ew",pady=6)
tk.Label(decision_form,text="Predvideni namen",bg=PANEL,fg=GRAY,
         font=("Segoe UI",9,"bold"),width=24,anchor="w").grid(row=5,column=0,sticky="w",pady=6)
ttk.Combobox(
    decision_form,
    textvariable=buyback_purpose,
    values=list(BUYBACK_PURPOSE_LABELS.values()),
    state="readonly",
).grid(row=5,column=1,sticky="ew",pady=6)
ttk.Checkbutton(
    decision_form,
    text="Prodajalec podpisuje izjavo, da je lastnik in sme z napravo razpolagati",
    variable=buyback_ownership
).grid(row=6,column=0,columnspan=2,sticky="w",pady=(14,5))
ttk.Checkbutton(
    decision_form,
    text="Prodajalec je pred podpisom prejel obvestilo o obdelavi osebnih podatkov",
    variable=buyback_privacy
).grid(row=7,column=0,columnspan=2,sticky="w",pady=5)
tk.Label(
    decision_form,
    text=(
        "Priporočen način je bančno nakazilo. Gotovina je dovoljena samo do vključno "
        "420,00 EUR; enega odkupa ni dovoljeno razdeliti. Pri gotovini program samodejno "
        "pripravi povezan blagajniški izdatek za podpis. Zaloga nastane šele po potrditvi "
        "podpisa in dejanskega izplačila."
    ),
    bg="#F7F9FA",fg=GRAY,font=("Segoe UI",9),wraplength=760,justify="left",padx=10,pady=9
).grid(row=8,column=0,columnspan=2,sticky="ew",pady=(12,0))

buyback_actions = tk.Frame(buyback_page,bg=BG)
buyback_actions.pack(fill="x",pady=(10,0))
buyback_save_button = ttk.Button(
    buyback_actions,
    text="PREVERI, SHRANI IN USTVARI POGODBO",
    command=save_buyback_from_tab,
    style="Blue.TButton",
)
buyback_save_button.pack(side="left")
buyback_complete_button = ttk.Button(
    buyback_actions,
    text="POTRDI PODPIS IN IZPLAČILO",
    command=complete_prepared_buyback,
    state="disabled",
)
buyback_complete_button.pack(side="left", padx=(8,0))
ttk.Button(buyback_actions,text="ODPRI ZADNJI PDF",
           command=open_last_buyback_pdf).pack(side="left",padx=(8,0))
buyback_cancel_button = ttk.Button(
    buyback_actions,
    text="PREKLIČI OSNUTEK",
    command=cancel_prepared_buyback,
    state="disabled",
)
buyback_cancel_button.pack(side="left", padx=(8,0))
ttk.Button(
    buyback_actions,
    text="NAZAJ V SERVIS / ZAVRNI PONUDBO",
    command=cancel_service_buyback_offer_ui,
).pack(side="left", padx=(8,0))
ttk.Button(buyback_actions,text="NOV ODKUP / POČISTI",
           command=clear_buyback_form_safely).pack(side="right")

# Zaloga delov
inventory_toolbar = tk.Frame(tab_inventory_top, bg=PANEL)
inventory_toolbar.pack(fill="x", padx=10, pady=(10,6))

inventory_name_search_var = tk.StringVar()
inventory_barcode_search_var = tk.StringVar()
inventory_count_var = tk.StringVar(value="0 artiklov")

tk.Label(
    inventory_toolbar, text="Naziv",
    bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="left")

inventory_name_search_entry = ttk.Entry(
    inventory_toolbar, textvariable=inventory_name_search_var, width=28
)
inventory_name_search_entry.pack(side="left", padx=(6,12))

tk.Label(
    inventory_toolbar, text="Barkoda",
    bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="left")

inventory_barcode_search_entry = ttk.Entry(
    inventory_toolbar, textvariable=inventory_barcode_search_var, width=24
)
inventory_barcode_search_entry.pack(side="left", padx=(6,12))

tk.Label(
    inventory_toolbar,
    text="Iskanje se filtrira sproti",
    bg=PANEL, fg=GRAY, font=("Segoe UI",8)
).pack(side="left", padx=(0,12))

ttk.Button(
    inventory_toolbar, text="NOV ARTIKEL",
    command=inventory_add, style="Blue.TButton"
).pack(side="left", padx=(4,4))

ttk.Button(
    inventory_toolbar, text="UREDI",
    command=inventory_edit
).pack(side="left", padx=4)

ttk.Button(
    inventory_toolbar, text="DEAKTIVIRAJ",
    command=inventory_delete
).pack(side="left", padx=4)

ttk.Button(
    inventory_toolbar,
    text="OSVEŽI ZALOGO",
    command=refresh_stock_from_database
).pack(side="left", padx=(10,4))

tk.Label(
    inventory_toolbar, textvariable=inventory_count_var,
    bg=PANEL, fg=GRAY, font=("Segoe UI",9)
).pack(side="right")

inventory_table_frame = tk.Frame(
    tab_inventory_top, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
inventory_table_frame.pack(fill="both", expand=True, padx=10, pady=(0,10))

inventory_cols = ("id","barcode","name","code","stock","price","lot")
inventory_tree = ttk.Treeview(
    inventory_table_frame,
    columns=inventory_cols,
    show="headings",
    height=7
)
inventory_tree.heading("id", text="ID")
inventory_tree.heading("barcode", text="Barkoda")
inventory_tree.heading("name", text="Artikel")
inventory_tree.heading("code", text="Interna koda")
inventory_tree.heading("stock", text="Zaloga")
inventory_tree.heading("price", text="Cena")
inventory_tree.heading("lot", text="LOT / serijska")

inventory_tree.column("id", width=0, minwidth=0, stretch=False)
inventory_tree.column("barcode", width=180)
inventory_tree.column("name", width=270)
inventory_tree.column("code", width=150)
inventory_tree.column("stock", width=80, anchor="center")
inventory_tree.column("price", width=100, anchor="e")
inventory_tree.column("lot", width=200)

inventory_scroll = ttk.Scrollbar(
    inventory_table_frame,
    orient="vertical",
    command=inventory_tree.yview
)
inventory_tree.configure(yscrollcommand=inventory_scroll.set)
inventory_tree.pack(side="left", fill="both", expand=True)
inventory_scroll.pack(side="right", fill="y")

inventory_tree.bind("<Double-1>", inventory_edit)
inventory_name_search_var.trace_add("write", refresh_inventory_table)
inventory_barcode_search_var.trace_add("write", refresh_inventory_table)

# Cenik in dobavitelj – dobaviteljska cena je vedno ločena od lokalne zaloge.
supplier_page = tk.Frame(tab_supplier_top, bg=BG)
supplier_page.pack(fill="both", expand=True, padx=12, pady=12)

supplier_head = tk.Frame(supplier_page, bg=BG)
supplier_head.pack(fill="x", pady=(0, 8))
tk.Label(
    supplier_head,
    text="DOBAVITELJSKE CENE IN IZRAČUN PONUDBE",
    bg=BG, fg=DARK, font=("Segoe UI", 15, "bold")
).pack(side="left")
supplier_count_var = tk.StringVar(value="0 delov")
tk.Label(
    supplier_head, textvariable=supplier_count_var,
    bg=BG, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="right")

supplier_toolbar = tk.Frame(supplier_page, bg=PANEL)
supplier_toolbar.pack(fill="x", pady=(0, 8))
supplier_search_var = tk.StringVar()
tk.Label(
    supplier_toolbar, text="Model ali del",
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="left", padx=(10, 6), pady=9)
supplier_search_entry = ttk.Entry(
    supplier_toolbar, textvariable=supplier_search_var, width=30
)
supplier_search_entry.pack(side="left", padx=(0, 10), pady=7)
ttk.Button(
    supplier_toolbar,
    text="OSVEŽI FIXSHOP ZDAJ",
    command=lambda: start_supplier_refresh(True),
    style="Blue.TButton",
).pack(side="left", padx=4, pady=5)
ttk.Button(
    supplier_toolbar,
    text="DODAJ V LOKALNO BAZO (KOLIČINA 0)",
    command=supplier_add_to_local_inventory,
).pack(side="left", padx=4, pady=5)
ttk.Button(
    supplier_toolbar,
    text="IZVOZI ZA SPLET",
    command=supplier_export_site_prices,
).pack(side="left", padx=4, pady=5)

supplier_status_var = tk.StringVar(
    value=(
        "Dobaviteljska zaloga ni lokalna zaloga. Fiksna cena vključuje organizirani "
        "prevzem in vračilo; namenski prevzem je ločena ponudba."
    )
)
tk.Label(
    supplier_page,
    textvariable=supplier_status_var,
    bg="#FFF4D6", fg=WARNING,
    font=("Segoe UI", 9, "bold"),
    anchor="w", justify="left", padx=10, pady=7,
).pack(fill="x", pady=(0, 8))

supplier_table_frame = tk.Frame(
    supplier_page, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
)
supplier_table_frame.pack(fill="both", expand=True)
supplier_cols = (
    "id", "model", "repair", "quality", "supplier_price", "pickup",
    "onsite", "local", "supplier", "target", "checked",
)
supplier_tree = ttk.Treeview(
    supplier_table_frame, columns=supplier_cols, show="headings", height=18
)
supplier_headings = {
    "id": "ID",
    "model": "Model",
    "repair": "Poseg",
    "quality": "Kakovost",
    "supplier_price": "FixShop cena",
    "pickup": "Organiz. prevzem + vračilo",
    "onsite": "Servis na domu",
    "local": "Lokalna zaloga",
    "supplier": "Dobavitelj",
    "target": "Plan vozilo+doma",
    "checked": "Cena preverjena",
}
for column, label in supplier_headings.items():
    supplier_tree.heading(column, text=label)
supplier_tree.column("id", width=0, minwidth=0, stretch=False)
supplier_tree.column("model", width=125)
supplier_tree.column("repair", width=100)
supplier_tree.column("quality", width=140)
supplier_tree.column("supplier_price", width=100, anchor="e")
supplier_tree.column("pickup", width=115, anchor="e")
supplier_tree.column("onsite", width=110, anchor="e")
supplier_tree.column("local", width=170)
supplier_tree.column("supplier", width=205)
supplier_tree.column("target", width=125, anchor="center")
supplier_tree.column("checked", width=130)
supplier_tree.tag_configure("quote", foreground=WARNING)
supplier_tree.tag_configure("no_local", background="#FFF7F7")

supplier_scroll_y = ttk.Scrollbar(
    supplier_table_frame, orient="vertical", command=supplier_tree.yview
)
supplier_scroll_x = ttk.Scrollbar(
    supplier_table_frame, orient="horizontal", command=supplier_tree.xview
)
supplier_tree.configure(
    yscrollcommand=supplier_scroll_y.set,
    xscrollcommand=supplier_scroll_x.set,
)
supplier_tree.grid(row=0, column=0, sticky="nsew")
supplier_scroll_y.grid(row=0, column=1, sticky="ns")
supplier_scroll_x.grid(row=1, column=0, sticky="ew")
supplier_table_frame.grid_rowconfigure(0, weight=1)
supplier_table_frame.grid_columnconfigure(0, weight=1)
supplier_search_var.trace_add("write", refresh_supplier_catalog_table)

# Reklamacije
claim_device_summary = tk.StringVar(value="Ni povezane naprave.")
claim_device_detail = tk.StringVar(value="Priklopi iPhone in ga zaznaj v zavihku Naprava.")
claim_service_count = tk.StringVar(value="")
claim_history_count = tk.StringVar(value="Reklamacije: 0")
claim_selected_service_id = tk.StringVar(value="")
claim_selected_claim_id = tk.StringVar(value="")
claim_form_title = tk.StringVar(value="NOVA REKLAMACIJA")

claim_service_choice = tk.StringVar()
claim_service_map = {}

claim_service_no = tk.StringVar(value="")
claim_service_date = tk.StringVar(value="")
claim_invoice = tk.StringVar(value="")
claim_warranty = tk.StringVar(value="")
claim_customer = tk.StringVar(value="")
claim_address = tk.StringVar(value="")
claim_phone = tk.StringVar(value="")
claim_original_work = tk.StringVar(value="")
claim_original_parts = tk.StringVar(value="")
claim_status = tk.StringVar(value="ODPRTA")

claim_header = tk.Frame(tab_claims_top, bg=PANEL)
claim_header.pack(fill="x", padx=10, pady=(9,4))

tk.Label(
    claim_header, textvariable=claim_device_summary,
    bg=PANEL, fg=DARK, font=("Segoe UI", 12, "bold")
).pack(side="left")
tk.Label(
    claim_header, textvariable=claim_history_count,
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9)
).pack(side="right")
tk.Label(
    tab_claims_top, textvariable=claim_device_detail,
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9)
).pack(anchor="w", padx=10, pady=(0,6))

claim_service_bar = tk.Frame(
    tab_claims_top, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
claim_service_bar.pack(fill="x", padx=10, pady=(0,8))

tk.Label(
    claim_service_bar, text="Povezan servis",
    bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="left", padx=(10,6), pady=8)

claim_service_combo = ttk.Combobox(
    claim_service_bar,
    textvariable=claim_service_choice,
    state="readonly",
    width=35
)
claim_service_combo.pack(side="left", pady=8)
claim_service_combo.bind("<<ComboboxSelected>>", on_claim_service_combo)

tk.Label(
    claim_service_bar, textvariable=claim_service_count,
    bg=PANEL, fg=GRAY, font=("Segoe UI",8)
).pack(side="right", padx=10)

claim_body = tk.Frame(tab_claims_top, bg=PANEL)
claim_body.pack(fill="both", expand=True, padx=10, pady=(0,8))
claim_body.grid_columnconfigure(0, weight=1, uniform="claim")
claim_body.grid_columnconfigure(1, weight=1, uniform="claim")
claim_body.grid_rowconfigure(0, weight=1)

# Levo: samo zgodovina reklamacij
claim_left = tk.Frame(
    claim_body, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
claim_left.grid(row=0, column=0, sticky="nsew", padx=(0,5))

claim_history_head = tk.Frame(claim_left, bg=PANEL)
claim_history_head.pack(fill="x", padx=10, pady=(8,5))
tk.Label(
    claim_history_head, text="Zgodovina reklamacij naprave",
    bg=PANEL, fg=DARK, font=("Segoe UI", 10, "bold")
).pack(side="left")

claim_history_frame = tk.Frame(claim_left, bg=PANEL)
claim_history_frame.pack(fill="both", expand=True, padx=8, pady=(0,8))

claim_history_tree = ttk.Treeview(
    claim_history_frame,
    columns=("id","service_id","claim","date","service","status","description","result"),
    show="headings",
    height=8
)
claim_history_tree.heading("id", text="ID")
claim_history_tree.heading("service_id", text="Service ID")
claim_history_tree.heading("claim", text="Reklamacija")
claim_history_tree.heading("date", text="Datum")
claim_history_tree.heading("service", text="Servis")
claim_history_tree.heading("status", text="Status")
claim_history_tree.heading("description", text="Opis težave")
claim_history_tree.heading("result", text="Ugotovitev / rešitev")

claim_history_tree.column("id", width=0, minwidth=0, stretch=False)
claim_history_tree.column("service_id", width=0, minwidth=0, stretch=False)
claim_history_tree.column("claim", width=145)
claim_history_tree.column("date", width=120)
claim_history_tree.column("service", width=105)
claim_history_tree.column("status", width=105)
claim_history_tree.column("description", width=230)
claim_history_tree.column("result", width=230)

claim_history_vsb = ttk.Scrollbar(
    claim_history_frame, orient="vertical", command=claim_history_tree.yview
)
claim_history_hsb = ttk.Scrollbar(
    claim_history_frame, orient="horizontal", command=claim_history_tree.xview
)
claim_history_tree.configure(
    yscrollcommand=claim_history_vsb.set,
    xscrollcommand=claim_history_hsb.set
)
claim_history_tree.grid(row=0, column=0, sticky="nsew")
claim_history_vsb.grid(row=0, column=1, sticky="ns")
claim_history_hsb.grid(row=1, column=0, sticky="ew")
claim_history_frame.grid_rowconfigure(0, weight=1)
claim_history_frame.grid_columnconfigure(0, weight=1)
claim_history_tree.bind("<<TreeviewSelect>>", open_claim_history_service)

claim_history_actions = tk.Frame(claim_left, bg=PANEL)
claim_history_actions.pack(fill="x", padx=8, pady=(0,8))
ttk.Button(
    claim_history_actions,
    text="NATISNI OZNAČENO",
    command=print_selected_claim
).pack(side="left")

# Desno: podatki povezanega originalnega servisa
claim_right = tk.Frame(
    claim_body, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
claim_right.grid(row=0, column=1, sticky="nsew", padx=(5,0))

tk.Label(
    claim_right, text="Povezani servis",
    bg=PANEL, fg=DARK, font=("Segoe UI", 10, "bold")
).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8,5))

claim_info_fields = [
    ("Servis", claim_service_no),
    ("Datum", claim_service_date),
    ("Račun", claim_invoice),
    ("Garancija", claim_warranty),
    ("Stranka", claim_customer),
    ("Naslov", claim_address),
    ("Telefon", claim_phone),
]
for rr, (lab, var) in enumerate(claim_info_fields, start=1):
    tk.Label(
        claim_right, text=lab, bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8, "bold"), width=10, anchor="w"
    ).grid(row=rr, column=0, sticky="nw", padx=(10,5), pady=2)
    tk.Label(
        claim_right, textvariable=var, bg=PANEL, fg=DARK,
        font=("Segoe UI", 8), anchor="w", justify="left",
        wraplength=350
    ).grid(row=rr, column=1, sticky="nw", padx=(0,10), pady=2)

tk.Label(
    claim_right, text="Opravljeno", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 8, "bold"), width=10, anchor="nw"
).grid(row=8, column=0, sticky="nw", padx=(10,5), pady=2)
tk.Label(
    claim_right, textvariable=claim_original_work, bg=PANEL, fg=DARK,
    font=("Segoe UI", 8), anchor="w", justify="left",
    wraplength=350
).grid(row=8, column=1, sticky="nw", padx=(0,10), pady=2)

tk.Label(
    claim_right, text="Deli", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 8, "bold"), width=10, anchor="nw"
).grid(row=9, column=0, sticky="nw", padx=(10,5), pady=(2,8))
tk.Label(
    claim_right, textvariable=claim_original_parts, bg=PANEL, fg=DARK,
    font=("Segoe UI", 8), anchor="w", justify="left",
    wraplength=350
).grid(row=9, column=1, sticky="nw", padx=(0,10), pady=(2,8))
claim_right.grid_columnconfigure(1, weight=1)

# Spodaj: reklamacija
claim_form = tk.Frame(
    tab_claims_top, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
claim_form.pack(fill="x", padx=10, pady=(0,10))
claim_form.grid_columnconfigure(1, weight=1)
claim_form.grid_columnconfigure(3, weight=1)

tk.Label(
    claim_form, textvariable=claim_form_title,
    bg=PANEL, fg=BLUE_DARK, font=("Segoe UI", 10, "bold")
).grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8,5))

def _claim_text(parent, row, col, title):
    tk.Label(
        parent, text=title, bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8, "bold")
    ).grid(row=row, column=col, sticky="nw", padx=(10,5), pady=3)
    txt = tk.Text(
        parent, height=2, font=("Segoe UI", 9),
        wrap="word", relief="solid", bd=1
    )
    txt.grid(row=row, column=col+1, sticky="ew", padx=(0,10), pady=3)
    return txt

claim_description = _claim_text(claim_form, 1, 0, "Opis težave")
claim_intake_condition = _claim_text(claim_form, 1, 2, "Stanje ob sprejemu")
claim_customer_request = _claim_text(claim_form, 2, 0, "Zahteva stranke")
claim_finding = _claim_text(claim_form, 2, 2, "Ugotovitev servisa")
claim_resolution = _claim_text(claim_form, 3, 0, "Rešitev")

tk.Label(
    claim_form, text="Status", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 8, "bold")
).grid(row=3, column=2, sticky="w", padx=(10,5), pady=3)

claim_status_combo = ttk.Combobox(
    claim_form,
    textvariable=claim_status,
    values=["ODPRTA", "V OBDELAVI", "ZAKLJUČENA", "ZAVRNJENA"],
    state="readonly",
    width=18
)
claim_status_combo.grid(row=3, column=3, sticky="w", padx=(0,10), pady=3)

claim_buttons = tk.Frame(claim_form, bg=PANEL)
claim_buttons.grid(row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=(6,8))

claim_save_button = ttk.Button(
    claim_buttons,
    text="SHRANI REKLAMACIJO",
    command=save_claim_from_tab,
    style="Blue.TButton"
)
claim_save_button.pack(side="left")

ttk.Button(
    claim_buttons,
    text="NOVA REKLAMACIJA",
    command=new_claim_for_selected_service
).pack(side="left", padx=(8,0))


def on_top_tab_changed(_event=None):
    try:
        selected = top_nb.tab(top_nb.select(), "text")
    except Exception:
        return

    if selected != "Servisni list" and service_editor_active:
        pause_active_service_timer(reason="MODULE_SWITCH", quiet=True)

    if selected == "Reklamacije" and not device_is_identified():
        top_nb.select(tab_device_top)
        messagebox.showwarning(
            "MikroFix",
            "Za reklamacije mora biti naprava najprej identificirana prek USB."
        )
        return

    if selected == "Servisni list":
        if not service_editor_active:
            show_service_history()
        else:
            refresh_service_tab_history()
    elif selected == "Zaloga delov":
        refresh_inventory_table()
    elif selected == "Cenik in dobavitelj":
        refresh_supplier_catalog_table()
    elif selected == "Reklamacije":
        refresh_claims_tab()

top_nb.bind("<<NotebookTabChanged>>", on_top_tab_changed)

# Servisni list: najprej zgodovina, nato obrazec za nov servis
service_editor_active = False
service_history_device = tk.StringVar(value="")
service_history_count = tk.StringVar(value="Servisi: 0")

service_home = tk.Frame(tab_service_top, bg=PANEL)
service_home.pack(fill="both", expand=True, padx=8, pady=8)

service_home_head = tk.Frame(service_home, bg=PANEL)
service_home_head.pack(fill="x", pady=(0,8))

tk.Label(
    service_home_head,
    text="Servisna čakalna vrsta in zgodovina",
    bg=PANEL, fg=DARK, font=("Segoe UI",15,"bold")
).pack(side="left")
tk.Label(
    service_home_head,
    textvariable=service_history_count,
    bg=PANEL, fg=GRAY, font=("Segoe UI",9)
).pack(side="right")

tk.Label(
    service_home,
    textvariable=service_history_device,
    bg=PANEL, fg=GRAY, font=("Segoe UI",9)
).pack(anchor="w", pady=(0,8))

service_open_orders_box = ttk.LabelFrame(
    service_home, text="Odprti sprejemi – čas se začne šele z gumbom ZAČNI SERVIS"
)
service_open_orders_box.pack(fill="x", pady=(0, 10))
service_open_order_actions = tk.Frame(service_open_orders_box, bg=PANEL)
service_open_order_actions.pack(fill="x", padx=8, pady=(8, 4))
service_open_order_status = tk.StringVar(value="")
ttk.Button(
    service_open_order_actions, text="ZAČNI / NADALJUJ IZBRANI SERVIS",
    command=start_selected_service_open_order, style="Blue.TButton",
).pack(side="left")
ttk.Button(
    service_open_order_actions, text="A4 PDF / TISK",
    command=lambda: open_selected_service_intake_output("A4"),
).pack(side="left", padx=(8, 0))
ttk.Button(
    service_open_order_actions, text="80-mm OZNAKA / TISK",
    command=lambda: open_selected_service_intake_output("TAG"),
).pack(side="left", padx=(8, 0))
ttk.Button(
    service_open_order_actions, text="POSODOBI",
    command=refresh_service_open_orders,
).pack(side="left", padx=(8, 0))
tk.Label(
    service_open_order_actions, textvariable=service_open_order_status,
    bg=PANEL, fg=GRAY,
).pack(side="right")

service_open_order_tree = ttk.Treeview(
    service_open_orders_box,
    columns=("id", "order", "created", "customer", "device", "serial", "started", "request"),
    show="headings", selectmode="browse", height=7,
)
for col, label, width in (
    ("id", "ID", 0), ("order", "Nalog", 125),
    ("created", "Sprejem", 140), ("customer", "Stranka", 165),
    ("device", "Naprava", 150), ("serial", "S/N", 105),
    ("started", "Začetek dela", 145), ("request", "Zahteva", 300),
):
    service_open_order_tree.heading(col, text=label)
    if col == "id":
        service_open_order_tree.column(col, width=0, minwidth=0, stretch=False)
    else:
        service_open_order_tree.column(col, width=width, minwidth=75)
service_open_order_tree.pack(fill="x", padx=8, pady=(0, 8))
service_open_order_tree.bind("<Double-1>", start_selected_service_open_order)

service_history_frame = tk.Frame(
    service_home, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
service_history_frame.pack(fill="both", expand=True)

service_history_tree = ttk.Treeview(
    service_history_frame,
    columns=("id","service","date","customer","invoice","warranty","custody","work"),
    show="headings"
)
for col, text, width in [
    ("id","ID",0),
    ("service","Servis",120),
    ("date","Datum",135),
    ("customer","Stranka",180),
    ("invoice","Račun",110),
    ("warranty","Garancija",130),
    ("custody","Naprava / hramba",255),
    ("work","Opravljeno",430),
]:
    service_history_tree.heading(col, text=text)
    if col == "id":
        service_history_tree.column(col, width=0, minwidth=0, stretch=False)
    else:
        service_history_tree.column(col, width=width, minwidth=80)

service_hist_vsb = ttk.Scrollbar(
    service_history_frame, orient="vertical",
    command=service_history_tree.yview
)
service_hist_hsb = ttk.Scrollbar(
    service_history_frame, orient="horizontal",
    command=service_history_tree.xview
)
service_history_tree.configure(
    yscrollcommand=service_hist_vsb.set,
    xscrollcommand=service_hist_hsb.set
)
service_history_tree.grid(row=0, column=0, sticky="nsew")
service_hist_vsb.grid(row=0, column=1, sticky="ns")
service_hist_hsb.grid(row=1, column=0, sticky="ew")
service_history_frame.grid_rowconfigure(0, weight=1)
service_history_frame.grid_columnconfigure(0, weight=1)
service_history_tree.bind("<Double-1>", open_service_from_history)

service_home_actions = tk.Frame(service_home, bg=PANEL)
service_home_actions.pack(fill="x", pady=(10,0))

ttk.Button(
    service_home_actions,
    text="ISKANJE / QR KODA ODPRTEGA SPREJEMA",
    command=start_new_service,
    style="Blue.TButton"
).pack(side="left")

ttk.Button(
    service_home_actions,
    text="NOVO SERVISNO NAROČILO",
    command=lambda: top_nb.select(tab_intake_top)
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="ODPRI IZBRANI SERVIS",
    command=open_service_from_history
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="REKLAMACIJA ZA IZBRANI SERVIS",
    command=open_claim_for_service_from_history
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="NATISNI OZNAČENO",
    command=print_selected_service
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="ODPRI BLAGAJNO ZA IZBRANI SERVIS",
    command=lambda: prepare_invoice_for_service(selected_service_history_id()),
    style="Blue.TButton",
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="ODPRI BLAGAJNO ZA ZADNJI SERVIS",
    command=lambda: prepare_invoice_for_service(last_saved_service_id),
).pack(side="left", padx=(8,0))

service_custody_actions = tk.Frame(service_home, bg=PANEL)
service_custody_actions.pack(fill="x", pady=(6, 0))
ttk.Button(
    service_custody_actions,
    text="POTRDI PREVZEM NAPRAVE",
    command=mark_selected_service_picked_up,
).pack(side="left")

ttk.Button(
    service_custody_actions,
    text="EVIDENTIRAJ KONČNI POZIV",
    command=record_selected_final_notice,
).pack(side="left", padx=(8,0))

ttk.Button(
    service_home_actions,
    text="PREGLED VSEH SERVISOV",
    command=lambda: open_service_browser("all")
).pack(side="right")

# Urejevalnik novega servisnega lista je skrit, dokler ga ne ustvariš.
service_editor = tk.Frame(tab_service_top, bg=PANEL)

# Pred osebnimi podatki je obvezna ustrezna identifikacija naprave.
service_identity_bar = tk.Frame(
    service_editor, bg="#F7F9FA",
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
service_identity_bar.pack(fill="x", padx=8, pady=(8,0))

tk.Label(
    service_identity_bar,
    text="Identifikacija naprave:",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",9,"bold")
).pack(side="left", padx=(12,6), pady=8)

tk.Label(
    service_identity_bar,
    textvariable=connected_state,
    bg="#F7F9FA", fg=DARK,
    font=("Segoe UI",9,"bold")
).pack(side="left", pady=8)

tk.Label(
    service_identity_bar,
    text="  •  MicroDETECT ali fizični prepis druge naprave",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",9)
).pack(side="left", padx=(4,10), pady=8)

service_detect_button = ttk.Button(
    service_identity_bar,
    text="MicroDETECT",
    command=detect,
    style="Blue.TButton"
)
service_detect_button.pack(side="right", padx=10, pady=6)

service_clock_var = tk.StringVar(value="--:--:--")
service_timer_started_var = tk.StringVar(value="—")
service_timer_elapsed_var = tk.StringVar(value="00:00:00")
service_timer_state_var = tk.StringVar(value="ČAS JE USTAVLJEN")
service_timer_bar = tk.Frame(
    service_editor, bg="#EAF5EE",
    highlightthickness=1, highlightbackground="#B7D8C2",
)
service_timer_bar.pack(fill="x", padx=8, pady=(7, 0))
for label_text, variable, width in (
    ("URA", service_clock_var, 12),
    ("PRVI ZAČETEK", service_timer_started_var, 23),
    ("DEJANSKI ČAS", service_timer_elapsed_var, 15),
    ("STANJE", service_timer_state_var, 20),
):
    box = tk.Frame(service_timer_bar, bg="#EAF5EE")
    box.pack(side="left", padx=(12, 8), pady=7)
    tk.Label(
        box, text=label_text, bg="#EAF5EE", fg=GRAY,
        font=("Segoe UI", 7, "bold"), anchor="w",
    ).pack(anchor="w")
    tk.Label(
        box, textvariable=variable, bg="#EAF5EE", fg=DARK,
        font=("Segoe UI", 12, "bold"), width=width, anchor="w",
    ).pack(anchor="w")
service_timer_toggle_button = ttk.Button(
    service_timer_bar, text="USTAVI ČAS", command=pause_active_service_timer,
    style="Blue.TButton",
)
service_timer_toggle_button.pack(side="right", padx=12, pady=12)

service_actions = tk.Frame(service_editor, bg=PANEL)
service_actions.pack(side="bottom", fill="x", padx=10, pady=10)

service_save_button = ttk.Button(
    service_actions,
    text="ZAKLJUČI SERVIS IN PRIPRAVI RAČUN",
    command=save,
    style="Blue.TButton"
)
service_save_button.pack(side="left")

ttk.Button(
    service_actions,
    text="NAZAJ NA ZGODOVINO",
    command=show_service_history
).pack(side="left", padx=(8,0))

ttk.Button(
    service_actions,
    text="NOV / POČISTI",
    command=lambda: clear_form(keep_device=True)
).pack(side="left", padx=(8,0))

ttk.Button(
    service_actions,
    text="PONUDBA ZA ODKUP",
    command=start_service_buyback_offer,
).pack(side="left", padx=(8,0))

ttk.Button(
    service_actions,
    text="ODPRI BLAGAJNO ZA ZADNJI SERVIS",
    command=prepare_invoice_for_service,
).pack(side="right")

service_frame = ttk.LabelFrame(
    service_editor, text="NOV SERVISNI LIST", padding=10
)
service_frame.pack(fill="both", expand=True, padx=8, pady=(8,0))

service_nb = ttk.Notebook(service_frame)
service_nb.pack(fill="both", expand=True)

# Tab: osnovno
tab_main = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_main, text="Servis")

for r, (lab, var) in enumerate([
    ("Stranka", customer),
    ("Naslov", customer_address),
    ("Telefon", customer_phone),
    ("Številka računa", invoice_no),
]):
    tk.Label(tab_main, text=lab, bg=PANEL, fg=GRAY,
             font=("Segoe UI",9,"bold"), width=22, anchor="w").grid(
                 row=r, column=0, sticky="w", padx=(12,8), pady=5)
    ttk.Entry(tab_main, textvariable=var, width=55).grid(
        row=r, column=1, sticky="ew", padx=(0,12), pady=5)

tk.Label(tab_main, text="Kratek opis opravljenega dela",
         bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold"),
         width=22, anchor="nw").grid(row=4, column=0, sticky="nw", padx=(12,8), pady=6)

work = tk.Text(tab_main, height=8, font=("Segoe UI",10), wrap="word",
               bg="white", fg=DARK, relief="solid", bd=1)
work.grid(row=4, column=1, sticky="nsew", padx=(0,12), pady=6)
tab_main.grid_columnconfigure(1, weight=1)
tab_main.grid_rowconfigure(4, weight=1)

# Odobritev je potrebna samo, kadar pripravljene postavke presežejo znesek
# prvotne ponudbe na servisnem naročilu.
tab_approval = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_approval, text="Odobritev dodatnega dela")
service_approved_limit_var = tk.StringVar()
service_approval_channel_var = tk.StringVar()

approval_card = tk.Frame(
    tab_approval, bg=PANEL, highlightthickness=1, highlightbackground=LIGHT_GRAY
)
approval_card.pack(fill="x", padx=16, pady=16)
tk.Label(
    approval_card,
    text="Izpolni samo, če končni deli in delo presegajo prvotno ponudbo.",
    bg=PANEL, fg=DARK, font=("Segoe UI", 10, "bold"),
).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 10))
tk.Label(
    approval_card, text="Odobren znesek [EUR]", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9, "bold"),
).grid(row=1, column=0, sticky="w", padx=14, pady=7)
ttk.Entry(
    approval_card, textvariable=service_approved_limit_var, width=20
).grid(row=1, column=1, sticky="w", padx=14, pady=7)
tk.Label(
    approval_card, text="Kanal dokazila", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9, "bold"),
).grid(row=2, column=0, sticky="w", padx=14, pady=7)
ttk.Combobox(
    approval_card, textvariable=service_approval_channel_var,
    values=["PODPIS", "E-POŠTA", "SMS"], state="readonly", width=18,
).grid(row=2, column=1, sticky="w", padx=14, pady=7)
ttk.Button(
    approval_card, text="PRIPNI DOKAZILO", command=add_approval_evidence,
    style="Blue.TButton",
).grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 14))
tk.Label(
    tab_approval,
    text=(
        "Dovoljena je fotografija podpisane odobritve ali posnetek prejetega "
        "e-poštnega/SMS sporočila. Dokazilo se arhivira skupaj s servisom."
    ),
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9), justify="left", wraplength=900,
).pack(anchor="w", padx=18)

# Tab: ročno vnesene storitvene postavke. Program izračuna samo vsoto.
tab_service_lines = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_service_lines, text="Storitvene postavke")

service_line_kind_var = tk.StringVar(value=SERVICE_LINE_KIND_LABELS["T1"])
service_line_description_var = tk.StringVar()
service_line_quantity_var = tk.StringVar(value="0")
service_line_price_var = tk.StringVar(
    value=f"{float(service_tariff_snapshot('T1', 0, RUNTIME.settings)['price']):.2f}"
)
service_lines_total_var = tk.StringVar(value="Skupaj storitve: 0,00 €")

service_line_form = tk.Frame(
    tab_service_lines, bg="#F7F9FA",
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
)
service_line_form.pack(fill="x", padx=12, pady=(12, 8))

for column, (label_v, width_v) in enumerate((
    ("Tarifa", 20), ("Opis", 46), ("Dodatnih 30 min", 14), ("Izračunana cena", 14),
)):
    tk.Label(
        service_line_form, text=label_v, bg="#F7F9FA", fg=GRAY,
        font=("Segoe UI", 8, "bold"),
    ).grid(row=0, column=column, sticky="w", padx=6, pady=(8, 2))

service_line_kind_combo = ttk.Combobox(
    service_line_form, textvariable=service_line_kind_var,
    values=list(SERVICE_LINE_KIND_LABELS.values()), state="readonly", width=20,
)
service_line_kind_combo.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 8))
service_line_description_entry = ttk.Entry(
    service_line_form, textvariable=service_line_description_var, width=46,
)
service_line_description_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(0, 8))
service_line_quantity_entry = ttk.Entry(
    service_line_form, textvariable=service_line_quantity_var, width=10,
)
service_line_quantity_entry.grid(row=1, column=2, sticky="ew", padx=6, pady=(0, 8))
service_line_price_entry = ttk.Entry(
    service_line_form, textvariable=service_line_price_var, width=14,
    state="readonly",
)
service_line_price_entry.grid(row=1, column=3, sticky="ew", padx=6, pady=(0, 8))
ttk.Button(
    service_line_form, text="DODAJ POSTAVKO",
    command=add_manual_service_line, style="Blue.TButton",
).grid(row=1, column=4, padx=8, pady=(0, 8))
service_line_form.grid_columnconfigure(1, weight=1)

service_lines_tree = ttk.Treeview(
    tab_service_lines,
    columns=(
        "kind", "description", "quantity", "unit_price", "total",
        "tariff_version", "billable_minutes", "first_rate", "extra_rate",
    ),
    show="headings", height=9,
)
for column, label_v, width_v in (
    ("kind", "Vrsta", 150),
    ("description", "Opis", 430),
    ("quantity", "Količina", 90),
    ("unit_price", "Cena / enoto", 110),
    ("total", "Skupaj", 110),
):
    service_lines_tree.heading(column, text=label_v)
    service_lines_tree.column(column, width=width_v, anchor="e" if column in {"quantity", "unit_price", "total"} else "w")
for hidden_column in ("tariff_version", "billable_minutes", "first_rate", "extra_rate"):
    service_lines_tree.heading(hidden_column, text=hidden_column)
    service_lines_tree.column(hidden_column, width=0, minwidth=0, stretch=False)
service_lines_tree.pack(fill="both", expand=True, padx=12, pady=(0, 6))

service_line_actions = tk.Frame(tab_service_lines, bg=PANEL)
service_line_actions.pack(fill="x", padx=12, pady=(0, 10))
ttk.Button(
    service_line_actions, text="ODSTRANI IZBRANO",
    command=remove_manual_service_line,
).pack(side="left")
tk.Label(
    service_line_actions,
    text=f"Brez samodejnih postavk • minimum L0 {LABOR_FLOOR_EUR:.2f} € / enoto",
    bg=PANEL, fg=GRAY, font=("Segoe UI", 8),
).pack(side="left", padx=12)
tk.Label(
    service_line_actions, textvariable=service_lines_total_var,
    bg=PANEL, fg=DARK, font=("Segoe UI", 10, "bold"),
).pack(side="right")

service_line_description_entry.bind(
    "<Return>", lambda _event: (add_manual_service_line(), "break")[1]
)
service_line_kind_combo.bind("<<ComboboxSelected>>", refresh_service_tariff_preview)
service_line_quantity_var.trace_add("write", refresh_service_tariff_preview)

# Tab: deli
tab_parts = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_parts, text="Vgrajeni deli")
service_nb.hide(tab_service_lines)

# Iskanje po istem principu kot v Zalogi delov.
service_search_panel = tk.Frame(
    tab_parts, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
service_search_panel.pack(fill="x", padx=12, pady=(12,8))

service_article_name_var = tk.StringVar()
service_article_barcode_var = tk.StringVar()
service_article_result_count = tk.StringVar(value="0 zadetkov")

search_toolbar = tk.Frame(service_search_panel, bg=PANEL)
search_toolbar.pack(fill="x", padx=12, pady=(10,7))

tk.Label(
    search_toolbar, text="Naziv",
    bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="left")

service_article_name_entry = ttk.Entry(
    search_toolbar,
    textvariable=service_article_name_var,
    width=28
)
service_article_name_entry.pack(side="left", padx=(6,14))

tk.Label(
    search_toolbar, text="Barkoda",
    bg=PANEL, fg=GRAY, font=("Segoe UI",9,"bold")
).pack(side="left")

service_article_barcode_entry = ttk.Entry(
    search_toolbar,
    textvariable=service_article_barcode_var,
    width=26
)
service_article_barcode_entry.pack(side="left", padx=(6,14))
service_article_barcode_entry.bind("<Return>", service_barcode_enter)

tk.Label(
    search_toolbar,
    text="Iskanje se filtrira sproti",
    bg=PANEL, fg=GRAY, font=("Segoe UI",8)
).pack(side="left")

tk.Label(
    search_toolbar,
    textvariable=service_article_result_count,
    bg=PANEL, fg=GRAY, font=("Segoe UI",8)
).pack(side="right")

service_search_results = tk.Frame(service_search_panel, bg=PANEL)
service_search_results.pack(fill="x", padx=12, pady=(0,8))

service_article_search_tree = ttk.Treeview(
    service_search_results,
    columns=("id","barcode","name","code","stock","price","lot"),
    show="headings",
    height=5
)
for col, text, width in [
    ("id", "ID", 0),
    ("barcode", "Barkoda", 175),
    ("name", "Artikel", 260),
    ("code", "Interna koda", 145),
    ("stock", "Zaloga", 70),
    ("price", "Cena", 90),
    ("lot", "LOT / serijska", 180),
]:
    service_article_search_tree.heading(col, text=text)
    if col == "id":
        service_article_search_tree.column(
            col, width=0, minwidth=0, stretch=False
        )
    else:
        service_article_search_tree.column(
            col, width=width, minwidth=60
        )

service_search_scroll = ttk.Scrollbar(
    service_search_results,
    orient="vertical",
    command=service_article_search_tree.yview
)
service_article_search_tree.configure(
    yscrollcommand=service_search_scroll.set
)
service_article_search_tree.pack(
    side="left", fill="both", expand=True
)
service_search_scroll.pack(side="right", fill="y")

service_article_search_tree.bind(
    "<Double-1>", add_selected_service_article
)

service_search_actions = tk.Frame(service_search_panel, bg=PANEL)
service_search_actions.pack(fill="x", padx=12, pady=(0,10))

ttk.Button(
    service_search_actions,
    text="DODAJ IZBRANI",
    command=add_selected_service_article,
    style="Blue.TButton"
).pack(side="left")

tk.Label(
    service_search_actions,
    text="Namig: skeniraj celo barkodo in pritisni Enter za takojšnje dodajanje.",
    bg=PANEL, fg=GRAY, font=("Segoe UI",8)
).pack(side="left", padx=(12,0))

# Trenutno vgrajeni deli.
tk.Label(
    tab_parts,
    text="Vgrajeni deli v tem servisu",
    bg=PANEL, fg=DARK,
    font=("Segoe UI",10,"bold")
).pack(anchor="w", padx=12, pady=(4,2))

parts_tree = ttk.Treeview(
    tab_parts,
    columns=("article_id","part","code","barcode","lot","price"),
    show="headings",
    height=6
)
parts_tree.heading("article_id", text="ID")
parts_tree.heading("part", text="Artikel")
parts_tree.heading("code", text="Interna koda")
parts_tree.heading("barcode", text="Barkoda")
parts_tree.heading("lot", text="LOT / serijska")
parts_tree.heading("price", text="Cena")
parts_tree.column("article_id", width=0, minwidth=0, stretch=False)
parts_tree.column("part", width=280)
parts_tree.column("code", width=150)
parts_tree.column("barcode", width=210)
parts_tree.column("lot", width=190)
parts_tree.column("price", width=90, anchor="e")
parts_tree.pack(fill="both", expand=True, padx=12, pady=6)

part_actions = tk.Frame(tab_parts, bg=PANEL)
part_actions.pack(fill="x", padx=12, pady=(0,10))
ttk.Button(
    part_actions,
    text="ODSTRANI IZBRANO",
    command=remove_selected_part
).pack(side="right")

service_article_name_var.trace_add(
    "write", search_service_articles
)
service_article_barcode_var.trace_add(
    "write", search_service_articles
)

# Test tabs
post_test_vars = {}
tab_pre = tk.Frame(service_nb, bg=PANEL)
tab_post = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_pre, text="Test PRED")
service_nb.add(tab_post, text="Test PO")
make_test_tab(tab_pre, pre_test_vars)
make_test_tab(tab_post, post_test_vars)

# Photos tab
tab_photos = tk.Frame(service_nb, bg=PANEL)
service_nb.add(tab_photos, text="Fotografije")


def set_service_test_tabs_for_device(is_iphone):
    """Testa PRED/PO sta del standardiziranega postopka samo za iPhone."""
    if is_iphone:
        visible = set(service_nb.tabs())
        if str(tab_pre) not in visible:
            service_nb.insert(tab_photos, tab_pre, text="Test PRED")
        visible = set(service_nb.tabs())
        if str(tab_post) not in visible:
            service_nb.insert(tab_photos, tab_post, text="Test PO")
        return

    for variables in (pre_test_vars, post_test_vars):
        for variable in variables.values():
            variable.set("N/A")
    if service_nb.select() in {str(tab_pre), str(tab_post)}:
        service_nb.select(0)
    service_nb.hide(tab_pre)
    service_nb.hide(tab_post)
photo_top = tk.Frame(tab_photos, bg=PANEL)
photo_top.pack(fill="x", padx=12, pady=(12,6))
photo_stage = tk.StringVar(value="PRED")
tk.Label(photo_top, text="Faza", bg=PANEL, fg=GRAY,
         font=("Segoe UI",9,"bold")).pack(side="left")
ttk.Combobox(photo_top, textvariable=photo_stage, values=["PRED","PO"],
             state="readonly", width=10).pack(side="left", padx=8)
ttk.Button(photo_top, text="DODAJ FOTOGRAFIJE",
           command=add_photos, style="Blue.TButton").pack(side="left")
photo_count = tk.StringVar(value="0 fotografij")
tk.Label(photo_top, textvariable=photo_count, bg=PANEL, fg=GRAY,
         font=("Segoe UI",9)).pack(side="right")

photos_list = tk.Listbox(tab_photos, font=("Segoe UI",10), height=10,
                         bg="white", fg=DARK, selectmode="extended")
photos_list.pack(fill="both", expand=True, padx=12, pady=6)
ttk.Button(tab_photos, text="ODSTRANI IZBRANO",
           command=remove_selected_photo).pack(anchor="e", padx=12, pady=(0,10))

refresh_stock_from_database(show_message=False)
refresh_supplier_catalog_table()
refresh_service_tab_history()
refresh_claims_tab()
update_workflow_lock()

if RUNTIME.settings["supplier"]["fixshop"].get("auto_refresh", True):
    root.after(1200, lambda: start_supplier_refresh(False))

init_auth_db()
protect_file_permissions(DB)
protect_file_permissions(AUTH_DB)
protect_file_permissions(INVENTORY_DB)

if os.environ.get("MIKROFIX_SMOKE_TEST") == "1":
    def _layout_bottom_ok(dialog):
        root.update_idletasks()
        buttons = []
        pending = list(dialog.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, ttk.Button):
                buttons.append(widget)
        dialog_top = dialog.winfo_rooty()
        dialog_bottom = dialog_top + dialog.winfo_height()
        return bool(buttons) and all(
            button.winfo_ismapped()
            and button.winfo_rooty() + button.winfo_height() <= dialog_bottom
            for button in buttons
        )

    AUTH_SESSION.current["permissions"] = {
        PERMISSION_SERVICE,
        PERMISSION_CASHIER,
        PERMISSION_OFFICE,
        PERMISSION_BUYBACK,
    }
    show_start_menu()
    root.update()
    if main_menu_frame is None or not _layout_bottom_ok(main_menu_frame):
        raise RuntimeError("Spodnji gumbi začetnega menija niso vidni.")
    main_menu_frame.pack_forget()
    outer.pack(fill="both", expand=True, padx=22, pady=16)

    if not launch_office("--open-tab", "receipts"):
        raise RuntimeError("Office se ni odprl v isti prijavljeni seji.")
    root.update()
    if main_menu_frame is not None and main_menu_frame.winfo_exists():
        main_menu_frame.pack_forget()
    outer.pack(fill="both", expand=True, padx=22, pady=16)

    identify_other_device()
    root.update_idletasks()
    device_dialog = next(
        child for child in root.winfo_children() if isinstance(child, tk.Toplevel)
    )
    if not _layout_bottom_ok(device_dialog):
        raise RuntimeError("Spodnji gumbi obrazca Druga naprava niso vidni.")
    device_dialog.destroy()
    root.update_idletasks()
    root.destroy()
elif run_pin_login(root):
    root.deiconify()
    try:
        root.state("zoomed")
    except Exception:
        pass
    root.after(180, show_start_menu)
    root.mainloop()
else:
    root.destroy()
