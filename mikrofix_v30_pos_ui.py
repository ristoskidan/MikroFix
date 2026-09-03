"""Dedicated responsive cashier window for MikroFIX v30."""

from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import uuid
import tkinter as tk
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from mikrofix_printing import preferred_windows_printer, send_pdf_to_windows_printer
from mikrofix_upnqr import UpnQrError, generate_upnqr_pdf
from mikrofix_v30_cash import (
    book_cash_disbursement,cash_session_snapshot,close_cash_session,
    generate_cash_journal_pdf,open_cash_session,pending_cash_disbursements,
    record_cash_entry,
)
from mikrofix_v30_pos import (
    PosError, PosLine, abandon_pos_draft, calculate_line, dec,
    default_service_description, ensure_pos_schema,
    generate_pos_pdfs, generate_pos_preview_pdfs, issue_invoice,
    eligible_storned_invoices, latest_pending_print, load_active_pos_draft,
    mark_pdf_ready, mark_printed, register_copy, save_pos_draft,
    prepare_used_goods_agreement, set_used_goods_agreement_signed,
    storno_invoice,
    search_inventory, service_price,
)


BG = "#F2F4F6"
PANEL = "#FFFFFF"
BLUE = "#2F80C7"
DARK = "#2E3439"
GRAY = "#606A73"
GREEN = "#2C7A4B"
RED = "#A63A3A"

PAYMENT_LABELS = {
    "CASH": "Gotovina",
    "BANK_TRANSFER": "Bančno nakazilo (TRR)",
    "SPLIT": "Razdeljeno plačilo",
}


class PosWindow:
    def __init__(
        self, parent, *, runtime, office_db, inventory_db, service_db, pdf_dir,
        operator_code, service_id=None, service_loader=None, service_finalizer=None,
        on_close=None,
    ):
        self.parent = parent
        self.runtime = runtime
        self.office_db = Path(office_db)
        self.inventory_db = Path(inventory_db)
        self.service_db = Path(service_db)
        self.pdf_dir = Path(pdf_dir)
        self.operator_code = str(operator_code or "-")
        self.service_id = service_id
        self.service_loader = service_loader
        self.service_finalizer = service_finalizer
        self.on_close_callback = on_close
        self.lines: list[PosLine] = []
        self.used_device_ids: set[int] = set()
        self.used_agreement_id = None
        self.used_agreement_path = None
        self.issue_token = str(uuid.uuid4())
        self.payment_reference = self._reference_for_token(self.issue_token)
        self.issued_id = None
        self.issued_no = ""
        self.print_confirmed = False
        self.review_ready = False
        self.a4_path = None
        self.receipt_path = None
        self.preview_a4_path = None
        self.preview_receipt_path = None
        self.review_fingerprint = ""
        self.replacement_of_invoice_id = None
        self.replacement_of_invoice_no = ""
        self._balancing_payment = False
        self._restoring_draft = True
        self._draft_after_id = None
        self._draft_started = False
        configured_premises = list(
            self.runtime.settings.get("business", {}).get("premises") or []
        )
        self.premise_choices = [str(value).strip() for value in configured_premises if str(value).strip()]
        self.premise_var = tk.StringVar(value=str(self.runtime.active_premise))

        ensure_pos_schema(self.office_db)
        parent.withdraw()
        self.win = tk.Toplevel(parent)
        self.win.title("MikroFIX – Blagajna")
        self.win.configure(bg=BG)
        self.win.minsize(1050, 650)
        try:
            self.win.state("zoomed")
        except Exception:
            self.win.geometry("1280x760")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<F10>", self.on_f10)
        self.win.bind("<F5>", lambda _e: self.new_invoice())
        self.win.bind("<Shift-F5>", lambda _e: self.choose_replacement_source())
        self.win.bind("<F6>", lambda _e: self.choose_ready_service())
        self.win.bind("<F7>", lambda _e: self.edit_discount())
        self.win.bind("<F8>", self.print_upn_qr)
        self.win.bind("<F9>", lambda _e: self.print_copy())
        self.win.bind("<F1>", lambda _e: self.search_entry.focus_set())
        self.win.bind("<F2>", lambda _e: self.add_selected_stock())
        self.win.bind("<F3>", lambda _e: self.add_service("T1"))
        self.win.bind("<F4>", lambda _e: self.add_service("T2"))
        self.win.bind("<F11>", lambda _e: self.change_qty(1))
        self.win.bind("<F12>", lambda _e: self.change_qty(-1))
        self.win.bind("<Delete>", lambda _e: self.remove_line())
        self._build()
        self._restoring_draft = False
        self.refresh_stock()
        pending = latest_pending_print(self.office_db)
        if pending:
            self._resume_pending_print(pending)
        elif service_id and service_loader:
            self._load_service(service_id)
            self._save_draft_now()
        else:
            self._offer_draft_restore()
        self.search_entry.focus_set()

    def _paper_width(self) -> int:
        """Vrne podprto širino papirja tudi pri prazni ali poškodovani nastavitvi."""
        raw = self.runtime.settings.get("printer", {}).get("paper_width_mm", 80)
        try:
            width = int(raw)
        except (TypeError, ValueError):
            width = 80
        return width if width in (58, 80) else 80

    def _build(self):
        header = tk.Frame(self.win, bg=DARK, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="BLAGAJNA", bg=DARK, fg="white", font=("Segoe UI", 20, "bold")).pack(side="left", padx=20)
        self.header_status = tk.StringVar(
            value=(
                f"Uporabnik: {self.operator_code}  •  "
                f"{self.premise_var.get()}/{self.runtime.electronic_device}  •  "
                "TEST – FURS izklopljen"
            )
        )
        tk.Label(header, textvariable=self.header_status, bg=DARK, fg="#D9E3EA", font=("Segoe UI", 9)).pack(side="left", padx=15)
        ttk.Button(header, text="NAZAJ", command=self.close).pack(side="right", padx=16, pady=12)
        ttk.Button(
            header,text="BLAGAJNIŠKI DNEVNIK",command=self.manage_cash_journal
        ).pack(side="right",padx=(0,8),pady=12)
        self.cash_status_var=tk.StringVar(value="GOTOVINA: preverjanje …")
        tk.Label(
            header,textvariable=self.cash_status_var,bg=DARK,fg="#F4D35E",
            font=("Segoe UI",8,"bold"),
        ).pack(side="right",padx=8)
        premise_box = tk.Frame(header, bg=DARK)
        premise_box.pack(side="right", padx=(8, 0), pady=9)
        tk.Label(
            premise_box, text="KRAJ IZDAJE RAČUNA", bg=DARK, fg="#D9E3EA",
            font=("Segoe UI", 7, "bold"),
        ).pack(anchor="w")
        self.premise_combo = ttk.Combobox(
            premise_box, textvariable=self.premise_var,
            values=self.premise_choices, state="readonly", width=10,
        )
        self.premise_combo.pack()
        self.premise_combo.bind("<<ComboboxSelected>>", self.premise_changed)

        workspace = tk.Frame(self.win, bg=BG)
        workspace.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        rail = tk.Frame(workspace, bg=DARK, width=132)
        rail.pack(side="left", fill="y", padx=(0, 8))
        rail.pack_propagate(False)
        for text_value, command in (
            ("F1  ISKANJE", lambda: self.search_entry.focus_set()),
            ("F2  DODAJ ARTIKEL", self.add_selected_stock),
            ("F3  + T1 DELO", lambda: self.add_service("T1")),
            ("F4  + T2 TEREN", lambda: self.add_service("T2")),
            ("F5  NOV RAČUN", self.new_invoice),
            ("⇄ VEŽI NA STORNO", self.choose_replacement_source),
            ("F6  SERVIS ZA RAČUN", self.choose_ready_service),
            ("F7  RABAT", self.edit_discount),
            ("F8  UPN QR", self.print_upn_qr),
            ("F9  KOPIJA", self.print_copy),
            ("F10 PREGLED / IZDAJA", self.on_f10),
            ("F11 KOLIČINA +", lambda: self.change_qty(1)),
            ("F12 KOLIČINA –", lambda: self.change_qty(-1)),
            ("DEL ODSTRANI", self.remove_line),
        ):
            tk.Button(
                rail, text=text_value, command=command, bg=DARK, fg="white",
                activebackground=BLUE, activeforeground="white", relief="flat",
                font=("Segoe UI", 8, "bold"), justify="left", anchor="w",
                padx=8, pady=5,
            ).pack(fill="x", padx=4, pady=(4, 0))
        body = tk.PanedWindow(workspace, orient="horizontal", sashwidth=6, bg=BG, bd=0)
        body.pack(side="left", fill="both", expand=True)
        left = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground="#D8DEE3")
        right = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground="#D8DEE3")
        body.add(left, minsize=390, width=500)
        body.add(right, minsize=580)

        tk.Label(left, text="ISKANJE / SKENIRANJE ZALOGE", bg=PANEL, fg=DARK, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 5))
        search_row = tk.Frame(left, bg=PANEL)
        search_row.pack(fill="x", padx=12, pady=(0, 8))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var, font=("Segoe UI", 12))
        self.search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="IŠČI", command=self.refresh_stock).pack(side="left", padx=(7, 0))
        self.search_entry.bind("<Return>", self.search_enter)
        self.search_var.trace_add("write", lambda *_: self.win.after(120, self.refresh_stock))

        stock_wrap = tk.Frame(left, bg=PANEL)
        stock_wrap.pack(fill="both", expand=True, padx=12)
        cols = ("barcode", "name", "stock", "price")
        self.stock_tree = ttk.Treeview(stock_wrap, columns=cols, show="headings", selectmode="browse")
        for key, text, width, anchor in (
            ("barcode", "Barkoda/koda", 120, "w"), ("name", "Artikel", 240, "w"),
            ("stock", "Na voljo", 65, "e"), ("price", "Cena", 75, "e"),
        ):
            self.stock_tree.heading(key, text=text)
            self.stock_tree.column(key, width=width, anchor=anchor, stretch=key == "name")
        sy = ttk.Scrollbar(stock_wrap, orient="vertical", command=self.stock_tree.yview)
        self.stock_tree.configure(yscrollcommand=sy.set)
        self.stock_tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.stock_tree.bind("<Double-1>", lambda _e: self.add_selected_stock())
        self.stock_tree.bind("<Return>", lambda _e: self.add_selected_stock())
        self.stock_rows = {}

        tk.Label(right, text="RAČUN – POSTAVKE", bg=PANEL, fg=DARK, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 5))
        cart_wrap = tk.Frame(right, bg=PANEL)
        cart_wrap.pack(fill="both", expand=True, padx=12)
        cols = ("source", "name", "qty", "unit", "discount", "total")
        self.cart_tree = ttk.Treeview(cart_wrap, columns=cols, show="headings", selectmode="browse", height=9)
        for key, text, width, anchor in (
            ("source", "Izvor", 105, "w"), ("name", "Postavka", 235, "w"),
            ("qty", "Kol.", 55, "e"),
            ("unit", "Cena", 80, "e"), ("discount", "Rabat", 75, "e"), ("total", "Znesek", 90, "e"),
        ):
            self.cart_tree.heading(key, text=text)
            self.cart_tree.column(key, width=width, anchor=anchor, stretch=key == "name")
        cy = ttk.Scrollbar(cart_wrap, orient="vertical", command=self.cart_tree.yview)
        self.cart_tree.configure(yscrollcommand=cy.set)
        self.cart_tree.pack(side="left", fill="both", expand=True)
        cy.pack(side="right", fill="y")
        self.cart_tree.bind("<Double-1>", lambda _e: self.edit_discount())

        details = tk.Frame(right, bg="#F7F9FA", highlightthickness=1, highlightbackground="#E1E5E8")
        details.pack(fill="x", padx=12, pady=(0, 8))
        self.customer_name = tk.StringVar(value="Končni kupec")
        self.customer_address = tk.StringVar()
        self.customer_phone = tk.StringVar()
        self.customer_email = tk.StringVar()
        self.customer_tax = tk.StringVar()
        fields = (
            ("Kupec", self.customer_name), ("Naslov", self.customer_address),
            ("Telefon", self.customer_phone), ("E-pošta", self.customer_email),
            ("Davčna št. kupca (če je potrebna)", self.customer_tax),
        )
        for i, (label, var) in enumerate(fields):
            tk.Label(details, text=label, bg="#F7F9FA", fg=GRAY, font=("Segoe UI", 8, "bold")).grid(row=(i//2)*2, column=i%2, sticky="w", padx=9, pady=(7,1))
            ttk.Entry(details, textvariable=var).grid(row=(i//2)*2+1, column=i%2, sticky="ew", padx=9, pady=(0,7))
            var.trace_add("write", lambda *_: self.invalidate_review())
        details.columnconfigure(0, weight=1)
        details.columnconfigure(1, weight=1)
        self.replacement_var = tk.StringVar(value="Nadomestni račun: ni povezave")
        tk.Label(
            details, textvariable=self.replacement_var, bg="#F7F9FA", fg=BLUE,
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).grid(row=6, column=0, columnspan=2, sticky="ew", padx=9, pady=(0, 7))

        payment = tk.Frame(right, bg=PANEL)
        payment.pack(fill="x", padx=12, pady=(0, 7))
        self.payment_mode = tk.StringVar(value="CASH")
        tk.Label(payment, text="Plačilo", bg=PANEL, fg=GRAY, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        for col, (text, value) in enumerate((("Gotovina", "CASH"), ("TRR", "BANK_TRANSFER"), ("Razdeljeno", "SPLIT")), start=1):
            ttk.Radiobutton(payment, text=text, variable=self.payment_mode, value=value, command=self.payment_changed).grid(row=0, column=col, padx=5)
        self.cash_amount = tk.StringVar(value="0.00")
        self.bank_amount = tk.StringVar(value="0.00")
        self.cash_received = tk.StringVar(value="0.00")
        self.bank_received = tk.BooleanVar(value=False)
        tk.Label(payment, text="Gotovina na računu", bg=PANEL, fg=GRAY).grid(row=1, column=0, sticky="w", pady=(7,0))
        self.cash_entry = ttk.Entry(payment, textvariable=self.cash_amount, width=11)
        self.cash_entry.grid(row=1, column=1, sticky="w", pady=(7,0))
        tk.Label(payment, text="TRR", bg=PANEL, fg=GRAY).grid(row=1, column=2, sticky="e", pady=(7,0))
        self.bank_entry = ttk.Entry(payment, textvariable=self.bank_amount, width=11)
        self.bank_entry.grid(row=1, column=3, sticky="w", pady=(7,0))
        tk.Label(payment, text="Prejeti bankovec", bg=PANEL, fg=GRAY).grid(row=2, column=0, sticky="w", pady=(5,0))
        ttk.Entry(payment, textvariable=self.cash_received, width=11).grid(row=2, column=1, sticky="w", pady=(5,0))
        self.change_var = tk.StringVar(value="Vračilo: 0,00 EUR")
        tk.Label(payment, textvariable=self.change_var, bg=PANEL, fg=GREEN, font=("Segoe UI", 9, "bold")).grid(row=2, column=2, columnspan=2, sticky="e", pady=(5,0))
        self.bank_received_check = ttk.Checkbutton(
            payment,
            text="Nakazilo je dejansko prejeto na TRR",
            variable=self.bank_received,
            command=self.invalidate_review,
        )
        self.bank_received_check.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.bank_amount.trace_add("write", self._bank_amount_changed)
        self.cash_amount.trace_add("write", self._payment_field_changed)
        self.cash_received.trace_add("write", self._payment_field_changed)

        self.used_signed = tk.BooleanVar(value=False)
        self.used_frame = tk.Frame(right, bg=PANEL)
        self.used_check = ttk.Checkbutton(
            self.used_frame,
            text="Kupec je podpisal ločen dogovor za 12 mesecev (obnovljena rabljena naprava)",
            variable=self.used_signed, command=self.used_signed_changed,
        )
        self.used_check.pack(side="left", fill="x", expand=True)
        ttk.Button(
            self.used_frame, text="PRIPRAVI / NATISNI A4 DOGOVOR",
            command=self.prepare_used_agreement,
        ).pack(side="right", padx=(8,0))

        footer = tk.Frame(self.win, bg=PANEL, highlightthickness=1, highlightbackground="#D8DEE3")
        footer.pack(fill="x", padx=12, pady=(0, 10))
        self.total_var = tk.StringVar(value="SKUPAJ: 0,00 EUR")
        tk.Label(footer, textvariable=self.total_var, bg=PANEL, fg=DARK, font=("Segoe UI", 18, "bold")).pack(side="left", padx=15, pady=10)
        self.state_var = tk.StringVar(value="Dodaj postavke in odpri končni pregled.")
        tk.Label(footer, textvariable=self.state_var, bg=PANEL, fg=GRAY, font=("Segoe UI", 9)).pack(side="left", padx=15)
        self.autosave_var = tk.StringVar(value="Osnutek: brez sprememb")
        tk.Label(
            footer, textvariable=self.autosave_var, bg=PANEL, fg=GREEN,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=8)
        self.review_btn = ttk.Button(footer, text="KONČNI PREGLED", command=self.review)
        self.review_btn.pack(side="right", padx=8, pady=10)
        self.f10_btn = ttk.Button(footer, text="PREGLED / IZDAJA  F10", command=self.on_f10)
        self.f10_btn.pack(side="right", padx=(8, 0), pady=10)
        self.payment_changed()
        self.update_cash_status()

    @property
    def labor_floor(self):
        return self.runtime.settings.get("pricing", {}).get("l0_eur", 22)

    @staticmethod
    def _reference_for_token(token):
        digits = str(uuid.UUID(str(token)).int)[-16:].zfill(16)
        return f"SI00 {digits}"

    def invalidate_review(self):
        if self.issued_id:
            return
        self.review_ready = False
        self.review_fingerprint = ""
        if hasattr(self, "review_btn"):
            self.review_btn.configure(text="KONČNI PREGLED", command=self.review)
        self._schedule_draft_save()

    def _has_meaningful_draft(self):
        if self.lines or self.service_id or self.replacement_of_invoice_id:
            return True
        if self.customer_name.get().strip() not in {"", "Končni kupec"}:
            return True
        return any(
            value.get().strip()
            for value in (
                self.customer_address, self.customer_phone,
                self.customer_email, self.customer_tax,
            )
        )

    def _draft_data(self):
        return {
            "schema_version": 1,
            "lines": [
                {
                    "line_kind": line.line_kind,
                    "name": line.name,
                    "code": line.code,
                    "barcode": line.barcode,
                    "article_id": line.article_id,
                    "quantity": str(line.quantity),
                    "base_unit_price": str(line.base_unit_price),
                    "acquisition_cost": str(line.acquisition_cost),
                    "requested_discount_pct": str(line.requested_discount_pct),
                    "service_tier": line.service_tier,
                    "stock_already_consumed": bool(line.stock_already_consumed),
                    "origin_kind": line.origin_kind,
                    "origin_id": line.origin_id,
                    "source_locked": bool(line.source_locked),
                }
                for line in self.lines
            ],
            "customer": self.customer_values(),
            "payment_mode": self.payment_mode.get(),
            "cash_amount": self.cash_amount.get(),
            "bank_amount": self.bank_amount.get(),
            "cash_received": self.cash_received.get(),
            "bank_received": bool(self.bank_received.get()),
            "premise_id": self.premise_var.get(),
            "service_id": self.service_id,
            "used_device_ids": sorted(self.used_device_ids),
            "used_agreement_id": self.used_agreement_id,
            "used_agreement_path": str(self.used_agreement_path or ""),
            "used_signed": bool(self.used_signed.get()),
            "payment_reference": self.payment_reference,
            "replacement_of_invoice_id": self.replacement_of_invoice_id,
            "replacement_of_invoice_no": self.replacement_of_invoice_no,
        }

    def _schedule_draft_save(self):
        if self._restoring_draft or self.issued_id or not hasattr(self, "win"):
            return
        if self._draft_after_id is not None:
            try:
                self.win.after_cancel(self._draft_after_id)
            except Exception:
                pass
        if hasattr(self, "autosave_var"):
            self.autosave_var.set("Osnutek: shranjevanje …")
        self._draft_after_id = self.win.after(350, self._save_draft_now)

    def _save_draft_now(self):
        self._draft_after_id = None
        if self._restoring_draft or self.issued_id:
            return
        if not self._draft_started and not self._has_meaningful_draft():
            if hasattr(self, "autosave_var"):
                self.autosave_var.set("Osnutek: brez sprememb")
            return
        self._draft_started = True
        try:
            save_pos_draft(
                self.office_db,
                issue_token=self.issue_token,
                operator_code=self.operator_code,
                premise_id=self.premise_var.get(),
                device_id=self.runtime.electronic_device,
                payload=self._draft_data(),
            )
            if hasattr(self, "autosave_var"):
                from datetime import datetime
                self.autosave_var.set(
                    f"Osnutek shranjen {datetime.now().strftime('%H:%M:%S')}"
                )
        except Exception as exc:
            if hasattr(self, "autosave_var"):
                self.autosave_var.set(f"NAPAKA SHRANJEVANJA: {exc}")

    def _offer_draft_restore(self):
        try:
            stored = load_active_pos_draft(
                self.office_db,
                operator_code=self.operator_code,
                device_id=self.runtime.electronic_device,
            )
        except Exception as exc:
            messagebox.showerror("Obnovitev osnutka", str(exc), parent=self.win)
            return
        if not stored:
            return
        if not messagebox.askyesno(
            "Obnovitev osnutka",
            "Najden je varno shranjen neizdani račun iz "
            f"{str(stored['updated_at']).replace('T', ' ')}. Nadaljujem ta osnutek?",
            parent=self.win,
        ):
            abandon_pos_draft(self.office_db, str(stored["issue_token"]))
            return
        payload = stored["payload"]
        try:
            self._restoring_draft = True
            self.issue_token = str(stored["issue_token"])
            self.lines = [
                PosLine(
                    line_kind=str(item["line_kind"]),
                    name=str(item["name"]),
                    code=str(item.get("code") or ""),
                    barcode=str(item.get("barcode") or ""),
                    article_id=item.get("article_id"),
                    quantity=dec(item.get("quantity")),
                    base_unit_price=dec(item.get("base_unit_price")),
                    acquisition_cost=dec(item.get("acquisition_cost")),
                    requested_discount_pct=dec(item.get("requested_discount_pct")),
                    service_tier=str(item.get("service_tier") or ""),
                    stock_already_consumed=bool(item.get("stock_already_consumed")),
                    origin_kind=str(item.get("origin_kind") or "POS_ADDED"),
                    origin_id=item.get("origin_id"),
                    source_locked=bool(item.get("source_locked")),
                )
                for item in payload.get("lines", [])
            ]
            customer = payload.get("customer") or {}
            self.customer_name.set(str(customer.get("name") or "Končni kupec"))
            self.customer_address.set(str(customer.get("address") or ""))
            self.customer_phone.set(str(customer.get("phone") or ""))
            self.customer_email.set(str(customer.get("email") or ""))
            self.customer_tax.set(str(customer.get("tax_number") or ""))
            premise = str(payload.get("premise_id") or "")
            if premise in self.premise_choices:
                self.premise_var.set(premise)
            self.service_id = payload.get("service_id")
            self.used_device_ids = {int(value) for value in payload.get("used_device_ids", [])}
            self.used_agreement_id = payload.get("used_agreement_id")
            agreement_path = str(payload.get("used_agreement_path") or "")
            self.used_agreement_path = Path(agreement_path) if agreement_path else None
            self.used_signed.set(bool(payload.get("used_signed")))
            self.payment_reference = str(
                payload.get("payment_reference") or self._reference_for_token(self.issue_token)
            )
            self.replacement_of_invoice_id = payload.get("replacement_of_invoice_id")
            self.replacement_of_invoice_no = str(
                payload.get("replacement_of_invoice_no") or ""
            )
            if self.replacement_of_invoice_id:
                self.replacement_var.set(
                    f"Nadomestni račun za {self.replacement_of_invoice_no or '-'}"
                )
            self.payment_mode.set(str(payload.get("payment_mode") or "CASH"))
            self.payment_changed()
            self.cash_amount.set(str(payload.get("cash_amount") or "0.00"))
            self.bank_amount.set(str(payload.get("bank_amount") or "0.00"))
            self.cash_received.set(str(payload.get("cash_received") or "0.00"))
            self.bank_received.set(bool(payload.get("bank_received")))
            self.recalc()
            self._draft_started = True
            self.autosave_var.set(
                f"Osnutek obnovljen {str(stored['updated_at']).replace('T', ' ')}"
            )
            self.state_var.set("Obnovljen je neizdani račun. Pred izdajo ga ponovno preglej.")
        except Exception as exc:
            messagebox.showerror(
                "Obnovitev osnutka",
                f"Osnutka ni bilo mogoče obnoviti:\n{exc}",
                parent=self.win,
            )
        finally:
            self._restoring_draft = False

    def _payment_field_changed(self, *_args):
        if self._balancing_payment:
            return
        self.update_change()
        self.invalidate_review()

    def _bank_amount_changed(self, *_args):
        if self._balancing_payment:
            return
        if self.payment_mode.get() == "SPLIT":
            try:
                bank = dec(self.bank_amount.get())
                remainder = self.total - bank
                self._balancing_payment = True
                self.cash_amount.set(f"{max(Decimal('0'), remainder):.2f}")
            except Exception:
                pass
            finally:
                self._balancing_payment = False
        self.update_change()
        self.invalidate_review()

    def selected_premise(self):
        premise = str(self.premise_var.get() or "").strip()
        if premise not in self.premise_choices:
            raise PosError("Izberi veljavni poslovni prostor izdaje računa.")
        return premise

    def premise_changed(self, _event=None):
        if self.issued_id:
            return
        self.invalidate_review()
        self.header_status.set(
            f"Uporabnik: {self.operator_code}  •  "
            f"{self.premise_var.get()}/{self.runtime.electronic_device}  •  "
            "TEST – FURS izklopljen"
        )
        if self.premise_var.get() == "MFT":
            self.state_var.set(
                "MFT je prijavljen kot premični objekt A; račun izdaj samo v premičnem objektu."
            )
        self.update_cash_status()

    def update_cash_status(self):
        if not hasattr(self,"cash_status_var"):
            return
        try:
            session,_entries=cash_session_snapshot(
                self.office_db,premise_id=str(self.premise_var.get()),
                device_id=self.runtime.electronic_device,
            )
            if session:
                self.cash_status_var.set(
                    f"GOTOVINA: {session['session_no']} • {float(session['expected_cash']):.2f} EUR"
                )
            else:
                self.cash_status_var.set("GOTOVINA: BLAGAJNA ZAPRTA")
        except Exception as exc:
            self.cash_status_var.set(f"GOTOVINA: napaka {exc}")

    def manage_cash_journal(self):
        win=tk.Toplevel(self.win);win.title("MikroFIX – blagajniški dnevnik")
        win.geometry("980x600");win.transient(self.win)
        summary=tk.StringVar()
        tk.Label(win,textvariable=summary,font=("Segoe UI",11,"bold"),justify="left").pack(fill="x",padx=12,pady=10)
        columns=("no","time","type","ref","description","in","out","balance")
        tree=ttk.Treeview(win,columns=columns,show="headings")
        for key,label,width,anchor in (
            ("no","Št.",45,"e"),("time","Čas",75,"w"),("type","Vrsta",110,"w"),
            ("ref","Listina",110,"w"),("description","Opis",280,"w"),
            ("in","Prejemek",90,"e"),("out","Izdatek",90,"e"),("balance","Saldo",90,"e"),
        ):
            tree.heading(key,text=label);tree.column(key,width=width,anchor=anchor,stretch=key=="description")
        tree.pack(fill="both",expand=True,padx=12,pady=(0,8))
        current={"session":None}

        def refresh():
            session,entries=cash_session_snapshot(
                self.office_db,premise_id=str(self.premise_var.get()),device_id=self.runtime.electronic_device
            );current["session"]=session
            for iid in tree.get_children():tree.delete(iid)
            if not session:
                summary.set(f"{self.premise_var.get()}/{self.runtime.electronic_device} – blagajna je ZAPRTA")
            else:
                summary.set(
                    f"{session['session_no']} • odprta {str(session['opened_at']).replace('T',' ')} • "
                    f"začetno {float(session['opening_cash']):.2f} EUR • prejemki {float(session['cash_receipts']):.2f} EUR • "
                    f"izdatki {float(session['cash_expenses']):.2f} EUR • pričakovano {float(session['expected_cash']):.2f} EUR"
                )
                for x in entries:
                    amount=dec(x["amount"])
                    tree.insert("","end",values=(x["entry_no"],str(x["occurred_at"])[11:19],x["entry_type"],
                        x["reference_no"] or "-",x["description"],f"{amount:.2f}" if amount>0 else "",
                        f"{-amount:.2f}" if amount<0 else "",f"{dec(x['balance_after']):.2f}"))
            self.update_cash_status()

        def open_register():
            if current["session"]:
                messagebox.showinfo("Blagajna","Blagajna je že odprta.",parent=win);return
            value=simpledialog.askfloat("Odprtje blagajne","Začetna gotovina v blagajni:",initialvalue=0,minvalue=0,parent=win)
            if value is None:return
            try:
                _sid,no=open_cash_session(self.office_db,premise_id=str(self.premise_var.get()),
                    device_id=self.runtime.electronic_device,operator_code=self.operator_code,opening_cash=value)
                refresh();messagebox.showinfo("Blagajna",f"Odprta je blagajna {no}.",parent=win)
            except Exception as exc:messagebox.showerror("Blagajna",str(exc),parent=win)

        def manual_entry(direction):
            session=current["session"]
            if not session:messagebox.showwarning("Blagajna","Najprej odpri blagajno.",parent=win);return
            value=simpledialog.askfloat("Blagajniški dogodek","Znesek:",minvalue=0.01,parent=win)
            if value is None:return
            reference=simpledialog.askstring("Blagajniški dogodek","Številka povezane listine (npr. BI-…):",parent=win)
            if not reference:return
            description=simpledialog.askstring("Blagajniški dogodek","Namen oziroma opis:",parent=win)
            if not description:return
            c=sqlite3.connect(str(self.office_db))
            try:
                c.execute("BEGIN IMMEDIATE")
                record_cash_entry(c,session_id=int(session["id"]),
                    entry_type="CASH_RECEIPT" if direction>0 else "CASH_EXPENSE",
                    amount=dec(value)*direction,reference_type="MANUAL_DOCUMENT",reference_id=None,
                    reference_no=reference,description=description,operator_code=self.operator_code)
                c.commit();refresh()
            except Exception as exc:c.rollback();messagebox.showerror("Blagajna",str(exc),parent=win)
            finally:c.close()

        def export(session_id):
            path=generate_cash_journal_pdf(self.office_db,session_id=int(session_id),
                output_dir=self.pdf_dir,company=self._company_data())
            if os.name=="nt":os.startfile(str(path))

        def import_pending_disbursement():
            if not current["session"]:
                messagebox.showwarning(
                    "Blagajna", "Pred knjiženjem izdatka odpri blagajno.", parent=win
                )
                return
            try:
                rows = pending_cash_disbursements(self.service_db)
            except Exception as exc:
                messagebox.showerror("Blagajna", str(exc), parent=win)
                return
            if not rows:
                messagebox.showinfo(
                    "Blagajna", "Ni potrjenih odkupnih izdatkov, ki bi čakali na knjiženje.",
                    parent=win,
                )
                return
            picker = tk.Toplevel(win)
            picker.title("Čakajoči blagajniški izdatki odkupa")
            picker.transient(win)
            picker.grab_set()
            picker.geometry("820x430")
            columns = ("voucher", "buyback", "recipient", "purpose", "amount")
            pending_tree = ttk.Treeview(
                picker, columns=columns, show="headings", selectmode="browse"
            )
            for key, label, width in (
                ("voucher", "Izdatek", 110), ("buyback", "Odkup", 120),
                ("recipient", "Prejemnik", 180), ("purpose", "Namen", 300),
                ("amount", "Znesek", 90),
            ):
                pending_tree.heading(key, text=label)
                pending_tree.column(key, width=width, stretch=key == "purpose")
            pending_tree.pack(fill="both", expand=True, padx=12, pady=(12, 8))
            by_id = {int(row["id"]): row for row in rows}
            for row in rows:
                pending_tree.insert(
                    "", "end", iid=str(row["id"]),
                    values=(
                        row["voucher_no"], row["buyback_no"], row["recipient_name"],
                        row["purpose"], f"{dec(row['amount']):.2f} EUR",
                    ),
                )

            def book_selected():
                selected = pending_tree.selection()
                if not selected:
                    return
                row = by_id[int(selected[0])]
                if not messagebox.askyesno(
                    "Knjiži blagajniški izdatek",
                    f"Izdatek {row['voucher_no']} v znesku {dec(row['amount']):.2f} EUR "
                    "bo nespremenljivo knjižen v trenutno blagajno. Nadaljujem?",
                    parent=picker,
                ):
                    return
                try:
                    result = book_cash_disbursement(
                        office_db=self.office_db,
                        service_db=self.service_db,
                        voucher_id=int(row["id"]),
                        premise_id=str(self.premise_var.get()),
                        device_id=self.runtime.electronic_device,
                        operator_code=self.operator_code,
                    )
                    picker.destroy()
                    refresh()
                    messagebox.showinfo(
                        "Blagajna",
                        f"Izdatek {result['voucher_no']} je knjižen kot vrstica "
                        f"{result['entry_no']} blagajniškega dnevnika.",
                        parent=win,
                    )
                except Exception as exc:
                    messagebox.showerror("Blagajna", str(exc), parent=picker)

            picker_buttons = ttk.Frame(picker)
            picker_buttons.pack(fill="x", padx=12, pady=(0, 12))
            ttk.Button(
                picker_buttons, text="KNJIŽI IZBRANI IZDATEK", command=book_selected
            ).pack(side="right")
            ttk.Button(
                picker_buttons, text="ZAPRI BREZ KNJIŽENJA", command=picker.destroy
            ).pack(side="right", padx=8)
            pending_tree.bind("<Double-1>", lambda _event: book_selected())
            first = pending_tree.get_children()
            if first:
                pending_tree.selection_set(first[0])
                pending_tree.focus(first[0])

        def close_register():
            session=current["session"]
            if not session:messagebox.showwarning("Blagajna","Blagajna ni odprta.",parent=win);return
            service_connection=sqlite3.connect(str(self.service_db))
            try:
                pending=service_connection.execute(
                    "SELECT voucher_no FROM cash_disbursements "
                    "WHERE status IN ('PREPARED','CONFIRMED') ORDER BY id"
                ).fetchall()
            except sqlite3.Error:
                pending=[]
            finally:
                service_connection.close()
            if pending:
                proceed = messagebox.askyesno(
                    "Zaprtje blagajne",
                    "Naslednji pripravljeni izdatki še niso bili potrjeni in niso vključeni "
                    "v ta blagajniški dnevnik:\n\n"
                    + "\n".join(f"• {row[0]}" for row in pending)
                    + "\n\nBlagajno je vseeno dovoljeno zapreti. Nadaljujem?",
                    parent=win,
                )
                if not proceed:
                    return
            counted=simpledialog.askfloat("Zaprtje blagajne","Dejansko prešteta gotovina:",
                initialvalue=float(session["expected_cash"]),minvalue=0,parent=win)
            if counted is None:return
            note=simpledialog.askstring("Zaprtje blagajne","Opomba k razliki (lahko prazno):",parent=win) or ""
            try:
                result=close_cash_session(self.office_db,premise_id=str(self.premise_var.get()),
                    device_id=self.runtime.electronic_device,operator_code=self.operator_code,
                    counted_cash=counted,note=note)
                export(result["id"]);refresh()
                messagebox.showinfo("Blagajna",f"{result['session_no']} je zaprta. Razlika: {result['difference']:.2f} EUR.",parent=win)
            except Exception as exc:messagebox.showerror("Blagajna",str(exc),parent=win)

        buttons=tk.Frame(win);buttons.pack(fill="x",padx=12,pady=(0,12))
        ttk.Button(buttons,text="ODPRI BLAGAJNO",command=open_register).pack(side="left")
        ttk.Button(buttons,text="BLAGAJNIŠKI PREJEMEK",command=lambda:manual_entry(1)).pack(side="left",padx=5)
        ttk.Button(buttons,text="BLAGAJNIŠKI IZDATEK",command=lambda:manual_entry(-1)).pack(side="left")
        ttk.Button(
            buttons,text="PRENESI ODKUPNI IZDATEK",command=import_pending_disbursement
        ).pack(side="left",padx=5)
        ttk.Button(buttons,text="IZVOZI PDF",command=lambda:export(current["session"]["id"]) if current["session"] else None).pack(side="right",padx=5)
        ttk.Button(buttons,text="ZAPRI BLAGAJNO",command=close_register).pack(side="right")
        refresh()

    def refresh_stock(self):
        if self.issued_id:
            return
        query = self.search_var.get()
        try:
            rows = search_inventory(self.inventory_db, query)
        except Exception as exc:
            self.state_var.set(f"Zaloge ni mogoče prebrati: {exc}")
            return
        self.stock_rows.clear()
        for iid in self.stock_tree.get_children():
            self.stock_tree.delete(iid)
        for row in rows:
            iid = str(row["id"])
            self.stock_rows[iid] = row
            available = float(row["stock_qty"] or 0) - float(row["reserved_qty"] or 0)
            self.stock_tree.insert("", "end", iid=iid, values=(row["barcode"], row["article_name"], f"{available:.2f}", f"{float(row['price']):.2f}"))

    def search_enter(self, _event=None):
        children = self.stock_tree.get_children()
        exact = [iid for iid in children if str(self.stock_rows[iid]["barcode"]).casefold() == self.search_var.get().strip().casefold()]
        if len(exact) == 1:
            self.stock_tree.selection_set(exact[0])
            self.add_selected_stock()
        elif len(children) == 1:
            self.stock_tree.selection_set(children[0])
            self.add_selected_stock()
        return "break"

    def add_selected_stock(self):
        if self.issued_id:
            return
        selection = self.stock_tree.selection()
        if not selection:
            messagebox.showinfo("Blagajna", "Izberi artikel iz zaloge.", parent=self.win)
            return
        row = self.stock_rows[selection[0]]
        article_id = int(row["id"])
        for index, line in enumerate(self.lines):
            if (
                line.line_kind == "ARTICLE" and line.article_id == article_id
                and not line.source_locked
            ):
                available = dec(row["stock_qty"]) - dec(row["reserved_qty"])
                if dec(line.quantity) + Decimal("1") > available:
                    messagebox.showwarning("Blagajna", "Na zalogi ni dovolj kosov.", parent=self.win)
                    return
                self.lines[index] = replace(line, quantity=dec(line.quantity) + Decimal("1"))
                self.recalc()
                return
        self.lines.append(PosLine(
            line_kind="ARTICLE", article_id=article_id, barcode=str(row["barcode"] or ""),
            name=str(row["article_name"]), code=str(row["internal_code"] or ""),
            quantity=Decimal("1.00"), base_unit_price=dec(row["price"]),
            acquisition_cost=dec(row["acquisition_cost"]),
        ))
        if str(row.get("source_kind") or "") == "BUYBACK_DEVICE":
            self.used_device_ids.add(article_id)
        self.search_var.set("")
        self.recalc()

    def add_service(self, tier):
        if self.issued_id:
            return
        extra = simpledialog.askinteger("Blagajna", "Število dodatnih začetih 30-minutnih enot:", initialvalue=0, minvalue=0, maxvalue=100, parent=self.win)
        if extra is None:
            return
        price = service_price(tier, extra, self.runtime.settings)
        description = default_service_description(tier)
        self.lines.append(PosLine(
            line_kind="SERVICE", name=description, code=tier,
            service_tier=tier, base_unit_price=price,
        ))
        self.recalc()

    def selected_line_index(self):
        sel = self.cart_tree.selection()
        return int(sel[0]) if sel else None

    def change_qty(self, delta):
        idx = self.selected_line_index()
        if idx is None or self.issued_id:
            return
        line = self.lines[idx]
        if line.source_locked:
            messagebox.showinfo(
                "Blagajna",
                "Količina postavke, prenesene iz servisa, je zaklenjena.",
                parent=self.win,
            )
            return
        if line.line_kind != "ARTICLE":
            return
        qty = dec(line.quantity) + Decimal(delta)
        if qty <= 0:
            self.remove_line()
            return
        row = next((r for r in search_inventory(self.inventory_db, line.barcode, 20) if int(r["id"]) == line.article_id), None)
        if row and qty > dec(row["stock_qty"]) - dec(row["reserved_qty"]):
            messagebox.showwarning("Blagajna", "Na zalogi ni dovolj kosov.", parent=self.win)
            return
        self.lines[idx] = replace(line, quantity=qty)
        self.recalc()

    def edit_discount(self):
        idx = self.selected_line_index()
        if idx is None or self.issued_id:
            return
        line = self.lines[idx]
        value = simpledialog.askfloat("Blagajna", "Rabat v %:", initialvalue=float(line.requested_discount_pct), minvalue=0, maxvalue=100, parent=self.win)
        if value is None:
            return
        self.lines[idx] = replace(line, requested_discount_pct=dec(value))
        self.recalc()

    def remove_line(self):
        idx = self.selected_line_index()
        if idx is None or self.issued_id:
            return
        line = self.lines[idx]
        if line.source_locked:
            messagebox.showinfo(
                "Blagajna",
                "Postavka pripada povezanemu servisu in je v Blagajni ni mogoče odstraniti.",
                parent=self.win,
            )
            return
        line = self.lines.pop(idx)
        if line.article_id:
            self.used_device_ids.discard(line.article_id)
        self.recalc()

    def recalc(self):
        self.review_ready = False
        self.review_btn.configure(text="KONČNI PREGLED")
        for iid in self.cart_tree.get_children():
            self.cart_tree.delete(iid)
        total = Decimal("0")
        try:
            for index, line in enumerate(self.lines):
                calc = calculate_line(line, self.labor_floor)
                total += calc.line_total
                source_label = (
                    "SERVIS – ZAKL." if line.source_locked else "DODATNA PRODAJA"
                )
                self.cart_tree.insert(
                    "", "end", iid=str(index),
                    values=(
                        source_label, line.name, f"{line.quantity:.2f}",
                        f"{line.base_unit_price:.2f}",
                        f"{calc.applied_discount_pct:.2f} %", f"{calc.line_total:.2f}",
                    ),
                )
        except PosError as exc:
            self.state_var.set(str(exc))
        self.total = total.quantize(Decimal("0.01"))
        self.total_var.set(f"SKUPAJ: {self.total:.2f} EUR".replace(".", ","))
        self.used_frame.pack_forget()
        if self.used_device_ids:
            self.used_frame.pack(fill="x", padx=12, pady=(0, 7))
        self.payment_changed()

    def payment_changed(self):
        if not hasattr(self, "total"):
            self.total = Decimal("0")
        mode = self.payment_mode.get()
        self.invalidate_review()
        if mode == "CASH":
            self.bank_received.set(False)
            self.cash_amount.set(f"{self.total:.2f}")
            self.bank_amount.set("0.00")
            self.cash_entry.configure(state="disabled")
            self.bank_entry.configure(state="disabled")
            self.bank_received_check.configure(state="disabled")
        elif mode == "BANK_TRANSFER":
            self.cash_amount.set("0.00")
            self.bank_amount.set(f"{self.total:.2f}")
            self.cash_entry.configure(state="disabled")
            self.bank_entry.configure(state="disabled")
            self.bank_received_check.configure(state="normal")
        else:
            self._balancing_payment = True
            try:
                self.bank_amount.set("0.00")
                self.cash_amount.set(f"{self.total:.2f}")
            finally:
                self._balancing_payment = False
            self.cash_entry.configure(state="disabled")
            self.bank_entry.configure(state="normal")
            self.bank_received_check.configure(state="normal")
        self.update_change()

    def update_change(self):
        try:
            cash = dec(self.cash_amount.get())
            received = dec(self.cash_received.get())
            change = max(Decimal("0"), received - cash)
            self.change_var.set(f"Vračilo: {change:.2f} EUR".replace(".", ","))
        except Exception:
            self.change_var.set("Vračilo: –")

    def payment_values(self):
        mode = self.payment_mode.get()
        if mode == "CASH":
            return {"CASH": self.total}
        if mode == "BANK_TRANSFER":
            return {"BANK_TRANSFER": self.total}
        cash, bank = dec(self.cash_amount.get()), dec(self.bank_amount.get())
        if cash <= 0 or bank <= 0:
            raise PosError("Razdeljeno plačilo zahteva pozitiven gotovinski in TRR delež.")
        if cash + bank != self.total:
            raise PosError("Pri razdeljenem plačilu mora biti vsota gotovine in TRR natančno enaka računu.")
        return {"CASH": cash, "BANK_TRANSFER": bank}

    def _current_fingerprint(self):
        payload = {
            "lines": [
                {
                    "kind": line.line_kind, "name": line.name, "code": line.code,
                    "barcode": line.barcode, "article_id": line.article_id,
                    "quantity": str(line.quantity), "price": str(line.base_unit_price),
                    "cost": str(line.acquisition_cost),
                    "discount": str(line.requested_discount_pct),
                    "tier": line.service_tier, "consumed": line.stock_already_consumed,
                    "origin_kind": line.origin_kind, "origin_id": line.origin_id,
                    "locked": line.source_locked,
                }
                for line in self.lines
            ],
            "payments": {key: str(value) for key, value in self.payment_values().items()},
            "cash_received": str(dec(self.cash_received.get())),
            "bank_received": bool(self.bank_received.get()),
            "customer": self.customer_values(),
            "premise": self.selected_premise(),
            "device": self.runtime.electronic_device,
            "used_signed": bool(self.used_signed.get()),
            "service_id": self.service_id,
            "payment_reference": self.payment_reference,
            "replacement_of_invoice_id": self.replacement_of_invoice_id,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def review(self):
        if self.issued_id:
            return
        try:
            if not self.lines:
                raise PosError("Račun nima nobene postavke.")
            payments = self.payment_values()
            if self.payment_mode.get() == "SPLIT" and payments.get("BANK_TRANSFER", 0):
                if not self.bank_received.get():
                    raise PosError(
                        "Pri razdeljenem plačilu potrdi, da je TRR del dejansko prejet."
                    )
            if payments.get("CASH", 0) and dec(self.cash_received.get()) not in (0,):
                if dec(self.cash_received.get()) < dec(payments["CASH"]):
                    raise PosError("Prejeti bankovec je nižji od gotovinskega dela računa.")
            if self.used_device_ids and not self.used_signed.get():
                raise PosError("Za obnovljeno rabljeno napravo manjka podpisan 12-mesečni dogovor.")
            premise = self.selected_premise()
            calculated = [calculate_line(x, self.labor_floor) for x in self.lines]
            summary = "\n".join(
                f"• {x.source.name}: {x.line_total:.2f} EUR "
                f"(rabat {x.applied_discount_pct:.2f} %)"
                for x in calculated
            )
            premise_note = (
                "\nMFT je tip A: račun mora biti izdan v premičnem objektu."
                if premise == "MFT" else ""
            )
            replacement_note = (
                f"\nNadomestni račun za: {self.replacement_of_invoice_no}"
                if self.replacement_of_invoice_id else ""
            )
            customer = self.customer_values()
            if not messagebox.askyesno(
                "Končni pregled",
                f"{summary}\n\nSKUPAJ: {self.total:.2f} EUR\n"
                f"Plačilo: {PAYMENT_LABELS.get(self.payment_mode.get(), self.payment_mode.get())}\n"
                f"Kupec: {customer.get('name') or 'Končni kupec'}\n"
                f"Naslov: {customer.get('address') or '-'}\n"
                f"Telefon/e-pošta: {customer.get('phone') or '-'} / {customer.get('email') or '-'}\n"
                f"Poslovni prostor / naprava: {premise}/{self.runtime.electronic_device}"
                f"{premise_note}{replacement_note}\n\n"
                "Prvi F10 zdaj izdela nespremenjeni predogled A4 in 80 mm brez izdaje. "
                "Nadaljujem?",
                parent=self.win,
            ):
                return
            self.preview_a4_path, self.preview_receipt_path = generate_pos_preview_pdfs(
                lines=self.lines, payments=payments, customer=customer,
                labor_floor=self.labor_floor, output_dir=self.pdf_dir,
                company=self._company_data(), premise=premise,
                device=self.runtime.electronic_device, token=self.issue_token,
                paper_width_mm=self._paper_width(),
                replacement_of_invoice_no=self.replacement_of_invoice_no,
            )
            self.review_fingerprint = self._current_fingerprint()
            self.review_ready = True
            self.state_var.set(
                "Predogled A4 in 80 mm je pripravljen. Naslednji F10 izda račun in ga natisne."
            )
            self.review_btn.configure(text="ODPRI PREDOGLED", command=self.open_preview)
        except PosError as exc:
            messagebox.showwarning("Blagajna", str(exc), parent=self.win)

    def customer_values(self):
        return {
            "name": self.customer_name.get(), "address": self.customer_address.get(),
            "phone": self.customer_phone.get(), "email": self.customer_email.get(),
            "tax_number": self.customer_tax.get(),
        }

    def open_preview(self):
        target = self.preview_a4_path or self.preview_receipt_path
        if target and Path(target).is_file() and os.name == "nt":
            os.startfile(str(target))
        else:
            messagebox.showinfo("Blagajna", "Predogled še ni pripravljen.", parent=self.win)

    def on_f10(self, _event=None):
        if self.issued_id:
            if self.print_confirmed:
                return "break"
            try:
                self._make_original_pdfs_and_print()
            except Exception as exc:
                self.state_var.set(
                    f"Račun {self.issued_no} je izdan, originalni tisk pa ni potrjen."
                )
                self._post_issue_controls(printed=False)
                messagebox.showerror(
                    "Blagajna",
                    f"Tisk ni bil potrjen. Račun ostaja varno izdan; ponovno tiskaj isti račun "
                    f"ali odpri njegov PDF.\n\n{exc}",
                    parent=self.win,
                )
            return "break"
        if not self.review_ready:
            self.review()
            return "break"
        try:
            if self.review_fingerprint != self._current_fingerprint():
                self.invalidate_review()
                raise PosError(
                    "Podatki so se po predogledu spremenili. F10 ponovno izdela predogled."
                )
        except Exception as exc:
            messagebox.showwarning("Blagajna", str(exc), parent=self.win)
            return "break"
        try:
            self.issued_id, self.issued_no, self.total = issue_invoice(
                office_db=self.office_db, inventory_db=self.inventory_db,
                service_db=self.service_db, lines=self.lines,
                payments=self.payment_values(), customer=self.customer_values(),
                operator_code=self.operator_code,
                operator_tax_number=str(
                    self.runtime.settings.get("business", {}).get("operator_tax_number") or ""
                ),
                premise_id=self.selected_premise(),
                electronic_device_id=self.runtime.electronic_device,
                labor_floor=self.labor_floor,
                issue_token=self.issue_token, linked_service_id=self.service_id,
                used_goods_12m_signed=self.used_signed.get(),
                bank_transfer_received=self.bank_received.get(),
                payment_reference=self.payment_reference,
                used_goods_agreement_id=self.used_agreement_id,
                replacement_of_invoice_id=self.replacement_of_invoice_id,
            )
            self.header_status.set(f"Račun {self.issued_no}  •  {self.total:.2f} EUR  •  TEST/FURS izklopljen")
            self.premise_combo.configure(state="disabled")
            self.refresh_stock()
        except Exception as exc:
            messagebox.showerror("Blagajna", f"Račun ni bil izdan. Zaloga ni bila spremenjena.\n\n{exc}", parent=self.win)
            return "break"
        try:
            self._make_original_pdfs_and_print()
        except Exception as exc:
            self.state_var.set(
                f"Račun {self.issued_no} JE IZDAN. PDF/tisk še ni uspel; zaloga je že spremenjena."
            )
            self._post_issue_controls(printed=False)
            messagebox.showerror(
                "Blagajna – račun je že izdan",
                f"Račun {self.issued_no} je varno knjižen in zaloga je spremenjena. "
                "Ne izdajaj novega računa. Ponovi samo izdelavo PDF-ja in tisk.\n\n{exc}",
                parent=self.win,
            )
        return "break"

    def _company_data(self):
        company = dict(self.runtime.settings.get("business", {}))
        company.update({"short_name": "MikroFix", "address": "Paradiž 73, 2282 Cirkulane"})
        return company

    def prepare_used_agreement(self):
        try:
            self.used_signed.set(False)
            self.used_agreement_id, agreement_no, self.used_agreement_path = (
                prepare_used_goods_agreement(
                    office_db=self.office_db, inventory_db=self.inventory_db,
                    article_ids=self.used_device_ids, customer=self.customer_values(),
                    issue_token=self.issue_token, output_dir=self.pdf_dir,
                    company=self._company_data(), operator_code=self.operator_code,
                )
            )
            if os.name == "nt":
                os.startfile(str(self.used_agreement_path))
            self.state_var.set(
                f"Dogovor {agreement_no} je pripravljen. Natisni ga na A4; po podpisu označi potrditev."
            )
        except Exception as exc:
            messagebox.showwarning("Dogovor za rabljeno napravo", str(exc), parent=self.win)

    def used_signed_changed(self):
        signed = bool(self.used_signed.get())
        if not self.used_agreement_id:
            self.used_signed.set(False)
            messagebox.showwarning(
                "Dogovor za rabljeno napravo",
                "Najprej pripravi in natisni A4 dogovor.", parent=self.win,
            )
            return
        try:
            set_used_goods_agreement_signed(
                self.office_db, int(self.used_agreement_id), signed
            )
            self.invalidate_review()
        except Exception as exc:
            self.used_signed.set(False)
            messagebox.showerror("Dogovor za rabljeno napravo", str(exc), parent=self.win)

    def print_upn_qr(self, _event=None):
        if not getattr(self, "total", Decimal("0")):
            messagebox.showinfo("UPN QR", "Najprej dodaj postavke na račun.", parent=self.win)
            return "break"
        try:
            path = generate_upnqr_pdf(
                output_dir=self.pdf_dir, amount=self.total,
                reference=self.payment_reference, payer=self.customer_values(),
                company=self._company_data(),
                paper_width_mm=self._paper_width(),
            )
            printer = preferred_windows_printer(
                str(self.runtime.settings.get("printer", {}).get("windows_printer_name") or "")
            )
            if not printer:
                raise PosError("Windows ni našel fizičnega termičnega tiskalnika.")
            result = send_pdf_to_windows_printer(path, printer)
            self.state_var.set(
                f"UPN QR je natisnjen ({result.printer}, opravilo {result.job_id})."
            )
        except Exception as exc:
            if 'path' in locals() and Path(path).is_file() and os.name == "nt":
                os.startfile(str(path))
            messagebox.showwarning(
                "UPN QR",
                f"Neposredni tisk UPN QR ni uspel. PDF je odprt za ročni izpis.\n\n{exc}",
                parent=self.win,
            )
        return "break"

    def _make_original_pdfs_and_print(self):
        self.a4_path, self.receipt_path = generate_pos_pdfs(
            office_db=self.office_db, document_id=self.issued_id,
            output_dir=self.pdf_dir, company=self._company_data(),
            tax_note=self.runtime.tax_invoice_note,
            premise=str(self.premise_var.get() or self.runtime.active_premise),
            device=self.runtime.electronic_device,
            paper_width_mm=self._paper_width(),
        )
        if self.service_id:
            if not self.service_finalizer:
                raise PosError(
                    "Končnega servisnega lista ni mogoče izdelati v tej zagonski poti. "
                    "Blagajno odpri iz glavnega menija MikroFIX."
                )
            self.service_finalizer(int(self.service_id))
        # Izdelan PDF je varni original dokumenta in zadostuje za nadaljevanje.
        # Fizični tisk je ločen rezultat; gonilnik lahko izpis sprejme, ne da bi
        # vrnil preverljivo številko opravila.
        mark_pdf_ready(self.office_db, self.issued_id, self.operator_code)
        self.print_confirmed = True
        try:
            result = self._attempt_print(self.receipt_path)
        except Exception as exc:
            self.state_var.set(
                f"Račun {self.issued_no} je izdan; A4 in {self._paper_width()} mm PDF sta varno izdelana."
            )
            self._post_issue_controls(printed=False, document_ready=True)
            messagebox.showwarning(
                "Blagajna – PDF je pripravljen",
                "Račun je veljavno izdan in oba PDF-ja sta shranjena. "
                "Fizičnega izpisa ni bilo mogoče potrditi; račun lahko natisneš ročno.\n\n"
                f"{exc}",
                parent=self.win,
            )
            return
        if result.completed:
            mark_printed(self.office_db, self.issued_id, self.operator_code)
            self.state_var.set(
                f"Račun je izdan in fizični tisk potrjen ({result.printer}, opravilo {result.job_id})."
            )
            self._post_issue_controls(printed=True, document_ready=True)
        else:
            self.state_var.set(
                f"Račun je izdan; PDF je shranjen, gonilnik pa fizičnega izpisa ni potrdil."
            )
            self._post_issue_controls(printed=False, document_ready=True)

    def retry_original_print(self):
        if not self.issued_id or self.print_confirmed:
            return
        try:
            self._make_original_pdfs_and_print()
        except Exception as exc:
            self._post_issue_controls(printed=False)
            messagebox.showerror(
                "Blagajna",
                f"Račun {self.issued_no} je že izdan, izdelava PDF-ja oziroma tisk pa še ni uspel.\n\n{exc}",
                parent=self.win,
            )

    def _resume_pending_print(self, pending):
        self.lines.clear()
        self.issued_id = int(pending["id"])
        self.issued_no = str(pending["doc_no"])
        self.total = dec(pending["invoice_total"])
        self.service_id = pending.get("linked_service_id")
        pending_premise = str(pending.get("fiscal_premise") or "").strip()
        if pending_premise in self.premise_choices:
            self.premise_var.set(pending_premise)
        self.print_confirmed = False
        expected_a4 = self.pdf_dir / f"{self.issued_no}.pdf"
        paper_width = self._paper_width()
        expected_receipt = self.pdf_dir / f"{self.issued_no}_{paper_width}mm.pdf"
        self.a4_path = expected_a4 if expected_a4.is_file() else None
        self.receipt_path = expected_receipt if expected_receipt.is_file() else None
        self.total_var.set(f"SKUPAJ: {self.total:.2f} EUR".replace(".", ","))
        self.header_status.set(
            f"NEDOKONČAN TISK • račun {self.issued_no} • {self.total:.2f} EUR"
        )
        self.state_var.set(
            "Ta račun je že izdan. F10 ponovno izdela PDF in poskusi izpis istega originala."
        )
        self._post_issue_controls(printed=False)
        messagebox.showwarning(
            "Nedokončan tisk",
            f"Račun {self.issued_no} je bil že izdan, originalni tisk pa še ni potrjen. "
            "Blagajna je obnovila ta postopek; novega računa do zaključka ni mogoče izdati.",
            parent=self.win,
        )

    def _attempt_print(self, path):
        configured = str(
            self.runtime.settings.get("printer", {}).get("windows_printer_name") or ""
        ).strip()
        printer = preferred_windows_printer(configured)
        try:
            if printer:
                return send_pdf_to_windows_printer(path, printer)
            else:
                raise RuntimeError(
                    "Windows ni našel izbranega fizičnega termičnega tiskalnika."
                )
        except Exception as exc:
            raise PosError(
                f"Neposredni tisk ni uspel: {exc} PDF je pripravljen za ročni izpis."
            ) from exc

    def _post_issue_controls(self, printed, document_ready=False):
        ready = bool(document_ready or printed)
        if printed:
            self.f10_btn.configure(text="FIZIČNI TISK POTRJEN")
        elif ready:
            self.f10_btn.configure(text="PDF SHRANJEN – TISK NI POTRJEN")
        else:
            self.f10_btn.configure(text="PONOVI ORIGINALNI PDF / TISK  F10")
        if ready:
            self.review_btn.configure(text="NOV RAČUN", command=self.new_invoice)
        else:
            self.review_btn.configure(text="PONOVI PDF / TISK", command=self.retry_original_print)
        for child in getattr(self, "post_buttons", []):
            child.destroy()
        self.post_buttons = []
        footer = self.f10_btn.master
        if self.a4_path:
            b = ttk.Button(footer, text="ODPRI A4 PDF", command=lambda: os.startfile(str(self.a4_path)) if os.name == "nt" else None)
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
        if self.receipt_path:
            paper_width = self._paper_width()
            b = ttk.Button(footer, text=f"ODPRI {paper_width}-MM", command=lambda: os.startfile(str(self.receipt_path)) if os.name == "nt" else None)
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
        if ready:
            b = ttk.Button(footer, text="NATISNI KOPIJO", command=self.print_copy)
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
            b = ttk.Button(footer, text="STORNO RAČUNA", command=self.storno_current_invoice)
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)

    def print_copy(self):
        try:
            copy_no = self._next_copy_number()
            company = dict(self.runtime.settings.get("business", {})); company.update({"short_name":"MikroFix", "address":"Paradiž 73, 2282 Cirkulane"})
            self.a4_path, self.receipt_path = generate_pos_pdfs(
                office_db=self.office_db, document_id=self.issued_id, output_dir=self.pdf_dir,
                company=company, tax_note=self.runtime.tax_invoice_note,
                premise=self.runtime.active_premise, device=self.runtime.electronic_device,
                paper_width_mm=self._paper_width(), copy_no=copy_no,
            )
            self._attempt_print(self.receipt_path)
            confirmed_no = register_copy(self.office_db, self.issued_id, self.operator_code)
            if confirmed_no != copy_no:
                raise PosError("Številka natisnjene kopije se ne ujema z evidenco.")
            self.state_var.set(f"Izdelana in evidentirana je KOPIJA {copy_no}.")
        except Exception as exc:
            messagebox.showerror("Blagajna", str(exc), parent=self.win)

    def _next_copy_number(self):
        c = sqlite3.connect(str(self.office_db))
        try:
            row = c.execute(
                "SELECT print_status, COALESCE(print_copy_count,0) FROM documents WHERE id=?",
                (int(self.issued_id),),
            ).fetchone()
            if not row or row[0] not in ("PDF_READY", "PRINTED"):
                raise PosError("Kopija je dovoljena šele po izdelavi originalnega PDF-ja.")
            return int(row[1]) + 1
        finally:
            c.close()

    def storno_current_invoice(self):
        if not self.issued_id or not self.print_confirmed:
            return
        reason = simpledialog.askstring(
            "Storno računa", "Razlog za storno celotnega računa:", parent=self.win
        )
        if not reason:
            return
        if not messagebox.askyesno(
            "Storno računa",
            f"Račun {self.issued_no} bo v celoti storniran. Izvirnik ostane v evidenci, "
            "ustvari se nov storno dokument in povrne pripadajoča zaloga. Nadaljujem?",
            parent=self.win,
        ):
            return
        try:
            storno_id, storno_no = storno_invoice(
                office_db=self.office_db, inventory_db=self.inventory_db,
                service_db=self.service_db, document_id=int(self.issued_id),
                reason=reason, operator_code=self.operator_code,
            )
            a4, receipt = generate_pos_pdfs(
                office_db=self.office_db, document_id=storno_id,
                output_dir=self.pdf_dir, company=self._company_data(),
                tax_note=self.runtime.tax_invoice_note,
                premise=str(self.premise_var.get() or self.runtime.active_premise),
                device=self.runtime.electronic_device,
                paper_width_mm=self._paper_width(),
            )
            try:
                self._attempt_print(receipt)
                mark_printed(self.office_db, storno_id, self.operator_code)
                print_note = " Storno izpis je zaključen."
            except Exception:
                print_note = " Storno PDF je pripravljen za ročni izpis."
                if os.name == "nt":
                    os.startfile(str(a4))
            self.state_var.set(
                f"Račun {self.issued_no} je storniran z dokumentom {storno_no}.{print_note}"
            )
            self.refresh_stock()
            self.review_btn.configure(text="NOV RAČUN",command=self.new_invoice)
            for child in getattr(self,"post_buttons",[]):
                child.configure(state="disabled")
        except Exception as exc:
            messagebox.showerror("Storno računa",str(exc),parent=self.win)

    def new_invoice(self):
        if self.issued_id and not self.print_confirmed:
            messagebox.showwarning(
                "Blagajna", "Najprej potrdi uspešen tisk že izdanega računa.", parent=self.win
            )
            return
        if self._draft_after_id is not None:
            try:
                self.win.after_cancel(self._draft_after_id)
            except Exception:
                pass
            self._draft_after_id = None
        abandon_pos_draft(self.office_db, self.issue_token)
        self.lines.clear(); self.used_device_ids.clear(); self.issue_token=str(uuid.uuid4())
        self.payment_reference=self._reference_for_token(self.issue_token)
        self.used_agreement_id=None; self.used_agreement_path=None
        self.issued_id=None; self.issued_no=""; self.review_ready=False; self.service_id=None
        self.print_confirmed=False
        self.preview_a4_path=None; self.preview_receipt_path=None; self.review_fingerprint=""
        self.replacement_of_invoice_id=None; self.replacement_of_invoice_no=""
        self.replacement_var.set("Nadomestni račun: ni povezave")
        self._draft_started=False
        self.customer_name.set("Končni kupec"); self.customer_address.set(""); self.customer_phone.set(""); self.customer_email.set(""); self.customer_tax.set("")
        self.used_signed.set(False); self.payment_mode.set("CASH"); self.cash_received.set("0.00")
        self.bank_received.set(False)
        for child in getattr(self, "post_buttons", []): child.destroy()
        self.post_buttons=[]; self.f10_btn.configure(text="PREGLED / IZDAJA  F10")
        self.premise_combo.configure(state="readonly")
        self.review_btn.configure(text="KONČNI PREGLED", command=self.review)
        self.header_status.set(
            f"Uporabnik: {self.operator_code}  •  "
            f"{self.premise_var.get()}/{self.runtime.electronic_device}  •  "
            "TEST – FURS izklopljen"
        )
        self.state_var.set("Dodaj postavke in odpri končni pregled.")
        pending = latest_pending_print(self.office_db)
        if pending:
            self._resume_pending_print(pending)
            return
        self.recalc(); self.refresh_stock(); self.search_entry.focus_set()

    def choose_replacement_source(self):
        if self.issued_id:
            return
        try:
            rows = eligible_storned_invoices(self.office_db)
        except Exception as exc:
            messagebox.showerror(
                "Veži na storno", f"Storniranih računov ni mogoče prebrati:\n{exc}",
                parent=self.win,
            )
            return
        if not rows:
            messagebox.showinfo(
                "Veži na storno",
                "Ni storniranega računa brez že ustvarjenega nadomestnega računa.",
                parent=self.win,
            )
            return
        dialog = tk.Toplevel(self.win)
        dialog.title("Veži novi račun na storno")
        dialog.transient(self.win)
        dialog.grab_set()
        dialog.geometry("760x420")
        tree = ttk.Treeview(
            dialog, columns=("invoice", "storno", "date", "customer", "total"),
            show="headings", selectmode="browse",
        )
        for key, label, width in (
            ("invoice", "Stornirani račun", 145), ("storno", "Storno", 125),
            ("date", "Datum", 100), ("customer", "Kupec", 220),
            ("total", "Znesek", 90),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key == "customer")
        tree.pack(fill="both", expand=True, padx=12, pady=(12, 8))
        by_id = {int(row["id"]): row for row in rows}
        for row in rows:
            tree.insert(
                "", "end", iid=str(row["id"]),
                values=(
                    row["invoice_no"], row["storno_no"], row["date"],
                    row["customer"], f"{dec(row['invoice_total']):.2f} EUR",
                ),
            )

        def select_link():
            selected = tree.selection()
            if not selected:
                return
            row = by_id[int(selected[0])]
            self.replacement_of_invoice_id = int(row["id"])
            self.replacement_of_invoice_no = str(row["invoice_no"])
            self.replacement_var.set(
                f"Nadomestni račun za {row['invoice_no']} (storno {row['storno_no']})"
            )
            self.invalidate_review()
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="POVEŽI", command=select_link).pack(side="right")
        ttk.Button(buttons, text="PREKLIČI", command=dialog.destroy).pack(side="right", padx=8)
        tree.bind("<Double-1>", lambda _event: select_link())
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

    def _inventory_by_id(self, article_id):
        c=sqlite3.connect(str(self.inventory_db)); c.row_factory=sqlite3.Row
        try:
            row=c.execute("SELECT * FROM inventory_items WHERE id=?",(int(article_id),)).fetchone()
            return dict(row) if row else None
        finally: c.close()

    def ready_services(self):
        c = sqlite3.connect(str(self.service_db))
        c.row_factory = sqlite3.Row
        try:
            return [
                dict(row) for row in c.execute(
                    """
                    SELECT id, service_no, date, customer, model, serial
                    FROM service
                    WHERE COALESCE(invoice_no,'')=''
                      AND office_invoice_id IS NULL
                      AND service_order_id IS NOT NULL
                    ORDER BY id
                    """
                ).fetchall()
            ]
        finally:
            c.close()

    def choose_ready_service(self):
        if self.issued_id:
            return
        if not self.service_loader:
            messagebox.showwarning(
                "Blagajna", "Povezava s servisom ni na voljo.", parent=self.win
            )
            return
        try:
            rows = self.ready_services()
        except Exception as exc:
            messagebox.showerror(
                "Blagajna", f"Servisov za račun ni mogoče prebrati:\n{exc}",
                parent=self.win,
            )
            return
        if not rows:
            messagebox.showinfo(
                "Blagajna", "Ni servisov, ki bi čakali na izdajo računa.",
                parent=self.win,
            )
            return

        dialog = tk.Toplevel(self.win)
        dialog.title("Servisi, pripravljeni za račun")
        dialog.transient(self.win)
        dialog.grab_set()
        dialog.geometry("920x460")
        dialog.minsize(760, 380)
        tk.Label(
            dialog,
            text="Izberi servis in prenesi njegove storitve, dele ter kupca v blagajno.",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", padx=14, pady=(14, 8))
        wrap = ttk.Frame(dialog)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        tree = ttk.Treeview(
            wrap, columns=("no", "date", "customer", "device", "serial"),
            show="headings", selectmode="browse",
        )
        for key, label, width in (
            ("no", "Servis", 150), ("date", "Datum", 125),
            ("customer", "Stranka", 190), ("device", "Naprava", 190),
            ("serial", "Serijska", 160),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key in {"customer", "device"})
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for row in rows:
            tree.insert(
                "", "end", iid=str(row["id"]),
                values=(row["service_no"], row["date"], row["customer"], row["model"], row["serial"]),
            )

        def transfer():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(
                    "Blagajna", "Najprej označi servis.", parent=dialog
                )
                return
            service_id = int(selection[0])
            if self.lines and not messagebox.askyesno(
                "Blagajna",
                "Trenutne neizdane postavke bodo odstranjene in zamenjane s servisom. Nadaljujem?",
                parent=dialog,
            ):
                return
            dialog.destroy()
            self._load_service(service_id, replace_current=True)

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="PRENESI V BLAGAJNO", command=transfer).pack(side="right")
        ttk.Button(buttons, text="ZAPRI", command=dialog.destroy).pack(side="right", padx=8)
        tree.bind("<Double-1>", lambda _event: transfer())
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

    def _load_service(self, service_id, replace_current=False):
        try:
            if replace_current:
                self.lines.clear()
                self.used_device_ids.clear()
            self.service_id = int(service_id)
            c=sqlite3.connect(str(self.service_db))
            seed=self.service_loader(c, int(service_id), labor_floor_eur=float(self.labor_floor)); c.close()
            self.customer_name.set(seed.customer or "Končni kupec")
            self.customer_address.set(seed.customer_address or "")
            self.customer_phone.set(seed.customer_phone or "")
            self.customer_email.set(seed.customer_email or "")
            for line_id, (kind, description, quantity, price) in zip(
                seed.manual_line_ids, seed.manual_lines
            ):
                tier="T2" if str(kind).upper()=="T2" else "T1"
                self.lines.append(PosLine(
                    line_kind="SERVICE", name=description, code=tier,
                    service_tier=tier, quantity=dec(quantity),
                    base_unit_price=dec(price), origin_kind="SERVICE_MANUAL",
                    origin_id=int(line_id), source_locked=True,
                ))
            if not seed.manual_lines and not seed.manual_pricing and seed.labor_price > 0:
                self.lines.append(PosLine(
                    line_kind="SERVICE",
                    name=seed.work.strip() or "Servisna storitev / delo",
                    code="T1", service_tier="T1", quantity=Decimal("1"),
                    base_unit_price=dec(seed.labor_price), origin_kind="SERVICE_LEGACY",
                    origin_id=int(seed.service_id), source_locked=True,
                ))
            for line_id, (article_id, barcode, name, code, price) in zip(
                seed.part_line_ids, seed.parts
            ):
                row=self._inventory_by_id(article_id) or {}
                self.lines.append(PosLine(
                    line_kind="ARTICLE", article_id=article_id, barcode=barcode or "",
                    name=name or "Vgrajeni del", code=code or "", quantity=Decimal("1"),
                    base_unit_price=dec(price), acquisition_cost=dec(row.get("acquisition_cost",0)),
                    stock_already_consumed=True, origin_kind="SERVICE_PART",
                    origin_id=int(line_id), source_locked=True,
                ))
                if row.get("source_kind")=="BUYBACK_DEVICE": self.used_device_ids.add(int(article_id))
            self.recalc()
            self.state_var.set(f"Prevzete so postavke zaključenega servisa {seed.service_no}.")
        except Exception as exc:
            messagebox.showerror("Blagajna", f"Servisa ni mogoče prenesti v blagajno:\n{exc}", parent=self.win)

    def close(self):
        if self.issued_id is not None and not self.print_confirmed:
            if not messagebox.askyesno(
                "Nedokončan tisk",
                f"Račun {self.issued_no} je že izdan, originalni tisk pa še ni potrjen. "
                "Ob naslednjem odprtju blagajne se bo postopek samodejno obnovil. Zaprem?",
                parent=self.win,
            ):
                return
        if self.issued_id is None and self.lines:
            if not messagebox.askyesno(
                "Zapri blagajno",
                "Trenutni neizdani račun bo varno shranjen kot osnutek in ponujen ob "
                "naslednjem odprtju. Zaprem blagajno?",
                parent=self.win,
            ):
                return
        if self.issued_id is None and self._draft_started:
            if self._draft_after_id is not None:
                try:
                    self.win.after_cancel(self._draft_after_id)
                except Exception:
                    pass
                self._draft_after_id = None
            self._save_draft_now()
        self.win.destroy()
        if self.on_close_callback:
            self.on_close_callback()
        else:
            try: self.parent.destroy()
            except Exception: pass


def open_pos(parent, **kwargs):
    return PosWindow(parent, **kwargs)
