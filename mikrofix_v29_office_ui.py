"""Tk uporabniški vmesnik za enotni prejem blaga MikroFIX v29."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from mikrofix_v29_core import (
    ReceiptLine,
    V29Error,
    attach_invoice_to_receipt,
    create_and_book_receipt,
    recommend_price,
    storno_receipt,
)


SOURCE_LABELS = {
    "DOBAVNICA_IN_RAČUN": "Dobavnica in račun skupaj",
    "DOBAVNICA": "Samo dobavnica",
    "RAČUN": "Račun je tudi podlaga za prejem",
    "DRUGO": "Drug dokument",
}
TRACK_LABELS = {
    "QUANTITY": "Količinsko",
    "LOT": "Po seriji / LOT-u",
    "SERIAL": "Po posamezni serijski številki",
}
CONDITION_LABELS = {
    "NOVO": "Novo",
    "RABLJEN_TESTIRAN": "Rabljeno in testirano",
    "OBNOVLJENO": "Obnovljeno",
}
TAX_LABELS = {
    "UNCLASSIFIED": "Še ni davčno razvrščeno",
    "STANDARD": "Splošna stopnja",
    "REDUCED": "Znižana stopnja",
    "EXEMPT": "Oproščeno",
}


def _fit(window, width=1120, height=760):
    window.update_idletasks()
    sw = max(800, window.winfo_screenwidth())
    sh = max(600, window.winfo_screenheight())
    w = min(width, sw - 70)
    h = min(height, sh - 90)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    window.geometry(f"{w}x{h}+{x}+{y}")
    window.minsize(min(820, w), min(560, h))
    window.resizable(True, True)


def _float(value, label):
    try:
        result = float(str(value or "0").strip().replace(",", "."))
    except ValueError as exc:
        raise V29Error(f"{label} mora biti številka.") from exc
    if result < 0:
        raise V29Error(f"{label} ne sme biti negativna.")
    return round(result, 2)


class ReceiptPanel:
    def __init__(
        self,
        parent,
        *,
        office_db: Path,
        inventory_db: Path,
        pricing: dict,
        operator_code: str,
        colors: dict,
        on_inventory_changed=None,
    ):
        self.parent = parent
        self.office_db = Path(office_db)
        self.inventory_db = Path(inventory_db)
        self.pricing = pricing
        self.operator_code = operator_code
        self.colors = colors
        self.on_inventory_changed = on_inventory_changed
        self.status_var = tk.StringVar(value="Enotni prejem: dobavnica, račun ali oba skupaj.")
        self.filter_var = tk.StringVar(value="VSI")
        self._build()
        self.refresh()

    def office_connect(self):
        connection = sqlite3.connect(str(self.office_db), timeout=20)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def inventory_connect(self):
        connection = sqlite3.connect(str(self.inventory_db), timeout=20)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _build(self):
        bg = self.colors["bg"]
        panel = self.colors["panel"]
        gray = self.colors["gray"]
        dark = self.colors["dark"]

        page = tk.Frame(self.parent, bg=bg)
        page.pack(fill="both", expand=True, padx=14, pady=12)

        title = tk.Frame(page, bg=bg)
        title.pack(fill="x", pady=(0, 8))
        tk.Label(
            title, text="PREJEM BLAGA IN PREJETI RAČUNI",
            bg=bg, fg=dark, font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        tk.Label(
            title, textvariable=self.status_var,
            bg=bg, fg=gray, font=("Segoe UI", 9),
        ).pack(side="right")

        # Glavna dejanja so zgoraj in ostanejo vidna tudi na nižjem zaslonu.
        actions = tk.Frame(page, bg=panel, highlightthickness=1,
                           highlightbackground=self.colors["light_gray"])
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(
            actions, text="NOV PREJEM BLAGA", command=self.new_receipt,
            style="Blue.TButton",
        ).pack(side="left", padx=(10, 5), pady=8)
        ttk.Button(
            actions, text="PREGLED IZBRANEGA", command=self.view_receipt,
        ).pack(side="left", padx=5, pady=8)
        ttk.Button(
            actions, text="DODAJ / POVEŽI RAČUN", command=self.attach_invoice,
        ).pack(side="left", padx=5, pady=8)
        ttk.Button(
            actions, text="STORNIRAJ PREJEM", command=self.storno,
        ).pack(side="left", padx=5, pady=8)
        ttk.Button(
            actions, text="NOV POPRAVEK", command=self.new_correction,
        ).pack(side="left", padx=5, pady=8)
        ttk.Button(actions, text="OSVEŽI", command=self.refresh).pack(
            side="left", padx=5, pady=8
        )
        tk.Label(actions, text="Prikaži", bg=panel, fg=gray).pack(
            side="right", padx=(6, 10)
        )
        ttk.Combobox(
            actions, textvariable=self.filter_var,
            values=["VSI", "ČAKA NA RAČUN", "KNJIŽENO", "STORNIRANO"],
            state="readonly", width=18,
        ).pack(side="right", pady=8)
        self.filter_var.trace_add("write", lambda *_: self.refresh())

        table_frame = tk.Frame(page, bg=panel, highlightthickness=1,
                               highlightbackground=self.colors["light_gray"])
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("id", "no", "date", "partner", "source", "external",
                     "invoice", "cost", "status"),
            show="headings", selectmode="browse",
        )
        for column, label, width in [
            ("id", "ID", 0), ("no", "Prejem", 130), ("date", "Prejeto", 105),
            ("partner", "Dobavitelj", 220), ("source", "Vrsta dokumenta", 205),
            ("external", "Dobavnica / dokument", 160),
            ("invoice", "Prejeti račun", 155), ("cost", "Cena", 105),
            ("status", "Status", 115),
        ]:
            self.tree.heading(column, text=label)
            self.tree.column(
                column, width=width, minwidth=0 if column == "id" else 65,
                stretch=column in {"partner", "source"},
            )
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _event: self.view_receipt())

    def selected_id(self):
        selected = self.tree.selection()
        if not selected:
            return None
        values = self.tree.item(selected[0], "values")
        return int(values[0]) if values else None

    def view_receipt(self):
        receipt_id = self.selected_id()
        if not receipt_id:
            messagebox.showwarning("MikroFIX Office", "Izberi prejem.")
            return
        connection = self.office_connect()
        receipt = connection.execute(
            """
            SELECT receipt_no, partner_name_snapshot, received_at,
                   source_document_type, source_external_no,
                   invoice_external_no, invoice_date, provisional_cost,
                   status, correction_of_id, storno_reason, notes
            FROM receipts_v29 WHERE id=?
            """,
            (receipt_id,),
        ).fetchone()
        lines = connection.execute(
            """
            SELECT article_id, quantity, purchase_unit_cost,
                   confirmed_sale_price, lot_serial, location
            FROM receipt_lines_v29 WHERE receipt_id=? ORDER BY id
            """,
            (receipt_id,),
        ).fetchall()
        connection.close()
        inventory = self.inventory_connect()
        names = {
            int(row[0]): str(row[1]) for row in inventory.execute(
                "SELECT id, article_name FROM inventory_items"
            ).fetchall()
        }
        inventory.close()
        win = tk.Toplevel(self.parent)
        win.title(f"MikroFIX Office - {receipt[0]}")
        _fit(win, 980, 650)
        page = tk.Frame(win, bg=self.colors["bg"])
        page.pack(fill="both", expand=True, padx=16, pady=14)
        ttk.Button(page, text="ZAPRI", command=win.destroy).pack(anchor="e", pady=(0, 8))
        source_label = SOURCE_LABELS.get(receipt[3], receipt[3])
        info = (
            f"{receipt[0]}  •  {receipt[1]}  •  prejeto {receipt[2]}\n"
            f"{source_label}: {receipt[4] or '-'}  •  račun: {receipt[5] or '-'} "
            f"({receipt[6] or '-'})\nStatus: {receipt[8]}  •  "
            f"{'ZAČASNA NABAVNA CENA' if receipt[7] else 'račun povezan'}"
        )
        tk.Label(
            page, text=info, bg=self.colors["bg"], fg=self.colors["dark"],
            justify="left", font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(
            page, columns=("article", "qty", "cost", "sale", "lot", "location"),
            show="headings",
        )
        for col, label, width in (
            ("article", "Artikel", 330), ("qty", "Količina", 85),
            ("cost", "Nabavna / kos", 115), ("sale", "Zaklenjena cena", 120),
            ("lot", "LOT / serijska", 150), ("location", "Lokacija", 120),
        ):
            tree.heading(col, text=label)
            tree.column(col, width=width)
        for line in lines:
            tree.insert("", "end", values=(
                names.get(int(line[0]), f"Artikel ID {line[0]}"), f"{line[1]:g}",
                f"{line[2]:.2f} €", f"{line[3]:.2f} €", line[4] or "-", line[5] or "-",
            ))
        tree.pack(fill="both", expand=True)
        notes = receipt[11] or ""
        if receipt[9]:
            notes += f"\nPOP referenca: prejem ID {receipt[9]}"
        if receipt[10]:
            notes += f"\nStorno razlog: {receipt[10]}"
        tk.Label(
            page, text=notes.strip() or "Brez opombe.", bg=self.colors["bg"],
            fg=self.colors["gray"], justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def refresh(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        where = ""
        value = self.filter_var.get()
        params = []
        if value == "ČAKA NA RAČUN":
            where = "WHERE r.invoice_status='WAITING' AND r.status='BOOKED'"
        elif value == "KNJIŽENO":
            where = "WHERE r.status='BOOKED'"
        elif value == "STORNIRANO":
            where = "WHERE r.status='STORNIRANO'"
        connection = self.office_connect()
        rows = connection.execute(
            f"""
            SELECT r.id, r.receipt_no, r.received_at, r.partner_name_snapshot,
                   r.source_document_type, r.source_external_no,
                   CASE WHEN r.invoice_status='WAITING' THEN 'ČAKA NA RAČUN'
                        ELSE COALESCE(NULLIF(r.invoice_external_no,''),'PREJET') END,
                   COALESCE(SUM(l.quantity*l.purchase_unit_cost),0), r.status
            FROM receipts_v29 r
            LEFT JOIN receipt_lines_v29 l ON l.receipt_id=r.id
            {where}
            GROUP BY r.id
            ORDER BY r.id DESC
            """,
            params,
        ).fetchall()
        connection.close()
        for row in rows:
            self.tree.insert(
                "", "end",
                values=(
                    row[0], row[1], row[2], row[3],
                    SOURCE_LABELS.get(row[4], row[4]), row[5] or "—", row[6],
                    f"{float(row[7]):.2f} €", row[8],
                ),
            )
        self.status_var.set(f"{len(rows)} prejemov • zaloga se spremeni samo ob prejemu/stornu")

    def new_receipt(self, correction_of_id=None):
        ReceiptDialog(self, correction_of_id=correction_of_id)

    def new_correction(self):
        receipt_id = self.selected_id()
        if not receipt_id:
            messagebox.showwarning("MikroFIX Office", "Izberi stornirani prejem.")
            return
        connection = self.office_connect()
        row = connection.execute(
            "SELECT status, receipt_no FROM receipts_v29 WHERE id=?", (receipt_id,)
        ).fetchone()
        connection.close()
        if not row or row[0] != "STORNIRANO":
            messagebox.showwarning(
                "MikroFIX Office", "Popravek se lahko sklicuje samo na stornirani prejem."
            )
            return
        self.new_receipt(correction_of_id=receipt_id)

    def attach_invoice(self):
        receipt_id = self.selected_id()
        if not receipt_id:
            messagebox.showwarning("MikroFIX Office", "Izberi prejem, ki čaka na račun.")
            return
        win = tk.Toplevel(self.parent)
        win.title("MikroFIX – Dodaj prejeti račun")
        _fit(win, 620, 390)
        win.transient(self.parent.winfo_toplevel())
        win.grab_set()
        body = tk.Frame(win, bg=self.colors["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=14)
        actions = tk.Frame(body, bg=self.colors["bg"])
        actions.pack(fill="x", pady=(0, 10))
        number = tk.StringVar()
        inv_date = tk.StringVar(value=date.today().isoformat())
        path = tk.StringVar()

        def save():
            try:
                attach_invoice_to_receipt(
                    office_db=self.office_db, receipt_id=receipt_id,
                    invoice_external_no=number.get(), invoice_date=inv_date.get(),
                    file_path=path.get(), operator_code=self.operator_code,
                )
            except Exception as exc:
                messagebox.showerror("MikroFIX Office", str(exc), parent=win)
                return
            win.destroy()
            self.refresh()

        ttk.Button(actions, text="SHRANI RAČUN", command=save,
                   style="Blue.TButton").pack(side="left")
        ttk.Button(actions, text="PREKLIČI", command=win.destroy).pack(side="right")
        card = tk.Frame(body, bg=self.colors["panel"])
        card.pack(fill="both", expand=True)
        for row, (label, variable) in enumerate([
            ("Številka računa", number), ("Datum računa", inv_date),
            ("Datoteka PDF/slika", path),
        ]):
            tk.Label(card, text=label, bg=self.colors["panel"],
                     fg=self.colors["gray"]).grid(row=row, column=0, sticky="w", padx=12, pady=9)
            ttk.Entry(card, textvariable=variable).grid(
                row=row, column=1, sticky="ew", padx=(0, 8), pady=9
            )
        ttk.Button(
            card, text="IZBERI DATOTEKO",
            command=lambda: path.set(filedialog.askopenfilename(parent=win) or path.get()),
        ).grid(row=2, column=2, padx=(0, 12), pady=9)
        card.grid_columnconfigure(1, weight=1)

    def storno(self):
        receipt_id = self.selected_id()
        if not receipt_id:
            messagebox.showwarning("MikroFIX Office", "Izberi knjižen prejem.")
            return
        reason = simpledialog.askstring(
            "MikroFIX – Storno prejema",
            "Razlog storna (izvirni dokument ostane evidentiran):",
            parent=self.parent.winfo_toplevel(),
        )
        if not reason:
            return
        try:
            number = storno_receipt(
                office_db=self.office_db, inventory_db=self.inventory_db,
                receipt_id=receipt_id, reason=reason,
                operator_code=self.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        self.refresh()
        if self.on_inventory_changed:
            self.on_inventory_changed()
        messagebox.showinfo(
            "MikroFIX Office",
            f"Prejem {number} je označen STORNIRANO. Ustvarjeni so nasprotni premiki.",
        )


class ReceiptDialog:
    def __init__(self, panel: ReceiptPanel, *, correction_of_id=None):
        self.panel = panel
        self.correction_of_id = correction_of_id
        self.lines = []
        self.article_map = {}
        self.saving = False
        self.window = tk.Toplevel(panel.parent)
        self.window.title("MikroFIX – Nov enotni prejem blaga")
        _fit(self.window, 1220, 820)
        self.window.transient(panel.parent.winfo_toplevel())
        self.window.grab_set()
        self._build()
        self.refresh_articles()

    def _build(self):
        colors = self.panel.colors
        bg, panel = colors["bg"], colors["panel"]
        root = tk.Frame(self.window, bg=bg)
        root.pack(fill="both", expand=True, padx=16, pady=12)

        actions = tk.Frame(root, bg=bg)
        actions.pack(fill="x", pady=(0, 8))
        self.book_button = ttk.Button(
            actions, text="KNJIŽI PREJEM", command=self.book,
            style="Blue.TButton",
        )
        self.book_button.pack(side="left")
        ttk.Button(actions, text="NOV ARTIKEL", command=self.new_article).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="ODSTRANI VRSTICO", command=self.remove_line).pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="PREKLIČI", command=self.window.destroy).pack(side="right")

        title = "NOV POPRAVEK PREJEMA" if self.correction_of_id else "NOV PREJEM BLAGA"
        tk.Label(root, text=title, bg=bg, fg=colors["dark"],
                 font=("Segoe UI", 17, "bold")).pack(anchor="w")
        if self.correction_of_id:
            tk.Label(
                root, text=f"Oznaka POP • referenca na stornirani prejem ID {self.correction_of_id}",
                bg=bg, fg=colors["danger"], font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(0, 5))

        self.partner = tk.StringVar()
        self.source_type = tk.StringVar(value=SOURCE_LABELS["DOBAVNICA_IN_RAČUN"])
        self.source_no = tk.StringVar()
        self.invoice_no = tk.StringVar()
        self.received_date = tk.StringVar(value=date.today().isoformat())
        self.invoice_date = tk.StringVar(value=date.today().isoformat())
        self.source_file = tk.StringVar()
        self.invoice_file = tk.StringVar()
        self.notes = tk.StringVar()
        self.provisional = tk.BooleanVar(value=False)

        header = tk.Frame(root, bg=panel, highlightthickness=1,
                          highlightbackground=colors["light_gray"])
        header.pack(fill="x", pady=(6, 8))
        partners = self._partners()
        fields = [
            ("Dobavitelj", self.partner, partners, None),
            ("Vrsta spremljajočega dokumenta", self.source_type,
             list(SOURCE_LABELS.values()), "readonly"),
            ("Št. dobavnice / dokumenta", self.source_no, None, None),
            ("Št. prejetega računa", self.invoice_no, None, None),
            ("Datum fizičnega prejema", self.received_date, None, None),
            ("Datum računa", self.invoice_date, None, None),
            ("Datoteka dobavnice/dokumenta", self.source_file, None, None),
            ("Datoteka računa", self.invoice_file, None, None),
        ]
        for index, (label, variable, values, state) in enumerate(fields):
            row, side = divmod(index, 2)
            col = side * 2
            tk.Label(header, text=label, bg=panel, fg=colors["gray"],
                     font=("Segoe UI", 8, "bold")).grid(
                row=row, column=col, sticky="w", padx=(12, 6), pady=7
            )
            if values is not None:
                widget = ttk.Combobox(header, textvariable=variable, values=values,
                                      state=state or "normal")
            else:
                widget = ttk.Entry(header, textvariable=variable)
            widget.grid(row=row, column=col + 1, sticky="ew", padx=(0, 12), pady=7)
        file_actions = tk.Frame(header, bg=panel)
        file_actions.grid(row=4, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 4))
        ttk.Button(
            file_actions, text="IZBERI DATOTEKO DOBAVNICE",
            command=lambda: self.source_file.set(
                filedialog.askopenfilename(parent=self.window) or self.source_file.get()
            ),
        ).pack(side="left")
        ttk.Button(
            file_actions, text="IZBERI DATOTEKO RAČUNA",
            command=lambda: self.invoice_file.set(
                filedialog.askopenfilename(parent=self.window) or self.invoice_file.get()
            ),
        ).pack(side="left", padx=6)
        tk.Label(
            header, text="Opomba", bg=panel, fg=colors["gray"],
            font=("Segoe UI", 8, "bold"),
        ).grid(row=5, column=0, sticky="w", padx=(12, 6), pady=7)
        ttk.Entry(header, textvariable=self.notes).grid(
            row=5, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=7
        )
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(3, weight=1)
        self.source_type.trace_add("write", self._source_changed)

        line_card = tk.Frame(root, bg=panel, highlightthickness=1,
                             highlightbackground=colors["light_gray"])
        line_card.pack(fill="x", pady=(0, 8))
        self.article = tk.StringVar()
        self.qty = tk.StringVar(value="1")
        self.cost = tk.StringVar(value="0.00")
        self.recommended = tk.StringVar(value="0.00")
        self.sale = tk.StringVar(value="0.00")
        self.lot = tk.StringVar()
        self.location = tk.StringVar()

        line_fields = [
            ("Artikel iz kataloga", self.article), ("Količina", self.qty),
            ("Nabavna cena/kos", self.cost), ("Priporočena cena", self.recommended),
            ("Potrjena prodajna cena", self.sale), ("LOT / serijska", self.lot),
            ("Lokacija", self.location),
        ]
        self.article_combo = None
        for index, (label, variable) in enumerate(line_fields):
            tk.Label(line_card, text=label, bg=panel, fg=colors["gray"],
                     font=("Segoe UI", 8, "bold")).grid(
                row=0, column=index, sticky="w", padx=(8, 4), pady=(8, 2)
            )
            if index == 0:
                widget = ttk.Combobox(line_card, textvariable=variable, state="readonly", width=30)
                self.article_combo = widget
            else:
                widget = ttk.Entry(line_card, textvariable=variable, width=13)
            if variable is self.recommended:
                widget.configure(state="readonly")
            widget.grid(row=1, column=index, sticky="ew", padx=(8, 4), pady=(0, 8))
            line_card.grid_columnconfigure(index, weight=3 if index == 0 else 1)
        ttk.Button(line_card, text="DODAJ VRSTICO", command=self.add_line,
                   style="Blue.TButton").grid(row=1, column=len(line_fields), padx=8, pady=(0, 8))
        self.article.trace_add("write", lambda *_: self.recalculate())
        self.cost.trace_add("write", lambda *_: self.recalculate())

        table = tk.Frame(root, bg=panel)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table,
            columns=("article_id", "name", "qty", "cost", "recommended", "sale", "lot", "location"),
            show="headings", selectmode="browse",
        )
        for col, label, width in [
            ("article_id", "ID", 0), ("name", "Artikel", 310), ("qty", "Količina", 80),
            ("cost", "Nabavna/kos", 110), ("recommended", "Priporočena", 110),
            ("sale", "Potrjena cena", 120), ("lot", "LOT/SN", 160),
            ("location", "Lokacija", 130),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, minwidth=0 if col == "article_id" else 55,
                             stretch=col == "name")
        self.tree.pack(fill="both", expand=True)

    def _partners(self):
        connection = self.panel.office_connect()
        rows = connection.execute(
            "SELECT id, name FROM partners ORDER BY name COLLATE NOCASE"
        ).fetchall()
        connection.close()
        return [f"{row[0]} | {row[1]}" for row in rows]

    def _source_changed(self, *_):
        code = self.source_code()
        has_invoice = code in {"DOBAVNICA_IN_RAČUN", "RAČUN"}
        self.provisional.set(not has_invoice)
        if not has_invoice:
            self.invoice_no.set("")
            self.invoice_file.set("")

    def source_code(self):
        for code, label in SOURCE_LABELS.items():
            if label == self.source_type.get():
                return code
        return "DOBAVNICA_IN_RAČUN"

    def refresh_articles(self, select_id=None):
        connection = self.panel.inventory_connect()
        rows = connection.execute(
            """
            SELECT id, article_name, barcode, price_profile_code, price,
                   acquisition_cost
            FROM inventory_items WHERE active=1
            ORDER BY article_name COLLATE NOCASE
            """
        ).fetchall()
        connection.close()
        self.article_map = {
            f"{row[0]} | {row[1]} | {row[2]}": row for row in rows
        }
        self.article_combo.configure(values=list(self.article_map.keys()))
        if select_id:
            for display, row in self.article_map.items():
                if int(row[0]) == int(select_id):
                    self.article.set(display)
                    break

    def recalculate(self):
        row = self.article_map.get(self.article.get())
        if not row:
            return
        try:
            cost = _float(self.cost.get(), "Nabavna cena")
        except V29Error:
            return
        profile_code = str(row[3] or "SPLOŠNO")
        connection = self.panel.inventory_connect()
        profile_row = connection.execute(
            """
            SELECT version_no,tiers_json,warranty_reserve
            FROM price_profile_versions WHERE code=? AND active=1
            ORDER BY version_no DESC LIMIT 1
            """,
            (profile_code,),
        ).fetchone()
        if not profile_row:
            profile_row = connection.execute(
                """
                SELECT version_no,tiers_json,warranty_reserve
                FROM price_profile_versions WHERE code='SPLOŠNO' AND active=1
                ORDER BY version_no DESC LIMIT 1
                """
            ).fetchone()
            profile_code = "SPLOŠNO"
        connection.close()
        if not profile_row:
            return
        import json
        profile = {
            "version": profile_row[0], "tiers": json.loads(profile_row[1]),
            "warranty_reserve": profile_row[2],
        }
        rec = recommend_price(cost, profile, profile_code=profile_code)
        self.recommended.set(f"{rec.recommended_price:.2f}")
        current_sale = _float(row[4], "Cena")
        self.sale.set(f"{(current_sale if current_sale > 0 else rec.recommended_price):.2f}")

    def add_line(self):
        row = self.article_map.get(self.article.get())
        if not row:
            messagebox.showwarning("MikroFIX Office", "Izberi artikel iz kataloga.", parent=self.window)
            return
        try:
            qty = _float(self.qty.get(), "Količina")
            if qty <= 0:
                raise V29Error("Količina mora biti večja od nič.")
            cost = _float(self.cost.get(), "Nabavna cena")
            sale = _float(self.sale.get(), "Potrjena cena")
            if sale + 1e-9 < round(cost * 1.05, 2):
                raise V29Error("Potrjena cena je nižja od nabavne vrednosti + 5 %.")
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        line = ReceiptLine(int(row[0]), qty, cost, sale, self.lot.get(), self.location.get())
        self.lines.append(line)
        self.tree.insert(
            "", "end",
            values=(row[0], row[1], f"{qty:g}", f"{cost:.2f}",
                    self.recommended.get(), f"{sale:.2f}", self.lot.get(), self.location.get()),
        )
        self.article.set("")
        self.qty.set("1")
        self.cost.set("0.00")
        self.recommended.set("0.00")
        self.sale.set("0.00")
        self.lot.set("")

    def remove_line(self):
        selected = self.tree.selection()
        if not selected:
            return
        index = self.tree.index(selected[0])
        self.tree.delete(selected[0])
        if 0 <= index < len(self.lines):
            self.lines.pop(index)

    def new_article(self):
        ArticleDialog(self)

    def book(self):
        if self.saving:
            return
        partner_display = self.partner.get().strip()
        partner_id = None
        partner_name = partner_display
        if " | " in partner_display:
            left, partner_name = partner_display.split(" | ", 1)
            try:
                partner_id = int(left)
            except ValueError:
                partner_id = None
        if not partner_name:
            messagebox.showwarning("MikroFIX Office", "Izberi ali vpiši dobavitelja.", parent=self.window)
            return
        code = self.source_code()
        if code in {"DOBAVNICA_IN_RAČUN", "RAČUN"} and not self.invoice_no.get().strip():
            messagebox.showwarning("MikroFIX Office", "Pri tej vrsti prejema manjka številka računa.", parent=self.window)
            return
        self.saving = True
        self.book_button.configure(state="disabled", text="KNJIŽIM...")
        try:
            receipt_id, receipt_no = create_and_book_receipt(
                office_db=self.panel.office_db,
                inventory_db=self.panel.inventory_db,
                partner_id=partner_id, partner_name=partner_name,
                source_document_type=code,
                source_external_no=self.source_no.get(),
                invoice_external_no=self.invoice_no.get(),
                invoice_date=self.invoice_date.get(),
                received_at=self.received_date.get(),
                provisional_cost=self.provisional.get(),
                lines=self.lines,
                notes=self.notes.get(), operator_code=self.panel.operator_code,
                correction_of_id=self.correction_of_id,
                source_file_path=self.source_file.get(),
                invoice_file_path=self.invoice_file.get(),
            )
        except Exception as exc:
            self.saving = False
            self.book_button.configure(state="normal", text="KNJIŽI PREJEM")
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        self.window.destroy()
        self.panel.refresh()
        if self.panel.on_inventory_changed:
            self.panel.on_inventory_changed()
        messagebox.showinfo(
            "MikroFIX Office",
            f"Prejem {receipt_no} je knjižen. Zaloga je povečana samo enkrat.",
        )


class ArticleDialog:
    def __init__(self, receipt_dialog: ReceiptDialog):
        self.receipt_dialog = receipt_dialog
        self.window = tk.Toplevel(receipt_dialog.window)
        self.window.title("MikroFIX – Nov artikel v katalogu")
        _fit(self.window, 760, 620)
        self.window.transient(receipt_dialog.window)
        self.window.grab_set()
        colors = receipt_dialog.panel.colors
        root = tk.Frame(self.window, bg=colors["bg"])
        root.pack(fill="both", expand=True, padx=16, pady=12)
        actions = tk.Frame(root, bg=colors["bg"])
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="SHRANI ARTIKEL", command=self.save,
                   style="Blue.TButton").pack(side="left")
        ttk.Button(actions, text="PREKLIČI", command=self.window.destroy).pack(side="right")
        tk.Label(root, text="NOV ARTIKEL – KATALOG, BREZ ROČNE ZALOGE",
                 bg=colors["bg"], fg=colors["dark"],
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(0, 8))

        connection = receipt_dialog.panel.inventory_connect()
        profiles = [row[0] for row in connection.execute(
            "SELECT code FROM price_profiles WHERE active=1 ORDER BY label"
        ).fetchall()]
        connection.close()
        self.barcode = tk.StringVar()
        self.name = tk.StringVar()
        self.code = tk.StringVar()
        self.category = tk.StringVar(value="SPLOŠNO")
        self.profile = tk.StringVar(value="SPLOŠNO")
        self.supplier = tk.StringVar()
        self.supplier_sku = tk.StringVar()
        self.track = tk.StringVar(value=TRACK_LABELS["QUANTITY"])
        self.condition = tk.StringVar(value=CONDITION_LABELS["NOVO"])
        self.tax_classification = tk.StringVar(value=TAX_LABELS["UNCLASSIFIED"])
        self.location = tk.StringVar()
        card = tk.Frame(root, bg=colors["panel"], highlightthickness=1,
                        highlightbackground=colors["light_gray"])
        card.pack(fill="both", expand=True)
        fields = [
            ("Barkoda", self.barcode, None), ("Naziv", self.name, None),
            ("Interna koda", self.code, None), ("Kategorija", self.category, None),
            ("Cenovni profil", self.profile, profiles), ("Dobavitelj", self.supplier, None),
            ("Dobaviteljeva oznaka", self.supplier_sku, None),
            ("Način sledenja", self.track, list(TRACK_LABELS.values())),
            ("Stanje", self.condition, list(CONDITION_LABELS.values())),
            ("Davčna razvrstitev", self.tax_classification,
             list(TAX_LABELS.values())),
            ("Privzeta lokacija", self.location, None),
        ]
        for row, (label, variable, values) in enumerate(fields):
            tk.Label(card, text=label, bg=colors["panel"], fg=colors["gray"],
                     width=23, anchor="w").grid(row=row, column=0, sticky="w", padx=12, pady=7)
            if values:
                widget = ttk.Combobox(card, textvariable=variable, values=values, state="readonly")
            else:
                widget = ttk.Entry(card, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=7)
        card.grid_columnconfigure(1, weight=1)

    def save(self):
        barcode = self.barcode.get().strip()
        name = self.name.get().strip()
        if not barcode or not name:
            messagebox.showwarning("MikroFIX Office", "Barkoda in naziv sta obvezna.", parent=self.window)
            return
        connection = self.receipt_dialog.panel.inventory_connect()
        stamp = date.today().isoformat() + " 00:00:00"
        try:
            cursor = connection.execute(
                """
                INSERT INTO inventory_items(
                    barcode, article_name, internal_code, stock_qty, price,
                    lot_serial, created_at, updated_at, article_kind, category,
                    supplier_name, supplier_sku, track_mode, condition_grade,
                    location, reserved_qty, acquisition_cost, price_profile_code,
                    price_status, tax_classification, active
                ) VALUES(?,?,?,0,0,'',?,?, 'PART', ?,?,?,?,?,?,0,0,?, 'ČAKA_PREJEM',?,1)
                """,
                (
                    barcode, name, self.code.get().strip(), stamp, stamp,
                    self.category.get().strip() or "SPLOŠNO",
                    self.supplier.get().strip(), self.supplier_sku.get().strip(),
                    {v: k for k, v in TRACK_LABELS.items()}[self.track.get()],
                    {v: k for k, v in CONDITION_LABELS.items()}[self.condition.get()],
                    self.location.get().strip(),
                    self.profile.get() or "SPLOŠNO",
                    {v: k for k, v in TAX_LABELS.items()}[self.tax_classification.get()],
                ),
            )
            article_id = int(cursor.lastrowid)
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            connection.close()
            messagebox.showerror("MikroFIX Office", "Ta barkoda že obstaja.", parent=self.window)
            return
        connection.close()
        self.window.destroy()
        self.receipt_dialog.refresh_articles(select_id=article_id)


def build_receipts_tab(parent, **kwargs):
    return ReceiptPanel(parent, **kwargs)
