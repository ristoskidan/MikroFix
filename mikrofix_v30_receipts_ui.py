"""Tk zaslon za enotni prejem MikroFIX v30."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from mikrofix_v29_core import recommend_price
from mikrofix_v29_office_ui import ArticleDialog
from mikrofix_v30_receipts import (
    ReceiptError,
    ReceiptLineV30,
    add_supplement,
    complete_with_invoice,
    confirm_receipt,
    create_draft,
    delete_draft,
    ensure_receipt_schema,
    list_receipts,
    load_draft_payload,
    save_draft_payload,
    storno_receipt,
)


TYPE_LABELS = {
    "DOBAVNICA": "Dobavnica",
    "RACUN": "Prejeti račun",
    "DOBAVNICA_IN_RACUN": "Dobavnica in račun skupaj",
    "DRUGO": "Drug prevzemni dokument",
}
STATUS_LABELS = {
    "DRAFT": "OSNUTEK",
    "PENDING_INVOICE": "ČAKA NA RAČUN",
    "BOOKED": "KNJIŽENO",
    "STORNIRANO": "STORNIRANO",
}


def _fit(window, width=1180, height=710):
    window.update_idletasks()
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    width = min(width, max(820, sw - 50))
    height = min(height, max(560, sh - 80))
    window.geometry(f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//3)}")
    window.minsize(min(820, width), min(560, height))


def _decimal(value, label, scale="0.0001"):
    try:
        result = Decimal(str(value or "0").strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ReceiptError(f"{label} mora biti številka.") from exc
    if result < 0:
        raise ReceiptError(f"{label} ne sme biti negativna.")
    return result.quantize(Decimal(scale), rounding=ROUND_HALF_UP)


class ReceiptPanelV30:
    def __init__(
        self, parent, *, office_db: Path, inventory_db: Path, pricing: dict,
        operator_code: str, colors: dict, on_inventory_changed=None,
    ):
        self.parent = parent
        self.office_db = Path(office_db)
        self.inventory_db = Path(inventory_db)
        self.archive_dir = self.office_db.parent.parent / "office" / "documents" / "receipts"
        self.pricing = pricing
        self.operator_code = operator_code
        self.colors = colors
        self.on_inventory_changed = on_inventory_changed
        ensure_receipt_schema(self.office_db)
        self.filter_var = tk.StringVar(value="VSI")
        self.status_var = tk.StringVar(value="")
        self._build()
        self.refresh()

    def office_connect(self):
        connection = sqlite3.connect(str(self.office_db), timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def inventory_connect(self):
        connection = sqlite3.connect(str(self.inventory_db), timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def _build(self):
        colors = self.colors
        page = tk.Frame(self.parent, bg=colors["bg"])
        page.pack(fill="both", expand=True, padx=12, pady=10)
        title = tk.Frame(page, bg=colors["bg"])
        title.pack(fill="x", pady=(0, 6))
        tk.Label(
            title, text="PREJEM BLAGA IN PREJETI RAČUNI – v30",
            bg=colors["bg"], fg=colors["dark"], font=("Segoe UI", 15, "bold"),
        ).pack(side="left")
        tk.Label(title, textvariable=self.status_var, bg=colors["bg"],
                 fg=colors["gray"]).pack(side="right")

        actions = tk.Frame(page, bg=colors["panel"], highlightthickness=1,
                           highlightbackground=colors["light_gray"])
        actions.pack(fill="x", pady=(0, 7))
        for text, command, style in [
            ("NOV PREJEM", self.new_receipt, "Blue.TButton"),
            ("ODPRI OSNUTEK", self.open_draft, None),
            ("PREGLED", self.view_receipt, None),
            ("DODAJ RAČUN", self.attach_invoice, None),
            ("DODAJ DOPOLNILO", self.supplement, None),
            ("STORNIRAJ", self.storno, None),
            ("NOV POPRAVEK", self.new_correction, None),
            ("IZBRIŠI OSNUTEK", self.remove_draft, None),
        ]:
            ttk.Button(actions, text=text, command=command, style=style or "TButton").pack(
                side="left", padx=4, pady=7
            )
        ttk.Combobox(
            actions, textvariable=self.filter_var,
            values=["VSI", "OSNUTEK", "ČAKA NA RAČUN", "KNJIŽENO", "STORNIRANO"],
            state="readonly", width=17,
        ).pack(side="right", padx=8, pady=7)
        self.filter_var.trace_add("write", lambda *_: self.refresh())

        table = tk.Frame(page, bg=colors["panel"])
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table,
            columns=("id", "no", "date", "supplier", "type", "external", "invoice", "total", "status"),
            show="headings", selectmode="browse",
        )
        columns = [
            ("id", "ID", 0), ("no", "Prejem", 125), ("date", "Prejeto", 95),
            ("supplier", "Dobavitelj", 200), ("type", "Vrsta", 175),
            ("external", "Dokument", 140), ("invoice", "Račun", 130),
            ("total", "Znesek", 90), ("status", "Status", 120),
        ]
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=0 if key == "id" else 60,
                             stretch=key in {"supplier", "type"})
        self.tree.pack(side="left", fill="both", expand=True, padx=(7, 0), pady=7)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.pack(side="right", fill="y", padx=(0, 7), pady=7)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())

    def selected_id(self):
        selected = self.tree.selection()
        if not selected:
            return None
        return int(self.tree.item(selected[0], "values")[0])

    def selected_status(self):
        selected = self.tree.selection()
        if not selected:
            return ""
        return str(self.tree.item(selected[0], "values")[-1])

    def refresh(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        label = self.filter_var.get()
        reverse = {value: key for key, value in STATUS_LABELS.items()}
        rows = list_receipts(self.office_db, reverse.get(label, ""))
        for row in rows:
            self.tree.insert("", "end", values=(
                row["id"], row["receipt_no"] or "—", row["received_at"] or "—",
                row["partner_name_snapshot"] or "—",
                TYPE_LABELS.get(row["document_type"], row["document_type"]),
                row["external_document_no"] or "—", row["invoice_external_no"] or "—",
                f"{Decimal(row['document_total'] or '0'):.2f} €",
                STATUS_LABELS.get(row["status"], row["status"]),
            ))
        waiting = sum(1 for row in rows if row["status"] == "PENDING_INVOICE")
        drafts = sum(1 for row in rows if row["status"] == "DRAFT")
        self.status_var.set(f"{len(rows)} dokumentov • {drafts} osnutkov • {waiting} čaka na račun")

    def open_selected(self):
        if self.selected_status() == "OSNUTEK":
            self.open_draft()
        else:
            self.view_receipt()

    def new_receipt(self, correction_of_id=None):
        receipt_id, _ = create_draft(self.office_db, operator_code=self.operator_code)
        if correction_of_id:
            save_draft_payload(self.office_db, receipt_id, {"correction_of_id": correction_of_id})
        ReceiptDialogV30(self, receipt_id=receipt_id)

    def open_draft(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() != "OSNUTEK":
            messagebox.showwarning("MikroFIX Office", "Izberi osnutek.")
            return
        ReceiptDialogV30(self, receipt_id=receipt_id)

    def remove_draft(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() != "OSNUTEK":
            messagebox.showwarning("MikroFIX Office", "Izberi osnutek.")
            return
        if not messagebox.askyesno("MikroFIX Office", "Trajno izbrišem ta nepotrjeni osnutek?"):
            return
        try:
            delete_draft(self.office_db, receipt_id)
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        self.refresh()

    def attach_invoice(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() != "ČAKA NA RAČUN":
            messagebox.showwarning("MikroFIX Office", "Izberi prejem, ki čaka na račun.")
            return
        ReceiptDialogV30(self, receipt_id=receipt_id, invoice_mode=True)

    def supplement(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() == "OSNUTEK":
            messagebox.showwarning("MikroFIX Office", "Izberi potrjen dokument.")
            return
        paths = filedialog.askopenfilenames(
            parent=self.parent.winfo_toplevel(), title="Dopolnilne priloge",
            filetypes=[("Dokumenti", "*.pdf *.jpg *.jpeg *.png *.heic"), ("Vse", "*.*")],
        )
        if not paths:
            return
        try:
            add_supplement(
                office_db=self.office_db, archive_dir=self.archive_dir,
                receipt_id=receipt_id, attachment_paths=paths,
                operator_code=self.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        messagebox.showinfo("MikroFIX Office", "Dopolnilo je varno arhivirano.")

    def storno(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() not in {"KNJIŽENO", "ČAKA NA RAČUN"}:
            messagebox.showwarning("MikroFIX Office", "Izberi veljaven potrjen dokument.")
            return
        reason = simpledialog.askstring(
            "Storno celotnega dokumenta", "Obvezen razlog storna:",
            parent=self.parent.winfo_toplevel(),
        )
        if not reason:
            return
        try:
            number = storno_receipt(
                office_db=self.office_db, inventory_db=self.inventory_db,
                receipt_id=receipt_id, reason=reason, operator_code=self.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        self.refresh()
        if self.on_inventory_changed:
            self.on_inventory_changed()
        messagebox.showinfo("MikroFIX Office", f"Dokument {number} je v celoti storniran.")

    def new_correction(self):
        receipt_id = self.selected_id()
        if not receipt_id or self.selected_status() != "STORNIRANO":
            messagebox.showwarning("MikroFIX Office", "Izberi storniran dokument.")
            return
        self.new_receipt(correction_of_id=receipt_id)

    def view_receipt(self):
        receipt_id = self.selected_id()
        if not receipt_id:
            messagebox.showwarning("MikroFIX Office", "Izberi dokument.")
            return
        connection = self.office_connect()
        header = connection.execute("SELECT * FROM receipts_v30 WHERE id=?", (receipt_id,)).fetchone()
        lines = connection.execute(
            "SELECT * FROM receipt_lines_v30 WHERE receipt_id=? ORDER BY line_no", (receipt_id,)
        ).fetchall()
        attachments = connection.execute(
            "SELECT attachment_kind, original_name, sha256 FROM receipt_attachments_v30 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchall()
        connection.close()
        inv = self.inventory_connect()
        names = {int(r[0]): r[1] for r in inv.execute("SELECT id, article_name FROM inventory_items")}
        inv.close()
        win = tk.Toplevel(self.parent)
        win.title(f"MikroFIX – {header['receipt_no'] or 'OSNUTEK'}")
        _fit(win, 1020, 650)
        page = tk.Frame(win, bg=self.colors["bg"])
        page.pack(fill="both", expand=True, padx=14, pady=12)
        ttk.Button(page, text="ZAPRI", command=win.destroy).pack(anchor="e")
        text = (
            f"{header['receipt_no'] or 'OSNUTEK'} • {header['partner_name_snapshot'] or '—'}\n"
            f"{TYPE_LABELS.get(header['document_type'], header['document_type'])}: "
            f"{header['external_document_no'] or '—'} • račun {header['invoice_external_no'] or '—'}\n"
            f"Dokument {Decimal(header['document_total'] or '0'):.2f} € • prevoz "
            f"{Decimal(header['shipping_total'] or '0'):.4f} € • "
            f"{STATUS_LABELS.get(header['status'], header['status'])}"
        )
        tk.Label(page, text=text, bg=self.colors["bg"], fg=self.colors["dark"],
                 justify="left", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=6)
        tree = ttk.Treeview(
            page, columns=("article", "qty", "net", "discount", "vat", "total", "shipping", "landed", "sale"),
            show="headings",
        )
        for key, label, width in [
            ("article", "Artikel", 220), ("qty", "Kol.", 60), ("net", "Neto/kos", 85),
            ("discount", "Rabat %", 70), ("vat", "DDV %", 65), ("total", "Dobavitelj", 90),
            ("shipping", "Prevoz", 75), ("landed", "Nabavna/kos", 95), ("sale", "Prodajna", 90),
        ]:
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key == "article")
        for line in lines:
            tree.insert("", "end", values=(
                names.get(int(line["article_id"]), f"ID {line['article_id']}"),
                line["quantity"], line["net_unit_price"], line["discount_percent"],
                line["vat_rate"], line["supplier_line_total"], line["allocated_shipping"],
                line["landed_unit_cost"], line["confirmed_sale_price"],
            ))
        tree.pack(fill="both", expand=True, pady=5)
        attachment_text = ", ".join(f"{a['attachment_kind']}: {a['original_name']}" for a in attachments)
        tk.Label(page, text=f"Priloge: {attachment_text or '—'}", bg=self.colors["bg"],
                 fg=self.colors["gray"], anchor="w").pack(fill="x", pady=(4, 0))


class ReceiptDialogV30:
    def __init__(self, panel: ReceiptPanelV30, *, receipt_id: int, invoice_mode=False):
        self.panel = panel
        self.receipt_id = receipt_id
        self.invoice_mode = invoice_mode
        self.lines: list[dict] = []
        self.attachments: list[str] = []
        self.article_map = {}
        self.saving = False
        self.loading = True
        self.draft_saved = False
        self.autosave_job = None
        self.window = tk.Toplevel(panel.parent)
        self.window.title("MikroFIX – Dodaj prejeti račun" if invoice_mode else "MikroFIX – Nov prejem")
        _fit(self.window, 1200, 710)
        self.window.transient(panel.parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh_articles()
        self._load()
        self.loading = False
        self._schedule_autosave()

    def _var(self, value=""):
        variable = tk.StringVar(value=value)
        variable.trace_add("write", lambda *_: self._mark_changed())
        return variable

    def _mark_changed(self):
        if self.loading:
            return
        self.draft_saved = False
        if hasattr(self, "document_checked"):
            self.document_checked.set(False)
            self.amount_checked.set(False)
        self._set_confirmation_state()
        self._schedule_autosave()

    def _set_confirmation_state(self):
        if not hasattr(self, "document_check"):
            return
        check_state = "normal" if self.draft_saved else "disabled"
        self.document_check.configure(state=check_state)
        self.amount_check.configure(state=check_state)
        can_book = (
            self.draft_saved
            and bool(self.document_checked.get())
            and bool(self.amount_checked.get())
        )
        self.confirm_button.configure(state="normal" if can_book else "disabled")

    def _confirmation_changed(self):
        self._set_confirmation_state()

    def _build(self):
        colors = self.panel.colors
        root = tk.Frame(self.window, bg=colors["bg"])
        root.pack(fill="both", expand=True, padx=12, pady=9)
        actions = tk.Frame(root, bg=colors["bg"])
        actions.pack(fill="x", pady=(0, 7))
        ttk.Button(
            actions, text="SHRANI OSNUTEK", command=self.save_now,
            style="Blue.TButton",
        ).pack(side="left")
        self.confirm_button = ttk.Button(
            actions,
            text="KNJIŽI RAČUN IN SPROSTI ZALOGO" if self.invoice_mode else "KNJIŽI DOKUMENT",
            command=self.confirm, state="disabled",
        )
        self.confirm_button.pack(side="left", padx=(6, 0))
        self.saved_label = tk.Label(actions, text="OSNUTEK SE SHRANJUJE SAMODEJNO",
                                    bg=colors["bg"], fg=colors["gray"])
        self.saved_label.pack(side="left", padx=12)
        ttk.Button(actions, text="ZAPRI", command=self.close).pack(side="right")

        self.partner = self._var()
        self.document_type = self._var(TYPE_LABELS["DOBAVNICA_IN_RACUN"])
        self.custom_type = self._var()
        self.external_no = self._var()
        self.document_date = self._var(date.today().isoformat())
        self.received_at = self._var(date.today().isoformat())
        self.currency = self._var("EUR")
        self.document_total = self._var("0.00")
        self.shipping_total = self._var("0.0000")
        self.invoice_no = self._var()
        self.invoice_date = self._var(date.today().isoformat())
        self.invoice_due = self._var()
        self.payment_status = self._var("NEPLAČAN")
        self.notes = self._var()
        self.correction_of_id = None

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        self.document_tab = tk.Frame(notebook, bg=colors["panel"])
        self.lines_tab = tk.Frame(notebook, bg=colors["panel"])
        self.attachments_tab = tk.Frame(notebook, bg=colors["panel"])
        notebook.add(self.document_tab, text="1  DOKUMENT")
        notebook.add(self.lines_tab, text="2  POSTAVKE")
        notebook.add(self.attachments_tab, text="3  PRILOGE")
        self._build_document_tab()
        self._build_lines_tab()
        self._build_attachments_tab()

        confirmations = tk.Frame(
            root, bg=colors["panel"], highlightthickness=1,
            highlightbackground=colors["light_gray"],
        )
        confirmations.pack(fill="x", pady=(7, 0))
        self.document_checked = tk.BooleanVar(value=False)
        self.amount_checked = tk.BooleanVar(value=False)
        self.document_check = ttk.Checkbutton(
            confirmations,
            text="Prejeti dokument je primerjan z izvirnikom",
            variable=self.document_checked,
            command=self._confirmation_changed,
            state="disabled",
        )
        self.document_check.pack(side="left", padx=10, pady=8)
        self.amount_check = ttk.Checkbutton(
            confirmations,
            text="Zneski in postavke se popolnoma ujemajo",
            variable=self.amount_checked,
            command=self._confirmation_changed,
            state="disabled",
        )
        self.amount_check.pack(side="left", padx=10, pady=8)

    def _entry(self, parent, row, col, label, variable, values=None, readonly=False):
        colors = self.panel.colors
        tk.Label(parent, text=label, bg=colors["panel"], fg=colors["gray"],
                 font=("Segoe UI", 8, "bold")).grid(row=row, column=col, sticky="w", padx=10, pady=(7, 1))
        if values is not None:
            widget = ttk.Combobox(parent, textvariable=variable, values=values,
                                  state="readonly" if readonly else "normal")
        else:
            widget = ttk.Entry(parent, textvariable=variable, state="readonly" if readonly else "normal")
        widget.grid(row=row + 1, column=col, sticky="ew", padx=10, pady=(0, 7))
        return widget

    def _build_document_tab(self):
        partners = self._partners()
        fields = [
            (0, 0, "Dobavitelj", self.partner, partners, False),
            (0, 1, "Vrsta dokumenta", self.document_type, list(TYPE_LABELS.values()), True),
            (0, 2, "Naziv druge vrste", self.custom_type, None, False),
            (2, 0, "Dobaviteljeva št. dokumenta", self.external_no, None, False),
            (2, 1, "Datum dokumenta", self.document_date, None, False),
            (2, 2, "Datum fizičnega prejema", self.received_at, None, False),
            (4, 0, "Valuta", self.currency, ["EUR"], True),
            (4, 1, "Skupni znesek dokumenta (EUR)", self.document_total, None, False),
            (4, 2, "Skupni prevoz (4 decimalke)", self.shipping_total, None, False),
            (6, 0, "Številka prejetega računa", self.invoice_no, None, False),
            (6, 1, "Datum računa", self.invoice_date, None, False),
            (6, 2, "Rok plačila", self.invoice_due, None, False),
            (8, 0, "Plačilni status", self.payment_status, ["NEPLAČAN", "PLAČAN"], True),
            (8, 1, "Opomba", self.notes, None, False),
        ]
        self.header_widgets = []
        for args in fields:
            self.header_widgets.append(self._entry(self.document_tab, *args))
        for col in range(3):
            self.document_tab.grid_columnconfigure(col, weight=1)
        tk.Label(
            self.document_tab,
            text="Zneski dokumenta so na 2 decimalki; nabavne cene in razdelitev prevoza na 4. "
                 "Razlika mora biti vedno 0,00 €.",
            bg=self.panel.colors["panel"], fg=self.panel.colors["gray"],
        ).grid(row=10, column=0, columnspan=3, sticky="w", padx=10, pady=10)

    def _build_lines_tab(self):
        colors = self.panel.colors
        form = tk.Frame(self.lines_tab, bg=colors["panel"])
        form.pack(fill="x", padx=6, pady=5)
        self.article = self._var()
        self.qty = self._var("1")
        self.net = self._var("0.0000")
        self.discount = self._var("0")
        self.vat = self._var("22")
        self.supplier_total = self._var("0.00")
        self.sale = self._var("0.00")
        self.lot = self._var()
        self.location = self._var()
        labels = [
            ("Artikel", self.article, 28), ("Količina", self.qty, 9),
            ("Neto/kos", self.net, 10), ("Rabat %", self.discount, 8),
            ("DDV %", self.vat, 8), ("Dobaviteljev znesek", self.supplier_total, 13),
            ("Prodajna cena", self.sale, 11), ("LOT / SN", self.lot, 12),
            ("Lokacija", self.location, 10),
        ]
        self.article_combo = None
        for col, (label, variable, width) in enumerate(labels):
            tk.Label(form, text=label, bg=colors["panel"], fg=colors["gray"],
                     font=("Segoe UI", 8, "bold")).grid(row=0, column=col, sticky="w", padx=3)
            if col == 0:
                widget = ttk.Combobox(form, textvariable=variable, state="readonly", width=width)
                self.article_combo = widget
            else:
                widget = ttk.Entry(form, textvariable=variable, width=width)
            widget.grid(row=1, column=col, sticky="ew", padx=3, pady=(1, 5))
        buttons = tk.Frame(self.lines_tab, bg=colors["panel"])
        buttons.pack(fill="x", padx=8, pady=(0, 5))
        ttk.Button(buttons, text="DODAJ / POSODOBI POSTAVKO", command=self.add_or_update_line,
                   style="Blue.TButton").pack(side="left")
        ttk.Button(buttons, text="ODSTRANI POSTAVKO", command=self.remove_line).pack(side="left", padx=5)
        ttk.Button(buttons, text="NOV ARTIKEL", command=lambda: ArticleDialog(self)).pack(side="left", padx=5)
        self.sum_label = tk.Label(buttons, text="", bg=colors["panel"], fg=colors["dark"],
                                  font=("Segoe UI", 9, "bold"))
        self.sum_label.pack(side="right")
        self.tree = ttk.Treeview(
            self.lines_tab,
            columns=("article_id", "article", "qty", "net", "discount", "vat", "total", "sale", "lot"),
            show="headings", selectmode="browse",
        )
        for key, label, width in [
            ("article_id", "ID", 0), ("article", "Artikel", 260), ("qty", "Kol.", 65),
            ("net", "Neto/kos", 85), ("discount", "Rabat %", 70), ("vat", "DDV %", 65),
            ("total", "Dobavitelj", 95), ("sale", "Prodajna", 90), ("lot", "LOT/SN", 120),
        ]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=0 if key == "article_id" else 50,
                             stretch=key == "article")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.load_selected_line())

    def _build_attachments_tab(self):
        colors = self.panel.colors
        actions = tk.Frame(self.attachments_tab, bg=colors["panel"])
        actions.pack(fill="x", padx=10, pady=9)
        ttk.Button(actions, text="DODAJ PDF / SLIKO", command=self.add_attachments,
                   style="Blue.TButton").pack(side="left")
        ttk.Button(actions, text="ODSTRANI IZ SEZNAMA", command=self.remove_attachment).pack(side="left", padx=6)
        tk.Label(actions, text="Priloga ni obvezna • PDF, JPG, PNG ali HEIC • največ 25 MB",
                 bg=colors["panel"], fg=colors["gray"]).pack(side="right")
        self.attachment_list = tk.Listbox(self.attachments_tab, font=("Segoe UI", 10))
        self.attachment_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _partners(self):
        connection = self.panel.office_connect()
        rows = connection.execute("SELECT id, name FROM partners ORDER BY name COLLATE NOCASE").fetchall()
        connection.close()
        return [f"{row['id']} | {row['name']}" for row in rows]

    def refresh_articles(self, select_id=None):
        connection = self.panel.inventory_connect()
        rows = connection.execute(
            """
            SELECT id, article_name, barcode, price_profile_code, price, acquisition_cost
            FROM inventory_items WHERE active=1 ORDER BY article_name COLLATE NOCASE
            """
        ).fetchall()
        connection.close()
        self.article_map = {f"{r['id']} | {r['article_name']} | {r['barcode']}": r for r in rows}
        self.article_combo.configure(values=list(self.article_map))
        if select_id:
            for display, row in self.article_map.items():
                if int(row["id"]) == int(select_id):
                    self.article.set(display)
                    break

    def _load(self):
        if self.invoice_mode:
            connection = self.panel.office_connect()
            header = connection.execute("SELECT * FROM receipts_v30 WHERE id=?", (self.receipt_id,)).fetchone()
            rows = connection.execute(
                "SELECT * FROM receipt_lines_v30 WHERE receipt_id=? ORDER BY line_no", (self.receipt_id,)
            ).fetchall()
            connection.close()
            if not header or header["status"] != "PENDING_INVOICE":
                self.window.destroy()
                raise ReceiptError("Prejem ne čaka na račun.")
            self.partner.set(header["partner_name_snapshot"])
            self.document_type.set(TYPE_LABELS.get(header["document_type"], header["document_type"]))
            self.custom_type.set(header["custom_document_type"])
            self.external_no.set(header["external_document_no"])
            self.document_date.set(header["document_date"])
            self.received_at.set(header["received_at"])
            self.document_total.set(header["document_total"])
            self.shipping_total.set(header["shipping_total"])
            self.lines = [self._row_to_line(row) for row in rows]
            payload = load_draft_payload(self.panel.office_db, self.receipt_id)
            for name, variable in self._payload_vars().items():
                if name in payload and name in {
                    "document_total", "shipping_total", "invoice_no", "invoice_date",
                    "invoice_due", "payment_status", "notes",
                }:
                    variable.set(payload[name])
            if payload.get("lines"):
                self.lines = list(payload["lines"])
            self.attachments = list(payload.get("attachments") or [])
            self._refresh_lines()
            self._refresh_attachments()
            for widget in self.header_widgets[:6]:
                widget.configure(state="disabled")
            return
        payload = load_draft_payload(self.panel.office_db, self.receipt_id)
        if not payload:
            return
        for name, variable in self._payload_vars().items():
            if name in payload:
                variable.set(payload[name])
        self.correction_of_id = payload.get("correction_of_id")
        self.lines = list(payload.get("lines") or [])
        self.attachments = list(payload.get("attachments") or [])
        self._refresh_lines()
        self._refresh_attachments()

    def _row_to_line(self, row):
        return {
            "article_id": int(row["article_id"]), "quantity": row["quantity"],
            "net_unit_price": row["net_unit_price"], "discount_percent": row["discount_percent"],
            "vat_rate": row["vat_rate"], "supplier_line_total": row["supplier_line_total"],
            "confirmed_sale_price": row["confirmed_sale_price"],
            "supplier_barcode": row["supplier_barcode"], "lot_serial": row["lot_serial"],
            "location": row["location"],
        }

    def _payload_vars(self):
        return {
            "partner": self.partner, "document_type": self.document_type,
            "custom_type": self.custom_type, "external_no": self.external_no,
            "document_date": self.document_date, "received_at": self.received_at,
            "currency": self.currency, "document_total": self.document_total,
            "shipping_total": self.shipping_total, "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date, "invoice_due": self.invoice_due,
            "payment_status": self.payment_status, "notes": self.notes,
        }

    def _payload(self):
        payload = {name: variable.get() for name, variable in self._payload_vars().items()}
        payload.update(lines=self.lines, attachments=self.attachments,
                       correction_of_id=self.correction_of_id)
        return payload

    def _schedule_autosave(self):
        if not self.window.winfo_exists():
            return
        if self.autosave_job:
            self.window.after_cancel(self.autosave_job)
        self.autosave_job = self.window.after(700, self._autosave)

    def _autosave(self):
        self.autosave_job = None
        try:
            save_draft_payload(self.panel.office_db, self.receipt_id, self._payload())
            self.draft_saved = True
            self.saved_label.configure(text="OSNUTEK SHRANJEN")
        except Exception:
            self.draft_saved = False
            self.saved_label.configure(text="OSNUTKA NI BILO MOGOČE SHRANITI")
        self._set_confirmation_state()

    def save_now(self):
        if self.autosave_job:
            self.window.after_cancel(self.autosave_job)
            self.autosave_job = None
        self._autosave()

    def source_code(self):
        return {label: code for code, label in TYPE_LABELS.items()}.get(
            self.document_type.get(), "DOBAVNICA_IN_RACUN"
        )

    def _selected_article_row(self, article_id):
        for display, row in self.article_map.items():
            if int(row["id"]) == int(article_id):
                return display, row
        return "", None

    def add_or_update_line(self):
        row = self.article_map.get(self.article.get())
        if not row:
            messagebox.showwarning("MikroFIX Office", "Izberi artikel.", parent=self.window)
            return
        try:
            qty = _decimal(self.qty.get(), "Količina")
            if qty <= 0:
                raise ReceiptError("Količina mora biti večja od nič.")
            net = _decimal(self.net.get(), "Neto cena")
            discount = _decimal(self.discount.get(), "Rabat")
            vat = _decimal(self.vat.get(), "DDV")
            total = _decimal(self.supplier_total.get(), "Dobaviteljev znesek", "0.01")
            sale = _decimal(self.sale.get(), "Prodajna cena", "0.01")
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        line = {
            "article_id": int(row["id"]), "quantity": str(qty),
            "net_unit_price": str(net), "discount_percent": str(discount),
            "vat_rate": str(vat), "supplier_line_total": str(total),
            "confirmed_sale_price": str(sale), "supplier_barcode": str(row["barcode"] or ""),
            "lot_serial": self.lot.get().strip(), "location": self.location.get().strip(),
        }
        selected = self.tree.selection()
        if selected:
            index = self.tree.index(selected[0])
            if self.invoice_mode:
                old = self.lines[index]
                if old["article_id"] != line["article_id"] or Decimal(old["quantity"]) != qty:
                    messagebox.showerror(
                        "MikroFIX Office",
                        "Pri povezavi računa artikla in količine dobavnice ni dovoljeno spremeniti.",
                        parent=self.window,
                    )
                    return
            self.lines[index] = line
        else:
            if self.invoice_mode:
                messagebox.showwarning(
                    "MikroFIX Office", "Pri povezavi računa ni dovoljeno dodati nove postavke.",
                    parent=self.window,
                )
                return
            self.lines.append(line)
        self._refresh_lines()
        self.clear_line_form()
        self._mark_changed()

    def clear_line_form(self):
        self.article.set("")
        self.qty.set("1")
        self.net.set("0.0000")
        self.discount.set("0")
        self.vat.set("22")
        self.supplier_total.set("0.00")
        self.sale.set("0.00")
        self.lot.set("")
        self.location.set("")
        for item in self.tree.selection():
            self.tree.selection_remove(item)

    def load_selected_line(self):
        selected = self.tree.selection()
        if not selected:
            return
        line = self.lines[self.tree.index(selected[0])]
        display, _ = self._selected_article_row(line["article_id"])
        self.article.set(display)
        self.qty.set(line["quantity"])
        self.net.set(line["net_unit_price"])
        self.discount.set(line["discount_percent"])
        self.vat.set(line["vat_rate"])
        self.supplier_total.set(line["supplier_line_total"])
        self.sale.set(line["confirmed_sale_price"])
        self.lot.set(line.get("lot_serial", ""))
        self.location.set(line.get("location", ""))

    def remove_line(self):
        selected = self.tree.selection()
        if not selected:
            return
        if self.invoice_mode:
            messagebox.showwarning(
                "MikroFIX Office", "Postavk dobavnice pri povezavi računa ni dovoljeno odstraniti.",
                parent=self.window,
            )
            return
        self.lines.pop(self.tree.index(selected[0]))
        self._refresh_lines()
        self._mark_changed()

    def _refresh_lines(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        inv = self.panel.inventory_connect()
        names = {int(r[0]): r[1] for r in inv.execute("SELECT id, article_name FROM inventory_items")}
        inv.close()
        total = Decimal("0")
        for line in self.lines:
            total += Decimal(str(line["supplier_line_total"]))
            self.tree.insert("", "end", values=(
                line["article_id"], names.get(int(line["article_id"]), f"ID {line['article_id']}"),
                line["quantity"], line["net_unit_price"], line["discount_percent"],
                line["vat_rate"], line["supplier_line_total"], line["confirmed_sale_price"],
                line.get("lot_serial", ""),
            ))
        try:
            shipping = _decimal(self.shipping_total.get(), "Prevoz")
            expected = _decimal(self.document_total.get(), "Dokument", "0.01")
            difference = expected - (total + shipping).quantize(Decimal("0.01"))
            self.sum_label.configure(
                text=f"Postavke {total:.2f} € + prevoz {shipping:.4f} € • razlika {difference:.2f} €",
                fg=self.panel.colors["dark"] if difference == 0 else self.panel.colors["danger"],
            )
        except Exception:
            self.sum_label.configure(text="Preveri zneske.", fg=self.panel.colors["danger"])

    def add_attachments(self):
        paths = filedialog.askopenfilenames(
            parent=self.window, title="Priloge dokumenta",
            filetypes=[("Dokumenti", "*.pdf *.jpg *.jpeg *.png *.heic"), ("Vse", "*.*")],
        )
        for path in paths:
            if path not in self.attachments:
                self.attachments.append(path)
        self._refresh_attachments()
        self._mark_changed()

    def remove_attachment(self):
        selected = self.attachment_list.curselection()
        if not selected:
            return
        self.attachments.pop(int(selected[0]))
        self._refresh_attachments()
        self._mark_changed()

    def _refresh_attachments(self):
        self.attachment_list.delete(0, "end")
        for path in self.attachments:
            self.attachment_list.insert("end", path)

    def _partner(self):
        display = self.partner.get().strip()
        partner_id = None
        name = display
        if " | " in display:
            left, name = display.split(" | ", 1)
            try:
                partner_id = int(left)
            except ValueError:
                pass
        snapshot = {"name": name}
        if partner_id:
            connection = self.panel.office_connect()
            row = connection.execute("SELECT * FROM partners WHERE id=?", (partner_id,)).fetchone()
            connection.close()
            if row:
                snapshot = dict(row)
        return partner_id, name, snapshot

    def _line_objects(self):
        return [ReceiptLineV30(**line) for line in self.lines]

    def confirm(self):
        if self.saving:
            return
        if not (
            self.draft_saved
            and self.document_checked.get()
            and self.amount_checked.get()
        ):
            messagebox.showwarning(
                "MikroFIX Office",
                "Najprej shrani osnutek in potrdi obe preverjanji usklajenosti.",
                parent=self.window,
            )
            return
        self.saving = True
        self.confirm_button.configure(state="disabled")
        try:
            if self.invoice_mode:
                complete_with_invoice(
                    office_db=self.panel.office_db, inventory_db=self.panel.inventory_db,
                    archive_dir=self.panel.archive_dir, receipt_id=self.receipt_id,
                    invoice_external_no=self.invoice_no.get(), invoice_date=self.invoice_date.get(),
                    invoice_due_date=self.invoice_due.get(),
                    invoice_payment_status=self.payment_status.get().replace("Č", "C"),
                    document_total=self.document_total.get(), shipping_total=self.shipping_total.get(),
                    lines=self._line_objects(), attachment_paths=self.attachments,
                    operator_code=self.panel.operator_code,
                )
                result = "Račun je povezan; ista količina je zdaj sproščena v zalogo."
            else:
                partner_id, partner_name, snapshot = self._partner()
                _, number = confirm_receipt(
                    office_db=self.panel.office_db, inventory_db=self.panel.inventory_db,
                    archive_dir=self.panel.archive_dir, receipt_id=self.receipt_id,
                    partner_id=partner_id, partner_name=partner_name, partner_snapshot=snapshot,
                    document_type=self.source_code(), custom_document_type=self.custom_type.get(),
                    external_document_no=self.external_no.get(), document_date=self.document_date.get(),
                    received_at=self.received_at.get(), currency=self.currency.get(),
                    document_total=self.document_total.get(), shipping_total=self.shipping_total.get(),
                    invoice_external_no=self.invoice_no.get(), invoice_date=self.invoice_date.get(),
                    invoice_due_date=self.invoice_due.get(),
                    invoice_payment_status=self.payment_status.get().replace("Č", "C"),
                    lines=self._line_objects(), attachment_paths=self.attachments,
                    notes=self.notes.get(), operator_code=self.panel.operator_code,
                    correction_of_id=self.correction_of_id,
                )
                result = (
                    f"Dokument {number} je potrjen in zaloga sproščena."
                    if self.source_code() in {"RACUN", "DOBAVNICA_IN_RACUN"}
                    else f"Dokument {number} je potrjen; blago čaka na račun in ni na voljo."
                )
        except Exception as exc:
            self.saving = False
            self.confirm_button.configure(state="normal")
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        self.window.destroy()
        self.panel.refresh()
        if self.panel.on_inventory_changed:
            self.panel.on_inventory_changed()
        messagebox.showinfo("MikroFIX Office", result)

    def close(self):
        self._autosave()
        self.panel.refresh()
        self.window.destroy()


def build_receipts_tab(parent, **kwargs):
    return ReceiptPanelV30(parent, **kwargs)
