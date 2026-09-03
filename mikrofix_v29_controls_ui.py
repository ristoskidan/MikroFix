"""Office nadzorne strani v29: cenovni profili, zaloga in rabljene naprave."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from mikrofix_v29_core import (
    HarvestPart,
    V29Error,
    complete_refurbishment,
    harvest_donor_device,
    start_refurbishment,
    update_price_profile,
)
from mikrofix_v30_inventory import (
    InventoryExceptionLine,
    create_inventory_exception,
    storno_inventory_exception,
)


def _connect(path: Path):
    connection = sqlite3.connect(str(path), timeout=20)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _fit(window, width=980, height=700):
    window.update_idletasks()
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    width, height = min(width, sw - 70), min(height, sh - 90)
    window.geometry(f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//3)}")
    window.minsize(min(760, width), min(520, height))
    window.resizable(True, True)


class PriceProfilesPanel:
    def __init__(self, parent, *, inventory_db, colors):
        self.inventory_db = Path(inventory_db)
        self.colors = colors
        self._build(parent)
        self.refresh()

    def _build(self, parent):
        page = tk.Frame(parent, bg=self.colors["bg"])
        page.pack(fill="both", expand=True, padx=14, pady=12)
        actions = tk.Frame(page, bg=self.colors["panel"])
        actions.pack(fill="x", pady=(0, 8))
        tk.Label(
            actions, text="DINAMIČNI CENOVNI PROFILI",
            bg=self.colors["panel"], fg=self.colors["dark"],
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left", padx=10, pady=9)
        ttk.Button(
            actions, text="UREDI IZBRANI PROFIL", command=self.edit,
            style="Blue.TButton",
        ).pack(side="right", padx=10, pady=7)
        self.tree = ttk.Treeview(
            page,
            columns=("code", "target", "hard", "contribution", "consumables", "reserve", "round", "review"),
            show="headings",
        )
        for col, text, width in (
            ("code", "Profil", 230), ("target", "Ciljna marža", 105),
            ("hard", "Trdi minimum", 105), ("contribution", "Min. prispevek", 115),
            ("consumables", "Potrošni material", 120), ("reserve", "Garancijska rezerva", 125),
            ("round", "Zaokrožanje", 100), ("review", "Revizija", 90),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.edit())
        tk.Label(
            page,
            text=(
                "Profili dajejo priporočilo ob prejemu blaga. Končno ceno uporabnik "
                "potrdi v Office; sprememba profila ne spreminja že zaklenjenih cen."
            ),
            bg=self.colors["bg"], fg=self.colors["gray"], font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(8, 0))

    def refresh(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        connection = _connect(self.inventory_db)
        rows = connection.execute(
            """
            SELECT code, label, target_margin, hard_margin, min_contribution_eur,
                   consumables_eur, warranty_reserve, round_step_eur, needs_review
            FROM price_profiles WHERE active=1 ORDER BY code
            """
        ).fetchall()
        connection.close()
        for row in rows:
            self.tree.insert("", "end", values=(
                f"{row[0]} — {row[1]}", f"{row[2]*100:.1f} %", f"{row[3]*100:.1f} %",
                f"{row[4]:.2f} €", f"{row[5]:.2f} €", f"{row[6]*100:.1f} %",
                f"{row[7]:.2f} €", "ZA PREGLED" if row[8] else "POTRJENO",
            ), tags=(str(row[0]),))

    def edit(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("MikroFIX Office", "Izberi cenovni profil.")
            return
        code = self.tree.item(selected[0], "tags")[0]
        connection = _connect(self.inventory_db)
        row = connection.execute(
            """
            SELECT code, label, target_margin, hard_margin, min_contribution_eur,
                   consumables_eur, warranty_reserve, round_step_eur
            FROM price_profiles WHERE code=?
            """, (code,),
        ).fetchone()
        connection.close()
        win = tk.Toplevel(self.tree)
        win.title("MikroFIX Office - Cenovni profil")
        _fit(win, 620, 560)
        card = tk.Frame(win, bg=self.colors["panel"])
        card.pack(fill="both", expand=True, padx=18, pady=18)
        variables = [tk.StringVar(value=str(value)) for value in row]
        fields = (
            ("Koda", 0), ("Naziv", 1), ("Ciljna marža (0,30 = 30 %)", 2),
            ("Trdi minimum marže", 3), ("Minimalni prispevek €", 4),
            ("Potrošni material €", 5), ("Garancijska rezerva", 6),
            ("Korak zaokroževanja €", 7),
        )
        for index, (label, var_index) in enumerate(fields):
            tk.Label(card, text=label, bg=self.colors["panel"], fg=self.colors["gray"]).grid(
                row=index, column=0, sticky="w", padx=10, pady=7
            )
            entry = ttk.Entry(card, textvariable=variables[var_index])
            entry.grid(row=index, column=1, sticky="ew", padx=10, pady=7)
            if var_index == 0:
                entry.configure(state="readonly")
        card.grid_columnconfigure(1, weight=1)

        def save():
            try:
                update_price_profile(
                    self.inventory_db, code=variables[0].get(), label=variables[1].get(),
                    target_margin=variables[2].get(), hard_margin=variables[3].get(),
                    min_contribution_eur=variables[4].get(), consumables_eur=variables[5].get(),
                    warranty_reserve=variables[6].get(), round_step_eur=variables[7].get(),
                )
            except Exception as exc:
                messagebox.showerror("MikroFIX Office", str(exc), parent=win)
                return
            win.destroy()
            self.refresh()

        ttk.Button(card, text="SHRANI PROFIL", command=save, style="Blue.TButton").grid(
            row=len(fields), column=1, sticky="e", padx=10, pady=12
        )


class StockControlPanel:
    LABELS = {
        "ODPIS": "Odpis",
        "INTERNA_PORABA": "Interna poraba",
    }

    def __init__(self, parent, *, inventory_db, operator_code, colors, on_changed=None):
        self.inventory_db = Path(inventory_db)
        self.operator_code = operator_code
        self.colors = colors
        self.on_changed = on_changed
        self._build(parent)
        self.refresh()

    def _build(self, parent):
        page = tk.Frame(parent, bg=self.colors["bg"])
        page.pack(fill="both", expand=True, padx=14, pady=12)
        actions = tk.Frame(page, bg=self.colors["panel"])
        actions.pack(fill="x", pady=(0, 8))
        tk.Label(
            actions, text="ODPIS IN INTERNA PORABA",
            bg=self.colors["panel"], fg=self.colors["dark"],
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left", padx=10, pady=9)
        ttk.Button(actions, text="NOV DOKUMENT", command=self.new, style="Blue.TButton").pack(side="left", padx=6)
        ttk.Button(actions, text="STORNIRAJ IZBRANI", command=self.storno).pack(side="left", padx=6)
        ttk.Button(actions, text="OSVEŽI", command=self.refresh).pack(side="left", padx=6)
        self.documents = ttk.Treeview(
            page, columns=("id", "no", "type", "status", "reason", "date"), show="headings", height=9,
        )
        for col, text, width in (
            ("id", "ID", 0), ("no", "Dokument", 140), ("type", "Vrsta", 170),
            ("status", "Status", 110), ("reason", "Razlog", 390), ("date", "Datum", 155),
        ):
            self.documents.heading(col, text=text)
            self.documents.column(col, width=width, minwidth=0 if col == "id" else 70)
        self.documents.pack(fill="both", expand=True)
        tk.Label(
            page,
            text=(
                "Neposredni popravek količine ni dovoljen. Ta zaslon samo zmanjšuje "
                "zalogo s potrjenim odpisom ali interno porabo; inventura je ločen postopek."
            ),
            bg=self.colors["bg"], fg=self.colors["gray"], font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(8, 0))

    def refresh(self):
        for iid in self.documents.get_children():
            self.documents.delete(iid)
        connection = _connect(self.inventory_db)
        rows = connection.execute(
            """
            SELECT id, document_no, document_type, status, reason, created_at
            FROM inventory_exception_documents ORDER BY id DESC
            """
        ).fetchall()
        connection.close()
        for row in rows:
            self.documents.insert("", "end", values=(
                row[0], row[1], self.LABELS.get(row[2], row[2]), row[3], row[4], row[5],
            ))

    def selected_id(self):
        selected = self.documents.selection()
        if not selected:
            return None
        return int(self.documents.item(selected[0], "values")[0])

    def storno(self):
        document_id = self.selected_id()
        if not document_id:
            messagebox.showwarning("MikroFIX Office", "Izberi dokument.")
            return
        reason = simpledialog.askstring("Storno", "Obvezen razlog storna:", parent=self.documents)
        if not reason:
            return
        try:
            number = storno_inventory_exception(
                inventory_db=self.inventory_db, document_id=document_id,
                reason=reason, operator_code=self.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        messagebox.showinfo("MikroFIX Office", f"Dokument {number} je storniran.")
        self.refresh()
        if self.on_changed:
            self.on_changed()

    def new(self):
        ExceptionDialog(self)


class ExceptionDialog:
    def __init__(self, panel: StockControlPanel):
        self.panel = panel
        self.lines = []
        self.window = tk.Toplevel(panel.documents)
        self.window.title("MikroFIX Office - Odpis ali interna poraba")
        _fit(self.window, 960, 680)
        self.kind = tk.StringVar(value=panel.LABELS["ODPIS"])
        self.reason = tk.StringVar()
        self.article = tk.StringVar()
        self.quantity = tk.StringVar(value="1")
        self.lot = tk.StringVar()
        self.article_map = {}
        self._build()
        self._load_articles()

    def _build(self):
        page = tk.Frame(self.window, bg=self.panel.colors["bg"])
        page.pack(fill="both", expand=True, padx=16, pady=14)
        actions = tk.Frame(page, bg=self.panel.colors["bg"])
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="KNJIŽI DOKUMENT", command=self.book, style="Blue.TButton").pack(side="left")
        ttk.Button(actions, text="PREKLIČI", command=self.window.destroy).pack(side="right")
        form = tk.Frame(page, bg=self.panel.colors["panel"])
        form.pack(fill="x", pady=(0, 8))
        ttk.Combobox(form, textvariable=self.kind, values=list(self.panel.LABELS.values()), state="readonly", width=24).grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(form, textvariable=self.reason, width=64).grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        form.grid_columnconfigure(1, weight=1)
        tk.Label(form, text="Vrsta", bg=self.panel.colors["panel"], fg=self.panel.colors["gray"]).grid(row=1, column=0)
        tk.Label(form, text="Obvezen razlog", bg=self.panel.colors["panel"], fg=self.panel.colors["gray"]).grid(row=1, column=1)
        add = tk.Frame(page, bg=self.panel.colors["panel"])
        add.pack(fill="x", pady=(0, 8))
        self.article_combo = ttk.Combobox(add, textvariable=self.article, state="readonly", width=60)
        self.article_combo.pack(side="left", padx=8, pady=8, fill="x", expand=True)
        ttk.Entry(add, textvariable=self.quantity, width=12).pack(side="left", padx=6)
        ttk.Entry(add, textvariable=self.lot, width=18).pack(side="left", padx=6)
        ttk.Button(add, text="DODAJ", command=self.add_line).pack(side="left", padx=8)
        self.hint = tk.Label(
            page, text="Vpiši dejansko količino za odpis ali interno porabo. Pozitivni popravek zaloge tukaj ni mogoč.",
            bg=self.panel.colors["bg"], fg=self.panel.colors["gray"], font=("Segoe UI", 9),
        )
        self.hint.pack(anchor="w", pady=(0, 6))
        self.tree = ttk.Treeview(
            page, columns=("id", "article", "before", "input", "delta", "lot"), show="headings",
        )
        for col, text, width in (
            ("id", "ID", 0), ("article", "Artikel", 360), ("before", "Stanje prej", 100),
            ("input", "Vnos", 100), ("delta", "Premik", 100), ("lot", "LOT / serijska", 180),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=0 if col == "id" else 70)
        self.tree.pack(fill="both", expand=True)
        ttk.Button(page, text="ODSTRANI IZBRANO", command=self.remove).pack(anchor="w", pady=(8, 0))

    def _load_articles(self):
        connection = _connect(self.panel.inventory_db)
        rows = connection.execute(
            """
            SELECT id, barcode, article_name, stock_qty, lot_serial
            FROM inventory_items WHERE active=1 ORDER BY article_name
            """
        ).fetchall()
        connection.close()
        for row in rows:
            display = f"{row[2]} | {row[1]} | zaloga {row[3]:g}"
            self.article_map[display] = row
        self.article_combo.configure(values=list(self.article_map))

    def add_line(self):
        row = self.article_map.get(self.article.get())
        if not row:
            messagebox.showwarning("MikroFIX Office", "Izberi artikel.", parent=self.window)
            return
        try:
            entered = float(self.quantity.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("MikroFIX Office", "Vnos mora biti številka.", parent=self.window)
            return
        if entered < 0:
            messagebox.showwarning("MikroFIX Office", "Vnos ne sme biti negativen.", parent=self.window)
            return
        code = {value: key for key, value in self.panel.LABELS.items()}[self.kind.get()]
        delta = -entered
        if abs(delta) < 1e-9:
            messagebox.showinfo("MikroFIX Office", "Ta vrstica ne bi spremenila zaloge.", parent=self.window)
            return
        lot = self.lot.get().strip() or str(row[4] or "")
        self.lines.append(InventoryExceptionLine(int(row[0]), delta, lot))
        self.tree.insert("", "end", values=(row[0], row[2], f"{row[3]:g}", f"{entered:g}", f"{delta:+g}", lot))
        self.quantity.set("1")
        self.lot.set("")

    def remove(self):
        selected = list(self.tree.selection())
        indices = sorted((self.tree.index(iid) for iid in selected), reverse=True)
        for index in indices:
            del self.lines[index]
        for iid in selected:
            self.tree.delete(iid)

    def book(self):
        code = {value: key for key, value in self.panel.LABELS.items()}[self.kind.get()]
        try:
            _document_id, number = create_inventory_exception(
                inventory_db=self.panel.inventory_db, document_type=code,
                reason=self.reason.get(), lines=self.lines,
                operator_code=self.panel.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        self.window.destroy()
        self.panel.refresh()
        if self.panel.on_changed:
            self.panel.on_changed()
        messagebox.showinfo("MikroFIX Office", f"Dokument {number} je knjižen.")


class UsedDevicesPanel:
    PURPOSES = {"UNDECIDED": "Še ni odločeno", "REFURBISH": "Obnova", "PARTS": "Rezervni deli"}

    def __init__(self, parent, *, service_db, inventory_db, operator_code, colors, on_changed=None):
        self.service_db = Path(service_db)
        self.inventory_db = Path(inventory_db)
        self.operator_code = operator_code
        self.colors = colors
        self.on_changed = on_changed
        self._build(parent)
        self.refresh()

    def _build(self, parent):
        page = tk.Frame(parent, bg=self.colors["bg"])
        page.pack(fill="both", expand=True, padx=14, pady=12)
        actions = tk.Frame(page, bg=self.colors["panel"])
        actions.pack(fill="x", pady=(0, 8))
        tk.Label(actions, text="ODKUPLJENE NAPRAVE", bg=self.colors["panel"], fg=self.colors["dark"], font=("Segoe UI", 15, "bold")).pack(side="left", padx=10, pady=9)
        ttk.Button(actions, text="NAMEN NAPRAVE", command=self.set_purpose).pack(side="left", padx=5)
        ttk.Button(actions, text="ZAČNI OBNOVO", command=self.start_refurb, style="Blue.TButton").pack(side="left", padx=5)
        ttk.Button(actions, text="ZAKLJUČI OBNOVO", command=self.complete_refurb).pack(side="left", padx=5)
        ttk.Button(actions, text="RAZSTAVI ZA DELE", command=self.harvest).pack(side="left", padx=5)
        ttk.Button(actions, text="OSVEŽI", command=self.refresh).pack(side="left", padx=5)
        self.tree = ttk.Treeview(
            page, columns=("id", "code", "buyback", "model", "serial", "imei", "purpose", "status", "cost", "sale"), show="headings",
        )
        for col, text, width in (
            ("id", "ID", 0), ("code", "Zalogovna oznaka", 145), ("buyback", "Odkup", 135),
            ("model", "Model", 170), ("serial", "Serijska", 150), ("imei", "IMEI", 145),
            ("purpose", "Namen", 125), ("status", "Status", 125), ("cost", "Odkup", 90), ("sale", "Prodajna", 95),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=0 if col == "id" else 65)
        self.tree.pack(fill="both", expand=True)
        tk.Label(
            page, text="Nova odkupljena naprava vedno vstopi v KARANTENO. Obnova in pridobivanje delov sta ločena sledljiva postopka.",
            bg=self.colors["bg"], fg=self.colors["gray"], font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(8, 0))

    def refresh(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        connection = _connect(self.inventory_db)
        rows = connection.execute(
            """
            SELECT id, inventory_code, buyback_no, model, serial, imei,
                   purpose, status, acquisition_cost, sale_price
            FROM used_devices ORDER BY id DESC
            """
        ).fetchall()
        connection.close()
        for row in rows:
            self.tree.insert("", "end", values=(
                *row[:6], self.PURPOSES.get(row[6], row[6]), row[7],
                f"{row[8]:.2f} €", "-" if row[9] is None else f"{row[9]:.2f} €",
            ))

    def selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("MikroFIX Office", "Izberi odkupljeno napravo.")
            return None
        return self.tree.item(selected[0], "values")

    def set_purpose(self):
        values = self.selected()
        if not values:
            return
        chosen = simpledialog.askstring(
            "Namen naprave", "Vpiši OBNOVA, DELI ali NEODLOČENO:", parent=self.tree
        )
        mapping = {"OBNOVA": "REFURBISH", "DELI": "PARTS", "NEODLOČENO": "UNDECIDED"}
        purpose = mapping.get(str(chosen or "").strip().upper())
        if not purpose:
            return
        connection = _connect(self.inventory_db)
        row = connection.execute("SELECT status FROM used_devices WHERE id=?", (int(values[0]),)).fetchone()
        if not row or row[0] not in {"QUARANTINE", "DIAGNOSTICS"}:
            connection.close()
            messagebox.showerror("MikroFIX Office", "Namena v tem statusu ni več mogoče spreminjati.")
            return
        connection.execute("UPDATE used_devices SET purpose=?, updated_at=datetime('now','localtime') WHERE id=?", (purpose, int(values[0])))
        connection.commit()
        connection.close()
        self.refresh()

    def start_refurb(self):
        values = self.selected()
        if not values:
            return
        try:
            _order_id, number = start_refurbishment(
                service_db=self.service_db, inventory_db=self.inventory_db,
                device_id=int(values[0]), operator_code=self.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        messagebox.showinfo("MikroFIX Office", f"Odprta je naloga obnove {number}.")
        self.refresh()

    def complete_refurb(self):
        values = self.selected()
        if not values:
            return
        connection = _connect(self.service_db)
        row = connection.execute(
            """
            SELECT id, order_no FROM refurbishment_orders_v29
            WHERE used_device_id=? AND status<>'COMPLETED' ORDER BY id DESC LIMIT 1
            """, (int(values[0]),),
        ).fetchone()
        connection.close()
        if not row:
            messagebox.showwarning("MikroFIX Office", "Za napravo ni odprte obnove.")
            return
        labor = simpledialog.askfloat("Obnova", "Strošek dela €:", minvalue=0, parent=self.tree)
        if labor is None:
            return
        parts = simpledialog.askfloat("Obnova", "Strošek delov €:", minvalue=0, parent=self.tree)
        if parts is None:
            return
        other = simpledialog.askfloat("Obnova", "Drugi stroški €:", minvalue=0, parent=self.tree)
        if other is None:
            return
        sale = simpledialog.askfloat("Obnova", "Zaklenjena prodajna cena €:", minvalue=0, parent=self.tree)
        if sale is None:
            return
        if not messagebox.askyesno("Končni test", "Ali je naprava uspešno opravila celoten končni test?"):
            return
        try:
            number, total = complete_refurbishment(
                service_db=self.service_db, inventory_db=self.inventory_db,
                order_id=int(row[0]), labor_cost=labor, parts_cost=parts,
                other_cost=other, sale_price=sale, tests_passed=True,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc))
            return
        messagebox.showinfo("MikroFIX Office", f"Obnova {number} je zaključena. Skupni strošek: {total:.2f} €.")
        self.refresh()

    def harvest(self):
        values = self.selected()
        if not values:
            return
        DonorDialog(self, int(values[0]), str(values[1]))


class DonorDialog:
    def __init__(self, panel: UsedDevicesPanel, device_id: int, device_code: str):
        self.panel, self.device_id, self.device_code = panel, device_id, device_code
        self.parts = []
        self.article_map = {}
        self.window = tk.Toplevel(panel.tree)
        self.window.title(f"Razstavljanje {device_code}")
        _fit(self.window, 900, 650)
        self.article = tk.StringVar()
        self.grade = tk.StringVar(value="B")
        self.lot = tk.StringVar()
        self.location = tk.StringVar(value="KARANTENA-DELI")
        self._build()
        self._load()

    def _build(self):
        page = tk.Frame(self.window, bg=self.panel.colors["bg"])
        page.pack(fill="both", expand=True, padx=14, pady=12)
        actions = tk.Frame(page, bg=self.panel.colors["bg"])
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="ZAKLJUČI RAZSTAVLJANJE", command=self.finish, style="Blue.TButton").pack(side="left")
        ttk.Button(actions, text="PREKLIČI", command=self.window.destroy).pack(side="right")
        form = tk.Frame(page, bg=self.panel.colors["panel"])
        form.pack(fill="x", pady=(0, 8))
        self.combo = ttk.Combobox(form, textvariable=self.article, state="readonly", width=55)
        self.combo.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Combobox(form, textvariable=self.grade, values=["A", "B", "C"], state="readonly", width=6).pack(side="left", padx=5)
        ttk.Entry(form, textvariable=self.lot, width=18).pack(side="left", padx=5)
        ttk.Entry(form, textvariable=self.location, width=18).pack(side="left", padx=5)
        ttk.Button(form, text="DODAJ TESTIRAN DEL", command=self.add).pack(side="left", padx=8)
        self.tree = ttk.Treeview(page, columns=("id", "article", "grade", "test", "lot", "location"), show="headings")
        for col, text, width in (
            ("id", "ID", 0), ("article", "Artikel", 350), ("grade", "Razred", 80),
            ("test", "Test", 90), ("lot", "LOT / serijska", 150), ("location", "Lokacija", 140),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=0 if col == "id" else 60)
        self.tree.pack(fill="both", expand=True)
        ttk.Button(page, text="ODSTRANI IZBRANO", command=self.remove).pack(anchor="w", pady=(8, 0))
        tk.Label(page, text="V zalogo se prenesejo samo deli z opravljenim testom OK. Odkupna cena se enakomerno razdeli med vse dodane uporabne dele.", bg=self.panel.colors["bg"], fg=self.panel.colors["gray"], font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))

    def _load(self):
        connection = _connect(self.panel.inventory_db)
        rows = connection.execute("SELECT id, barcode, article_name FROM inventory_items WHERE active=1 ORDER BY article_name").fetchall()
        connection.close()
        for row in rows:
            display = f"{row[2]} | {row[1]}"
            self.article_map[display] = row
        self.combo.configure(values=list(self.article_map))

    def add(self):
        row = self.article_map.get(self.article.get())
        if not row:
            messagebox.showwarning("MikroFIX Office", "Izberi artikel.", parent=self.window)
            return
        part = HarvestPart(int(row[0]), self.grade.get(), "OK", self.lot.get().strip(), self.location.get().strip())
        self.parts.append(part)
        self.tree.insert("", "end", values=(row[0], row[2], part.condition_grade, "OK", part.lot_serial, part.location))
        self.lot.set("")

    def remove(self):
        selected = list(self.tree.selection())
        indices = sorted((self.tree.index(iid) for iid in selected), reverse=True)
        for index in indices:
            del self.parts[index]
        for iid in selected:
            self.tree.delete(iid)

    def finish(self):
        try:
            _harvest_id, number, allocated = harvest_donor_device(
                inventory_db=self.panel.inventory_db, device_id=self.device_id,
                parts=self.parts, operator_code=self.panel.operator_code,
            )
        except Exception as exc:
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.window)
            return
        self.window.destroy()
        self.panel.refresh()
        if self.panel.on_changed:
            self.panel.on_changed()
        messagebox.showinfo("MikroFIX Office", f"Zapisnik {number} je zaključen. Osnovna razdelitev: {allocated:.2f} € na del.")


def build_price_profiles_tab(parent, **kwargs):
    return PriceProfilesPanel(parent, **kwargs)


def build_stock_control_tab(parent, **kwargs):
    return StockControlPanel(parent, **kwargs)


def build_used_devices_tab(parent, **kwargs):
    return UsedDevicesPanel(parent, **kwargs)
