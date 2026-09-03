import os
import sys
import json
import math
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from datetime import date, datetime
from mikrofix_tk_bootstrap import prepare_tcl_tk

prepare_tcl_tk()

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from xml.sax.saxutils import escape as xml_escape

from mikrofix_auth import (
    AuthStore,
    PERMISSION_FURS_PRODUCTION,
    PERMISSION_MANAGE_INVENTORY,
    PERMISSION_MANAGE_PRICES,
    PERMISSION_MANAGE_USERS,
)
from mikrofix_user_ui import SessionAuthUI
from mikrofix_runtime import APP_VERSION, load_runtime
from mikrofix_invoice_bridge import load_service_invoice_seed
from mikrofix_v29_core import (
    V29Error,
    initialize_v29,
    trace_article,
    validate_document_line_price,
)
from mikrofix_v30_receipts_ui import build_receipts_tab
from mikrofix_v30_pos_ui import open_pos
from mikrofix_v30_pos import ensure_pos_schema, generate_pos_pdfs, register_copy
from mikrofix_v29_controls_ui import (
    build_price_profiles_tab,
    build_stock_control_tab,
    build_used_devices_tab,
)
from mikrofix_receipt import generate_test_receipt
from mikrofix_weekly_report import (
    FORMULA_VERSION as WEEKLY_FORMULA_VERSION,
    WeeklyReportError,
    ensure_inputs as ensure_weekly_inputs,
    evaluate_inputs as evaluate_weekly_inputs,
    generate_pdf as generate_weekly_pdf,
    latest_report as latest_weekly_report,
    load_inputs as load_weekly_inputs,
    refresh_public_prices as refresh_weekly_public_prices,
    report_for_week as weekly_report_for_week,
    week_bounds as weekly_week_bounds,
)
from mikrofix_furs import (
    FursTestClient,
    FursTestError,
    FursTestStore,
    FURS_TEST_BASE,
    FURS_TECHNICAL_VERSION,
    build_one_euro_test_invoice,
    build_test_business_premise,
    load_test_certificate,
    readiness_report,
)

APP_DIR = Path(__file__).resolve().parent
RUNTIME = load_runtime(APP_DIR)
OFFICE_DB = RUNTIME.db_path("office")
AUTH_DB = RUNTIME.db_path("auth")
INVENTORY_DB = RUNTIME.db_path("inventory")
SERVICE_DB = RUNTIME.db_path("service")
PDF_DIR = RUNTIME.office_data_dir / "pdf"
PDF_DIR.mkdir(parents=True, exist_ok=True)
WEEKLY_INPUT_DIR = RUNTIME.office_data_dir / "weekly_report"
WEEKLY_INPUT_PATH = WEEKLY_INPUT_DIR / "weekly_price_inputs.json"
WEEKLY_REPORT_DIR = RUNTIME.office_data_dir / "weekly_reports"
WEEKLY_SEED_PATH = APP_DIR / "mikrofix_weekly_prices_seed.json"
WEEKLY_SETTINGS = RUNTIME.settings["weekly_report"]

COMPANY_NAME = "MikroFIX"
COMPANY_ADDRESS = "Paradiž 73, 2282 Cirkulane"
COMPANY_PHONE = "+386 40 353 402"
COMPANY_EMAIL = "mikrofixsi@gmail.com"
COMPANY_WEB = "mikrofix.si"

TAX_MODE = RUNTIME.tax_mode
DEFAULT_VAT_RATE = RUNTIME.default_vat_rate
TAX_INVOICE_NOTE = RUNTIME.tax_invoice_note
FURS_ENABLED = bool(RUNTIME.settings["furs"].get("enabled", False))
TEST_NOTICE = (
    f"TEST • FURS PRODUKCIJA IZKLOPLJENA • "
    f"{RUNTIME.active_premise}/{RUNTIME.electronic_device}"
)
PRICING_REQUIRES_REVIEW = not bool(
    RUNTIME.settings["pricing"].get("legacy_flat_margin_enabled", False)
)

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

DOC_TYPES = [
    "DOBAVNICA",
    "PREJETI RAČUN",
    "IZDANI RAČUN",
    "PREDRAČUN",
    "REKLAMACIJA DOBAVITELJU",
]
NEW_DOC_TYPES = [
    "PREDRAČUN",
    "REKLAMACIJA DOBAVITELJU",
]
PARTNER_TYPES = ["DOBAVITELJ", "KUPEC", "OBOJE"]
DOC_PREFIX = {
    "DOBAVNICA": "DBV",
    "PREJETI RAČUN": "PRA",
    "IZDANI RAČUN": "IRA",
    "PREDRAČUN": "PDR",
    "REKLAMACIJA DOBAVITELJU": "RDO",
}

LABOR_FLOOR_EUR = float(RUNTIME.settings.get("pricing", {}).get("l0_eur", 22.0))


def requested_service_id():
    try:
        idx = sys.argv.index("--service-id")
        return int(sys.argv[idx + 1])
    except (ValueError, IndexError, TypeError):
        return None


def requested_new_invoice():
    return "--new-invoice" in sys.argv


def requested_open_tab():
    try:
        idx = sys.argv.index("--open-tab")
        return str(sys.argv[idx + 1]).strip().lower()
    except (ValueError, IndexError):
        return ""



def protect_file_permissions(path):
    """Na Windows omeji datoteko na trenutnega uporabnika + SYSTEM."""
    try:
        p = Path(path)
        if os.environ.get("MIKROFIX_SKIP_FILE_PROTECTION") == "1":
            return
        if os.name != "nt" or not p.exists():
            return
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            return
        subprocess.run(
            [
                "icacls", str(p),
                "/inheritance:r",
                "/grant:r", f"{user}:(F)",
                "SYSTEM:(F)"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
    except Exception:
        # ACL je dodatna zaščita; napaka ne sme ustaviti servisa.
        pass


_AUTH_STORE = None
_FURS_TEST_STORE = None


def auth_store():
    global _AUTH_STORE
    if _AUTH_STORE is None:
        _AUTH_STORE = AuthStore(AUTH_DB, protect_file_permissions)
    return _AUTH_STORE


def furs_test_store():
    global _FURS_TEST_STORE
    if _FURS_TEST_STORE is None:
        _FURS_TEST_STORE = FursTestStore(OFFICE_DB)
        _FURS_TEST_STORE.init()
    return _FURS_TEST_STORE


AUTH_SESSION = SessionAuthUI(
    auth_store,
    version_label=RUNTIME.version_label,
    login_context="MikroFIX Office",
    bg=BG,
    panel=PANEL,
    blue=BLUE,
    gray=GRAY,
    dark=DARK,
    light_gray=LIGHT_GRAY,
    danger=DANGER,
)
OFFICE_LAUNCHED_FROM_MAIN = False
OFFICE_EXIT_CODE = 0


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


def run_pin_login(parent):
    return AUTH_SESSION.login(parent)


def consume_main_session_handoff():
    """Prevzame enkratno sejo glavnega programa brez ponovnega vnosa PIN-a."""
    global OFFICE_LAUNCHED_FROM_MAIN
    token = os.environ.pop("MIKROFIX_SESSION_HANDOFF", "").strip()
    if not token:
        return False, False
    store = auth_store()
    store.init()
    user = store.consume_session_handoff(token, "OFFICE")
    if not user:
        return True, False
    AUTH_SESSION.adopt_authenticated_user(user)
    OFFICE_LAUNCHED_FROM_MAIN = True
    return True, True


def change_pin_dialog(parent):
    AUTH_SESSION.change_pin(parent)


def manage_users_dialog(parent):
    AUTH_SESSION.manage_users(parent)


def logout_and_lock():
    global OFFICE_EXIT_CODE
    audit_event("LOGOUT", "user", AUTH_SESSION.username)
    if OFFICE_LAUNCHED_FROM_MAIN:
        # Glavni program prejme posebno izhodno kodo in tam zahteva novo prijavo.
        OFFICE_EXIT_CODE = 20
        root.destroy()
        return
    root.withdraw()
    if run_pin_login(root):
        root.deiconify()
        try:
            root.state("zoomed")
        except Exception:
            pass
    else:
        root.destroy()


def office_connect():
    return RUNTIME.connect_sqlite("office", foreign_keys=True)


def inventory_connect():
    return RUNTIME.connect_sqlite("inventory", foreign_keys=True)


def service_connect():
    if not SERVICE_DB.exists():
        return None
    return RUNTIME.connect_sqlite("service", foreign_keys=True, read_only=True)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_text():
    return datetime.now().strftime("%Y-%m-%d")


def money(v):
    try:
        return f"{float(v or 0):.2f} €"
    except Exception:
        return "0.00 €"


def safe_float(v, default=0.0):
    try:
        return float(str(v or "").strip().replace(",", "."))
    except Exception:
        return default


def safe_int(v, default=0):
    try:
        return int(float(str(v or "").strip().replace(",", ".")))
    except Exception:
        return default


def ceil_to_5(value):
    value = max(0.0, safe_float(value))
    if value == 0:
        return 0.0
    return math.ceil((value - 1e-9) / 5.0) * 5.0


def pdf_safe(value):
    return xml_escape(str(value or "-")).replace("\n", "<br/>")


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
    c.commit()
    c.close()


def init_office_db():
    c = office_connect()

    c.execute("""
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
        )
    """)

    c.execute("""
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
        )
    """)

    c.execute("""
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
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS document_links(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_document_id INTEGER NOT NULL,
            to_document_id INTEGER NOT NULL,
            link_type TEXT,
            FOREIGN KEY(from_document_id) REFERENCES documents(id),
            FOREIGN KEY(to_document_id) REFERENCES documents(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS supplier_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            service_claim_id INTEGER,
            service_claim_no TEXT,
            supplier_resolution TEXT,
            status TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_partner_name ON partners(name COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_doc_date ON documents(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(doc_type)")
    columns = {row[1] for row in c.execute("PRAGMA table_info(documents)")}
    if "linked_service_id" not in columns:
        c.execute("ALTER TABLE documents ADD COLUMN linked_service_id INTEGER")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_service ON documents(linked_service_id)"
    )
    c.commit()
    c.close()
    protect_file_permissions(OFFICE_DB)


def next_document_no(doc_type):
    year = datetime.now().year
    prefix = DOC_PREFIX.get(doc_type, "DOC")
    c = office_connect()
    row = c.execute("""
        SELECT COUNT(*) FROM documents
        WHERE doc_type=? AND substr(date,1,4)=?
    """, (doc_type, str(year))).fetchone()
    c.close()
    n = int(row[0] or 0) + 1
    return f"{prefix}-{year}-{n:04d}"


def style_app():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        "TButton", font=("Segoe UI", 10), padding=(10, 7),
        background=PANEL, foreground=DARK
    )
    style.map("TButton", background=[("active", "#E7EDF2")])
    style.configure(
        "Blue.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8),
        background=BLUE, foreground="white",
        bordercolor=BLUE, lightcolor=BLUE, darkcolor=BLUE
    )
    style.map(
        "Blue.TButton",
        background=[("active", BLUE_DARK)],
        foreground=[("active", "white")]
    )
    style.configure(
        "Treeview", rowheight=27, font=("Segoe UI", 9),
        background="white", fieldbackground="white", foreground=DARK
    )
    style.configure(
        "Treeview.Heading", font=("Segoe UI", 9, "bold"),
        background="#E8EDF1", foreground=DARK
    )
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure(
        "TNotebook.Tab", font=("Segoe UI", 9, "bold"), padding=(14, 8)
    )


# ----------------------------------------------------------------------
# PARTNERJI
# ----------------------------------------------------------------------

partner_map = {}


def refresh_partner_combo():
    global partner_map
    partner_map = {}
    c = office_connect()
    rows = c.execute("""
        SELECT id, name, pcd_days
        FROM partners
        ORDER BY name COLLATE NOCASE
    """).fetchall()
    c.close()

    displays = []
    for pid, name_v, pcd_v in rows:
        display = f"{name_v}  |  PČD {float(pcd_v or 0):.1f} dni"
        displays.append(display)
        partner_map[display] = pid

    combo = globals().get("doc_partner_combo")
    if combo is not None:
        try:
            if combo.winfo_exists():
                combo["values"] = displays
        except Exception:
            pass


def _ensure_service_customer_partner(name, address, phone, email):
    """Vrni kupca za račun; ob prvem računu ga ustvari iz servisnega naloga."""
    name = str(name or "").strip()
    address = str(address or "").strip()
    if not name:
        return None
    c = office_connect()
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
        c.close()
        return int(row[0])
    now = now_text()
    cur = c.execute(
        """
        INSERT INTO partners(
            name, address, post_city, country, tax_no, registration_no, iban,
            phone, email, contact_person, partner_type, pcd_days, notes,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            name, address, "", "Slovenija", "", "", "",
            str(phone or "").strip(), str(email or "").strip(), "",
            "KUPEC", 0.0, "Ustvarjeno iz servisnega naročila.", now, now,
        ),
    )
    c.commit()
    partner_id = int(cur.lastrowid)
    c.close()
    return partner_id


def _invoice_item(name, code, unit_price, *, quantity=1, article_id=None, barcode=""):
    price = max(0.0, float(unit_price or 0))
    quantity = max(0.0, float(quantity or 0))
    return (
        article_id, barcode, name, code, quantity, price, 0.0, 0.0,
        0.0, price, 0.0, price, price, price, quantity * price, 0.0,
        quantity * price,
    )


def open_invoice_from_service_request(service_id):
    """Odpri obstoječ račun ali nov predizpolnjen osnutek iz servisa."""
    if not service_id:
        return
    c = office_connect()
    existing = c.execute(
        """
        SELECT id FROM documents
        WHERE linked_service_id=? AND doc_type='IZDANI RAČUN'
        ORDER BY id DESC LIMIT 1
        """,
        (int(service_id),),
    ).fetchone()
    c.close()
    if existing:
        load_document(int(existing[0]))
        return

    s = service_connect()
    try:
        service = load_service_invoice_seed(
            s, int(service_id), labor_floor_eur=LABOR_FLOOR_EUR
        )
    except Exception as exc:
        s.close()
        messagebox.showerror("MikroFIX Office", "Servisnega zapisa ni mogoče najti.")
        return
    s.close()

    partner_id = _ensure_service_customer_partner(
        service.customer,
        service.customer_address,
        service.customer_phone,
        service.customer_email,
    )
    refresh_partner_combo()
    partner_display = partner_display_for_id(partner_id, service.customer, 0)
    items = []
    if service.manual_lines:
        for line_kind, description, quantity, price in service.manual_lines:
            items.append(
                _invoice_item(
                    description,
                    str(line_kind or "STORITEV"),
                    price,
                    quantity=quantity,
                )
            )
    elif not service.manual_pricing and service.labor_price > 0:
        # Združljivost s servisnimi listi, ustvarjenimi pred v29.
        work_name = service.work.strip() or "Servisna storitev / delo"
        work_name = " ".join(work_name.split())[:180]
        items.append(
            _invoice_item(
                work_name,
                "STORITEV",
                max(LABOR_FLOOR_EUR, service.labor_price),
            )
        )
    for article_id, barcode, part_name, part_code, price in service.parts:
        items.append(
            _invoice_item(
                part_name or "Vgrajeni del",
                part_code or "DEL",
                price,
                article_id=article_id,
                barcode=barcode or "",
            )
        )

    context = {
        "doc_type": "IZDANI RAČUN",
        "partner_display": partner_display,
        "external_no": service.service_no,
        "date": today_text(),
        "order_date": "",
        "received_date": "",
        "due_date": "",
        "currency": "EUR",
        "delivery_cost": 0.0,
        "stock_effect": False,
        "claim_display": "",
        "notes": (
            f"Račun je pripravljen iz servisa {service.service_no}. "
            f"Naprava: {service.model}, oznaka {service.serial}."
        ),
        "linked_service_id": int(service_id),
    }
    open_document_items_window(context=context, initial_items=items)


def refresh_partners(*_args):
    if "partner_tree" not in globals():
        return

    q = partner_search_var.get().strip()
    for iid in partner_tree.get_children():
        partner_tree.delete(iid)

    sql = """
        SELECT id, name, partner_type, pcd_days, phone, email, tax_no
        FROM partners
    """
    params = []
    if q:
        sql += """
            WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? OR tax_no LIKE ?
        """
        p = f"%{q}%"
        params = [p, p, p, p]

    sql += " ORDER BY name COLLATE NOCASE"

    c = office_connect()
    rows = c.execute(sql, params).fetchall()
    c.close()

    for row in rows:
        partner_tree.insert(
            "", "end",
            values=(
                row[0], row[1], row[2],
                f"{float(row[3] or 0):.1f}",
                row[4] or "-", row[5] or "-", row[6] or "-"
            )
        )
    partner_count_var.set(f"{len(rows)} partnerjev")
    refresh_partner_combo()


def get_selected_partner_id():
    sel = partner_tree.selection()
    if not sel:
        return None
    vals = partner_tree.item(sel[0], "values")
    if not vals:
        return None
    try:
        return int(vals[0])
    except Exception:
        return None


def open_partner_editor(partner_id=None, on_saved_callback=None):
    existing = None
    if partner_id:
        c = office_connect()
        existing = c.execute("""
            SELECT id, name, address, post_city, country, tax_no,
                   registration_no, iban, phone, email, contact_person,
                   partner_type, pcd_days, notes
            FROM partners WHERE id=?
        """, (partner_id,)).fetchone()
        c.close()

    win = tk.Toplevel(root)
    win.title("MikroFIX Office - Partner")
    win.geometry("720x690")
    win.configure(bg=BG)
    win.transient(root)
    win.grab_set()

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=20, pady=18)

    tk.Label(
        outer,
        text="Uredi partnerja" if existing else "Nov partner",
        bg=BG, fg=DARK, font=("Segoe UI", 17, "bold")
    ).pack(anchor="w", pady=(0, 10))

    card = tk.Frame(
        outer, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True)

    names = [
        "Naziv", "Naslov", "Pošta / kraj", "Država", "Davčna številka",
        "Matična številka", "IBAN", "Telefon", "E-mail",
        "Kontaktna oseba", "Tip partnerja", "PČD [dni]"
    ]

    defaults = [
        existing[1] if existing else "",
        existing[2] if existing else "",
        existing[3] if existing else "",
        existing[4] if existing else "Slovenija",
        existing[5] if existing else "",
        existing[6] if existing else "",
        existing[7] if existing else "",
        existing[8] if existing else "",
        existing[9] if existing else "",
        existing[10] if existing else "",
        existing[11] if existing else "DOBAVITELJ",
        str(existing[12] if existing else "0"),
    ]
    vars_v = [tk.StringVar(value=v or "") for v in defaults]

    for i, (lab, var) in enumerate(zip(names, vars_v)):
        tk.Label(
            card, text=lab, bg=PANEL, fg=GRAY,
            font=("Segoe UI", 9, "bold"), width=20, anchor="w"
        ).grid(row=i, column=0, sticky="w", padx=(14, 8), pady=6)

        if lab == "Tip partnerja":
            w = ttk.Combobox(
                card, textvariable=var,
                values=PARTNER_TYPES, state="readonly", width=38
            )
        else:
            w = ttk.Entry(card, textvariable=var, width=42)
        w.grid(row=i, column=1, sticky="ew", padx=(0, 14), pady=6)

    tk.Label(
        card, text="Opomba", bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9, "bold"), width=20, anchor="nw"
    ).grid(row=len(names), column=0, sticky="nw", padx=(14, 8), pady=6)

    notes = tk.Text(card, height=5, wrap="word", font=("Segoe UI", 9))
    notes.grid(
        row=len(names), column=1, sticky="nsew",
        padx=(0, 14), pady=6
    )
    if existing and existing[13]:
        notes.insert("1.0", existing[13])

    card.grid_columnconfigure(1, weight=1)
    card.grid_rowconfigure(len(names), weight=1)

    def save_partner():
        name_v = vars_v[0].get().strip()
        if not name_v:
            messagebox.showwarning(
                "MikroFIX Office", "Vpiši naziv partnerja.", parent=win
            )
            return

        pcd_v = safe_float(vars_v[11].get(), -1)
        if pcd_v < 0:
            messagebox.showwarning(
                "MikroFIX Office",
                "PČD mora biti število 0 ali več.",
                parent=win
            )
            return

        vals = [
            vars_v[0].get().strip(),
            vars_v[1].get().strip(),
            vars_v[2].get().strip(),
            vars_v[3].get().strip(),
            vars_v[4].get().strip(),
            vars_v[5].get().strip(),
            vars_v[6].get().strip(),
            vars_v[7].get().strip(),
            vars_v[8].get().strip(),
            vars_v[9].get().strip(),
            vars_v[10].get().strip(),
            pcd_v,
            notes.get("1.0", "end").strip(),
        ]

        c = office_connect()
        now = now_text()
        if existing:
            saved_partner_id = existing[0]
            partner_event = "PARTNER_UPDATED"
            c.execute("""
                UPDATE partners
                SET name=?, address=?, post_city=?, country=?, tax_no=?,
                    registration_no=?, iban=?, phone=?, email=?,
                    contact_person=?, partner_type=?, pcd_days=?, notes=?,
                    updated_at=?
                WHERE id=?
            """, (*vals, now, existing[0]))
        else:
            partner_event = "PARTNER_CREATED"
            cur = c.execute("""
                INSERT INTO partners(
                    name, address, post_city, country, tax_no,
                    registration_no, iban, phone, email, contact_person,
                    partner_type, pcd_days, notes, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (*vals, now, now))
            saved_partner_id = cur.lastrowid
        c.commit()
        c.close()
        audit_event(
            partner_event,
            "partner",
            saved_partner_id,
            {"name": name_v},
        )
        refresh_partners()
        if on_saved_callback:
            try:
                on_saved_callback()
            except Exception:
                pass
        win.destroy()

    buttons = tk.Frame(outer, bg=BG)
    buttons.pack(fill="x", pady=(10, 0))
    ttk.Button(
        buttons, text="SHRANI",
        command=save_partner, style="Blue.TButton"
    ).pack(side="left")
    ttk.Button(
        buttons, text="PREKLIČI", command=win.destroy
    ).pack(side="left", padx=(8, 0))


def partner_add():
    open_partner_editor()


def partner_edit(_event=None):
    pid = get_selected_partner_id()
    if pid:
        open_partner_editor(pid)


def partner_delete():
    pid = get_selected_partner_id()
    if not pid:
        return
    c = office_connect()
    used = c.execute(
        "SELECT COUNT(*) FROM documents WHERE partner_id=?",
        (pid,)
    ).fetchone()[0]
    if used:
        c.close()
        messagebox.showwarning(
            "MikroFIX Office",
            "Partner je že uporabljen na dokumentih in ga ni mogoče izbrisati."
        )
        return

    row = c.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()
    deleted = False
    if messagebox.askyesno(
        "MikroFIX Office",
        f"Izbrišem partnerja {row[0] if row else pid}?"
    ):
        c.execute("DELETE FROM partners WHERE id=?", (pid,))
        c.commit()
        deleted = True
    c.close()
    if deleted:
        audit_event(
            "PARTNER_DELETED",
            "partner",
            pid,
            {"name": row[0] if row else ""},
        )
    refresh_partners()


# ----------------------------------------------------------------------
# ARTIKLI
# ----------------------------------------------------------------------

def refresh_office_articles(*_args):
    if "article_tree" not in globals():
        return

    name_q = article_name_search_var.get().strip()
    barcode_q = article_barcode_search_var.get().strip()

    for iid in article_tree.get_children():
        article_tree.delete(iid)

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

    for r in rows:
        article_tree.insert(
            "", "end",
            values=(
                r[0], r[1], r[2], r[3] or "-",
                int(r[4] or 0), money(r[5]), r[6] or "-"
            )
        )
    article_count_var.set(f"{len(rows)} artiklov")


def show_selected_article_trace():
    selected = article_tree.selection()
    if not selected:
        messagebox.showwarning("MikroFIX Office", "Izberi artikel.")
        return
    values = article_tree.item(selected[0], "values")
    try:
        trace = trace_article(INVENTORY_DB, int(values[0]))
    except Exception as exc:
        messagebox.showerror("MikroFIX Office", str(exc))
        return
    win = tk.Toplevel(root)
    win.title(f"MikroFIX Office - Sled artikla {values[2]}")
    win.geometry("1120x700")
    win.minsize(820, 560)
    win.resizable(True, True)
    page = tk.Frame(win, bg=BG)
    page.pack(fill="both", expand=True, padx=14, pady=12)
    ttk.Button(page, text="ZAPRI", command=win.destroy).pack(anchor="e", pady=(0, 6))
    item = trace["article"]
    tk.Label(
        page,
        text=(
            f"{item['article_name']}  •  {item['barcode']}  •  stanje {item['stock_qty']:g}\n"
            f"Nabavna {float(item.get('acquisition_cost') or 0):.2f} €  •  "
            f"aktivna cena {float(item.get('price') or 0):.2f} €  •  "
            f"vir {item.get('source_kind') or '-'}"
        ),
        bg=BG, fg=DARK, justify="left", font=("Segoe UI", 11, "bold"),
    ).pack(anchor="w", pady=(0, 8))
    detail_nb = ttk.Notebook(page)
    detail_nb.pack(fill="both", expand=True)

    def add_table(title, rows, columns):
        tab = tk.Frame(detail_nb, bg=PANEL)
        detail_nb.add(tab, text=title)
        tree = ttk.Treeview(tab, columns=[col[0] for col in columns], show="headings")
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width)
        for row in rows:
            tree.insert("", "end", values=[row.get(col[0], "") for col in columns])
        tree.pack(fill="both", expand=True, padx=8, pady=8)

    add_table("Premiki", trace["movements"], (
        ("movement_no", "Premik", 150), ("created_at", "Datum", 145),
        ("quantity_delta", "+ / -", 75), ("balance_after", "Stanje", 80),
        ("movement_type", "Vrsta", 155), ("source_type", "Vir", 150),
        ("reason", "Razlog", 330),
    ))
    add_table("Cene", trace["prices"], (
        ("active_from", "Velja od", 145), ("acquisition_cost", "Nabavna", 90),
        ("recommended_price", "Priporočena", 100), ("confirmed_price", "Potrjena", 100),
        ("profile_code", "Profil", 170), ("reason", "Razlog", 300),
    ))
    add_table("Donorski izvor", trace["donor_sources"], (
        ("harvest_no", "Zapisnik", 140), ("inventory_code", "Naprava", 140),
        ("buyback_no", "Odkup", 140), ("allocated_cost", "Dodeljen strošek", 120),
        ("condition_grade", "Stanje", 90), ("test_result", "Test", 90),
    ))


def refresh_all_shared_data(show_message=False):
    refresh_office_articles()
    refresh_doc_article_search()
    refresh_service_readonly()
    refresh_claim_combo()
    if show_message:
        messagebox.showinfo(
            "MikroFIX Office",
            "Skupna zaloga in servisni zapisi so ponovno prebrani."
        )


# ----------------------------------------------------------------------
# SERVISI / REKLAMACIJE - samo branje
# ----------------------------------------------------------------------

service_claim_map = {}


def refresh_service_readonly(*_args):
    if "service_tree" not in globals():
        return

    for iid in service_tree.get_children():
        service_tree.delete(iid)

    if not SERVICE_DB.exists():
        service_read_status.set("mikrofix.db ni najden.")
        return

    q = service_search_var.get().strip()
    c = service_connect()
    try:
        sql = """
            SELECT id, service_no, date, customer, model, invoice_no,
                   serial, imei, work
            FROM service
        """
        params = []
        if q:
            p = f"%{q}%"
            sql += """
                WHERE service_no LIKE ? OR customer LIKE ? OR model LIKE ?
                   OR invoice_no LIKE ? OR serial LIKE ? OR imei LIKE ?
            """
            params = [p, p, p, p, p, p]
        sql += " ORDER BY id DESC LIMIT 500"
        rows = c.execute(sql, params).fetchall()
    except sqlite3.Error:
        rows = []
    c.close()

    for r in rows:
        service_tree.insert(
            "", "end",
            values=(
                r[0], r[1] or "-", r[2] or "-", r[3] or "-",
                r[4] or "-", r[5] or "-", (r[8] or "")[:160]
            )
        )
    service_read_status.set(f"{len(rows)} servisov")


def refresh_claim_combo():
    global service_claim_map
    service_claim_map = {}

    displays = [""]
    if SERVICE_DB.exists():
        c = service_connect()
        try:
            rows = c.execute("""
                SELECT cl.id, cl.claim_no, cl.date, cl.status,
                       s.service_no, s.customer
                FROM claims cl
                JOIN service s ON s.id=cl.service_id
                ORDER BY cl.id DESC
                LIMIT 500
            """).fetchall()
        except sqlite3.Error:
            rows = []
        c.close()

        for cid, cno, cdate, cstatus, sno, customer_v in rows:
            display = (
                f"{cno or ('R-' + str(cid))} | {sno or '-'} | "
                f"{customer_v or '-'} | {cstatus or '-'}"
            )
            displays.append(display)
            service_claim_map[display] = (
                int(cid), cno or f"R-{cid}"
            )

    combo = globals().get("doc_claim_combo")
    if combo is not None:
        try:
            if combo.winfo_exists():
                combo["values"] = displays
        except Exception:
            pass



def open_readonly_service(_event=None):
    sel = service_tree.selection()
    if not sel:
        return
    vals = service_tree.item(sel[0], "values")
    if not vals:
        return
    sid = int(vals[0])

    c = service_connect()
    if c is None:
        return
    row = c.execute("""
        SELECT service_no, date, customer, customer_address, customer_phone,
               model, serial, imei, ios, work, invoice_no, warranty_until
        FROM service WHERE id=?
    """, (sid,)).fetchone()
    parts = c.execute("""
        SELECT part_name, part_id, barcode, lot_serial, unit_price
        FROM service_parts WHERE service_id=?
    """, (sid,)).fetchall()
    c.close()
    if not row:
        return

    win = tk.Toplevel(root)
    win.title(f"MikroFIX Office - {row[0]}")
    win.geometry("760x620")
    win.configure(bg=BG)

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=18, pady=16)

    tk.Label(
        outer, text=f"Servis {row[0]}",
        bg=BG, fg=DARK, font=("Segoe UI", 17, "bold")
    ).pack(anchor="w")

    info = tk.Frame(
        outer, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    info.pack(fill="x", pady=(10, 8))

    fields = [
        ("Datum", row[1]), ("Stranka", row[2]), ("Naslov", row[3]),
        ("Telefon", row[4]), ("Model", row[5]), ("iOS", row[8]),
        ("Račun", row[10]), ("Garancija do", row[11]),
    ]
    for i, (lab, val) in enumerate(fields):
        tk.Label(
            info, text=lab, bg=PANEL, fg=GRAY,
            font=("Segoe UI", 9, "bold"), width=16, anchor="w"
        ).grid(row=i, column=0, sticky="w", padx=(12, 8), pady=4)
        tk.Label(
            info, text=val or "-", bg=PANEL, fg=DARK,
            font=("Segoe UI", 9), anchor="w"
        ).grid(row=i, column=1, sticky="w", padx=(0, 12), pady=4)

    tk.Label(
        outer, text="Opravljeno delo",
        bg=BG, fg=GRAY, font=("Segoe UI", 9, "bold")
    ).pack(anchor="w")
    txt = tk.Text(outer, height=5, wrap="word", font=("Segoe UI", 9))
    txt.pack(fill="x", pady=(3, 8))
    txt.insert("1.0", row[9] or "")
    txt.configure(state="disabled")

    tree = ttk.Treeview(
        outer, columns=("part","code","barcode","lot","price"),
        show="headings", height=6
    )
    for col, name_v, width in [
        ("part","Artikel",220), ("code","Koda",120),
        ("barcode","Barkoda",160), ("lot","LOT",140), ("price","Cena",90)
    ]:
        tree.heading(col, text=name_v)
        tree.column(col, width=width)
    tree.pack(fill="both", expand=True)
    for p in parts:
        tree.insert(
            "", "end",
            values=(p[0] or "-", p[1] or "-", p[2] or "-",
                    p[3] or "-", money(p[4]))
        )


# ----------------------------------------------------------------------
# DOKUMENTI
# ----------------------------------------------------------------------

current_doc_id = None
current_doc_status = "OSNUTEK"
doc_partner_display_map = {}


def doc_stock_default_for_type(doc_type):
    # V29 zalogo poveča izključno enotni tok »Prejem blaga«.
    # Stari dokumentni obrazec ostane za izdane dokumente in zgodovino.
    return False


def on_doc_type_changed(_event=None):
    if current_doc_id is None:
        doc_stock_effect_var.set(doc_stock_default_for_type(doc_type_var.get()))
    refresh_doc_article_search()


def refresh_doc_article_search(*_args):
    """Enotno live iskanje: isti niz se išče v nazivu ALI barkodi."""
    tree = globals().get("doc_article_search_tree")
    if tree is None:
        return
    try:
        if not tree.winfo_exists():
            return
    except Exception:
        return

    query_var = globals().get("doc_article_search_var")
    q = query_var.get().strip() if query_var is not None else ""

    for iid in tree.get_children():
        tree.delete(iid)

    sql = """
        SELECT id, barcode, article_name, internal_code,
               stock_qty, price, lot_serial
        FROM inventory_items
    """
    params = []
    if q:
        sql += """
            WHERE COALESCE(active, 1)=1
              AND (article_name LIKE ? OR barcode LIKE ?)
        """
        like_q = f"%{q}%"
        params = [like_q, like_q]
    else:
        sql += " WHERE COALESCE(active, 1)=1 "

    sql += """
        ORDER BY
            CASE
                WHEN barcode = ? THEN 0
                WHEN article_name LIKE ? THEN 1
                ELSE 2
            END,
            article_name COLLATE NOCASE
        LIMIT 100
    """
    exact_q = q
    starts_q = f"{q}%" if q else "%"
    params.extend([exact_q, starts_q])

    c = inventory_connect()
    rows = c.execute(sql, params).fetchall()
    c.close()

    first_iid = None
    for r in rows:
        iid = tree.insert(
            "", "end",
            values=(
                r[0], r[1], r[2], r[3] or "-",
                int(r[4] or 0), f"{float(r[5] or 0):.2f}", r[6] or "-"
            )
        )
        if first_iid is None:
            first_iid = iid

    count_var = globals().get("doc_article_count_var")
    if count_var is not None:
        count_var.set(f"{len(rows)} zadetkov")

    # Prvi rezultat je vedno označen, fokus pa ostane v iskalnem polju.
    if first_iid is not None:
        tree.selection_set(first_iid)
        tree.focus(first_iid)
        tree.see(first_iid)


def get_doc_selected_article():
    tree = globals().get("doc_article_search_tree")
    if tree is None:
        return None
    sel = tree.selection()
    if not sel:
        children = tree.get_children()
        if not children:
            return None
        sel = (children[0],)
        tree.selection_set(sel[0])
        tree.focus(sel[0])
    vals = tree.item(sel[0], "values")
    return vals if vals else None


def move_doc_article_selection(step):
    """↑/↓ izbira rezultatov, ne da bi zapustili iskalno polje."""
    tree = globals().get("doc_article_search_tree")
    if tree is None:
        return "break"
    children = list(tree.get_children())
    if not children:
        return "break"

    sel = tree.selection()
    current = sel[0] if sel and sel[0] in children else children[0]
    idx = children.index(current)
    idx = max(0, min(len(children) - 1, idx + step))
    target = children[idx]
    tree.selection_set(target)
    tree.focus(target)
    tree.see(target)
    return "break"


def doc_article_search_enter(_event=None):
    vals = get_doc_selected_article()
    if not vals:
        return "break"
    open_doc_line_editor(article_values=vals)
    return "break"


def add_manual_service_line():
    """Dodaj delo/storitev, ki ni skladiščni artikel."""
    if current_doc_status == "KNJIŽENO":
        return
    description = simpledialog.askstring(
        "MikroFIX Office",
        "Naziv dela ali storitve:",
        initialvalue="Servisna storitev / delo",
        parent=doc_editor_window or root,
    )
    if not description or not description.strip():
        return
    open_doc_line_editor(
        article_values=(
            None,
            "",
            description.strip(),
            "STORITEV",
            "-",
            LABOR_FLOOR_EUR,
            "",
        )
    )


def on_doc_article_selected(_event=None):
    # v2.2: izbor v tabeli ne polni več ločenih polj; Enter odpre obrazec.
    return



def doc_line_records():
    rows = []
    for iid in doc_lines_tree.get_children():
        vals = list(doc_lines_tree.item(iid, "values"))
        if not vals:
            continue
        rows.append({
            "iid": iid,
            "article_id": safe_int(vals[0], 0) or None,
            "barcode": str(vals[1]),
            "name": str(vals[2]),
            "code": "" if vals[3] == "-" else str(vals[3]),
            "qty": safe_float(vals[4]),
            "unit_price": safe_float(vals[5]),
            "discount": safe_float(vals[6]),
            "vat": safe_float(vals[7]),
            "delivery_share": safe_float(vals[8]),
            "actual_unit_cost": safe_float(vals[9]),
            "margin": safe_float(vals[10]),
            "calculated": safe_float(vals[11]),
            "recommended": safe_float(vals[12]),
            "sell": safe_float(vals[13]),
            "line_net": safe_float(vals[14]),
            "line_vat": safe_float(vals[15]),
            "line_total": safe_float(vals[16]),
        })
    return rows


def recalc_doc_lines():
    rows = doc_line_records()
    delivery = max(0.0, safe_float(doc_delivery_var.get()))
    base_values = []
    total_base = 0.0

    for r in rows:
        qty = max(0.0, r["qty"])
        price = max(0.0, r["unit_price"])
        disc = min(100.0, max(0.0, r["discount"]))
        base = qty * price * (1.0 - disc / 100.0)
        base_values.append(base)
        total_base += base

    total_net = 0.0
    total_vat = 0.0

    for index, r in enumerate(rows):
        qty = max(0.0, r["qty"])
        base = base_values[index]
        if total_base > 0:
            dshare = delivery * (base / total_base)
        elif rows:
            dshare = delivery / len(rows)
        else:
            dshare = 0.0

        actual_unit = ((base + dshare) / qty) if qty > 0 else 0.0
        calc = actual_unit * (1.0 + max(0.0, r["margin"]) / 100.0)
        recommended = ceil_to_5(calc)

        # Če je prodajna cena prazna/0, naj Office uporabi priporočeno /5.
        sell = r["sell"] if r["sell"] > 0 else recommended

        vat = base * max(0.0, r["vat"]) / 100.0
        line_total = base + vat

        total_net += base
        total_vat += vat

        doc_lines_tree.item(
            r["iid"],
            values=(
                r["article_id"] or "",
                r["barcode"],
                r["name"],
                r["code"] or "-",
                f"{qty:.2f}",
                f"{r['unit_price']:.2f}",
                f"{r['discount']:.2f}",
                f"{r['vat']:.2f}",
                f"{dshare:.2f}",
                f"{actual_unit:.2f}",
                f"{r['margin']:.2f}",
                f"{calc:.2f}",
                f"{recommended:.2f}",
                f"{sell:.2f}",
                f"{base:.2f}",
                f"{vat:.2f}",
                f"{line_total:.2f}",
            )
        )

    doc_total_net_var.set(money(total_net))
    doc_total_vat_var.set(money(total_vat))
    doc_total_delivery_var.set(money(delivery))
    doc_total_all_var.set(money(total_net + total_vat + delivery))


def _provisional_doc_line_math(
    qty, unit_price, discount, vat_pct, margin_pct, sell_price,
    editing_iid=None
):
    qty = max(0.0, float(qty or 0))
    unit_price = max(0.0, float(unit_price or 0))
    discount = min(100.0, max(0.0, float(discount or 0)))
    vat_pct = (
        0.0
        if TAX_MODE == "NON_VAT"
        else max(0.0, float(vat_pct or 0))
    )
    margin_pct = max(0.0, float(margin_pct or 0))
    sell_price = max(0.0, float(sell_price or 0))

    base = qty * unit_price * (1.0 - discount / 100.0)

    other_base = 0.0
    tree = globals().get("doc_lines_tree")
    if tree is not None:
        for iid in tree.get_children():
            if editing_iid is not None and str(iid) == str(editing_iid):
                continue
            vals = tree.item(iid, "values")
            if vals and len(vals) > 14:
                other_base += max(0.0, safe_float(vals[14]))

    delivery = max(0.0, safe_float(doc_delivery_var.get()))
    total_base = other_base + base
    delivery_share = (
        delivery * base / total_base
        if total_base > 0 else 0.0
    )

    actual_unit_cost = (
        (base + delivery_share) / qty
        if qty > 0 else 0.0
    )
    calculated_price = actual_unit_cost * (1.0 + margin_pct / 100.0)
    recommended_price = ceil_to_5(calculated_price)
    final_sell = sell_price if sell_price > 0 else recommended_price

    vat_value = base * vat_pct / 100.0
    line_total = base + vat_value

    profit_per_unit = final_sell - actual_unit_cost
    actual_margin_pct = (
        profit_per_unit / actual_unit_cost * 100.0
        if actual_unit_cost > 0 else 0.0
    )

    return {
        "base": base,
        "delivery_share": delivery_share,
        "actual_unit_cost": actual_unit_cost,
        "calculated_price": calculated_price,
        "recommended_price": recommended_price,
        "final_sell": final_sell,
        "vat_value": vat_value,
        "line_total": line_total,
        "profit_per_unit": profit_per_unit,
        "actual_margin_pct": actual_margin_pct,
    }


def _focus_and_select(widget):
    try:
        widget.focus_set()
        widget.selection_range(0, "end")
        widget.icursor("end")
    except Exception:
        pass


def open_doc_line_editor(article_values=None, line_iid=None):
    """Mini obrazec artikla. Enter potrjuje polje in gre na naslednjega."""
    if current_doc_status == "KNJIŽENO":
        return

    if line_iid is not None:
        vals = doc_lines_tree.item(line_iid, "values")
        if not vals:
            return

        article_id_v = vals[0]
        barcode_v = vals[1]
        name_v = vals[2]
        code_v = "" if vals[3] == "-" else vals[3]
        qty_initial = vals[4]
        price_initial = vals[5]
        discount_initial = vals[6]
        vat_initial = vals[7]
        margin_initial = vals[10]
        sell_initial = vals[13]

        stock_v = "-"
        lot_v = "-"
        acquisition_cost_v = 0.0
        if article_id_v:
            c = inventory_connect()
            row = c.execute("""
                SELECT stock_qty, lot_serial, acquisition_cost
                FROM inventory_items
                WHERE id=?
            """, (article_id_v,)).fetchone()
            c.close()
            if row:
                stock_v = int(row[0] or 0)
                lot_v = row[1] or "-"
                acquisition_cost_v = safe_float(row[2])
    else:
        if not article_values:
            return
        article_id_v = article_values[0]
        barcode_v = article_values[1]
        name_v = article_values[2]
        code_v = "" if article_values[3] == "-" else article_values[3]
        stock_v = article_values[4]
        current_sell_v = safe_float(article_values[5])
        lot_v = article_values[6]
        acquisition_cost_v = 0.0
        if article_id_v:
            c = inventory_connect()
            row = c.execute(
                "SELECT acquisition_cost FROM inventory_items WHERE id=?",
                (article_id_v,),
            ).fetchone()
            c.close()
            if row:
                acquisition_cost_v = safe_float(row[0])

        qty_initial = "1"
        if doc_type_var.get() in ("IZDANI RAČUN", "PREDRAČUN"):
            price_initial = f"{current_sell_v:.2f}"
        else:
            price_initial = "0.00"
        discount_initial = "0"
        vat_initial = f"{DEFAULT_VAT_RATE:.2f}"
        margin_initial = "0" if PRICING_REQUIRES_REVIEW else "70"
        sell_initial = "0.00"

    win = tk.Toplevel(doc_editor_window or root)
    win.title(
        "MikroFIX Office - Uredi artikel"
        if line_iid is not None
        else "MikroFIX Office - Sprejeti artikel"
    )
    win.geometry("760x680")
    win.minsize(640, 560)
    win.resizable(True, True)
    win.configure(bg=BG)
    win.transient(doc_editor_window or root)
    win.grab_set()

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=22, pady=18)

    tk.Label(
        outer,
        text=name_v,
        bg=BG, fg=DARK,
        font=("Segoe UI", 17, "bold")
    ).pack(anchor="w")

    tk.Label(
        outer,
        text=f"Barkoda: {barcode_v or '-'}  •  Zaloga: {stock_v}  •  LOT: {lot_v or '-'}",
        bg=BG, fg=GRAY,
        font=("Segoe UI", 9)
    ).pack(anchor="w", pady=(2,10))

    card = tk.Frame(
        outer, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="x")

    qty_var = tk.StringVar(value=str(qty_initial))
    price_var = tk.StringVar(value=str(price_initial))
    discount_var = tk.StringVar(value=str(discount_initial))
    vat_var = tk.StringVar(value=str(vat_initial))
    margin_var = tk.StringVar(value=str(margin_initial))
    sell_var = tk.StringVar(value=str(sell_initial))

    # Prodajna cena privzeto avtomatsko sledi priporočeni ceni /5.
    # Če jo uporabnik ročno spremeni, se avtomatika ustavi za ta artikel.
    sell_price_manual = {"value": False}
    last_auto_sell = {"value": None}
    auto_setting_sell = {"value": False}

    form_defs = [
        ("Količina", qty_var),
        ("Cena / kos", price_var),
        ("Rabat %", discount_var),
        ("DDV %", vat_var),
        ("Marža %", margin_var),
        ("Prodajna cena / kos", sell_var),
    ]

    entries = []
    for i, (lab, var) in enumerate(form_defs):
        tk.Label(
            card, text=lab,
            bg=PANEL, fg=GRAY,
            font=("Segoe UI", 9, "bold"),
            width=24, anchor="w"
        ).grid(
            row=i, column=0,
            sticky="w", padx=(14,8), pady=8
        )
        ent = ttk.Entry(
            card,
            textvariable=var,
            font=("Segoe UI", 11),
            width=28
        )
        ent.grid(
            row=i, column=1,
            sticky="ew", padx=(0,14), pady=8,
            ipady=3
        )
        entries.append(ent)

    card.grid_columnconfigure(1, weight=1)

    if TAX_MODE == "NON_VAT":
        vat_var.set("0.00")
        entries[3].configure(state="readonly")

    if PRICING_REQUIRES_REVIEW:
        tk.Label(
            outer,
            text=(
                "Cenovna matrika L0/marž je v pripravi. "
                "Prodajno ceno preveri in potrdi ročno."
            ),
            bg=BG, fg=WARNING, font=("Segoe UI", 9, "bold")
        ).pack(anchor="w", pady=(8, 0))

    calc_var = tk.StringVar()
    calc = tk.Label(
        outer,
        textvariable=calc_var,
        bg="#F7F9FA", fg=DARK,
        font=("Segoe UI", 9),
        justify="left", anchor="w",
        padx=14, pady=10,
        highlightthickness=1,
        highlightbackground=LIGHT_GRAY
    )
    calc.pack(fill="x", pady=(10,0))

    def calculate_preview(*_args):
        qty = max(0.0, safe_float(qty_var.get()))
        price = max(0.0, safe_float(price_var.get()))
        discount = min(100.0, max(0.0, safe_float(discount_var.get())))
        vat = (
            0.0
            if TAX_MODE == "NON_VAT"
            else max(0.0, safe_float(vat_var.get()))
        )
        margin = max(0.0, safe_float(margin_var.get()))
        sell = max(0.0, safe_float(sell_var.get()))

        # Najprej izračun brez ročne prodajne cene, da dobimo priporočeno /5.
        auto_math = _provisional_doc_line_math(
            qty, price, discount, vat, margin, 0,
            editing_iid=line_iid
        )
        recommended = auto_math["recommended_price"]

        if not sell_price_manual["value"]:
            current_sell = max(0.0, safe_float(sell_var.get()))
            last_auto = last_auto_sell["value"]
            if (
                current_sell <= 0
                or last_auto is None
                or abs(current_sell - float(last_auto)) < 1e-9
            ):
                auto_setting_sell["value"] = True
                sell_var.set(f"{recommended:.2f}")
                auto_setting_sell["value"] = False
                last_auto_sell["value"] = recommended
                sell = recommended

        m = _provisional_doc_line_math(
            qty, price, discount, vat, margin, sell,
            editing_iid=line_iid
        )
        calc_var.set(
            f"Neto vrstice: {money(m['base'])}    "
            f"Delež dostave: {money(m['delivery_share'])}\n"
            f"Dejanska nabavna / kos: {money(m['actual_unit_cost'])}    "
            f"Izračunana cena: {money(m['calculated_price'])}\n"
            f"Priporočena /5: {money(m['recommended_price'])}    "
            f"Končna prodajna: {money(m['final_sell'])}\n"
            f"DDV: {money(m['vat_value'])}    "
            f"Skupaj: {money(m['line_total'])}    "
            f"Marža pri končni ceni: {m['actual_margin_pct']:.1f}%"
        )

    def on_sell_price_changed(*_args):
        if auto_setting_sell["value"]:
            return
        current = max(0.0, safe_float(sell_var.get()))
        last_auto = last_auto_sell["value"]
        if last_auto is None:
            return
        if abs(current - float(last_auto)) >= 1e-9:
            sell_price_manual["value"] = True
        calculate_preview()

    for var in (
        qty_var, price_var, discount_var,
        vat_var, margin_var
    ):
        var.trace_add("write", calculate_preview)

    sell_var.trace_add("write", on_sell_price_changed)

    def validate_and_save():
        qty = safe_float(qty_var.get(), 0)
        if qty <= 0 or abs(qty - round(qty)) > 1e-9:
            messagebox.showwarning(
                "MikroFIX Office",
                "Količina mora biti celo število večje od 0.",
                parent=win
            )
            _focus_and_select(entries[0])
            return

        qty = int(round(qty))
        unit_price = max(0.0, safe_float(price_var.get()))
        discount = min(100.0, max(0.0, safe_float(discount_var.get())))
        vat = (
            0.0
            if TAX_MODE == "NON_VAT"
            else max(0.0, safe_float(vat_var.get()))
        )
        margin = max(0.0, safe_float(margin_var.get()))
        sell = max(0.0, safe_float(sell_var.get()))

        if doc_type_var.get() in ("IZDANI RAČUN", "PREDRAČUN"):
            article_kind = (
                "PART"
                if article_id_v and str(code_v).upper() != "STORITEV"
                else "SERVICE"
            )
            try:
                validate_document_line_price(
                    line_kind=article_kind,
                    base_price=unit_price,
                    discount_pct=discount,
                    acquisition_cost=acquisition_cost_v,
                    labor_floor_eur=LABOR_FLOOR_EUR,
                )
            except V29Error as exc:
                messagebox.showwarning(
                    "MikroFIX Office - dovoljen rabat",
                    str(exc),
                    parent=win,
                )
                _focus_and_select(entries[1])
                return

        m = _provisional_doc_line_math(
            qty, unit_price, discount, vat, margin, sell,
            editing_iid=line_iid
        )
        final_sell = m["final_sell"]

        values = (
            article_id_v or "",
            barcode_v or "",
            name_v or "",
            code_v or "-",
            f"{qty:.2f}",
            f"{unit_price:.2f}",
            f"{discount:.2f}",
            f"{vat:.2f}",
            f"{m['delivery_share']:.2f}",
            f"{m['actual_unit_cost']:.2f}",
            f"{margin:.2f}",
            f"{m['calculated_price']:.2f}",
            f"{m['recommended_price']:.2f}",
            f"{final_sell:.2f}",
            f"{m['base']:.2f}",
            f"{m['vat_value']:.2f}",
            f"{m['line_total']:.2f}",
        )

        if line_iid is None:
            new_iid = doc_lines_tree.insert(
                "", "end", values=values
            )
            doc_lines_tree.selection_set(new_iid)
            doc_lines_tree.focus(new_iid)
            doc_lines_tree.see(new_iid)
        else:
            doc_lines_tree.item(
                line_iid, values=values
            )
            doc_lines_tree.selection_set(line_iid)
            doc_lines_tree.focus(line_iid)
            doc_lines_tree.see(line_iid)

        recalc_doc_lines()
        win.destroy()

        # Po potrditvi takoj nazaj na iskanje - pripravljeno za naslednji artikel.
        search_var = globals().get("doc_article_search_var")
        if search_var is not None:
            search_var.set("")
        search_entry = globals().get("doc_article_search_entry")
        if search_entry is not None:
            try:
                search_entry.focus_set()
            except Exception:
                pass

    # ENTER = potrdi trenutno polje in pojdi naprej.
    for i, ent in enumerate(entries):
        ent.bind(
            "<FocusIn>",
            lambda e, w=ent: w.after_idle(
                lambda: _focus_and_select(w)
            )
        )
        if i < len(entries) - 1:
            nxt = entries[i + 1]
            ent.bind(
                "<Return>",
                lambda e, w=nxt: (
                    _focus_and_select(w), "break"
                )[1]
            )
        else:
            ent.bind(
                "<Return>",
                lambda e: (
                    validate_and_save(), "break"
                )[1]
            )

    buttons = tk.Frame(outer, bg=BG)
    # Dejanja ostanejo na vrhu tudi na manjših zaslonih.
    buttons.pack(fill="x", pady=(12, 8), before=card)

    ttk.Button(
        buttons,
        text="PREKLIČI",
        command=win.destroy
    ).pack(side="left")

    ttk.Button(
        buttons,
        text="SHRANI ARTIKEL",
        command=validate_and_save,
        style="Blue.TButton"
    ).pack(side="right")

    win.bind("<Escape>", lambda e: win.destroy())

    calculate_preview()

    if line_iid is not None:
        stored_sell = max(0.0, safe_float(sell_initial))
        auto_sell = last_auto_sell["value"]
        if auto_sell is not None and abs(stored_sell - float(auto_sell)) >= 1e-9:
            sell_price_manual["value"] = True
            auto_setting_sell["value"] = True
            sell_var.set(f"{stored_sell:.2f}")
            auto_setting_sell["value"] = False
            calculate_preview()

    win.after(80, lambda: _focus_and_select(entries[0]))


def edit_selected_doc_line(_event=None):
    if current_doc_status == "KNJIŽENO":
        return "break"
    sel = doc_lines_tree.selection()
    if not sel:
        return "break"
    open_doc_line_editor(line_iid=sel[0])
    return "break"


def remove_doc_line(_event=None):
    if current_doc_status == "KNJIŽENO":
        return "break"
    sel = list(doc_lines_tree.selection())
    if not sel:
        return "break"
    for iid in sel:
        doc_lines_tree.delete(iid)
    recalc_doc_lines()

    search_entry = globals().get("doc_article_search_entry")
    if search_entry is not None:
        try:
            search_entry.focus_set()
        except Exception:
            pass
    return "break"



def doc_partner_id_from_combo():
    return partner_map.get(doc_partner_var.get().strip())


def selected_linked_claim():
    return service_claim_map.get(doc_claim_var.get().strip())


def get_partner_snapshot(partner_id):
    if not partner_id:
        return ("", "", "", 0.0)
    c = office_connect()
    row = c.execute("""
        SELECT name,
               trim(COALESCE(address,'') || CASE WHEN post_city<>'' THEN ', ' || post_city ELSE '' END),
               tax_no, pcd_days
        FROM partners WHERE id=?
    """, (partner_id,)).fetchone()
    c.close()
    return row or ("", "", "", 0.0)


def validate_document():
    if doc_type_var.get() not in DOC_TYPES:
        messagebox.showwarning("MikroFIX Office", "Izberi vrsto dokumenta.")
        return False
    if not doc_date_var.get().strip():
        messagebox.showwarning("MikroFIX Office", "Vpiši datum dokumenta.")
        return False
    if not doc_lines_tree.get_children() and doc_type_var.get() != "REKLAMACIJA DOBAVITELJU":
        messagebox.showwarning("MikroFIX Office", "Dokument nima nobene vrstice.")
        return False
    if doc_stock_effect_var.get():
        messagebox.showwarning(
            "MikroFIX Office",
            "Stari dokumentni obrazec v v29 ne sme spreminjati zaloge. "
            "Za fizični prejem uporabi zavihek Prejem blaga.",
        )
        return False
    if doc_type_var.get() in ("IZDANI RAČUN", "PREDRAČUN"):
        inventory = inventory_connect()
        try:
            for line in doc_line_records():
                acquisition_cost = 0.0
                line_kind = "SERVICE"
                if line["article_id"]:
                    row = inventory.execute(
                        "SELECT acquisition_cost, tax_classification FROM inventory_items WHERE id=?",
                        (int(line["article_id"]),),
                    ).fetchone()
                    acquisition_cost = safe_float(row[0]) if row else 0.0
                    if TAX_MODE != "NON_VAT" and (
                        not row or str(row[1] or "UNCLASSIFIED") == "UNCLASSIFIED"
                    ):
                        raise V29Error(
                            f"Artikel »{line['name']}« nima potrjene davčne razvrstitve."
                        )
                    line_kind = "PART"
                validate_document_line_price(
                    line_kind=line_kind,
                    base_price=line["unit_price"],
                    discount_pct=line["discount"],
                    acquisition_cost=acquisition_cost,
                    labor_floor_eur=LABOR_FLOOR_EUR,
                )
        except V29Error as exc:
            messagebox.showwarning("MikroFIX Office - dovoljen rabat", str(exc))
            return False
        finally:
            inventory.close()
    return True


def save_document(status_override=None, quiet=False):
    global current_doc_id, current_doc_status

    if current_doc_status == "KNJIŽENO":
        if not quiet:
            messagebox.showwarning(
                "MikroFIX Office",
                "Knjiženega dokumenta ni več mogoče spreminjati."
            )
        return current_doc_id

    if not validate_document():
        return None

    recalc_doc_lines()
    partner_id = doc_partner_id_from_combo()
    pname, paddr, ptax, ppcd = get_partner_snapshot(partner_id)
    linked = selected_linked_claim()
    linked_id = linked[0] if linked else None
    linked_no = linked[1] if linked else ""

    c = office_connect()
    now = now_text()
    status_v = status_override or "OSNUTEK"
    document_event = (
        "DOCUMENT_CREATED" if current_doc_id is None else "DOCUMENT_UPDATED"
    )

    if current_doc_id is None:
        doc_no_v = next_document_no(doc_type_var.get())
        cur = c.execute("""
            INSERT INTO documents(
                doc_no, doc_type, partner_id,
                partner_name_snapshot, partner_address_snapshot,
                partner_tax_snapshot, partner_pcd_snapshot,
                external_no, date, order_date, received_date, due_date,
                currency, delivery_cost, stock_effect, status,
                linked_claim_id, linked_claim_no, linked_service_id, notes,
                created_at, updated_at, booked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            doc_no_v, doc_type_var.get(), partner_id,
            pname, paddr, ptax, float(ppcd or 0),
            doc_external_no_var.get().strip(),
            doc_date_var.get().strip(),
            doc_order_date_var.get().strip(),
            doc_received_date_var.get().strip(),
            doc_due_date_var.get().strip(),
            doc_currency_var.get().strip() or "EUR",
            max(0.0, safe_float(doc_delivery_var.get())),
            1 if doc_stock_effect_var.get() else 0,
            status_v,
            linked_id, linked_no, doc_linked_service_id,
            get_doc_notes_text(),
            now, now,
            now if status_v == "KNJIŽENO" else None
        ))
        current_doc_id = cur.lastrowid
        doc_no_display_var.set(doc_no_v)
    else:
        c.execute("""
            UPDATE documents
            SET doc_type=?, partner_id=?,
                partner_name_snapshot=?, partner_address_snapshot=?,
                partner_tax_snapshot=?, partner_pcd_snapshot=?,
                external_no=?, date=?, order_date=?, received_date=?, due_date=?,
                currency=?, delivery_cost=?, stock_effect=?, status=?,
                linked_claim_id=?, linked_claim_no=?, linked_service_id=?, notes=?,
                updated_at=?, booked_at=?
            WHERE id=?
        """, (
            doc_type_var.get(), partner_id,
            pname, paddr, ptax, float(ppcd or 0),
            doc_external_no_var.get().strip(),
            doc_date_var.get().strip(),
            doc_order_date_var.get().strip(),
            doc_received_date_var.get().strip(),
            doc_due_date_var.get().strip(),
            doc_currency_var.get().strip() or "EUR",
            max(0.0, safe_float(doc_delivery_var.get())),
            1 if doc_stock_effect_var.get() else 0,
            status_v,
            linked_id, linked_no, doc_linked_service_id,
            get_doc_notes_text(),
            now,
            now if status_v == "KNJIŽENO" else None,
            current_doc_id
        ))
        c.execute("DELETE FROM document_items WHERE document_id=?", (current_doc_id,))

    rows = doc_line_records()
    for r in rows:
        c.execute("""
            INSERT INTO document_items(
                document_id, article_id, barcode, article_name, internal_code,
                qty, unit_price, discount_pct, vat_pct, delivery_share,
                actual_unit_cost, margin_pct, calculated_price,
                recommended_price, sell_price, line_net, line_vat, line_total
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            current_doc_id, r["article_id"], r["barcode"], r["name"], r["code"],
            r["qty"], r["unit_price"], r["discount"], r["vat"],
            r["delivery_share"], r["actual_unit_cost"], r["margin"],
            r["calculated"], r["recommended"], r["sell"],
            r["line_net"], r["line_vat"], r["line_total"]
        ))

    c.commit()
    c.close()

    audit_event(
        document_event,
        "document",
        current_doc_id,
        {
            "document_no": doc_no_display_var.get(),
            "document_type": doc_type_var.get(),
            "status": status_v,
            "items_count": len(rows),
        },
    )

    current_doc_status = status_v
    doc_status_var.set(status_v)
    refresh_documents()
    if not quiet:
        messagebox.showinfo(
            "MikroFIX Office",
            f"Dokument {doc_no_display_var.get()} je shranjen kot {status_v}."
        )
    return current_doc_id


def book_document():
    global current_doc_status

    if doc_type_var.get() == "IZDANI RAČUN":
        messagebox.showwarning(
            "MikroFIX Office",
            "Izdani račun je mogoče ustvariti samo v namenski Blagajni. "
            "Stari Office postopek je zaradi enotne evidence plačil, zaloge in tiska blokiran.",
        )
        return

    if current_doc_status == "KNJIŽENO":
        messagebox.showwarning(
            "MikroFIX Office", "Dokument je že knjižen."
        )
        return

    doc_id = save_document(status_override="OSNUTEK", quiet=True)
    if not doc_id:
        return

    if not messagebox.askyesno(
        "Knjiži dokument",
        "Po knjiženju dokumenta ni več mogoče spreminjati.\n\n"
        "Pri neposredni prodaji se fizični artikli odpišejo iz zaloge. Deli, "
        "že porabljeni pri povezanem servisu, se ne odpišejo ponovno.\n\n"
        "Nadaljujem?"
    ):
        return

    c = office_connect()
    doc = c.execute("""
        SELECT stock_effect, status, doc_type, linked_service_id,
               partner_name_snapshot, COALESCE(used_goods_12m_signed, 0)
        FROM documents WHERE id=?
    """, (doc_id,)).fetchone()

    if not doc or doc[1] == "KNJIŽENO":
        c.close()
        return

    items = c.execute("""
        SELECT id, article_id, qty, sell_price
        FROM document_items WHERE document_id=?
    """, (doc_id,)).fetchall()
    try:
        if int(doc[0] or 0):
            raise RuntimeError(
                "Stari način 'Poveča zalogo' je v v29 blokiran. Uporabi Prejem blaga."
            )
        c.execute("ATTACH DATABASE ? AS inv", (str(INVENTORY_DB),))
        c.execute("ATTACH DATABASE ? AS servicedb", (str(SERVICE_DB),))
        used_device_lines = []
        for line_id, article_id_v, _qty_v, _sell_v in items:
            if not article_id_v:
                continue
            source = c.execute(
                "SELECT source_kind, source_id FROM inv.inventory_items WHERE id=?",
                (int(article_id_v),),
            ).fetchone()
            if source and source[0] == "BUYBACK_DEVICE":
                used_device_lines.append((int(line_id), int(source[1])))
        used_goods_signed = bool(doc[5])
        if used_device_lines and not used_goods_signed:
            used_goods_signed = messagebox.askyesno(
                "Obnovljena rabljena naprava",
                "Ali je kupec podpisal ločen dogovor, da se obdobje odgovornosti "
                "za neskladnost rabljene naprave skrajša na 12 mesecev?\n\n"
                "Brez podpisanega dogovora programa računa ne bo knjižil.",
            )
            if not used_goods_signed:
                raise RuntimeError(
                    "Manjka podpisan ločen dogovor za 12-mesečno obdobje rabljene naprave."
                )
        c.execute("BEGIN IMMEDIATE")

        already_consumed = {}
        linked_service_id = doc[3]
        if linked_service_id:
            for article_id_v, count_v in c.execute(
                """
                SELECT article_id, COUNT(*) FROM servicedb.service_parts
                WHERE service_id=? AND article_id IS NOT NULL GROUP BY article_id
                """,
                (int(linked_service_id),),
            ).fetchall():
                already_consumed[int(article_id_v)] = float(count_v or 0)

        if doc[2] == "IZDANI RAČUN":
            for line_id, article_id_v, qty_v, _sell_v in items:
                if not article_id_v:
                    continue
                article_id_v = int(article_id_v)
                qty_v = float(qty_v or 0)
                covered = min(qty_v, already_consumed.get(article_id_v, 0.0))
                already_consumed[article_id_v] = max(
                    0.0, already_consumed.get(article_id_v, 0.0) - covered
                )
                quantity_to_issue = round(qty_v - covered, 4)
                if quantity_to_issue <= 0:
                    continue
                row = c.execute(
                    """
                    SELECT stock_qty, article_name, lot_serial, location
                    FROM inv.inventory_items WHERE id=? AND active=1
                    """,
                    (article_id_v,),
                ).fetchone()
                if not row or float(row[0] or 0) + 1e-9 < quantity_to_issue:
                    raise RuntimeError(
                        f"Premalo zaloge za neposredno prodajo: "
                        f"{row[1] if row else 'artikel ID ' + str(article_id_v)}."
                    )
                balance_after = round(float(row[0] or 0) - quantity_to_issue, 4)
                c.execute(
                    "UPDATE inv.inventory_items SET stock_qty=?, updated_at=? WHERE id=?",
                    (balance_after, now_text(), article_id_v),
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
                        f"PREM-PRO-{doc_id:06d}-{int(line_id)}", now_text(),
                        article_id_v, -quantity_to_issue, balance_after,
                        "SALE_OUT", "OFFICE_INVOICE", int(doc_id), int(line_id),
                        row[2] or "", row[3] or "", "KUPEC",
                        f"Prodaja po dokumentu {doc_no_display_var.get()}",
                        RUNTIME.operator_code,
                    ),
                )
                source = c.execute(
                    "SELECT source_kind, source_id FROM inv.inventory_items WHERE id=?",
                    (article_id_v,),
                ).fetchone()
                if source and source[0] == "BUYBACK_DEVICE":
                    c.execute(
                        """
                        UPDATE inv.used_devices
                        SET status='SOLD', buyer_name=?, sale_document_id=?,
                            warranty_until=date(?,'+12 months'), updated_at=?
                        WHERE id=?
                        """,
                        (
                            doc[4] or "", int(doc_id), now_text(), now_text(),
                            int(source[1]),
                        ),
                    )

        c.execute("""
            UPDATE documents
            SET status='KNJIŽENO', booked_at=?, updated_at=?,
                used_goods_12m_signed=?
            WHERE id=?
        """, (now_text(), now_text(), 1 if used_goods_signed else 0, doc_id))
        if doc[2] == "IZDANI RAČUN" and linked_service_id:
            c.execute(
                """
                UPDATE servicedb.service
                SET invoice_no=?, office_invoice_id=? WHERE id=?
                """,
                (doc_no_display_var.get(), int(doc_id), int(linked_service_id)),
            )
        c.commit()
    except Exception as exc:
        c.rollback()
        c.close()
        messagebox.showerror(
            "MikroFIX Office",
            f"Knjiženje ni uspelo:\n{exc}\n\n"
            "Dokument je ostal OSNUTEK in zaloga ni bila spremenjena.",
        )
        return
    c.close()

    audit_event(
        "DOCUMENT_BOOKED",
        "document",
        doc_id,
        {
            "document_no": doc_no_display_var.get(),
            "stock_effect": bool(doc[0]),
            "items_count": len(items),
        },
    )

    current_doc_status = "KNJIŽENO"
    doc_status_var.set("KNJIŽENO")
    set_document_editable(False)
    refresh_documents()
    refresh_office_articles()
    refresh_doc_article_search()

    messagebox.showinfo(
        "MikroFIX Office",
        f"Dokument {doc_no_display_var.get()} je knjižen."
    )


# new_document: v1 implementacija odstranjena; uporablja se v2 spodaj.


# set_document_editable: v1 implementacija odstranjena; uporablja se v2 spodaj.


# refresh_documents: v1 implementacija odstranjena; uporablja se v2 spodaj.


def get_selected_document_id():
    sel = documents_tree.selection()
    if not sel:
        return None
    vals = documents_tree.item(sel[0], "values")
    if not vals:
        return None
    try:
        return int(vals[0])
    except Exception:
        return None


# load_document: v1 implementacija odstranjena; uporablja se v2 spodaj.


def register_pdf_font():
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        raise RuntimeError("Manjka ReportLab.")

    candidates = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                pdfmetrics.registerFont(TTFont("MikroFIXOfficeFont", str(p)))
                return "MikroFIXOfficeFont"
            except Exception:
                pass
    return "Helvetica"


def generate_document_pdf(doc_id):
    ensure_pos_schema(OFFICE_DB)
    c = office_connect()
    pos_row = c.execute(
        "SELECT pos_issue_token, print_status FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    c.close()
    if pos_row and pos_row[0]:
        if pos_row[1] == "PRINTED":
            raise RuntimeError(
                "Potrjenega POS računa ni dovoljeno ponovno izdelati kot ORIGINAL. "
                "Uporabi gumb PDF / TISK v zgodovini, ki izdela oštevilčeno kopijo."
            )
        company = dict(RUNTIME.settings.get("business", {}))
        company.update({
            "short_name": COMPANY_NAME,
            "address": COMPANY_ADDRESS,
        })
        a4_path, _ = generate_pos_pdfs(
            office_db=OFFICE_DB,
            document_id=doc_id,
            output_dir=PDF_DIR,
            company=company,
            tax_note=TAX_INVOICE_NOTE,
            premise=RUNTIME.active_premise,
            device=RUNTIME.electronic_device,
            paper_width_mm=int(RUNTIME.settings["printer"]["paper_width_mm"]),
        )
        return a4_path
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        )
    except ImportError:
        raise RuntimeError(
            "Manjka ReportLab. Zaženi namesti_v28.bat iz MikroFIX mape."
        )

    c = office_connect()
    doc = c.execute("""
        SELECT doc_no, doc_type, partner_name_snapshot,
               partner_address_snapshot, partner_tax_snapshot,
               external_no, date, order_date, received_date, due_date,
               currency, delivery_cost, status, linked_claim_no, notes,
               partner_pcd_snapshot
        FROM documents WHERE id=?
    """, (doc_id,)).fetchone()
    items = c.execute("""
        SELECT article_name, internal_code, barcode, qty, unit_price,
               discount_pct, vat_pct, actual_unit_cost, margin_pct,
               recommended_price, sell_price, line_net, line_vat
        FROM document_items
        WHERE document_id=?
        ORDER BY id
    """, (doc_id,)).fetchall()
    c.close()

    if not doc:
        raise RuntimeError("Dokument ni najden.")

    font = register_pdf_font()
    path = PDF_DIR / f"{doc[0]}.pdf"

    document = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "T", parent=styles["Title"], fontName=font,
        fontSize=18, leading=22, textColor=colors.HexColor(BLUE_DARK)
    )
    body = ParagraphStyle(
        "B", parent=styles["BodyText"], fontName=font,
        fontSize=8.5, leading=11
    )
    h2 = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontName=font,
        fontSize=10.5, leading=13, textColor=colors.HexColor(DARK)
    )

    story = [
        Table([[
            [
                Paragraph(f"<b>{COMPANY_NAME}</b>", body),
                Paragraph(COMPANY_ADDRESS, body),
                Paragraph(COMPANY_PHONE, body),
                Paragraph(COMPANY_EMAIL, body),
                Paragraph(COMPANY_WEB, body),
            ],
            [
                Paragraph(f"<b>{pdf_safe(doc[2] or 'Partner')}</b>", body),
                Paragraph(pdf_safe(doc[3] or "-"), body),
                Paragraph(f"Davčna: {pdf_safe(doc[4] or '-')}", body),
                Paragraph(f"PČD: {float(doc[15] or 0):.1f} dni", body),
            ]
        ]], colWidths=[90*mm, 90*mm]),
        Spacer(1, 7*mm),
        Paragraph(f"{pdf_safe(doc[1])}: {pdf_safe(doc[0])}", title),
        Spacer(1, 4*mm),
    ]
    if RUNTIME.is_test or not FURS_ENABLED:
        story.insert(
            2,
            Paragraph(
                "<b>TESTNI DOKUMENT – NI ZA IZDAJO KOT FISKALNI RAČUN</b>",
                ParagraphStyle(
                    "TestNotice", parent=body, alignment=1,
                    textColor=colors.HexColor(DANGER), fontSize=10, leading=13,
                ),
            ),
        )

    info = [
        ["Datum", doc[6] or "-"],
        ["Zunanja št.", doc[5] or "-"],
        ["Datum naročila", doc[7] or "-"],
        ["Datum prejema", doc[8] or "-"],
        ["Rok plačila", doc[9] or "-"],
        ["Status", doc[12] or "-"],
        ["Povezana reklamacija", doc[13] or "-"],
    ]
    it = Table(info, colWidths=[45*mm, 135*mm])
    it.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),font),
        ("FONTSIZE",(0,0),(-1,-1),8.5),
        ("TEXTCOLOR",(0,0),(0,-1),colors.HexColor(GRAY)),
        ("LINEBELOW",(0,0),(-1,-1),0.25,colors.HexColor("#E4E8EB")),
        ("PADDING",(0,0),(-1,-1),4),
    ]))
    story += [it, Spacer(1, 5*mm)]

    customer_document = doc[1] in {"IZDANI RAČUN", "PREDRAČUN"}
    if customer_document:
        rows = [["Artikel", "Kol.", "Cena", "Rabat", "DDV", "Znesek"]]
        column_widths = [75*mm, 16*mm, 24*mm, 20*mm, 18*mm, 27*mm]
    else:
        rows = [[
            "Artikel", "Kol.", "Cena", "Rabat", "DDV",
            "Dej. nab.", "Marža", "Prip. /5", "Prod. cena"
        ]]
        column_widths = [
            42*mm, 13*mm, 19*mm, 16*mm, 15*mm, 21*mm, 16*mm, 20*mm, 20*mm
        ]
    net = vat = 0.0
    for r in items:
        if customer_document:
            rows.append([
                r[0] or "-", f"{float(r[3] or 0):.2f}",
                f"{float(r[4] or 0):.2f}", f"{float(r[5] or 0):.1f}%",
                f"{float(r[6] or 0):.1f}%",
                f"{float(r[11] or 0) + float(r[12] or 0):.2f}",
            ])
        else:
            rows.append([
                r[0] or "-", f"{float(r[3] or 0):.2f}",
                f"{float(r[4] or 0):.2f}", f"{float(r[5] or 0):.1f}%",
                f"{float(r[6] or 0):.1f}%", f"{float(r[7] or 0):.2f}",
                f"{float(r[8] or 0):.1f}%", f"{float(r[9] or 0):.2f}",
                f"{float(r[10] or 0):.2f}",
            ])
        net += float(r[11] or 0)
        vat += float(r[12] or 0)

    table = Table(
        rows,
        colWidths=column_widths,
        repeatRows=1
    )
    table.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),font),
        ("FONTSIZE",(0,0),(-1,-1),7),
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E8EDF1")),
        ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#D3D9DE")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("PADDING",(0,0),(-1,-1),3),
    ]))
    story.append(table)

    delivery = float(doc[11] or 0)
    if TAX_MODE == "NON_VAT":
        totals = [
            ["Vrednost", money(net)],
            ["Dostava", money(delivery)],
            ["Skupaj", money(net + delivery)],
        ]
    else:
        totals = [
            ["Neto", money(net)],
            ["DDV", money(vat)],
            ["Dostava", money(delivery)],
            ["Skupaj", money(net + vat + delivery)],
        ]
    tt = Table(totals, colWidths=[40*mm, 35*mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),font),
        ("FONTSIZE",(0,0),(-1,-1),9),
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("LINEABOVE",(0,-1),(-1,-1),0.5,colors.HexColor(DARK)),
        ("PADDING",(0,0),(-1,-1),4),
    ]))
    story += [Spacer(1, 5*mm), tt]

    if TAX_MODE == "NON_VAT" and TAX_INVOICE_NOTE:
        story += [
            Spacer(1, 3*mm),
            Paragraph(pdf_safe(TAX_INVOICE_NOTE), body),
        ]

    if doc[14]:
        story += [
            Spacer(1, 5*mm),
            Paragraph("Opomba", h2),
            Paragraph(pdf_safe(doc[14]), body),
        ]

    document.build(story)
    return path


def generate_document_receipt_pdf(doc_id):
    ensure_pos_schema(OFFICE_DB)
    c = office_connect()
    pos_row = c.execute(
        "SELECT pos_issue_token, print_status FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    c.close()
    if pos_row and pos_row[0]:
        if pos_row[1] == "PRINTED":
            raise RuntimeError(
                "Potrjenega POS računa ni dovoljeno ponovno izdelati kot ORIGINAL. "
                "Uporabi oštevilčeno kopijo."
            )
        company = dict(RUNTIME.settings.get("business", {}))
        company.update({
            "short_name": COMPANY_NAME,
            "address": COMPANY_ADDRESS,
        })
        _, receipt_path = generate_pos_pdfs(
            office_db=OFFICE_DB,
            document_id=doc_id,
            output_dir=PDF_DIR,
            company=company,
            tax_note=TAX_INVOICE_NOTE,
            premise=RUNTIME.active_premise,
            device=RUNTIME.electronic_device,
            paper_width_mm=int(RUNTIME.settings["printer"]["paper_width_mm"]),
        )
        return receipt_path
    c = office_connect()
    doc = c.execute("""
        SELECT doc_no, doc_type, partner_name_snapshot, date,
               delivery_cost
        FROM documents WHERE id=?
    """, (doc_id,)).fetchone()
    items = c.execute("""
        SELECT article_name, qty, line_net, line_vat
        FROM document_items
        WHERE document_id=?
        ORDER BY id
    """, (doc_id,)).fetchall()
    c.close()
    if not doc:
        raise RuntimeError("Dokument ni najden.")

    path = PDF_DIR / f"{doc[0]}_58mm_TEST.pdf"
    return generate_test_receipt(
        path,
        company={
            "name": COMPANY_NAME,
            "address": COMPANY_ADDRESS,
            "phone": COMPANY_PHONE,
            "email": COMPANY_EMAIL,
        },
        document={
            "number": doc[0],
            "type": doc[1],
            "partner": doc[2],
            "date": doc[3],
            "delivery": doc[4],
        },
        items=[{
            "name": row[0],
            "qty": row[1],
            "line_net": row[2],
            "line_vat": row[3],
        } for row in items],
        premise=RUNTIME.active_premise,
        device=RUNTIME.electronic_device,
        tax_note=TAX_INVOICE_NOTE if TAX_MODE == "NON_VAT" else "",
        paper_width_mm=int(RUNTIME.settings["printer"]["paper_width_mm"]),
    )


# print_selected_document_pdf: v1 implementacija odstranjena; uporablja se v2 spodaj.



# ----------------------------------------------------------------------
# DOKUMENTI v2 - zgodovina + dvostopenjski vnos
# ----------------------------------------------------------------------

doc_editor_window = None
doc_notes_value = ""
doc_header_context = None

DOCUMENT_SORTS = {
    "Po imenu partnerja": "LOWER(COALESCE(d.partner_name_snapshot,''))",
    "Po ceni": "total_amount",
    "Po datumu vnosa": "d.created_at",
    "Po datumu knjiženja": "COALESCE(d.booked_at,'')",
    "Po številki dobavnice": "LOWER(COALESCE(d.external_no,''))",
}


def get_doc_notes_text():
    widget = globals().get("doc_notes")
    if widget is not None:
        try:
            if widget.winfo_exists():
                return widget.get("1.0", "end").strip()
        except Exception:
            pass
    return str(globals().get("doc_notes_value", "") or "").strip()


def normalize_filter_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y."):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError("Datum vpiši kot DD.MM.YYYY ali YYYY-MM-DD.")


def search_documents():
    try:
        normalize_filter_date(document_date_exact_var.get())
        normalize_filter_date(document_date_from_var.get())
        normalize_filter_date(document_date_to_var.get())
    except ValueError as e:
        messagebox.showwarning("MikroFIX Office", str(e))
        return
    refresh_documents()


def clear_document_filters():
    document_search_var.set("")
    document_type_filter_var.set("VSI TIPI")
    document_sort_var.set("Po datumu vnosa")
    document_sort_direction_var.set("Padajoče")
    document_date_exact_var.set("")
    document_date_from_var.set("")
    document_date_to_var.set("")
    refresh_documents()


def refresh_documents(*_args):
    if "documents_tree" not in globals():
        return

    try:
        exact_date = normalize_filter_date(document_date_exact_var.get())
        date_from = normalize_filter_date(document_date_from_var.get())
        date_to = normalize_filter_date(document_date_to_var.get())
    except Exception:
        # Med inicializacijo ali ročnim pisanjem ne ruši tabele.
        return

    q = document_search_var.get().strip()
    type_filter = document_type_filter_var.get().strip()
    sort_label = document_sort_var.get().strip()
    direction = (
        "ASC"
        if document_sort_direction_var.get().strip() == "Naraščajoče"
        else "DESC"
    )
    sort_expr = DOCUMENT_SORTS.get(
        sort_label,
        "d.created_at"
    )

    for iid in documents_tree.get_children():
        documents_tree.delete(iid)

    sql = """
        SELECT
            d.id,
            COALESCE(d.fiscal_invoice_no, d.doc_no),
            d.doc_type,
            d.date,
            d.partner_name_snapshot,
            d.external_no,
            d.status,
            d.created_at,
            d.booked_at,
            d.partner_pcd_snapshot,
            (
                COALESCE((
                    SELECT SUM(di.line_total)
                    FROM document_items di
                    WHERE di.document_id=d.id
                ), 0) + COALESCE(d.delivery_cost, 0)
            ) AS total_amount
        FROM documents d
    """
    where = []
    params = []

    if q:
        p = f"%{q}%"
        where.append("""
            (
                d.doc_no LIKE ?
                OR COALESCE(d.fiscal_invoice_no,'') LIKE ?
                OR d.partner_name_snapshot LIKE ?
                OR d.external_no LIKE ?
                OR d.linked_claim_no LIKE ?
            )
        """)
        params.extend([p, p, p, p, p])

    if type_filter and type_filter != "VSI TIPI":
        where.append("d.doc_type=?")
        params.append(type_filter)

    if exact_date:
        where.append("substr(d.date,1,10)=?")
        params.append(exact_date)
    else:
        if date_from:
            where.append("substr(d.date,1,10)>=?")
            params.append(date_from)
        if date_to:
            where.append("substr(d.date,1,10)<=?")
            params.append(date_to)

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += f" ORDER BY {sort_expr} {direction}, d.id DESC LIMIT 1000"

    c = office_connect()
    rows = c.execute(sql, params).fetchall()
    c.close()

    for r in rows:
        documents_tree.insert(
            "", "end",
            values=(
                r[0],
                r[1] or "-",
                r[2] or "-",
                r[3] or "-",
                r[4] or "-",
                r[5] or "-",
                money(r[10]),
                r[6] or "-",
                r[7] or "-",
                r[8] or "-",
                f"{float(r[9] or 0):.1f} dni",
            )
        )

    document_count_var.set(f"{len(rows)} dokumentov")


def set_document_editable(enabled):
    state = "normal" if enabled else "disabled"

    for w in globals().get("doc_edit_entries", []):
        try:
            if w.winfo_exists():
                w.configure(state=state)
        except Exception:
            pass

    for w in globals().get("doc_edit_buttons", []):
        try:
            if w.winfo_exists():
                w.configure(state=state)
        except Exception:
            pass

    # Search ostane omogočen tudi pri knjiženem dokumentu, dodajanje pa ne.
    try:
        if globals().get("doc_stock_check") is not None:
            doc_stock_check.configure(state=state)
    except Exception:
        pass


def close_document_editor():
    global doc_editor_window
    if doc_editor_window is not None:
        try:
            doc_editor_window.destroy()
        except Exception:
            pass
    doc_editor_window = None


def partner_display_for_id(partner_id, fallback_name="", fallback_pcd=0):
    for display, pid in partner_map.items():
        if pid == partner_id:
            return display
    if fallback_name:
        return f"{fallback_name}  |  PČD {float(fallback_pcd or 0):.1f} dni"
    return ""


def current_partner_details_from_display(display):
    pid = partner_map.get(display)
    if not pid:
        return None
    c = office_connect()
    row = c.execute("""
        SELECT id, name, address, post_city, country, tax_no,
               phone, email, contact_person, partner_type, pcd_days
        FROM partners
        WHERE id=?
    """, (pid,)).fetchone()
    c.close()
    return row


def choose_document_type():
    win = tk.Toplevel(root)
    win.title("MikroFIX Office - Nov dokument")
    win.geometry("520x315")
    win.resizable(False, False)
    win.configure(bg=BG)
    win.transient(root)
    win.grab_set()

    card = tk.Frame(
        win, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True, padx=24, pady=24)

    tk.Label(
        card,
        text="Izberi tip dokumenta",
        bg=PANEL, fg=DARK,
        font=("Segoe UI", 17, "bold")
    ).pack(anchor="w", padx=18, pady=(18,6))

    tk.Label(
        card,
        text="Po izbiri se odpre prvi korak za partnerja in podatke dokumenta.",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9)
    ).pack(anchor="w", padx=18, pady=(0,16))

    type_var = tk.StringVar(value=NEW_DOC_TYPES[0])
    combo = ttk.Combobox(
        card,
        textvariable=type_var,
        values=NEW_DOC_TYPES,
        state="readonly",
        font=("Segoe UI", 11),
        width=35
    )
    combo.pack(fill="x", padx=18, pady=6)

    def continue_to_header():
        selected = type_var.get().strip()
        if selected not in NEW_DOC_TYPES:
            return
        win.destroy()
        open_document_header_form(selected)

    continue_btn = ttk.Button(
        card,
        text="NADALJUJ",
        command=continue_to_header,
        style="Blue.TButton"
    )
    continue_btn.pack(anchor="e", padx=18, pady=(22,6))

    combo.bind(
        "<Return>",
        lambda e: (continue_to_header(), "break")[1]
    )
    combo.focus_set()


def open_document_header_form(doc_type, initial=None, callback=None):
    """1. korak: partner + podatki dokumenta."""
    refresh_partner_combo()
    refresh_claim_combo()

    initial = dict(initial or {})
    win = tk.Toplevel(root)
    win.title(f"MikroFIX Office - {doc_type}")
    win.geometry("860x690")
    win.minsize(780, 640)
    win.configure(bg=BG)
    win.transient(root)
    win.grab_set()

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=22, pady=18)

    tk.Label(
        outer,
        text=doc_type,
        bg=BG, fg=DARK,
        font=("Segoe UI", 19, "bold")
    ).pack(anchor="w")
    tk.Label(
        outer,
        text="1 / 2  •  Partner in podatki dokumenta",
        bg=BG, fg=GRAY,
        font=("Segoe UI", 9)
    ).pack(anchor="w", pady=(2,10))

    card = tk.Frame(
        outer, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True)

    partner_var = tk.StringVar(value=initial.get("partner_display", ""))
    external_var = tk.StringVar(value=initial.get("external_no", ""))
    date_var = tk.StringVar(value=initial.get("date", today_text()))
    order_date_var = tk.StringVar(value=initial.get("order_date", ""))
    received_date_var = tk.StringVar(value=initial.get("received_date", ""))
    due_date_var = tk.StringVar(value=initial.get("due_date", ""))
    currency_var = tk.StringVar(value=initial.get("currency", "EUR"))
    delivery_var = tk.StringVar(value=str(initial.get("delivery_cost", "0.00")))
    stock_var = tk.BooleanVar(
        value=initial.get("stock_effect", doc_stock_default_for_type(doc_type))
    )
    claim_var = tk.StringVar(value=initial.get("claim_display", ""))

    partner_box = tk.Frame(card, bg=PANEL)
    partner_box.pack(fill="x", padx=16, pady=(14,8))

    tk.Label(
        partner_box, text="Partner",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9, "bold")
    ).grid(row=0, column=0, sticky="w", padx=(0,8), pady=5)

    partner_combo = ttk.Combobox(
        partner_box,
        textvariable=partner_var,
        state="readonly",
        width=52,
        values=list(partner_map.keys())
    )
    partner_combo.grid(row=0, column=1, sticky="ew", pady=5)

    ttk.Button(
        partner_box,
        text="NOV PARTNER",
        command=lambda: open_partner_editor(
            on_saved_callback=lambda: (
                refresh_partner_combo(),
                partner_combo.configure(values=list(partner_map.keys()))
            )
        )
    ).grid(row=0, column=2, padx=(8,0), pady=5)

    partner_box.grid_columnconfigure(1, weight=1)

    partner_info_var = tk.StringVar(value="Izberi partnerja.")

    info_label = tk.Label(
        card,
        textvariable=partner_info_var,
        bg="#F7F9FA", fg=DARK,
        font=("Segoe UI", 9),
        justify="left", anchor="w",
        padx=12, pady=9
    )
    info_label.pack(fill="x", padx=16, pady=(0,10))

    def update_partner_info(_event=None):
        row = current_partner_details_from_display(partner_var.get())
        if not row:
            partner_info_var.set("Izberi partnerja.")
            return
        partner_info_var.set(
            f"{row[1]}  •  PČD {float(row[10] or 0):.1f} dni\n"
            f"{row[2] or '-'}, {row[3] or '-'}  •  {row[4] or '-'}\n"
            f"Davčna: {row[5] or '-'}  •  Telefon: {row[6] or '-'}  •  E-mail: {row[7] or '-'}"
        )

    partner_combo.bind("<<ComboboxSelected>>", update_partner_info)
    update_partner_info()

    form = tk.Frame(card, bg=PANEL)
    form.pack(fill="x", padx=16)

    fields = [
        ("Št. dobavnice / dokumenta", external_var),
        ("Datum dokumenta", date_var),
        ("Datum naročila", order_date_var),
        ("Datum prejema", received_date_var),
        ("Rok plačila", due_date_var),
        ("Valuta", currency_var),
        ("Dostava €", delivery_var),
    ]

    header_entries = []
    for i, (lab, var) in enumerate(fields):
        row = i // 2
        side = i % 2
        col = side * 2
        tk.Label(
            form, text=lab,
            bg=PANEL, fg=GRAY,
            font=("Segoe UI", 8, "bold")
        ).grid(row=row, column=col, sticky="w", padx=(0,6), pady=6)
        ent = ttk.Entry(
            form, textvariable=var
        )
        ent.grid(
            row=row, column=col+1, sticky="ew",
            padx=(0 if side == 0 else 8, 14), pady=6
        )
        header_entries.append(ent)

    form.grid_columnconfigure(1, weight=1)
    form.grid_columnconfigure(3, weight=1)

    claim_row = tk.Frame(card, bg=PANEL)
    claim_row.pack(fill="x", padx=16, pady=(4,5))

    tk.Label(
        claim_row,
        text="Servisna reklamacija",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8, "bold")
    ).pack(side="left")

    claim_combo = ttk.Combobox(
        claim_row,
        textvariable=claim_var,
        values=list(service_claim_map.keys()),
        state="readonly",
        width=62
    )
    claim_combo.pack(side="left", padx=(8,0), fill="x", expand=True)

    stock_check = tk.Checkbutton(
        card,
        text="Zalogo spreminja samo zavihek Prejem blaga",
        variable=stock_var,
        bg=PANEL, fg=DARK,
        activebackground=PANEL,
        font=("Segoe UI", 9),
        state="disabled",
    )
    stock_check.pack(anchor="w", padx=16, pady=(6,4))

    tk.Label(
        card, text="Opomba",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8, "bold")
    ).pack(anchor="w", padx=16, pady=(6,3))

    notes = tk.Text(card, height=5, wrap="word", font=("Segoe UI", 9))
    notes.pack(fill="both", expand=True, padx=16, pady=(0,12))
    notes.insert("1.0", initial.get("notes", ""))

    buttons = tk.Frame(outer, bg=BG)
    buttons.pack(fill="x", pady=(10,0))

    ttk.Button(
        buttons,
        text="PREKLIČI",
        command=win.destroy
    ).pack(side="left")

    def continue_next():
        if not partner_var.get().strip() and doc_type != "PREDRAČUN":
            if not messagebox.askyesno(
                "MikroFIX Office",
                "Partner ni izbran. Nadaljujem brez partnerja?",
                parent=win
            ):
                return

        try:
            if date_var.get().strip():
                normalize_filter_date(date_var.get())
            if order_date_var.get().strip():
                normalize_filter_date(order_date_var.get())
            if received_date_var.get().strip():
                normalize_filter_date(received_date_var.get())
            if due_date_var.get().strip():
                normalize_filter_date(due_date_var.get())
        except ValueError as e:
            messagebox.showwarning("MikroFIX Office", str(e), parent=win)
            return

        context = {
            "doc_type": doc_type,
            "partner_display": partner_var.get().strip(),
            "external_no": external_var.get().strip(),
            "date": normalize_filter_date(date_var.get()) if date_var.get().strip() else today_text(),
            "order_date": normalize_filter_date(order_date_var.get()) if order_date_var.get().strip() else "",
            "received_date": normalize_filter_date(received_date_var.get()) if received_date_var.get().strip() else "",
            "due_date": normalize_filter_date(due_date_var.get()) if due_date_var.get().strip() else "",
            "currency": currency_var.get().strip() or "EUR",
            "delivery_cost": max(0.0, safe_float(delivery_var.get())),
            "stock_effect": bool(stock_var.get()),
            "claim_display": claim_var.get().strip(),
            "notes": notes.get("1.0", "end").strip(),
            "linked_service_id": initial.get("linked_service_id"),
        }

        win.destroy()
        if callback:
            callback(context)
        else:
            open_document_items_window(context=context)

    continue_btn = ttk.Button(
        buttons,
        text="NADALJUJ",
        command=continue_next,
        style="Blue.TButton"
    )
    continue_btn.pack(side="right")

    # Keyboard-first potek: Enter IN Tab samo premakneta fokus na naslednje polje.
    # V večvrstični opombi Enter ostane nova vrstica; Tab gre na NADALJUJ.
    focus_chain = [partner_combo] + header_entries + [claim_combo, stock_check, notes, continue_btn]

    def focus_widget(widget):
        try:
            widget.focus_set()
            if isinstance(widget, ttk.Entry):
                widget.selection_range(0, "end")
            return "break"
        except Exception:
            return "break"

    def bind_next(widget, next_widget, bind_return=True, bind_tab=True):
        if bind_return:
            widget.bind(
                "<Return>",
                lambda e, w=next_widget: focus_widget(w)
            )
        if bind_tab:
            widget.bind(
                "<Tab>",
                lambda e, w=next_widget: focus_widget(w)
            )

    # Partner -> vsa polja -> reklamacija -> checkbox -> opomba.
    for i in range(len(focus_chain) - 1):
        widget = focus_chain[i]
        next_widget = focus_chain[i + 1]

        # Text: Enter mora ostati nova vrstica.
        if widget is notes:
            bind_next(widget, next_widget, bind_return=False, bind_tab=True)
        else:
            bind_next(widget, next_widget, bind_return=True, bind_tab=True)

    # Shift+Tab nazaj po istem zaporedju.
    for i in range(1, len(focus_chain)):
        widget = focus_chain[i]
        prev_widget = focus_chain[i - 1]
        widget.bind(
            "<Shift-Tab>",
            lambda e, w=prev_widget: focus_widget(w)
        )

    # Ctrl+Enter iz opombe neposredno nadaljuje.
    notes.bind(
        "<Control-Return>",
        lambda e: (continue_next(), "break")[1]
    )

    # Po odprtju je fokus na izbiri partnerja.
    win.after(100, partner_combo.focus_set)


def get_existing_document_data(doc_id):
    c = office_connect()
    doc = c.execute("""
        SELECT id, doc_no, doc_type, partner_id,
               partner_name_snapshot, partner_pcd_snapshot,
               external_no, date, order_date, received_date, due_date,
               currency, delivery_cost, stock_effect, status,
               linked_claim_id, linked_claim_no, notes, linked_service_id
        FROM documents
        WHERE id=?
    """, (doc_id,)).fetchone()

    items = c.execute("""
        SELECT article_id, barcode, article_name, internal_code,
               qty, unit_price, discount_pct, vat_pct, delivery_share,
               actual_unit_cost, margin_pct, calculated_price,
               recommended_price, sell_price, line_net, line_vat, line_total
        FROM document_items
        WHERE document_id=?
        ORDER BY id
    """, (doc_id,)).fetchall()
    c.close()

    if not doc:
        return None, []

    claim_display = ""
    if doc[15]:
        for display_v, data in service_claim_map.items():
            if data[0] == doc[15]:
                claim_display = display_v
                break

    context = {
        "doc_type": doc[2],
        "partner_display": partner_display_for_id(doc[3], doc[4], doc[5]),
        "external_no": doc[6] or "",
        "date": doc[7] or "",
        "order_date": doc[8] or "",
        "received_date": doc[9] or "",
        "due_date": doc[10] or "",
        "currency": doc[11] or "EUR",
        "delivery_cost": float(doc[12] or 0),
        "stock_effect": bool(doc[13]),
        "claim_display": claim_display,
        "notes": doc[17] or "",
        "linked_service_id": doc[18],
        "doc_no": doc[1],
        "status": doc[14],
    }
    return context, items


def load_document(doc_id=None):
    if doc_id is None:
        doc_id = get_selected_document_id()
    if not doc_id:
        messagebox.showwarning(
            "MikroFIX Office",
            "Najprej izberi dokument."
        )
        return

    context, items = get_existing_document_data(doc_id)
    if not context:
        return

    open_document_items_window(
        context=context,
        doc_id=doc_id,
        initial_items=items,
        status=context.get("status", "OSNUTEK")
    )


def new_document():
    choose_document_type()


def update_open_document_header(context):
    global doc_header_context, doc_notes_value, doc_linked_service_id
    doc_header_context = dict(context)
    doc_notes_value = context.get("notes", "")
    doc_linked_service_id = context.get("linked_service_id")

    doc_type_var.set(context.get("doc_type", doc_type_var.get()))
    doc_partner_var.set(context.get("partner_display", ""))
    doc_external_no_var.set(context.get("external_no", ""))
    doc_date_var.set(context.get("date", today_text()))
    doc_order_date_var.set(context.get("order_date", ""))
    doc_received_date_var.set(context.get("received_date", ""))
    doc_due_date_var.set(context.get("due_date", ""))
    doc_currency_var.set(context.get("currency", "EUR"))
    doc_delivery_var.set(str(context.get("delivery_cost", 0.0)))
    doc_stock_effect_var.set(bool(context.get("stock_effect", False)))
    doc_claim_var.set(context.get("claim_display", ""))

    update_document_editor_summary()
    recalc_doc_lines()


def edit_open_document_header():
    if current_doc_status == "KNJIŽENO":
        return
    open_document_header_form(
        doc_type_var.get(),
        initial=dict(doc_header_context or {}),
        callback=update_open_document_header
    )


def update_document_editor_summary():
    if "doc_editor_summary_var" not in globals():
        return
    partner = doc_partner_var.get().split("  |  ")[0] if doc_partner_var.get() else "-"
    doc_editor_summary_var.set(
        f"{doc_type_var.get()}  •  {partner}  •  "
        f"Datum: {doc_date_var.get() or '-'}  •  "
        f"Št. dokumenta: {doc_external_no_var.get() or '-'}  •  "
        f"Dostava: {money(doc_delivery_var.get())}"
    )


def on_editor_close():
    global doc_editor_window
    try:
        doc_editor_window.destroy()
    except Exception:
        pass
    doc_editor_window = None


def open_document_items_window(context, doc_id=None, initial_items=None, status="OSNUTEK"):
    global current_doc_id, current_doc_status, doc_editor_window
    global doc_no_display_var, doc_status_var, doc_type_var, doc_partner_var
    global doc_external_no_var, doc_date_var, doc_order_date_var
    global doc_received_date_var, doc_due_date_var, doc_currency_var
    global doc_delivery_var, doc_stock_effect_var, doc_claim_var
    global doc_notes_value, doc_header_context
    global doc_linked_service_id
    global doc_article_search_var, doc_article_search_entry
    global doc_article_count_var, doc_article_search_tree
    global doc_lines_tree
    global doc_total_net_var, doc_total_vat_var, doc_total_delivery_var, doc_total_all_var
    global doc_edit_entries, doc_edit_combos, doc_edit_buttons, doc_stock_check
    global doc_editor_summary_var

    close_document_editor()

    current_doc_id = doc_id
    current_doc_status = status or "OSNUTEK"
    doc_header_context = dict(context or {})
    doc_notes_value = context.get("notes", "")
    doc_linked_service_id = context.get("linked_service_id")

    win = tk.Toplevel(root)
    doc_editor_window = win
    win.title("MikroFIX Office - Vnos artiklov")
    win.geometry("1320x820")
    win.minsize(1080, 700)
    win.configure(bg=BG)
    win.transient(root)
    win.protocol("WM_DELETE_WINDOW", on_editor_close)

    try:
        win.state("zoomed")
    except Exception:
        pass

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=18, pady=14)

    # Vars expected by existing save/book logic.
    doc_no_display_var = tk.StringVar(
        value=context.get("doc_no", "NOV")
    )
    doc_status_var = tk.StringVar(value=current_doc_status)
    doc_type_var = tk.StringVar(value=context.get("doc_type", "PREJETI RAČUN"))
    doc_partner_var = tk.StringVar(value=context.get("partner_display", ""))
    doc_external_no_var = tk.StringVar(value=context.get("external_no", ""))
    doc_date_var = tk.StringVar(value=context.get("date", today_text()))
    doc_order_date_var = tk.StringVar(value=context.get("order_date", ""))
    doc_received_date_var = tk.StringVar(value=context.get("received_date", ""))
    doc_due_date_var = tk.StringVar(value=context.get("due_date", ""))
    doc_currency_var = tk.StringVar(value=context.get("currency", "EUR"))
    doc_delivery_var = tk.StringVar(value=str(context.get("delivery_cost", 0.0)))
    doc_stock_effect_var = tk.BooleanVar(value=bool(context.get("stock_effect", False)))
    doc_claim_var = tk.StringVar(value=context.get("claim_display", ""))

    head = tk.Frame(outer, bg=BG)
    head.pack(fill="x", pady=(0,8))

    tk.Label(
        head,
        textvariable=doc_no_display_var,
        bg=BG, fg=DARK,
        font=("Segoe UI", 18, "bold")
    ).pack(side="left")
    tk.Label(
        head,
        textvariable=doc_status_var,
        bg=BG, fg=BLUE_DARK,
        font=("Segoe UI", 10, "bold")
    ).pack(side="right")

    doc_editor_summary_var = tk.StringVar()
    tk.Label(
        outer,
        textvariable=doc_editor_summary_var,
        bg=PANEL, fg=DARK,
        font=("Segoe UI", 9),
        anchor="w", padx=12, pady=9,
        highlightthickness=1,
        highlightbackground=LIGHT_GRAY
    ).pack(fill="x", pady=(0,8))
    update_document_editor_summary()

    editor_actions = tk.Frame(outer, bg=BG)
    editor_actions.pack(fill="x", pady=(0,7))

    edit_header_btn = ttk.Button(
        editor_actions,
        text="PODATKI DOKUMENTA",
        command=edit_open_document_header
    )
    edit_header_btn.pack(side="left")

    tk.Label(
        editor_actions,
        text="2 / 2  •  Artikli in izračun",
        bg=BG, fg=GRAY,
        font=("Segoe UI", 9)
    ).pack(side="left", padx=12)

    doc_edit_entries = []
    doc_edit_combos = []
    doc_edit_buttons = [edit_header_btn]
    doc_stock_check = None

    # Enotno keyboard-first iskanje artiklov.
    panel = tk.Frame(
        outer, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    panel.pack(fill="x", pady=(0,8))

    search_top = tk.Frame(panel, bg=PANEL)
    search_top.pack(fill="x", padx=12, pady=(10,5))

    tk.Label(
        search_top,
        text="Poišči artikel",
        bg=PANEL, fg=DARK,
        font=("Segoe UI", 10, "bold")
    ).pack(side="left")

    tk.Label(
        search_top,
        text="vpiši del naziva ali barkode • Enter odpre prvi označen rezultat • ↑/↓ spremeni izbor",
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8)
    ).pack(side="left", padx=(12,0))

    doc_article_count_var = tk.StringVar(value="0 zadetkov")
    tk.Label(
        search_top,
        textvariable=doc_article_count_var,
        bg=PANEL, fg=GRAY,
        font=("Segoe UI", 8)
    ).pack(side="right")

    doc_article_search_var = tk.StringVar()
    doc_article_search_entry = ttk.Entry(
        panel,
        textvariable=doc_article_search_var,
        font=("Segoe UI", 12),
    )
    doc_article_search_entry.pack(
        fill="x", padx=12, pady=(0,8), ipady=5
    )

    doc_article_search_tree = ttk.Treeview(
        panel,
        columns=("id","barcode","name","code","stock","price","lot"),
        show="headings",
        height=6,
        selectmode="browse"
    )
    for col, txt, width in [
        ("id","ID",0),
        ("barcode","Barkoda",175),
        ("name","Artikel",330),
        ("code","Interna koda",140),
        ("stock","Zaloga",75),
        ("price","Trenutna cena",100),
        ("lot","LOT / serijska",190),
    ]:
        doc_article_search_tree.heading(col, text=txt)
        if col == "id":
            doc_article_search_tree.column(
                col, width=0, minwidth=0, stretch=False
            )
        else:
            doc_article_search_tree.column(col, width=width)

    doc_article_search_tree.pack(
        fill="x", padx=12, pady=(0,10)
    )

    doc_article_search_var.trace_add(
        "write", refresh_doc_article_search
    )
    doc_article_search_entry.bind(
        "<Return>", doc_article_search_enter
    )
    doc_article_search_entry.bind(
        "<Down>", lambda e: move_doc_article_selection(1)
    )
    doc_article_search_entry.bind(
        "<Up>", lambda e: move_doc_article_selection(-1)
    )
    doc_article_search_tree.bind(
        "<Double-1>", lambda e: doc_article_search_enter()
    )
    doc_article_search_tree.bind(
        "<Return>", doc_article_search_enter
    )

    manual_service_btn = ttk.Button(
        search_top,
        text="DODAJ DELO / STORITEV",
        command=add_manual_service_line,
    )
    manual_service_btn.pack(side="right", padx=(8, 12))
    doc_edit_buttons.append(manual_service_btn)

    # Lines table
    doc_lines_tree = ttk.Treeview(
        outer,
        columns=(
            "article_id","barcode","name","code","qty","price","discount","vat",
            "delivery","actual","margin","calc","recommended","sell",
            "net","vatval","total"
        ),
        show="headings",
        height=11
    )
    line_columns = [
        ("article_id","ID",0),
        ("barcode","Barkoda",105),
        ("name","Artikel",190),
        ("code","Koda",85),
        ("qty","Kol.",55),
        ("price","Cena",70),
        ("discount","Rab.%",55),
        ("vat","DDV",48),
        ("delivery","Dost.",58),
        ("actual","Dej.nab.",70),
        ("margin","Mar.%",58),
        ("calc","Izrač.",70),
        ("recommended","Prip./5",70),
        ("sell","Prod.",70),
        ("net","Neto",70),
        ("vatval","DDV €",65),
        ("total","Skupaj",75),
    ]
    for col, txt, width in line_columns:
        doc_lines_tree.heading(col, text=txt)
        if col == "article_id":
            doc_lines_tree.column(
                col, width=0, minwidth=0, stretch=False
            )
        else:
            doc_lines_tree.column(col, width=width, minwidth=45)

    doc_lines_tree.pack(fill="both", expand=True)

    if initial_items:
        for r in initial_items:
            doc_lines_tree.insert(
                "", "end",
                values=(
                    r[0] or "", r[1] or "", r[2] or "", r[3] or "-",
                    f"{float(r[4] or 0):.2f}",
                    f"{float(r[5] or 0):.2f}",
                    f"{float(r[6] or 0):.2f}",
                    f"{float(r[7] or 0):.2f}",
                    f"{float(r[8] or 0):.2f}",
                    f"{float(r[9] or 0):.2f}",
                    f"{float(r[10] or 0):.2f}",
                    f"{float(r[11] or 0):.2f}",
                    f"{float(r[12] or 0):.2f}",
                    f"{float(r[13] or 0):.2f}",
                    f"{float(r[14] or 0):.2f}",
                    f"{float(r[15] or 0):.2f}",
                    f"{float(r[16] or 0):.2f}",
                )
            )

    bottom = tk.Frame(outer, bg=BG)
    bottom.pack(fill="x", pady=(8,0))

    edit_line_btn = ttk.Button(
        bottom,
        text="UREDI IZBRANO",
        command=edit_selected_doc_line
    )
    edit_line_btn.pack(side="left")
    doc_edit_buttons.append(edit_line_btn)

    remove_btn = ttk.Button(
        bottom,
        text="IZBRIŠI IZBRANO",
        command=remove_doc_line
    )
    remove_btn.pack(side="left", padx=(6,0))
    doc_edit_buttons.append(remove_btn)

    doc_lines_tree.bind(
        "<Double-1>", edit_selected_doc_line
    )
    doc_lines_tree.bind(
        "<Return>", edit_selected_doc_line
    )
    doc_lines_tree.bind(
        "<F2>", edit_selected_doc_line
    )
    doc_lines_tree.bind(
        "<Delete>", remove_doc_line
    )

    draft_btn = ttk.Button(
        bottom,
        text="SHRANI OSNUTEK",
        command=lambda: save_document("OSNUTEK"),
        style="Blue.TButton"
    )
    draft_btn.pack(side="left", padx=(8,4))
    doc_edit_buttons.append(draft_btn)

    book_btn = ttk.Button(
        bottom,
        text="KNJIŽI DOKUMENT",
        command=book_document
    )
    book_btn.pack(side="left", padx=4)
    doc_edit_buttons.append(book_btn)

    ttk.Button(
        bottom,
        text="PDF / TISK",
        command=print_selected_document_pdf
    ).pack(side="left", padx=4)

    doc_total_net_var = tk.StringVar(value="0.00 €")
    doc_total_vat_var = tk.StringVar(value="0.00 €")
    doc_total_delivery_var = tk.StringVar(value="0.00 €")
    doc_total_all_var = tk.StringVar(value="0.00 €")

    totals = tk.Frame(bottom, bg=BG)
    totals.pack(side="right")

    for lab, var in [
        ("Neto", doc_total_net_var),
        ("DDV", doc_total_vat_var),
        ("Dostava", doc_total_delivery_var),
        ("Skupaj", doc_total_all_var),
    ]:
        f = tk.Frame(totals, bg=BG)
        f.pack(side="left", padx=8)
        tk.Label(
            f, text=lab,
            bg=BG, fg=GRAY,
            font=("Segoe UI", 7, "bold")
        ).pack()
        tk.Label(
            f, textvariable=var,
            bg=BG, fg=DARK,
            font=("Segoe UI", 9, "bold")
        ).pack()

    doc_delivery_var.trace_add(
        "write", lambda *_: (
            recalc_doc_lines(),
            update_document_editor_summary()
        )
    )

    refresh_doc_article_search()
    recalc_doc_lines()
    set_document_editable(current_doc_status != "KNJIŽENO")

    # Keyboard-first: po odprtju je kurzor takoj v enotnem iskalniku.
    if current_doc_status != "KNJIŽENO":
        win.after(100, lambda: doc_article_search_entry.focus_set())


def print_selected_document_pdf():
    editor = globals().get("doc_editor_window")
    editor_open = False
    if editor is not None:
        try:
            editor_open = bool(editor.winfo_exists())
        except Exception:
            editor_open = False

    doc_id = current_doc_id if editor_open else get_selected_document_id()
    if not doc_id:
        messagebox.showwarning(
            "MikroFIX Office",
            "Dokument mora biti najprej shranjen ali izbran v zgodovini."
        )
        return

    try:
        ensure_pos_schema(OFFICE_DB)
        connection = office_connect()
        pos_row = connection.execute(
            "SELECT pos_issue_token, print_status FROM documents WHERE id=?",
            (int(doc_id),),
        ).fetchone()
        connection.close()
        if pos_row and pos_row[0]:
            copy_no = 0
            if pos_row[1] == "PRINTED":
                copy_no = register_copy(OFFICE_DB, int(doc_id), RUNTIME.operator_code)
            company = dict(RUNTIME.settings.get("business", {}))
            company.update({"short_name": COMPANY_NAME, "address": COMPANY_ADDRESS})
            p, receipt_p = generate_pos_pdfs(
                office_db=OFFICE_DB,
                document_id=int(doc_id),
                output_dir=PDF_DIR,
                company=company,
                tax_note=TAX_INVOICE_NOTE,
                premise=RUNTIME.active_premise,
                device=RUNTIME.electronic_device,
                paper_width_mm=int(RUNTIME.settings["printer"]["paper_width_mm"]),
                copy_no=copy_no,
            )
        else:
            p = generate_document_pdf(doc_id)
            receipt_p = generate_document_receipt_pdf(doc_id)
    except Exception as e:
        messagebox.showerror("MikroFIX Office", str(e))
        return

    win = tk.Toplevel(root)
    win.title("MikroFIX Office - PDF")
    win.geometry("500x390")
    win.configure(bg=BG)
    win.transient(root)

    card = tk.Frame(
        win, bg=PANEL,
        highlightthickness=1, highlightbackground=LIGHT_GRAY
    )
    card.pack(fill="both", expand=True, padx=22, pady=22)

    tk.Label(
        card, text="Dokumenta sta pripravljena",
        bg=PANEL, fg=DARK, font=("Segoe UI", 15, "bold")
    ).pack(anchor="w", padx=16, pady=(16, 12))

    ttk.Button(
        card, text="ODPRI A4 PDF",
        command=lambda: os.startfile(str(p)) if os.name == "nt" else None,
        style="Blue.TButton"
    ).pack(fill="x", padx=16, pady=5)

    ttk.Button(
        card, text="ODPRI 58-MM TEST",
        command=lambda: os.startfile(str(receipt_p)) if os.name == "nt" else None,
        style="Blue.TButton"
    ).pack(fill="x", padx=16, pady=5)

    def save_as(source):
        target = filedialog.asksaveasfilename(
            parent=win,
            defaultextension=".pdf",
            initialfile=Path(source).name,
            filetypes=[("PDF", "*.pdf")]
        )
        if target:
            shutil.copy2(source, target)

    ttk.Button(
        card, text="SHRANI A4 KOT PDF", command=lambda: save_as(p)
    ).pack(fill="x", padx=16, pady=5)

    ttk.Button(
        card, text="SHRANI 58-MM KOT PDF", command=lambda: save_as(receipt_p)
    ).pack(fill="x", padx=16, pady=5)

    def print_it():
        if os.name == "nt":
            try:
                os.startfile(str(p), "print")
            except Exception as e:
                messagebox.showerror(
                    "MikroFIX Office", str(e), parent=win
                )

    ttk.Button(
        card, text="TISKAJ A4", command=print_it
    ).pack(fill="x", padx=16, pady=5)

    tk.Label(
        card,
        text="Izbran je Epson TM-T20II (M267D), 80 mm USB. Neposredni tisk vključimo po namestitvi uradnega gonilnika in fizičnem preizkusu.",
        bg=PANEL, fg=GRAY, font=("Segoe UI", 8), wraplength=430,
    ).pack(fill="x", padx=16, pady=(8, 12))


def _configured_furs_test_certificate_path():
    raw = str(RUNTIME.settings.get("furs", {}).get("test_certificate_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


def _configured_furs_test_ca_path():
    raw = str(RUNTIME.settings.get("furs", {}).get("test_ca_bundle_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


def _configured_furs_test_premise_id():
    return str(
        RUNTIME.settings.get("furs", {}).get("test_business_premise_id")
        or "TESTMFD"
    ).strip()


def _configured_furs_test_device_id():
    return str(
        RUNTIME.settings.get("furs", {}).get("test_electronic_device_id")
        or "PIS"
    ).strip()


def _save_furs_test_certificate_path(path):
    """Shrani samo pot do testnega .p12; gesla nikoli ne shranjuje."""
    settings_path = RUNTIME.settings_path
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.setdefault("furs", {})["test_certificate_path"] = str(Path(path).resolve())
    temporary = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, settings_path)
    protect_file_permissions(settings_path)
    RUNTIME.settings.setdefault("furs", {})["test_certificate_path"] = str(
        Path(path).resolve()
    )


def refresh_furs_test_console():
    if "furs_readiness_tree" not in globals():
        return
    for iid in furs_readiness_tree.get_children():
        furs_readiness_tree.delete(iid)
    for item in readiness_report(RUNTIME.settings, APP_DIR):
        furs_readiness_tree.insert(
            "", "end",
            values=(item["status"], item["label"], item["detail"]),
            tags=(item["status"],),
        )
    furs_readiness_tree.tag_configure("OK", foreground=SUCCESS)
    furs_readiness_tree.tag_configure("BLOCK", foreground=DANGER)
    furs_readiness_tree.tag_configure("INFO", foreground=GRAY)

    for iid in furs_test_events_tree.get_children():
        furs_test_events_tree.delete(iid)
    rows = furs_test_store().recent(100)
    for row in rows:
        outcome = row["eor"] or row["error_code"] or "-"
        furs_test_events_tree.insert(
            "", "end",
            values=(
                row["id"], row["created_at"], row["invoice_number"],
                f"{row['premise_id']}/{row['device_id']}", row["status"],
                row["attempt_count"], outcome,
            ),
            tags=(row["status"],),
        )
    furs_test_events_tree.tag_configure("SENT", foreground=SUCCESS)
    furs_test_events_tree.tag_configure("FAILED", foreground=DANGER)
    furs_test_events_tree.tag_configure("PENDING", foreground=WARNING)

    path = _configured_furs_test_certificate_path()
    furs_certificate_var.set(path.name if path and path.is_file() else "NI IZBRANO")


def choose_furs_test_certificate():
    selected = filedialog.askopenfilename(
        parent=root,
        title="Izberi FURS TEST potrdilo",
        filetypes=[("FURS potrdilo", "*.p12 *.pfx"), ("Vse datoteke", "*.*")],
    )
    if not selected:
        return
    password = simpledialog.askstring(
        "FURS TEST potrdilo",
        "Vpiši geslo testnega potrdila. Geslo ne bo shranjeno:",
        parent=root,
        show="•",
    )
    if password is None:
        return
    try:
        bundle = load_test_certificate(selected, password)
        _save_furs_test_certificate_path(selected)
    except FursTestError as exc:
        messagebox.showerror("FURS TEST", str(exc), parent=root)
        return
    audit_event(
        "FURS_TEST_CERTIFICATE_SELECTED",
        "furs_test",
        None,
        {
            "filename": Path(selected).name,
            "test_tax_last4": str(bundle.tax_number)[-4:],
            "valid_until": bundle.valid_until_text,
        },
    )
    furs_test_status_var.set(
        f"TESTNO potrdilo preverjeno • velja do {bundle.valid_until_text}"
    )
    refresh_furs_test_console()
    messagebox.showinfo(
        "FURS TEST",
        "Testno potrdilo je preverjeno. Shranjena je samo pot do datoteke; "
        "geslo ni bilo shranjeno.",
        parent=root,
    )


def _set_furs_test_busy(busy):
    state = "disabled" if busy else "normal"
    for widget in globals().get("furs_action_buttons", []):
        try:
            widget.configure(state=state)
        except Exception:
            pass


def _prompt_furs_test_bundle():
    path = _configured_furs_test_certificate_path()
    if path is None or not path.is_file():
        messagebox.showwarning(
            "FURS TEST",
            "Najprej izberi namensko FURS TEST potrdilo .p12. Produkcijsko "
            "potrdilo v tej konzoli ni dovoljeno.",
            parent=root,
        )
        return None
    password = simpledialog.askstring(
        "FURS TEST potrdilo",
        "Vpiši geslo testnega potrdila. Geslo se ne shranjuje:",
        parent=root,
        show="•",
    )
    if password is None:
        return None
    try:
        return load_test_certificate(path, password)
    except FursTestError as exc:
        messagebox.showerror("FURS TEST", str(exc), parent=root)
        return None


def _new_furs_test_client():
    return FursTestClient(
        timeout_seconds=int(
            RUNTIME.settings.get("furs", {}).get("test_timeout_seconds", 15)
        ),
        ca_bundle_path=_configured_furs_test_ca_path(),
        protect_file=protect_file_permissions,
    )


def start_furs_test_action(action):
    if RUNTIME.settings.get("environment") != "TEST":
        messagebox.showerror(
            "FURS TEST", "Ta funkcija je dovoljena samo v izdaji TEST.", parent=root
        )
        return
    bundle = _prompt_furs_test_bundle()
    if bundle is None:
        return

    _set_furs_test_busy(True)
    if action == "echo":
        furs_test_status_var.set("Povezovanje s FURS TEST • ECHO …")
    elif action == "register":
        furs_test_status_var.set("Prijavljam ločeni poslovni prostor samo v FURS TEST …")
    else:
        furs_test_status_var.set("Pripravljam in pošiljam anonimiziran račun 1,00 EUR …")

    def worker():
        queue_id = None
        try:
            client = _new_furs_test_client()
            if action == "echo":
                echoed = client.echo(bundle, "mikrofix")
                result = ("echo", echoed)
            elif action == "register":
                premise_id = _configured_furs_test_premise_id()
                device_id = _configured_furs_test_device_id()
                if furs_test_store().is_premise_registered(
                    bundle.tax_number, premise_id
                ):
                    result = ("premise_known", premise_id)
                else:
                    payload = build_test_business_premise(
                        bundle,
                        business_premise_id=premise_id,
                        software_supplier_tax_number=RUNTIME.settings.get(
                            "business", {}
                        ).get("tax_number", ""),
                    )
                    response, _request_token, raw_response = (
                        client.register_business_premise(bundle, payload)
                    )
                    if not response.ok:
                        raise FursTestError(
                            response.error_code or "FURS_PREMISE_REJECTED",
                            response.error_message
                            or "FURS TEST je zavrnil prijavo testnega prostora.",
                        )
                    furs_test_store().mark_premise_registered(
                        test_tax_number=bundle.tax_number,
                        premise_id=premise_id,
                        device_id=device_id,
                        result=response,
                        raw_response=raw_response,
                    )
                    result = ("premise", response, premise_id)
            elif action == "invoice":
                premise_id = _configured_furs_test_premise_id()
                device_id = _configured_furs_test_device_id()
                if not furs_test_store().is_premise_registered(
                    bundle.tax_number, premise_id
                ):
                    raise FursTestError(
                        "TEST_PREMISE_NOT_REGISTERED",
                        "Najprej uspešno prijavi testni poslovni prostor. "
                        "Produkcijska MFD/MFT v testnem okolju ne veljata.",
                    )
                payload = build_one_euro_test_invoice(
                    bundle,
                    business_premise_id=premise_id,
                    electronic_device_id=device_id,
                )
                queue_id = furs_test_store().enqueue(payload)
                response, _request_token, raw_response = client.submit_invoice(
                    bundle, payload
                )
                if response.ok:
                    furs_test_store().mark_sent(queue_id, response, raw_response)
                    result = ("invoice", response)
                else:
                    raise FursTestError(
                        response.error_code or "FURS_REJECTED",
                        response.error_message or "FURS TEST je zavrnil testni račun.",
                    )
            else:
                raise FursTestError("ACTION_UNKNOWN", "Neznano testno dejanje.")
        except Exception as exc:
            if queue_id is not None:
                try:
                    code = getattr(exc, "code", type(exc).__name__)
                    furs_test_store().mark_failed(queue_id, code, str(exc))
                except Exception:
                    pass
            root.after(0, lambda e=exc: finish_error(e))
            return
        root.after(0, lambda value=result: finish_success(value))

    def finish_success(result):
        _set_furs_test_busy(False)
        refresh_furs_test_console()
        if result[0] == "echo":
            furs_test_status_var.set("FURS TEST POVEZAN • odgovor ECHO prejet")
            audit_event("FURS_TEST_ECHO_OK", "furs_test", None, {"endpoint": FURS_TEST_BASE})
            messagebox.showinfo(
                "FURS TEST",
                "Dvosmerna šifrirana povezava deluje. FURS TEST je odgovoril nazaj.",
                parent=root,
            )
        elif result[0] in {"premise", "premise_known"}:
            premise_id = result[-1]
            furs_test_status_var.set(
                f"FURS TEST PROSTOR PRIPRAVLJEN • {premise_id}/"
                f"{_configured_furs_test_device_id()}"
            )
            audit_event(
                "FURS_TEST_PREMISE_READY",
                "furs_test",
                None,
                {
                    "premise_id": premise_id,
                    "device_id": _configured_furs_test_device_id(),
                    "already_registered": result[0] == "premise_known",
                },
            )
            messagebox.showinfo(
                "FURS TEST",
                "Ločeni testni poslovni prostor je pripravljen.\n\n"
                f"Prostor: {premise_id}\n"
                f"Naprava: {_configured_furs_test_device_id()}\n\n"
                "To ne spremeni produkcijskih MFD/MFT.",
                parent=root,
            )
        else:
            response = result[1]
            furs_test_status_var.set(f"FURS TEST POTRJENO • EOR {response.eor}")
            audit_event(
                "FURS_TEST_INVOICE_CONFIRMED",
                "furs_test",
                None,
                {"eor": response.eor, "message_id": response.message_id},
            )
            messagebox.showinfo(
                "FURS TEST",
                "Anonimiziran testni račun je potrjen.\n\n"
                f"EOR: {response.eor}\n\n"
                "To ni pravi račun in nima pravnega ali računovodskega učinka.",
                parent=root,
            )

    def finish_error(exc):
        _set_furs_test_busy(False)
        refresh_furs_test_console()
        code = getattr(exc, "code", type(exc).__name__)
        furs_test_status_var.set(f"FURS TEST NAPAKA • {code}")
        audit_event(
            "FURS_TEST_FAILED",
            "furs_test",
            None,
            {"code": code, "message": str(exc)[:500]},
        )
        messagebox.showerror("FURS TEST", str(exc), parent=root)

    threading.Thread(target=worker, daemon=True, name=f"FURS-TEST-{action}").start()


# ----------------------------------------------------------------------
# TEDENSKO INTERNO POROČILO ODKUPNIH CEN
# ----------------------------------------------------------------------

weekly_report_busy = False


def _open_local_path(path):
    path = Path(path)
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def refresh_weekly_report_view(*_args):
    if "weekly_report_tree" not in globals():
        return
    for iid in weekly_report_tree.get_children():
        weekly_report_tree.delete(iid)
    try:
        ensure_weekly_inputs(WEEKLY_INPUT_PATH, WEEKLY_SEED_PATH)
        protect_file_permissions(WEEKLY_INPUT_PATH)
        payload = load_weekly_inputs(WEEKLY_INPUT_PATH)
        rows = evaluate_weekly_inputs(payload, WEEKLY_SETTINGS, as_of=date.today())
    except Exception as exc:
        weekly_report_status_var.set(f"BLOKIRANO • {exc}")
        weekly_report_last_var.set("Zadnji PDF: ni podatka")
        return

    valid = 0
    invalid_details = []
    for row in rows:
        if row.status == "OK":
            valid += 1
            reference = money(row.reference_eur)
            refurbishment = money(row.refurbishment_eur)
            recommended = money(row.recommended_eur)
            maximum = money(row.maximum_eur)
        else:
            reference = refurbishment = recommended = maximum = "N/A"
            invalid_details.append(f"{row.model}: {row.detail}")
        weekly_report_tree.insert(
            "",
            "end",
            values=(
                f"{row.model} {row.storage}",
                reference,
                refurbishment,
                recommended,
                maximum,
                row.source_count,
                row.status,
            ),
            tags=(row.status,),
        )
    weekly_report_tree.tag_configure("OK", foreground=SUCCESS)
    weekly_report_tree.tag_configure("N/A", foreground=DANGER)

    monday, sunday = weekly_week_bounds(date.today())
    if valid:
        status = (
            f"PRIPRAVLJENO • {valid}/{len(rows)} veljavnih modelov • "
            f"obdobje {monday.strftime('%d.%m.')}-{sunday.strftime('%d.%m.%Y')}"
        )
        if invalid_details:
            status += f" • {len(invalid_details)} vrstic N/A"
    else:
        detail = invalid_details[0] if invalid_details else "Ni veljavnih vhodnih podatkov."
        status = f"BLOKIRANO • {detail}"
    weekly_report_status_var.set(status)

    latest = latest_weekly_report(WEEKLY_REPORT_DIR)
    weekly_report_last_var.set(
        f"Zadnji PDF: {latest.name}" if latest else "Zadnji PDF: še ni ustvarjen"
    )


def _set_weekly_report_busy(busy, text=None):
    global weekly_report_busy
    weekly_report_busy = bool(busy)
    state = "disabled" if busy else "normal"
    for widget in globals().get("weekly_report_action_buttons", []):
        try:
            widget.configure(state=state)
        except Exception:
            pass
    if text and "weekly_report_status_var" in globals():
        weekly_report_status_var.set(text)


def create_weekly_report(show_message=True):
    try:
        ensure_weekly_inputs(WEEKLY_INPUT_PATH, WEEKLY_SEED_PATH)
        path, rows = generate_weekly_pdf(
            WEEKLY_INPUT_PATH,
            WEEKLY_REPORT_DIR,
            WEEKLY_SETTINGS,
            as_of=date.today(),
            protect=protect_file_permissions,
        )
    except Exception as exc:
        audit_event(
            "WEEKLY_REPORT_BLOCKED", "weekly_report", None,
            {"message": str(exc)[:500]},
        )
        refresh_weekly_report_view()
        if show_message:
            messagebox.showerror("Tedensko poročilo", str(exc), parent=root)
        return None

    audit_event(
        "WEEKLY_REPORT_CREATED",
        "weekly_report",
        path.name,
        {
            "formula": WEEKLY_FORMULA_VERSION,
            "valid_rows": sum(row.status == "OK" for row in rows),
            "invalid_rows": sum(row.status != "OK" for row in rows),
        },
    )
    refresh_weekly_report_view()
    if show_message:
        messagebox.showinfo(
            "Tedensko poročilo",
            f"Zasebni PDF je pripravljen:\n\n{path}",
            parent=root,
        )
    return path


def start_weekly_online_refresh(*, create_pdf_after=False, automatic=False):
    if weekly_report_busy:
        return
    _set_weekly_report_busy(True, "OSVEŽUJEM • berem javne cene …")

    def progress(done, total):
        try:
            root.after(
                0,
                lambda d=done, t=total: weekly_report_status_var.set(
                    f"OSVEŽUJEM • vir {d}/{t} …"
                ),
            )
        except Exception:
            pass

    def worker():
        report_path = None
        try:
            ensure_weekly_inputs(WEEKLY_INPUT_PATH, WEEKLY_SEED_PATH)
            result = refresh_weekly_public_prices(
                WEEKLY_INPUT_PATH,
                timeout=float(WEEKLY_SETTINGS.get("timeout_seconds", 15)),
                user_agent=str(
                    WEEKLY_SETTINGS.get("user_agent")
                    or "MikroFIX/28 TEST weekly-price-check"
                ),
                as_of=date.today(),
                progress=progress,
            )
            protect_file_permissions(WEEKLY_INPUT_PATH)
            if create_pdf_after:
                report_path, _rows = generate_weekly_pdf(
                    WEEKLY_INPUT_PATH,
                    WEEKLY_REPORT_DIR,
                    WEEKLY_SETTINGS,
                    as_of=date.today(),
                    protect=protect_file_permissions,
                )
            error = None
        except Exception as exc:
            result = None
            error = exc
        try:
            root.after(
                0,
                lambda r=result, e=error, p=report_path: finish(r, e, p),
            )
        except Exception:
            pass

    def finish(result, error, report_path):
        _set_weekly_report_busy(False)
        refresh_weekly_report_view()
        if error is not None:
            audit_event(
                "WEEKLY_REPORT_REFRESH_FAILED",
                "weekly_report",
                None,
                {"message": str(error)[:500], "automatic": bool(automatic)},
            )
            weekly_report_status_var.set(f"OSVEŽITEV NI USPELA • {error}")
            if not automatic:
                messagebox.showerror("Tedensko poročilo", str(error), parent=root)
            return
        audit_event(
            "WEEKLY_REPORT_REFRESHED",
            "weekly_report",
            report_path.name if report_path else None,
            {
                "requested": result["requested"],
                "succeeded": result["succeeded"],
                "failed": result["failed"],
                "automatic": bool(automatic),
                "pdf_created": bool(report_path),
            },
        )
        if report_path:
            weekly_report_status_var.set(
                f"PONEDELJKOV PDF PRIPRAVLJEN • {report_path.name}"
            )
        elif not automatic:
            messagebox.showinfo(
                "Tedensko poročilo",
                f"Osvežitev končana: {result['succeeded']} uspešnih, "
                f"{result['failed']} neuspešnih virov.",
                parent=root,
            )

    threading.Thread(
        target=worker,
        daemon=True,
        name="MikroFIX-weekly-report-refresh",
    ).start()


def open_latest_weekly_report():
    path = latest_weekly_report(WEEKLY_REPORT_DIR)
    if path is None:
        messagebox.showinfo(
            "Tedensko poročilo",
            "Tedenski PDF še ni ustvarjen. Klikni USTVARI TEDENSKI PDF.",
            parent=root,
        )
        return
    _open_local_path(path)


def open_weekly_report_folder():
    WEEKLY_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    _open_local_path(WEEKLY_REPORT_DIR)


def run_monday_weekly_report_if_due():
    if not bool(WEEKLY_SETTINGS.get("enabled", True)):
        return
    if not bool(WEEKLY_SETTINGS.get("automatic_monday", True)):
        return
    today = date.today()
    if today.weekday() != 0 or weekly_report_for_week(WEEKLY_REPORT_DIR, today):
        return
    start_weekly_online_refresh(create_pdf_after=True, automatic=True)



# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

init_inventory_db()
init_office_db()
ensure_pos_schema(OFFICE_DB)
initialize_v29(
    inventory_db=INVENTORY_DB,
    office_db=OFFICE_DB,
    service_db=SERVICE_DB,
    pricing=RUNTIME.settings["pricing"],
)

EMBEDDED_IN_MAIN = "MIKROFIX_PARENT" in globals() and MIKROFIX_PARENT is not None
root = tk.Toplevel(MIKROFIX_PARENT) if EMBEDDED_IN_MAIN else tk.Tk()
root.title(f"MikroFIX Office {RUNTIME.version_label}")
root.geometry("1280x760")
root.minsize(900, 620)
root.configure(bg=BG)
root.withdraw()

try:
    root.state("zoomed")
except Exception:
    pass

style_app()

outer = tk.Frame(root, bg=BG)
outer.pack(fill="both", expand=True, padx=20, pady=14)

header = tk.Frame(outer, bg=BG)
header.pack(fill="x", pady=(0, 10))

brand_left = tk.Frame(header, bg=BG)
brand_left.pack(side="left")

brand_logo = tk.Frame(brand_left, bg=BG)
brand_logo.pack(anchor="w")

tk.Label(
    brand_logo, text="Mikro", bg=BG, fg=GRAY,
    font=("Segoe UI", 25, "bold")
).pack(side="left")
tk.Label(
    brand_logo, text="FIX", bg=BG, fg=BLUE,
    font=("Segoe UI", 25, "bold")
).pack(side="left")

tk.Label(
    brand_left, text="OFFICE",
    bg=BG, fg=GRAY,
    font=("Segoe UI", 8, "bold")
).pack(anchor="center", pady=(0,0))

tk.Label(
    header, text=TEST_NOTICE,
    bg=BG, fg=DANGER, font=("Segoe UI", 9, "bold")
).pack(side="left", padx=(24, 0), pady=(12, 0))

ttk.Button(
    header, text="ODJAVA",
    command=logout_and_lock
).pack(side="right", padx=(8,0), pady=(6,0))

ttk.Button(
    header, text="SPREMENI PIN",
    command=lambda: change_pin_dialog(root)
).pack(side="right", padx=(8,0), pady=(6,0))

users_button = ttk.Button(
    header, text="UPORABNIKI",
    command=lambda: manage_users_dialog(root)
)
users_button.pack(side="right", padx=(8,0), pady=(6,0))
AUTH_SESSION.bind_admin_widget(users_button)

ttk.Button(
    header, text="POSODOBI SKUPNE PODATKE",
    command=lambda: refresh_all_shared_data(True)
).pack(side="right", pady=(6,0))

current_user_var = tk.StringVar()
AUTH_SESSION.bind_caption_variable(current_user_var)
tk.Label(
    header, textvariable=current_user_var,
    bg=BG, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="right", padx=(8,12), pady=(12,0))

nb = ttk.Notebook(outer)
nb.pack(fill="both", expand=True)

tab_receipts = tk.Frame(nb, bg=PANEL)
tab_stock_control = tk.Frame(nb, bg=PANEL)
tab_used_devices = tk.Frame(nb, bg=PANEL)
tab_price_profiles = tk.Frame(nb, bg=PANEL)
tab_documents = tk.Frame(nb, bg=PANEL)
tab_partners = tk.Frame(nb, bg=PANEL)
tab_articles = tk.Frame(nb, bg=PANEL)
tab_services = tk.Frame(nb, bg=PANEL)
tab_reports = tk.Frame(nb, bg=PANEL)
tab_furs_test = tk.Frame(nb, bg=PANEL)

nb.add(tab_stock_control, text="Zaloga")
nb.add(tab_used_devices, text="Odkup")
nb.add(tab_price_profiles, text="Cene")
nb.add(tab_documents, text="Dokumenti")
nb.add(tab_partners, text="Partnerji")
nb.add(tab_articles, text="Artikli")
nb.add(tab_services, text="Servisi")
if WEEKLY_SETTINGS.get("enabled", False):
    nb.add(tab_reports, text="Poročila")
nb.add(tab_furs_test, text="FURS TEST")

# Prejem je del iste dokumentne verige. Na vrhu zato ni več ločenega
# zavihka; znotraj Dokumentov sta pregled vseh listin in fizični prejem.
documents_workspace = ttk.Notebook(tab_documents)
documents_workspace.pack(fill="both", expand=True)
tab_document_list = tk.Frame(documents_workspace, bg=PANEL)
tab_document_receipts = tk.Frame(documents_workspace, bg=PANEL)
documents_workspace.add(tab_document_list, text="Vsi dokumenti")
documents_workspace.add(tab_document_receipts, text="Prejem blaga")

V29_RECEIPTS = build_receipts_tab(
    tab_document_receipts,
    office_db=OFFICE_DB,
    inventory_db=INVENTORY_DB,
    pricing=RUNTIME.settings["pricing"],
    operator_code=RUNTIME.operator_code,
    colors={
        "bg": BG, "panel": PANEL, "blue": BLUE, "gray": GRAY,
        "dark": DARK, "light_gray": LIGHT_GRAY, "danger": DANGER,
    },
    on_inventory_changed=lambda: refresh_office_articles(),
)
V29_STOCK_CONTROL = build_stock_control_tab(
    tab_stock_control,
    inventory_db=INVENTORY_DB,
    operator_code=RUNTIME.operator_code,
    colors={
        "bg": BG, "panel": PANEL, "blue": BLUE, "gray": GRAY,
        "dark": DARK, "light_gray": LIGHT_GRAY, "danger": DANGER,
    },
    on_changed=lambda: refresh_office_articles(),
)
V29_USED_DEVICES = build_used_devices_tab(
    tab_used_devices,
    service_db=SERVICE_DB,
    inventory_db=INVENTORY_DB,
    operator_code=RUNTIME.operator_code,
    colors={
        "bg": BG, "panel": PANEL, "blue": BLUE, "gray": GRAY,
        "dark": DARK, "light_gray": LIGHT_GRAY, "danger": DANGER,
    },
    on_changed=lambda: refresh_office_articles(),
)
V29_PRICE_PROFILES = build_price_profiles_tab(
    tab_price_profiles,
    inventory_db=INVENTORY_DB,
    colors={
        "bg": BG, "panel": PANEL, "blue": BLUE, "gray": GRAY,
        "dark": DARK, "light_gray": LIGHT_GRAY, "danger": DANGER,
    },
)


# ----- POROČILA
weekly_header = tk.Frame(tab_reports, bg=PANEL)
weekly_header.pack(fill="x", padx=18, pady=(16, 8))

tk.Label(
    weekly_header,
    text="TEDENSKI PREGLED ODKUPNIH CEN",
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 17, "bold"),
).pack(side="left")

tk.Label(
    weekly_header,
    text="ZA INTERNO UPORABO",
    bg="#FCECEC", fg=DANGER,
    font=("Segoe UI", 9, "bold"),
    padx=10, pady=5,
).pack(side="right")

weekly_report_status_var = tk.StringVar(value="Pripravljam pregled vhodnih cen …")
tk.Label(
    tab_reports,
    textvariable=weekly_report_status_var,
    bg="#F7F9FA", fg=BLUE_DARK,
    font=("Segoe UI", 11, "bold"),
    anchor="w", padx=14, pady=10,
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
).pack(fill="x", padx=18, pady=(0, 8))

weekly_report_last_var = tk.StringVar(value="Zadnji PDF: še ni ustvarjen")
tk.Label(
    tab_reports,
    textvariable=weekly_report_last_var,
    bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9),
    anchor="w",
).pack(fill="x", padx=18, pady=(0, 10))

weekly_actions = tk.Frame(tab_reports, bg=PANEL)
weekly_actions.pack(fill="x", padx=18, pady=(0, 12))
weekly_report_action_buttons = []

weekly_refresh_btn = ttk.Button(
    weekly_actions,
    text="OSVEŽI SPLETNE CENE",
    command=lambda: start_weekly_online_refresh(),
    style="Blue.TButton",
)
weekly_refresh_btn.pack(side="left")
weekly_report_action_buttons.append(weekly_refresh_btn)

weekly_create_btn = ttk.Button(
    weekly_actions,
    text="USTVARI TEDENSKI PDF",
    command=create_weekly_report,
)
weekly_create_btn.pack(side="left", padx=(8, 0))
weekly_report_action_buttons.append(weekly_create_btn)

weekly_open_btn = ttk.Button(
    weekly_actions,
    text="ODPRI ZADNJI PDF",
    command=open_latest_weekly_report,
)
weekly_open_btn.pack(side="left", padx=(8, 0))
weekly_report_action_buttons.append(weekly_open_btn)

weekly_folder_btn = ttk.Button(
    weekly_actions,
    text="ODPRI MAPO",
    command=open_weekly_report_folder,
)
weekly_folder_btn.pack(side="left", padx=(8, 0))
weekly_report_action_buttons.append(weekly_folder_btn)

ttk.Button(
    weekly_actions,
    text="OSVEŽI PREGLED",
    command=refresh_weekly_report_view,
).pack(side="right")

tk.Label(
    tab_reports,
    text=(
        "Ob ponedeljkovem zagonu Office najprej poskusi osvežiti javne cene, nato "
        "ustvari en zasebni PDF za tekoči teden. Če sta za model na voljo manj kot "
        "dva primerljiva vira ali je vir starejši od 8 dni, je vrstica N/A. "
        "Program poročila ne pošilja in ne izvede odkupa."
    ),
    bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9),
    justify="left", anchor="w", wraplength=1250,
).pack(fill="x", padx=18, pady=(0, 12))

weekly_report_tree_frame = tk.Frame(tab_reports, bg=PANEL)
weekly_report_tree_frame.pack(fill="both", expand=True, padx=18, pady=(0, 12))
weekly_report_tree = ttk.Treeview(
    weekly_report_tree_frame,
    columns=("model", "reference", "refurb", "recommended", "maximum", "sources", "status"),
    show="headings",
)
for col, label, width in [
    ("model", "Model / kapaciteta", 240),
    ("reference", "EU referenca", 130),
    ("refurb", "Standardna obnova", 150),
    ("recommended", "Priporočen odkup", 150),
    ("maximum", "MAX odkup", 130),
    ("sources", "Viri", 70),
    ("status", "Stanje", 90),
]:
    weekly_report_tree.heading(col, text=label)
    weekly_report_tree.column(col, width=width, minwidth=60)
weekly_report_tree.pack(side="left", fill="both", expand=True)
weekly_report_scroll = ttk.Scrollbar(
    weekly_report_tree_frame, orient="vertical", command=weekly_report_tree.yview
)
weekly_report_scroll.pack(side="right", fill="y")
weekly_report_tree.configure(yscrollcommand=weekly_report_scroll.set)

tk.Label(
    tab_reports,
    text=(
        f"Formula {WEEKLY_FORMULA_VERSION}: robustna mediana javnih ponudb − standardna "
        "obnova − varnostni prostor. MAX je zaokrožen navzdol; priporočeni odkup "
        "vključuje še pogajalsko rezervo. Celotna sled izračuna se shrani ob PDF."
    ),
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI", 8),
    justify="left", anchor="w", padx=12, pady=9,
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
).pack(fill="x", padx=18, pady=(0, 16))


# ----- FURS TEST
furs_test_header = tk.Frame(tab_furs_test, bg=PANEL)
furs_test_header.pack(fill="x", padx=18, pady=(16, 8))

tk.Label(
    furs_test_header,
    text="FURS TESTNA KONZOLA",
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 17, "bold"),
).pack(side="left")

tk.Label(
    furs_test_header,
    text="PRODUKCIJA JE KONSTRUKCIJSKO ZAKLENJENA",
    bg="#FCECEC", fg=DANGER,
    font=("Segoe UI", 9, "bold"),
    padx=10, pady=5,
).pack(side="right")

furs_test_status_var = tk.StringVar(
    value="Pripravljeno za lokalni pregled; omrežje še ni uporabljeno."
)
tk.Label(
    tab_furs_test,
    textvariable=furs_test_status_var,
    bg="#F7F9FA", fg=BLUE_DARK,
    font=("Segoe UI", 11, "bold"),
    anchor="w", padx=14, pady=10,
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
).pack(fill="x", padx=18, pady=(0, 10))

furs_cert_row = tk.Frame(tab_furs_test, bg=PANEL)
furs_cert_row.pack(fill="x", padx=18, pady=(0, 10))
tk.Label(
    furs_cert_row,
    text="Testno potrdilo:", bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9, "bold"),
).pack(side="left")
furs_certificate_var = tk.StringVar(value="NI IZBRANO")
tk.Label(
    furs_cert_row,
    textvariable=furs_certificate_var,
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 9),
).pack(side="left", padx=(6, 18))
tk.Label(
    furs_cert_row,
    text=f"Uradna specifikacija: {FURS_TECHNICAL_VERSION}",
    bg=PANEL, fg=GRAY,
    font=("Segoe UI", 8),
).pack(side="right")

furs_action_row = tk.Frame(tab_furs_test, bg=PANEL)
furs_action_row.pack(fill="x", padx=18, pady=(0, 12))
furs_action_buttons = []

furs_select_cert_btn = ttk.Button(
    furs_action_row,
    text="1  IZBERI TESTNO POTRDILO",
    command=choose_furs_test_certificate,
    style="Blue.TButton",
)
furs_select_cert_btn.pack(side="left")
furs_action_buttons.append(furs_select_cert_btn)

furs_echo_btn = ttk.Button(
    furs_action_row,
    text="2  PREVERI POVEZAVO (ECHO)",
    command=lambda: start_furs_test_action("echo"),
)
furs_echo_btn.pack(side="left", padx=(8, 0))
furs_action_buttons.append(furs_echo_btn)

furs_register_btn = ttk.Button(
    furs_action_row,
    text="3  PRIJAVI TESTNI PROSTOR",
    command=lambda: start_furs_test_action("register"),
)
furs_register_btn.pack(side="left", padx=(8, 0))
furs_action_buttons.append(furs_register_btn)

furs_invoice_btn = ttk.Button(
    furs_action_row,
    text="4  POŠLJI TESTNI RAČUN 1,00 EUR",
    command=lambda: start_furs_test_action("invoice"),
)
furs_invoice_btn.pack(side="left", padx=(8, 0))
furs_action_buttons.append(furs_invoice_btn)

furs_refresh_btn = ttk.Button(
    furs_action_row,
    text="OSVEŽI PREGLED",
    command=refresh_furs_test_console,
)
furs_refresh_btn.pack(side="right")
furs_action_buttons.append(furs_refresh_btn)

tk.Label(
    tab_furs_test,
    text=(
        "Testno potrdilo pridobi razvijalec pri FURS (sd.fu@gov.si). "
        "Izdano je na anonimizirano testno davčno številko. Geslo se ne shrani. "
        "Testni EOR nima pravnega ali računovodskega učinka."
    ),
    bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9),
    justify="left", anchor="w", wraplength=1250,
).pack(fill="x", padx=18, pady=(0, 12))

tk.Label(
    tab_furs_test,
    text="PRIPRAVLJENOST",
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 10, "bold"),
).pack(anchor="w", padx=18)

furs_readiness_tree = ttk.Treeview(
    tab_furs_test,
    columns=("status", "area", "detail"),
    show="headings",
    height=6,
)
for col, label, width in [
    ("status", "Stanje", 90),
    ("area", "Področje", 180),
    ("detail", "Ugotovitev", 820),
]:
    furs_readiness_tree.heading(col, text=label)
    furs_readiness_tree.column(col, width=width, minwidth=70)
furs_readiness_tree.pack(fill="x", padx=18, pady=(5, 14))

tk.Label(
    tab_furs_test,
    text="LOKALNA TESTNA SLED IN ČAKALNA VRSTA",
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 10, "bold"),
).pack(anchor="w", padx=18)

furs_events_frame = tk.Frame(tab_furs_test, bg=PANEL)
furs_events_frame.pack(fill="both", expand=True, padx=18, pady=(5, 16))
furs_test_events_tree = ttk.Treeview(
    furs_events_frame,
    columns=("id", "time", "number", "device", "status", "attempts", "result"),
    show="headings",
)
for col, label, width in [
    ("id", "ID", 55),
    ("time", "Ustvarjeno", 180),
    ("number", "Testna št.", 150),
    ("device", "Prostor/naprava", 130),
    ("status", "Stanje", 100),
    ("attempts", "Poskusi", 70),
    ("result", "EOR ali napaka", 360),
]:
    furs_test_events_tree.heading(col, text=label)
    furs_test_events_tree.column(col, width=width, minwidth=50)
furs_test_events_tree.pack(side="left", fill="both", expand=True)
furs_events_scroll = ttk.Scrollbar(
    furs_events_frame, orient="vertical", command=furs_test_events_tree.yview
)
furs_events_scroll.pack(side="right", fill="y")
furs_test_events_tree.configure(yscrollcommand=furs_events_scroll.set)


# ----- PARTNERJI
partner_top = tk.Frame(tab_partners, bg=PANEL)
partner_top.pack(fill="x", padx=10, pady=10)

partner_search_var = tk.StringVar()
partner_count_var = tk.StringVar(value="0 partnerjev")

tk.Label(
    partner_top, text="Iskanje",
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="left")

ttk.Entry(
    partner_top, textvariable=partner_search_var, width=34
).pack(side="left", padx=(6, 12))

ttk.Button(
    partner_top, text="NOV PARTNER",
    command=partner_add, style="Blue.TButton"
).pack(side="left")

ttk.Button(
    partner_top, text="UREDI",
    command=partner_edit
).pack(side="left", padx=5)

ttk.Button(
    partner_top, text="IZBRIŠI",
    command=partner_delete
).pack(side="left", padx=5)

tk.Label(
    partner_top, textvariable=partner_count_var,
    bg=PANEL, fg=GRAY
).pack(side="right")

partner_tree = ttk.Treeview(
    tab_partners,
    columns=("id","name","type","pcd","phone","email","tax"),
    show="headings"
)
for ccol, txt, width in [
    ("id","ID",0), ("name","Partner",280), ("type","Tip",110),
    ("pcd","PČD [dni]",90), ("phone","Telefon",130),
    ("email","E-mail",220), ("tax","Davčna",150)
]:
    partner_tree.heading(ccol, text=txt)
    if ccol == "id":
        partner_tree.column(ccol, width=0, minwidth=0, stretch=False)
    else:
        partner_tree.column(ccol, width=width)
partner_tree.pack(fill="both", expand=True, padx=10, pady=(0,10))
partner_tree.bind("<Double-1>", partner_edit)
partner_search_var.trace_add("write", refresh_partners)


# ----- ARTIKLI
article_top = tk.Frame(tab_articles, bg=PANEL)
article_top.pack(fill="x", padx=10, pady=10)

article_name_search_var = tk.StringVar()
article_barcode_search_var = tk.StringVar()
article_count_var = tk.StringVar(value="0 artiklov")

for lab, var, width in [
    ("Naziv", article_name_search_var, 30),
    ("Barkoda", article_barcode_search_var, 26)
]:
    tk.Label(
        article_top, text=lab, bg=PANEL, fg=GRAY,
        font=("Segoe UI", 9, "bold")
    ).pack(side="left")
    ttk.Entry(
        article_top, textvariable=var, width=width
    ).pack(side="left", padx=(6, 14))

ttk.Button(
    article_top, text="POSODOBI",
    command=refresh_office_articles
).pack(side="left")

ttk.Button(
    article_top, text="SLED ARTIKLA",
    command=show_selected_article_trace,
    style="Blue.TButton",
).pack(side="left", padx=(8, 0))

tk.Label(
    article_top, textvariable=article_count_var,
    bg=PANEL, fg=GRAY
).pack(side="right")

article_tree = ttk.Treeview(
    tab_articles,
    columns=("id","barcode","name","code","stock","price","lot"),
    show="headings"
)
for col, txt, width in [
    ("id","ID",0), ("barcode","Barkoda",180), ("name","Artikel",310),
    ("code","Interna koda",160), ("stock","Zaloga",90),
    ("price","Prodajna cena",120), ("lot","LOT / serijska",200)
]:
    article_tree.heading(col, text=txt)
    if col == "id":
        article_tree.column(col, width=0, minwidth=0, stretch=False)
    else:
        article_tree.column(col, width=width)
article_tree.pack(fill="both", expand=True, padx=10, pady=(0,10))

article_name_search_var.trace_add("write", refresh_office_articles)
article_barcode_search_var.trace_add("write", refresh_office_articles)


# ----- SERVISI READ ONLY
service_top = tk.Frame(tab_services, bg=PANEL)
service_top.pack(fill="x", padx=10, pady=10)

service_search_var = tk.StringVar()
service_read_status = tk.StringVar(value="")

tk.Label(
    service_top, text="Iskanje",
    bg=PANEL, fg=GRAY, font=("Segoe UI", 9, "bold")
).pack(side="left")
ttk.Entry(
    service_top, textvariable=service_search_var, width=38
).pack(side="left", padx=(6,12))
ttk.Button(
    service_top, text="POSODOBI",
    command=refresh_service_readonly
).pack(side="left")
tk.Label(
    service_top, textvariable=service_read_status,
    bg=PANEL, fg=GRAY
).pack(side="right")

service_tree = ttk.Treeview(
    tab_services,
    columns=("id","service","date","customer","model","invoice","work"),
    show="headings"
)
for col, txt, width in [
    ("id","ID",0), ("service","Servis",130), ("date","Datum",135),
    ("customer","Stranka",200), ("model","Model",145),
    ("invoice","Račun",120), ("work","Opravljeno",480)
]:
    service_tree.heading(col, text=txt)
    if col == "id":
        service_tree.column(col, width=0, minwidth=0, stretch=False)
    else:
        service_tree.column(col, width=width)
service_tree.pack(fill="both", expand=True, padx=10, pady=(0,10))
service_tree.bind("<Double-1>", open_readonly_service)
service_search_var.trace_add("write", refresh_service_readonly)


# ----- DOKUMENTI: enotna leva funkcijska vrstica + zgodovina + prejem
document_list_shell = tk.Frame(tab_document_list, bg=PANEL)
document_list_shell.pack(fill="both", expand=True)
document_rail = tk.Frame(
    document_list_shell, bg="#E8EDF2", width=185,
    highlightthickness=1, highlightbackground=LIGHT_GRAY,
)
document_rail.pack(side="left", fill="y")
document_rail.pack_propagate(False)

tk.Label(
    document_rail, text="DOKUMENTI", bg="#E8EDF2", fg=DARK,
    font=("Segoe UI", 11, "bold"), anchor="w",
).pack(fill="x", padx=12, pady=(16,10))

def select_receipt_workspace():
    documents_workspace.select(tab_document_receipts)

document_rail_actions = (
    ("F1  NOV DOKUMENT", choose_document_type),
    ("F2  ODPRI IZBRANI", load_document),
    ("F3  PREJEM BLAGA", select_receipt_workspace),
    ("F4  PDF / TISK", print_selected_document_pdf),
    ("F5  OSVEŽI", refresh_documents),
)
for label, command in document_rail_actions:
    ttk.Button(document_rail, text=label, command=command).pack(
        fill="x", padx=9, pady=4,
    )

doc_page = tk.Frame(document_list_shell, bg=PANEL)
doc_page.pack(side="left", fill="both", expand=True, padx=10, pady=10)

doc_title_row = tk.Frame(doc_page, bg=PANEL)
doc_title_row.pack(fill="x", pady=(0,8))

document_count_var = tk.StringVar(value="0 dokumentov")
tk.Label(
    doc_title_row,
    text="Zgodovina dokumentov",
    bg=PANEL, fg=DARK,
    font=("Segoe UI", 15, "bold")
).pack(side="left")
tk.Label(
    doc_title_row,
    textvariable=document_count_var,
    bg=PANEL, fg=GRAY,
    font=("Segoe UI", 9)
).pack(side="right")

filter_card = tk.Frame(
    doc_page, bg="#F7F9FA",
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
filter_card.pack(fill="x", pady=(0,10))

document_search_var = tk.StringVar()
document_type_filter_var = tk.StringVar(value="VSI TIPI")
document_sort_var = tk.StringVar(value="Po datumu vnosa")
document_sort_direction_var = tk.StringVar(value="Padajoče")
document_date_exact_var = tk.StringVar()
document_date_from_var = tk.StringVar()
document_date_to_var = tk.StringVar()

filter_row1 = tk.Frame(filter_card, bg="#F7F9FA")
filter_row1.pack(fill="x", padx=10, pady=(9,4))

tk.Label(
    filter_row1, text="Išči",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",8,"bold")
).pack(side="left")
ttk.Entry(
    filter_row1,
    textvariable=document_search_var,
    width=27
).pack(side="left", padx=(5,12))

tk.Label(
    filter_row1, text="Tip dokumenta",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",8,"bold")
).pack(side="left")
ttk.Combobox(
    filter_row1,
    textvariable=document_type_filter_var,
    values=["VSI TIPI"] + DOC_TYPES,
    state="readonly",
    width=24
).pack(side="left", padx=(5,12))

tk.Label(
    filter_row1, text="Sortiraj",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",8,"bold")
).pack(side="left")
ttk.Combobox(
    filter_row1,
    textvariable=document_sort_var,
    values=list(DOCUMENT_SORTS.keys()),
    state="readonly",
    width=24
).pack(side="left", padx=(5,12))

ttk.Combobox(
    filter_row1,
    textvariable=document_sort_direction_var,
    values=["Padajoče", "Naraščajoče"],
    state="readonly",
    width=13
).pack(side="left")

filter_row2 = tk.Frame(filter_card, bg="#F7F9FA")
filter_row2.pack(fill="x", padx=10, pady=(4,9))

for lab, var, width in [
    ("Datum od", document_date_from_var, 14),
    ("Datum do", document_date_to_var, 14),
    ("Natančen datum", document_date_exact_var, 14),
]:
    tk.Label(
        filter_row2, text=lab,
        bg="#F7F9FA", fg=GRAY,
        font=("Segoe UI",8,"bold")
    ).pack(side="left")
    ttk.Entry(
        filter_row2,
        textvariable=var,
        width=width
    ).pack(side="left", padx=(5,12))

ttk.Button(
    filter_row2,
    text="IŠČI",
    command=search_documents,
    style="Blue.TButton"
).pack(side="left", padx=(4,5))

ttk.Button(
    filter_row2,
    text="POČISTI FILTRE",
    command=clear_document_filters
).pack(side="left", padx=5)

tk.Label(
    filter_row2,
    text="Datum: DD.MM.YYYY ali YYYY-MM-DD",
    bg="#F7F9FA", fg=GRAY,
    font=("Segoe UI",8)
).pack(side="right")

doc_table_frame = tk.Frame(
    doc_page, bg=PANEL,
    highlightthickness=1, highlightbackground=LIGHT_GRAY
)
doc_table_frame.pack(fill="both", expand=True)

documents_tree = ttk.Treeview(
    doc_table_frame,
    columns=(
        "id","no","type","date","partner","external","total",
        "status","created","booked","pcd"
    ),
    show="headings"
)

for col, txt, width in [
    ("id","ID",0),
    ("no","Št. dokumenta",125),
    ("type","Tip",145),
    ("date","Datum",95),
    ("partner","Partner",190),
    ("external","Št. dobavnice / zunanja",155),
    ("total","Cena / skupaj",105),
    ("status","Status",95),
    ("created","Datum vnosa",145),
    ("booked","Datum knjiženja",145),
    ("pcd","PČD",80),
]:
    documents_tree.heading(col, text=txt)
    if col == "id":
        documents_tree.column(
            col, width=0, minwidth=0, stretch=False
        )
    else:
        documents_tree.column(
            col, width=width, minwidth=60
        )

doc_scroll = ttk.Scrollbar(
    doc_table_frame,
    orient="vertical",
    command=documents_tree.yview
)
documents_tree.configure(
    yscrollcommand=doc_scroll.set
)
documents_tree.pack(
    side="left", fill="both", expand=True,
    padx=(8,0), pady=8
)
doc_scroll.pack(
    side="right", fill="y",
    padx=(0,8), pady=8
)

documents_tree.bind(
    "<Double-1>",
    lambda e: load_document()
)

def show_document_context_menu(event):
    row_id = documents_tree.identify_row(event.y)
    if row_id:
        documents_tree.selection_set(row_id)
        documents_tree.focus(row_id)
    menu = tk.Menu(documents_tree, tearoff=False)
    menu.add_command(label="Nov dokument  (F1)", command=choose_document_type)
    if row_id:
        menu.add_command(label="Odpri izbrani  (F2)", command=load_document)
        menu.add_command(label="PDF / tisk  (F4)", command=print_selected_document_pdf)
    menu.add_separator()
    menu.add_command(label="Prejem blaga  (F3)", command=select_receipt_workspace)
    menu.add_command(label="Osveži  (F5)", command=refresh_documents)
    menu.tk_popup(event.x_root, event.y_root)

documents_tree.bind("<Button-3>", show_document_context_menu)

root.bind("<F1>", lambda _event: choose_document_type())
root.bind("<F2>", lambda _event: load_document())
root.bind("<F3>", lambda _event: select_receipt_workspace())
root.bind("<F4>", lambda _event: print_selected_document_pdf())
root.bind("<F5>", lambda _event: refresh_documents())

doc_actions = tk.Frame(doc_page, bg=PANEL)
doc_actions.pack(fill="x", pady=(0,10), before=doc_table_frame)

ttk.Button(
    doc_actions,
    text="DOKUMENT",
    command=choose_document_type,
    style="Blue.TButton"
).pack(side="left")

ttk.Button(
    doc_actions,
    text="ODPRI IZBRANI",
    command=load_document
).pack(side="left", padx=(8,4))

ttk.Button(
    doc_actions,
    text="PDF / TISK",
    command=print_selected_document_pdf
).pack(side="left", padx=4)

tk.Label(
    doc_actions,
    text="DOKUMENT → tip dokumenta → podatki partnerja → vnos artiklov",
    bg=PANEL, fg=GRAY,
    font=("Segoe UI",8)
).pack(side="right")


# startup
refresh_partners()
refresh_documents()
refresh_office_articles()
refresh_service_readonly()
refresh_claim_combo()
if WEEKLY_SETTINGS.get("enabled", False):
    refresh_weekly_report_view()
refresh_furs_test_console()

protect_file_permissions(OFFICE_DB)
protect_file_permissions(AUTH_DB)
protect_file_permissions(INVENTORY_DB)

handoff_provided, handoff_accepted = consume_main_session_handoff()


def apply_office_permissions():
    if not AUTH_SESSION.has_permission(PERMISSION_MANAGE_INVENTORY):
        nb.tab(tab_stock_control, state="hidden")
    if not AUTH_SESSION.has_permission(PERMISSION_MANAGE_PRICES):
        nb.tab(tab_price_profiles, state="hidden")
    if not AUTH_SESSION.has_permission(PERMISSION_FURS_PRODUCTION):
        nb.tab(tab_furs_test, state="hidden")
    users_button.configure(
        state=(
            "normal"
            if AUTH_SESSION.has_permission(PERMISSION_MANAGE_USERS)
            else "disabled"
        )
    )

if os.environ.get("MIKROFIX_SMOKE_TEST") == "1":
    root.update_idletasks()
    root.destroy()
elif handoff_provided and not handoff_accepted:
    messagebox.showerror(
        "MikroFIX",
        "Predaja prijavljene seje ni več veljavna. Vrni se v glavni program in poskusi znova.",
        parent=root,
    )
    root.destroy()
elif handoff_accepted or run_pin_login(root):
    apply_office_permissions()
    service_invoice_request = requested_service_id()
    pos_requested = bool(requested_new_invoice() or service_invoice_request)
    if not pos_requested:
        root.deiconify()
        try:
            root.state("zoomed")
        except Exception:
            pass
    if pos_requested:
        root.after(
            0,
            lambda sid=service_invoice_request: open_pos(
                root,
                runtime=RUNTIME,
                office_db=OFFICE_DB,
                inventory_db=INVENTORY_DB,
                service_db=SERVICE_DB,
                pdf_dir=PDF_DIR,
                operator_code=RUNTIME.operator_code,
                service_id=sid,
                service_loader=load_service_invoice_seed,
                service_finalizer=globals().get("MIKROFIX_FINALIZE_SERVICE_PDF"),
            ),
        )
    else:
        tab_request = requested_open_tab()
        tab_map = {
            "receipts": tab_documents,
            "stock": tab_stock_control,
            "used": tab_used_devices,
            "pricing": tab_price_profiles,
            "documents": tab_documents,
            "partners": tab_partners,
            "articles": tab_articles,
            "reports": tab_reports,
        }
        if tab_request in tab_map:
            root.after(200, lambda tab=tab_map[tab_request]: nb.select(tab))
            if tab_request == "receipts":
                root.after(250, lambda: documents_workspace.select(tab_document_receipts))
    if WEEKLY_SETTINGS.get("enabled", False):
        root.after(1200, run_monday_weekly_report_if_due)
    if EMBEDDED_IN_MAIN:
        MIKROFIX_PARENT.wait_window(root)
    else:
        root.mainloop()
else:
    root.destroy()

if OFFICE_EXIT_CODE:
    raise SystemExit(OFFICE_EXIT_CODE)
