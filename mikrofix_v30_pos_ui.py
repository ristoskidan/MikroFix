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

from mikrofix_tk_bootstrap import maximize_work_window

from mikrofix_printing import preferred_printer, send_pdf_to_printer
from mikrofix_platform import open_local_file
from mikrofix_ajpes import AjpesError, find_partner_by_tax_number
from mikrofix_upnqr import UpnQrError, generate_upnqr_pdf
from mikrofix_furs import (
    FursProductionClient, ProductionFiscalizer, load_production_certificate,
)
from mikrofix_v30_cash import (
    book_cash_disbursement,cash_session_snapshot,close_cash_session,
    generate_cash_journal_pdf,generate_central_cash_journal_pdf,
    open_cash_session,pending_cash_disbursements,
    record_cash_entry,list_cash_sessions,
)
from mikrofix_v30_pos import (
    PosError, PosLine, abandon_pos_draft, calculate_line, dec,
    default_service_description, ensure_pos_schema,
    generate_central_invoice_pdfs, generate_pos_pdfs,
    generate_pos_preview_pdfs, issue_invoice,
    eligible_storned_invoices, latest_pending_print, load_active_pos_draft,
    pos_draft_data_authority,
    mark_pdf_ready, mark_printed, register_copy, save_pos_draft,
    prepare_used_goods_agreement, set_used_goods_agreement_signed,
    customer_partner_by_id, list_customer_partners, save_customer_partner,
    pending_furs_documents, reconcile_pending_furs_document,
    open_sales_claim, search_issued_invoices,
    settle_unpaid_bank_invoice, storno_invoice, unpaid_bank_invoices,
    search_inventory, service_labor_for_tier_minutes, service_line_public_quantity,
    service_price,
    net_public_pos_lines,
)
from mikrofix_cashier_runtime import (
    CashierCommandRejected, CashierResultUncertain, CentralCashierWorkflow,
    direct_sale_lines_from_pos,
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
    "CARD": "Plačilna kartica",
    "BANK_TRANSFER": "Bančno nakazilo (TRR)",
    "SPLIT": "Razdeljeno plačilo",
}


class PosWindow:
    def __init__(
        self, parent, *, runtime, office_db, inventory_db, service_db, pdf_dir,
        operator_code, service_id=None, service_loader=None, service_finalizer=None,
        on_close=None, authenticated_username=None, action_permissions=None,
        audit_event=None, central_cashier: CentralCashierWorkflow | None = None,
    ):
        self.parent = parent
        self.runtime = runtime
        self.office_db = Path(office_db)
        self.inventory_db = Path(inventory_db)
        self.service_db = Path(service_db)
        self.pdf_dir = Path(pdf_dir)
        self.operator_code = str(operator_code or "-")
        self.authenticated_username = str(authenticated_username or "").strip()
        if not self.authenticated_username:
            raise PosError("Blagajne ni mogoče odpreti brez prijavljenega uporabnika.")
        self.action_permissions = {
            str(value or "").strip().upper() for value in (action_permissions or ())
        }
        self.audit_event = audit_event
        self.central_cashier = central_cashier
        self.central_mode = central_cashier is not None
        self.central_service_draft_id = None
        self.central_invoice = None
        self.central_quote = None
        self.central_operation_id = None
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
        self.customer_dialog = None
        self.customer_partner_id = None
        self.customer_partner_version = None
        self._setting_customer = False
        self.payment_dialog = None
        self.payment_upn_path = None
        self._production_fiscalizer = None
        self._production_certificate_password = None
        self.enabled_payment_methods = {
            str(value or "").strip().upper()
            for value in (
                self.runtime.settings.get("service_policy", {}).get("payment_methods")
                or ("CASH", "BANK_TRANSFER")
            )
        }
        configured_premises = list(
            self.runtime.settings.get("business", {}).get("premises") or []
        )
        self.premise_choices = [str(value).strip() for value in configured_premises if str(value).strip()]
        self.premise_var = tk.StringVar(value=str(self.runtime.active_premise))

        ensure_pos_schema(self.office_db)
        self._assert_recovery_mode_compatible()
        parent.withdraw()
        self.win = tk.Toplevel(parent)
        self.win.title("MikroFIX – Blagajna")
        self.win.configure(bg=BG)
        self.win.minsize(1050, 650)
        maximize_work_window(self.win, "1280x760")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<F10>", self.on_f10)
        self.win.bind("<F5>", lambda _e: self.new_invoice())
        self.win.bind("<Shift-F5>", lambda _e: self.choose_replacement_source())
        self.win.bind("<F6>", lambda _e: self.choose_ready_service())
        self.win.bind("<F7>", lambda _e: self.edit_discount())
        self.win.bind("<F8>", self.print_upn_qr)
        self.win.bind("<F9>", self.open_customer_dialog)
        self.win.bind("<F1>", lambda _e: self.search_entry.focus_set())
        self.win.bind("<F2>", lambda _e: self.add_selected_stock())
        self.win.bind("<F3>", lambda _e: self.add_service("T1"))
        self.win.bind("<F4>", lambda _e: self.add_service("T2"))
        self.win.bind("<Escape>", self.on_escape)
        self._build()
        self._restoring_draft = False
        self.refresh_stock()
        pending = None if self.central_mode else latest_pending_print(self.office_db)
        if pending:
            self._resume_pending_print(pending)
        elif service_id and service_loader:
            self._load_service(service_id)
            self._save_draft_now()
        else:
            self._offer_draft_restore()
        self.search_entry.focus_set()

    def _paper_width(self) -> int:
        """Termični izpisi MikroFIX so za dogovorjeni 80-mm Epson."""
        return 80

    def on_escape(self, _event=None):
        if self.issued_id:
            return "break"
        if self.lines:
            messagebox.showinfo(
                "Blagajna",
                "Račun vsebuje postavke. Posameznih vrstic ni mogoče izbrisati; "
                "uporabi negativno korekcijo ali F5 in potrdi opustitev celotnega osnutka.",
                parent=self.win,
            )
            return "break"
        self.close()
        return "break"

    def _can_storno(self) -> bool:
        return "STORNO_DOCUMENT" in self.action_permissions

    def _require_storno_permission(self) -> bool:
        if self._can_storno():
            return True
        if callable(self.audit_event):
            try:
                self.audit_event(
                    "PERMISSION_DENIED", "action", "STORNO_DOCUMENT",
                    {"module": "CASHIER"},
                )
            except Exception:
                pass
        messagebox.showwarning(
            "Blagajna",
            "Prijavljeni uporabnik nima dovoljenja za storno dokumentov.",
            parent=self.win,
        )
        return False

    def _build(self):
        header = tk.Frame(self.win, bg=DARK, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="BLAGAJNA", bg=DARK, fg="white", font=("Segoe UI", 20, "bold")).pack(side="left", padx=20)
        self.header_status = tk.StringVar(
            value=(
                f"Uporabnik: {self.authenticated_username}  •  FURS operater: {self.operator_code}  •  "
                f"{self.premise_var.get()}/{self.runtime.electronic_device}  •  "
                "TEST – FURS izklopljen"
            )
        )
        tk.Label(header, textvariable=self.header_status, bg=DARK, fg="#D9E3EA", font=("Segoe UI", 9)).pack(side="left", padx=15)
        ttk.Button(header, text="NAZAJ", command=self.close).pack(side="right", padx=16, pady=12)
        ttk.Button(
            header,text="BLAGAJNIŠKI DNEVNIK",command=self.manage_cash_journal
        ).pack(side="right",padx=(0,8),pady=12)
        ttk.Button(
            header,text="IZDANI RAČUNI",command=self.manage_issued_invoices
        ).pack(side="right",padx=(0,8),pady=12)
        ttk.Button(
            header, text="NEPLAČANI TRR", command=self.manage_unpaid_bank_invoices
        ).pack(side="right", padx=(0, 8), pady=12)
        ttk.Button(
            header, text="FURS USKLADITEV", command=self.manage_pending_furs
        ).pack(side="right", padx=(0, 8), pady=12)
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
        rail_actions = [
            ("F1  ISKANJE", lambda: self.search_entry.focus_set()),
            ("F2  DODAJ ARTIKEL", self.add_selected_stock),
            ("F3  T1 DELO", lambda: self.add_service("T1")),
            ("F4  T2 DELO", lambda: self.add_service("T2")),
            ("F5  NOV RAČUN", self.new_invoice),
            ("F6  SERVIS ZA RAČUN", self.choose_ready_service),
            ("F7  RABAT", self.edit_discount),
            ("F8  UPN QR ZA BANKO", self.print_upn_qr),
            ("F9  STRANKA", self.open_customer_dialog),
            ("F10 PREGLED / IZDAJA", self.on_f10),
        ]
        if self._can_storno():
            rail_actions.insert(5, ("⇄ VEŽI NA STORNO", self.choose_replacement_source))
        for text_value, command in rail_actions:
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

        customer_banner = tk.Frame(
            right, bg="#EAF5EE", highlightthickness=1,
            highlightbackground="#B9D9C5",
        )
        customer_banner.pack(fill="x", padx=12, pady=(12, 5))
        self.customer_summary_var = tk.StringVar(value="Kupec: Končni kupec")
        tk.Label(
            customer_banner, textvariable=self.customer_summary_var,
            bg="#EAF5EE", fg=GREEN, font=("Segoe UI", 10, "bold"),
            anchor="w", justify="left",
        ).pack(side="left", fill="x", expand=True, padx=10, pady=7)
        ttk.Button(
            customer_banner, text="IZBERI / DODAJ STRANKO  F9",
            command=self.open_customer_dialog,
        ).pack(side="right", padx=7, pady=5)
        tk.Label(right, text="RAČUN – POSTAVKE", bg=PANEL, fg=DARK, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(5, 5))
        cart_wrap = tk.Frame(right, bg=PANEL)
        cart_wrap.pack(fill="both", expand=True, padx=12)
        cols = ("source", "name", "qty", "unit", "discount", "total")
        self.cart_tree = ttk.Treeview(cart_wrap, columns=cols, show="headings", selectmode="browse", height=9)
        for key, text, width, anchor in (
            ("source", "Izvor", 105, "w"), ("name", "Postavka", 235, "w"),
            ("qty", "Čas / kol.", 110, "e"),
            ("unit", "Cena", 80, "e"), ("discount", "Rabat", 75, "e"), ("total", "Znesek", 90, "e"),
        ):
            self.cart_tree.heading(key, text=text)
            self.cart_tree.column(key, width=width, anchor=anchor, stretch=key == "name")
        cy = ttk.Scrollbar(cart_wrap, orient="vertical", command=self.cart_tree.yview)
        self.cart_tree.configure(yscrollcommand=cy.set)
        self.cart_tree.pack(side="left", fill="both", expand=True)
        cy.pack(side="right", fill="y")
        self.cart_tree.bind("<Double-1>", lambda _e: self.edit_line_dialog())
        self.cart_tree.bind("<Button-3>", lambda _e: self.edit_line_dialog())

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
            var.trace_add("write", self._customer_field_changed)
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
        payment_choices = []
        if "CASH" in self.enabled_payment_methods:
            payment_choices.append(("Gotovina", "CASH"))
        if "CARD" in self.enabled_payment_methods:
            payment_choices.append(("Kartica", "CARD"))
        if "BANK_TRANSFER" in self.enabled_payment_methods:
            payment_choices.append(("TRR", "BANK_TRANSFER"))
        if {"CASH", "BANK_TRANSFER"}.issubset(self.enabled_payment_methods):
            payment_choices.append(("Razdeljeno", "SPLIT"))
        for col, (text, value) in enumerate(payment_choices, start=1):
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

        # Stranka (F9) in plačilo/izdaja (F10) imata ločena koraka, zato glavni
        # blagajniški pogled ostane namenjen iskanju in postavkam.
        details.pack_forget()
        payment.pack_forget()

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
        return f"SI00{digits}"

    def invalidate_review(self):
        if self.issued_id:
            return
        self.review_ready = False
        self.review_fingerprint = ""
        if hasattr(self, "review_btn"):
            self.review_btn.configure(text="KONČNI PREGLED", command=self.review)
        self._schedule_draft_save()

    def _customer_field_changed(self, *_args):
        if not self._setting_customer:
            self.customer_partner_id = None
            self.customer_partner_version = None
        if hasattr(self, "customer_summary_var"):
            name = self.customer_name.get().strip() or "Končni kupec"
            address = self.customer_address.get().strip()
            tax = self.customer_tax.get().strip()
            suffix = ""
            if address:
                suffix += f" • {address}"
            if tax:
                suffix += f" • davčna {tax}"
            self.customer_summary_var.set(f"Kupec: {name}{suffix}")
            # Spremenjeni podatki razveljavijo že pripravljen dogovor, ker ta
            # pripada prejšnjemu nespremenljivemu posnetku kupca.
            self.used_agreement_id = None
            self.used_agreement_path = None
            if hasattr(self, "used_signed"):
                self.used_signed.set(False)
        self.invalidate_review()

    def _set_customer(self, customer, *, partner_id=None, partner_version=None):
        new_values = {
            "name": str(customer.get("name") or "Končni kupec"),
            "address": str(customer.get("address") or ""),
            "phone": str(customer.get("phone") or ""),
            "email": str(customer.get("email") or ""),
            "tax_number": str(customer.get("tax_number") or ""),
        }
        normalized_partner_id = (
            str(partner_id) if self.central_mode and partner_id is not None
            else (int(partner_id) if partner_id is not None else None)
        )
        identity_changed = (
            self.customer_partner_id != normalized_partner_id
            or self.customer_values() != new_values
        )
        self._setting_customer = True
        try:
            self.customer_name.set(new_values["name"])
            self.customer_address.set(new_values["address"])
            self.customer_phone.set(new_values["phone"])
            self.customer_email.set(new_values["email"])
            self.customer_tax.set(new_values["tax_number"])
            self.customer_partner_id = normalized_partner_id
            self.customer_partner_version = (
                int(partner_version) if partner_version is not None else None
            )
        finally:
            self._setting_customer = False
        if identity_changed:
            self.used_agreement_id = None
            self.used_agreement_path = None
            if hasattr(self, "used_signed"):
                self.used_signed.set(False)
        self.invalidate_review()

    def _has_meaningful_draft(self):
        return bool(self.lines or self.service_id or self.replacement_of_invoice_id)

    def _draft_data(self):
        return {
            "schema_version": 1,
            "data_authority": "CENTRAL" if self.central_mode else "LOCAL",
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
                    "service_billable_minutes": line.service_billable_minutes,
                    "service_tariff_version": line.service_tariff_version,
                    "service_first_rate": str(line.service_first_rate),
                    "service_extra_half_hour_rate": str(line.service_extra_half_hour_rate),
                    "stock_already_consumed": bool(line.stock_already_consumed),
                    "origin_kind": line.origin_kind,
                    "origin_id": line.origin_id,
                    "source_locked": bool(line.source_locked),
                    "requested_lot_serial": line.requested_lot_serial,
                    "service_elapsed_minutes": line.service_elapsed_minutes,
                    "central_line_id": line.central_line_id,
                }
                for line in self.lines
            ],
            "customer": self.customer_values(),
            "customer_partner_id": self.customer_partner_id,
            "customer_partner_version": self.customer_partner_version,
            "payment_mode": self.payment_mode.get(),
            "cash_amount": self.cash_amount.get(),
            "bank_amount": self.bank_amount.get(),
            "cash_received": self.cash_received.get(),
            "bank_received": bool(self.bank_received.get()),
            "premise_id": self.premise_var.get(),
            "service_id": self.service_id,
            "central_service_draft_id": self.central_service_draft_id,
            "used_device_ids": sorted(self.used_device_ids),
            "used_agreement_id": self.used_agreement_id,
            "used_agreement_path": str(self.used_agreement_path or ""),
            "used_signed": bool(self.used_signed.get()),
            "payment_reference": self.payment_reference,
            "replacement_of_invoice_id": self.replacement_of_invoice_id,
            "replacement_of_invoice_no": self.replacement_of_invoice_no,
        }

    def _assert_recovery_mode_compatible(self):
        """Fail closed before showing a window that could mix accounting modes."""
        expected = "CENTRAL" if self.central_mode else "LOCAL"
        if self.central_mode and latest_pending_print(self.office_db):
            raise PosError(
                "V lokalni Blagajni obstaja izdan račun, ki še čaka na prvotni "
                "izpis. Pred prehodom na centralno Blagajno ga je treba varno "
                "razrešiti v lokalnem načinu; zapis je ostal nedotaknjen."
            )
        stored = load_active_pos_draft(
            self.office_db,
            actor_username=self.authenticated_username,
            device_id=self.runtime.electronic_device,
        )
        if not stored:
            return
        actual = pos_draft_data_authority(stored.get("payload") or {})
        if actual != expected:
            actual_label = "centralnega" if actual == "CENTRAL" else "lokalnega"
            expected_label = "centralnega" if expected == "CENTRAL" else "lokalnega"
            raise PosError(
                f"Na tej postaji je odprt osnutek iz {actual_label} načina. "
                f"Ni ga dovoljeno odpreti v načinu {expected_label} knjiženja. "
                "Osnutek je ostal nedotaknjen."
            )

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
        if not self._has_meaningful_draft():
            if self._draft_started:
                abandon_pos_draft(
                    self.office_db, self.issue_token,
                    actor_username=self.authenticated_username,
                    device_id=self.runtime.electronic_device,
                )
                self._draft_started = False
                self.issue_token = str(uuid.uuid4())
                self.payment_reference = self._reference_for_token(self.issue_token)
                self.used_agreement_id = None
                self.used_agreement_path = None
                if hasattr(self, "used_signed"):
                    self.used_signed.set(False)
            if hasattr(self, "autosave_var"):
                self.autosave_var.set("Osnutek: prazen – ni shranjen")
            return
        self._draft_started = True
        try:
            save_pos_draft(
                self.office_db,
                issue_token=self.issue_token,
                operator_code=self.operator_code,
                actor_username=self.authenticated_username,
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
                actor_username=self.authenticated_username,
                device_id=self.runtime.electronic_device,
            )
        except Exception as exc:
            messagebox.showerror("Obnovitev osnutka", str(exc), parent=self.win)
            return
        if not stored:
            return
        stored_payload = stored.get("payload") or {}
        expected = "CENTRAL" if self.central_mode else "LOCAL"
        if pos_draft_data_authority(stored_payload) != expected:
            messagebox.showerror(
                "Obnovitev osnutka",
                "Izvor shranjenega osnutka se ne ujema z načinom Blagajne. "
                "Osnutek je ostal nedotaknjen.",
                parent=self.win,
            )
            return
        if not (
            stored_payload.get("lines") or stored_payload.get("service_id")
            or stored_payload.get("replacement_of_invoice_id")
        ):
            abandon_pos_draft(
                self.office_db, str(stored["issue_token"]),
                actor_username=self.authenticated_username,
                device_id=self.runtime.electronic_device,
            )
            if hasattr(self, "autosave_var"):
                self.autosave_var.set("Prazen stari osnutek je samodejno zaprt")
            return
        if not messagebox.askyesno(
            "Obnovitev osnutka",
            "Najden je varno shranjen neizdani račun iz "
            f"{str(stored['updated_at']).replace('T', ' ')}. Nadaljujem ta osnutek?",
            parent=self.win,
        ):
            abandon_pos_draft(
                self.office_db, str(stored["issue_token"]),
                actor_username=self.authenticated_username,
                device_id=self.runtime.electronic_device,
            )
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
                    service_billable_minutes=int(item.get("service_billable_minutes") or 0),
                    service_tariff_version=str(item.get("service_tariff_version") or ""),
                    service_first_rate=dec(item.get("service_first_rate")),
                    service_extra_half_hour_rate=dec(item.get("service_extra_half_hour_rate")),
                    stock_already_consumed=bool(item.get("stock_already_consumed")),
                    origin_kind=str(item.get("origin_kind") or "POS_ADDED"),
                    origin_id=item.get("origin_id"),
                    source_locked=bool(item.get("source_locked")),
                    requested_lot_serial=str(item.get("requested_lot_serial") or ""),
                    service_elapsed_minutes=int(
                        item.get("service_elapsed_minutes") or 0
                    ),
                    central_line_id=str(item.get("central_line_id") or uuid.uuid4()),
                )
                for item in payload.get("lines", [])
            ]
            customer = payload.get("customer") or {}
            restored_partner_id = payload.get("customer_partner_id")
            self._set_customer(
                customer, partner_id=restored_partner_id,
                partner_version=payload.get("customer_partner_version"),
            )
            premise = str(payload.get("premise_id") or "")
            if premise in self.premise_choices:
                self.premise_var.set(premise)
            self.service_id = payload.get("service_id")
            self.central_service_draft_id = payload.get("central_service_draft_id")
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
            f"Uporabnik: {self.authenticated_username}  •  FURS operater: {self.operator_code}  •  "
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
            if self.central_mode:
                session = self.central_cashier.current_cash_session(
                    str(self.premise_var.get()), self.runtime.electronic_device
                )
                if session:
                    self.cash_status_var.set(
                        f"GOTOVINA: {session['session_no']} • "
                        f"{dec(session['expected_cash']):.2f} EUR"
                    )
                else:
                    self.cash_status_var.set("GOTOVINA: BLAGAJNA ZAPRTA")
                return
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
        if self.central_mode:
            return self._manage_central_cash_journal()
        win=tk.Toplevel(self.win);win.title("MikroFIX – blagajniški dnevnik")
        win.transient(self.win);maximize_work_window(win,"1180x720")
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
        controls={}

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
            if controls:
                controls["open"].configure(state="disabled" if session else "normal")
                controls["close"].configure(state="normal" if session else "disabled")
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
            open_local_file(path)

        def daily_review():
            rows = list_cash_sessions(
                self.office_db, premise_id=str(self.premise_var.get()),
                device_id=self.runtime.electronic_device,
            )
            picker = tk.Toplevel(win)
            picker.title("Blagajniški dnevnik – pregled po dnevih")
            picker.transient(win)
            maximize_work_window(picker, "1100x680")
            search_var = tk.StringVar()
            ttk.Label(picker, text="Datum, številka ali stanje").pack(anchor="w", padx=14, pady=(14, 3))
            search = ttk.Entry(picker, textvariable=search_var)
            search.pack(fill="x", padx=14, pady=(0, 8))
            cols = ("no","date","status","entries","receipts","expenses","expected","difference")
            history = ttk.Treeview(picker, columns=cols, show="headings", selectmode="browse")
            for key, label, width in (
                ("no","Dnevnik",125),("date","Datum / odprtje",150),("status","Stanje",85),
                ("entries","Knjižb",65),("receipts","Prejemki",95),("expenses","Izdatki",95),
                ("expected","Pričakovano",100),("difference","Razlika",85),
            ):
                history.heading(key,text=label)
                history.column(key,width=width,anchor="e" if key in {"entries","receipts","expenses","expected","difference"} else "w")
            history.pack(fill="both",expand=True,padx=14,pady=(0,8))

            def fill(*_args):
                term=search_var.get().strip().casefold()
                for iid in history.get_children(): history.delete(iid)
                for row in rows:
                    if term and term not in f"{row['session_no']} {row['opened_at']} {row['status']}".casefold():
                        continue
                    expected = row["expected_cash"]
                    if expected is None:
                        expected = dec(row["opening_cash"]) + dec(row["receipts"]) - dec(row["expenses"])
                    history.insert("","end",iid=str(row["id"]),values=(
                        row["session_no"],str(row["opened_at"]).replace("T"," "),row["status"],
                        row["entry_count"],f"{dec(row['receipts']):.2f}",f"{dec(row['expenses']):.2f}",
                        f"{dec(expected):.2f}",f"{dec(row['difference']):.2f}" if row["difference"] is not None else "–",
                    ))

            def open_selected():
                selected=history.selection()
                if selected: export(int(selected[0]))

            search_var.trace_add("write",fill)
            actions=ttk.Frame(picker,padding=(14,0,14,14));actions.pack(fill="x")
            ttk.Button(actions,text="ZAPRI",command=picker.destroy).pack(side="left")
            ttk.Button(actions,text="ODPRI PDF POVZETEK + PRILOGO",command=open_selected).pack(side="right")
            history.bind("<Double-1>",lambda _e:open_selected())
            fill();search.focus_set()

        def import_pending_disbursement():
            if not current["session"]:
                messagebox.showwarning(
                    "Blagajna", "Pred knjiženjem izdatka odpri blagajno.", parent=win
                )
                return
            try:
                rows = pending_cash_disbursements(
                    office_db=self.office_db, service_db=self.service_db
                )
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

        primary=tk.Frame(win);primary.pack(fill="x",padx=12,pady=(0,8))
        controls["open"]=ttk.Button(primary,text="1  ODPRI BLAGAJNO",command=open_register)
        controls["open"].pack(side="left",fill="x",expand=True,padx=(0,5))
        controls["close"]=ttk.Button(primary,text="2  ZAPRI BLAGAJNO",command=close_register)
        controls["close"].pack(side="left",fill="x",expand=True,padx=5)
        ttk.Button(primary,text="3  PREGLED PO DNEVIH",command=daily_review).pack(side="left",fill="x",expand=True,padx=(5,0))
        buttons=tk.Frame(win);buttons.pack(fill="x",padx=12,pady=(0,12))
        ttk.Button(buttons,text="BLAGAJNIŠKI PREJEMEK",command=lambda:manual_entry(1)).pack(side="left",padx=5)
        ttk.Button(buttons,text="BLAGAJNIŠKI IZDATEK",command=lambda:manual_entry(-1)).pack(side="left")
        ttk.Button(
            buttons,text="PRENESI ODKUPNI IZDATEK",command=import_pending_disbursement
        ).pack(side="left",padx=5)
        ttk.Button(buttons,text="IZVOZI PDF",command=lambda:export(current["session"]["id"]) if current["session"] else None).pack(side="right",padx=5)
        refresh()

    def _manage_central_cash_journal(self):
        win = tk.Toplevel(self.win)
        win.title("MikroFIX – centralni blagajniški dnevnik")
        win.transient(self.win)
        maximize_work_window(win, "1180x720")
        summary = tk.StringVar()
        pending_text = tk.StringVar()
        ttk.Label(
            win, textvariable=summary, font=("Segoe UI", 11, "bold")
        ).pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(win, textvariable=pending_text, foreground=RED).pack(
            fill="x", padx=12, pady=(0, 8)
        )
        columns = ("no", "time", "type", "ref", "description", "in", "out", "balance")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        for key, label, width, anchor in (
            ("no", "Št.", 45, "e"), ("time", "Čas", 75, "w"),
            ("type", "Vrsta", 120, "w"), ("ref", "Listina", 120, "w"),
            ("description", "Opis", 300, "w"), ("in", "Prejemek", 90, "e"),
            ("out", "Izdatek", 90, "e"), ("balance", "Saldo", 90, "e"),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor=anchor, stretch=key == "description")
        tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        current = {"session": None, "pending": None}
        controls = {}

        def refresh():
            try:
                session = self.central_cashier.current_cash_session(
                    str(self.premise_var.get()), self.runtime.electronic_device
                )
                pending = self.central_cashier.unresolved_commands(limit=1)
            except Exception as exc:
                messagebox.showerror("Centralna blagajna", str(exc), parent=win)
                return
            current["session"] = session
            current["pending"] = pending[0] if pending else None
            for iid in tree.get_children():
                tree.delete(iid)
            if session is None:
                summary.set(
                    f"{self.premise_var.get()}/{self.runtime.electronic_device} – "
                    "blagajna je ZAPRTA"
                )
            else:
                summary.set(
                    f"{session['session_no']} • začetno "
                    f"{dec(session['opening_cash']):.2f} EUR • pričakovano "
                    f"{dec(session['expected_cash']):.2f} EUR"
                )
                for entry in session.get("entries", []):
                    amount = dec(entry["amount"])
                    tree.insert("", "end", values=(
                        entry["entry_no"], str(entry["occurred_at"])[11:19],
                        entry["entry_type"], entry.get("reference_no") or "-",
                        entry["description"], f"{amount:.2f}" if amount > 0 else "",
                        f"{-amount:.2f}" if amount < 0 else "",
                        f"{dec(entry['balance_after']):.2f}",
                    ))
            if current["pending"]:
                pending_text.set(
                    "NEDOKONČAN ZAHTEVEK: "
                    f"{current['pending']['operation_type']} • najprej ga uskladi"
                )
            else:
                pending_text.set("")
            if controls:
                controls["open"].configure(
                    state="disabled" if session or current["pending"] else "normal"
                )
                controls["close"].configure(
                    state="normal" if session and not current["pending"] else "disabled"
                )
                controls["retry"].configure(
                    state="normal" if current["pending"] else "disabled"
                )
            self.update_cash_status()

        def open_register():
            value = simpledialog.askfloat(
                "Odprtje blagajne", "Začetna gotovina v blagajni:",
                initialvalue=0, minvalue=0, parent=win,
            )
            if value is None:
                return
            try:
                result = self.central_cashier.open_cash_session(
                    premise_id=str(self.premise_var.get()),
                    electronic_device_id=self.runtime.electronic_device,
                    opening_cash=value,
                )
                refresh()
                messagebox.showinfo(
                    "Blagajna", f"Odprta je blagajna {result['session_no']}.", parent=win
                )
            except Exception as exc:
                messagebox.showerror("Blagajna", str(exc), parent=win)
                refresh()

        def manual_entry(direction):
            session = current["session"]
            if not session:
                return
            value = simpledialog.askfloat(
                "Blagajniški dogodek", "Znesek:", minvalue=0.01, parent=win
            )
            if value is None:
                return
            reference = simpledialog.askstring(
                "Blagajniški dogodek", "Številka povezane listine:", parent=win
            )
            if not reference:
                return
            description = simpledialog.askstring(
                "Blagajniški dogodek", "Namen oziroma opis:", parent=win
            )
            if not description:
                return
            try:
                self.central_cashier.record_manual_cash_entry(
                    session_id=str(session["session_id"]), direction=direction,
                    amount=value, reference_no=reference, description=description,
                )
            except Exception as exc:
                messagebox.showerror("Blagajna", str(exc), parent=win)
            refresh()

        def close_register():
            session = current["session"]
            if not session:
                return
            counted = simpledialog.askfloat(
                "Zaprtje blagajne", "Dejansko prešteta gotovina:",
                initialvalue=float(session["expected_cash"]), minvalue=0, parent=win,
            )
            if counted is None:
                return
            note = simpledialog.askstring(
                "Zaprtje blagajne", "Opomba k razliki (lahko prazno):", parent=win
            ) or ""
            try:
                result = self.central_cashier.close_cash_session(
                    session_id=str(session["session_id"]), counted_cash=counted,
                    note=note, expected_version=int(session["aggregate_version"]),
                )
            except Exception as exc:
                messagebox.showerror("Blagajna", str(exc), parent=win)
                refresh()
                return
            refresh()
            try:
                closed_snapshot = self.central_cashier.cash_session(
                    str(session["session_id"])
                )
                pdf_path = generate_central_cash_journal_pdf(
                    closed_snapshot, output_dir=self.pdf_dir,
                    company=self._company_data(),
                )
                open_local_file(pdf_path)
            except Exception as exc:
                messagebox.showwarning(
                    "Blagajna je zaprta",
                    f"{result['session_no']} je uspešno zaprta, vendar PDF-ja ni bilo "
                    f"mogoče pripraviti ali odpreti:\n{exc}", parent=win,
                )
                return
            messagebox.showinfo(
                "Blagajna",
                f"{result['session_no']} je zaprta. Razlika: "
                f"{dec(result['difference']):.2f} EUR.", parent=win,
            )

        def retry_pending():
            pending = current["pending"]
            if not pending:
                return
            if pending["operation_type"] not in {
                "OpenCashSession", "CloseCashSession", "RecordManualCashEntry"
            }:
                messagebox.showwarning(
                    "Uskladitev",
                    "Starejši zahtevek pripada računu ali partnerju. Uskladi ga v "
                    "ustreznem blagajniškem pogledu.", parent=win,
                )
                return
            try:
                self.central_cashier.retry_command(str(pending["operation_id"]))
            except Exception as exc:
                messagebox.showerror("Uskladitev", str(exc), parent=win)
            refresh()

        def show_history():
            try:
                rows = self.central_cashier.cash_sessions(
                    premise_id=str(self.premise_var.get()),
                    electronic_device_id=self.runtime.electronic_device,
                )
            except Exception as exc:
                messagebox.showerror("Pregled blagajn", str(exc), parent=win)
                return
            text = "\n".join(
                f"{row['session_no']} • {row['status']} • pričakovano "
                f"{dec(row['expected_cash']):.2f} EUR • razlika "
                f"{dec(row['difference']):.2f} EUR" if row.get("difference") is not None
                else f"{row['session_no']} • {row['status']} • pričakovano "
                     f"{dec(row['expected_cash']):.2f} EUR"
                for row in rows
            ) or "Ni blagajniških sej."
            messagebox.showinfo("Pregled blagajn", text, parent=win)

        primary = ttk.Frame(win)
        primary.pack(fill="x", padx=12, pady=(0, 8))
        controls["open"] = ttk.Button(primary, text="1  ODPRI BLAGAJNO", command=open_register)
        controls["open"].pack(side="left", fill="x", expand=True, padx=(0, 5))
        controls["close"] = ttk.Button(primary, text="2  ZAPRI BLAGAJNO", command=close_register)
        controls["close"].pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(primary, text="3  PREGLED PO DNEVIH", command=show_history).pack(
            side="left", fill="x", expand=True, padx=(5, 0)
        )
        actions = ttk.Frame(win)
        actions.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(
            actions, text="BLAGAJNIŠKI PREJEMEK",
            command=lambda: manual_entry("RECEIPT"),
        ).pack(side="left", padx=(0, 5))
        ttk.Button(
            actions, text="BLAGAJNIŠKI IZDATEK",
            command=lambda: manual_entry("EXPENSE"),
        ).pack(side="left")
        controls["retry"] = ttk.Button(
            actions, text="USKLADI NEDOKONČAN ZAHTEVEK", command=retry_pending
        )
        controls["retry"].pack(side="right")
        refresh()

    def manage_issued_invoices(self):
        if self.central_mode:
            return self._manage_central_issued_invoices()
        dialog = tk.Toplevel(self.win)
        dialog.title("MikroFix – izdani računi")
        dialog.transient(self.win)
        maximize_work_window(dialog, "1180x720")
        query = tk.StringVar()
        ttk.Label(dialog, text="Išči po številki, kupcu, naslovu ali datumu").pack(
            anchor="w", padx=14, pady=(14, 3)
        )
        search = ttk.Entry(dialog, textvariable=query)
        search.pack(fill="x", padx=14, pady=(0, 8))
        cols = ("no","date","type","customer","amount","status","copies")
        tree = ttk.Treeview(dialog, columns=cols, show="headings", selectmode="browse")
        for key, label, width in (
            ("no","Številka",145),("date","Datum",100),("type","Vrsta",120),
            ("customer","Kupec",260),("amount","Znesek",90),("status","Stanje",100),
            ("copies","Kopij",65),
        ):
            tree.heading(key,text=label)
            tree.column(key,width=width,anchor="e" if key in {"amount","copies"} else "w",stretch=key=="customer")
        tree.pack(fill="both",expand=True,padx=14,pady=(0,8))
        rows_by_id = {}

        def refresh_history(*_args):
            rows_by_id.clear()
            for iid in tree.get_children(): tree.delete(iid)
            for row in search_issued_invoices(self.office_db, query.get()):
                iid=str(row["id"]);rows_by_id[iid]=row
                tree.insert("","end",iid=iid,values=(
                    row["invoice_no"],row["date"],row["doc_type"],row["customer"],
                    f"{dec(row['invoice_total']):.2f} EUR",row["status"],row["copy_count"],
                ))

        def selected_row():
            selected=tree.selection()
            if not selected:
                messagebox.showinfo("Izdani računi","Izberi račun.",parent=dialog)
                return None
            return rows_by_id[selected[0]]

        def open_original(fmt):
            row=selected_row()
            if not row:return
            suffix="" if fmt=="A4" else f"_{self._paper_width()}mm"
            path=self.pdf_dir/f"{row['invoice_no']}{suffix}.pdf"
            if not path.is_file():
                try:
                    a4,receipt=generate_pos_pdfs(
                        office_db=self.office_db,document_id=int(row["id"]),output_dir=self.pdf_dir,
                        company=self._company_data(),tax_note=self.runtime.tax_invoice_note,
                        premise=self.selected_premise(),device=self.runtime.electronic_device,
                        paper_width_mm=self._paper_width(),test_mode=self.runtime.is_test,
                    )
                    path=a4 if fmt=="A4" else receipt
                except Exception as exc:
                    messagebox.showerror("Izdani računi",str(exc),parent=dialog);return
            open_local_file(path)

        def make_copy(fmt):
            row=selected_row()
            if not row:return
            try:
                c=sqlite3.connect(str(self.office_db))
                copy_no=int(c.execute(
                    "SELECT COALESCE(print_copy_count,0)+1 FROM documents WHERE id=?",
                    (int(row["id"]),),
                ).fetchone()[0]);c.close()
                a4,receipt=generate_pos_pdfs(
                    office_db=self.office_db,document_id=int(row["id"]),output_dir=self.pdf_dir,
                    company=self._company_data(),tax_note=self.runtime.tax_invoice_note,
                    premise=self.selected_premise(),device=self.runtime.electronic_device,
                    paper_width_mm=self._paper_width(),copy_no=copy_no,test_mode=self.runtime.is_test,
                )
                chosen=a4 if fmt=="A4" else receipt
                if fmt=="80": self._attempt_print(chosen)
                else: open_local_file(chosen)
                confirmed=register_copy(self.office_db,int(row["id"]),self.operator_code)
                if confirmed!=copy_no: raise PosError("Zaporedna številka kopije ni skladna.")
                refresh_history()
            except Exception as exc:
                messagebox.showerror("Kopija računa",str(exc),parent=dialog)

        def storno_selected():
            row=selected_row()
            if not row or not self._require_storno_permission():return
            reason=simpledialog.askstring("Storno računa","Razlog popolnega storna:",parent=dialog)
            if not reason:return
            try:
                _id,no=storno_invoice(
                    office_db=self.office_db,inventory_db=self.inventory_db,service_db=self.service_db,
                    document_id=int(row["id"]),reason=reason,operator_code=self.operator_code,
                    authenticated_username=self.authenticated_username,action_permissions=self.action_permissions,
                    fiscalization_required=self._fiscalizer_for_issue() is not None,
                    fiscalizer=self._production_fiscalizer,
                    numbering_structure=str(self.runtime.settings.get("business",{}).get("numbering_structure") or "B"),
                    test_mode=self.runtime.is_test,
                )
                messagebox.showinfo("Storno",f"Ustvarjen je {no}.",parent=dialog);refresh_history()
            except Exception as exc:messagebox.showerror("Storno",str(exc),parent=dialog)

        def claim_selected():
            row=selected_row()
            if not row:return
            reason=simpledialog.askstring("Nova reklamacija","Opis reklamacije:",parent=dialog)
            if not reason:return
            try:
                _id,no=open_sales_claim(self.office_db,document_id=int(row["id"]),reason=reason,operator_code=self.operator_code)
                messagebox.showinfo("Reklamacija",f"Odprta je reklamacija {no} za račun {row['invoice_no']}.",parent=dialog)
            except Exception as exc:messagebox.showerror("Reklamacija",str(exc),parent=dialog)

        query.trace_add("write",refresh_history)
        actions=ttk.Frame(dialog,padding=(14,0,14,14));actions.pack(fill="x")
        ttk.Button(actions,text="ZAPRI",command=dialog.destroy).pack(side="left")
        ttk.Button(actions,text="ODPRI A4",command=lambda:open_original("A4")).pack(side="left",padx=5)
        ttk.Button(actions,text="ODPRI 80 mm",command=lambda:open_original("80")).pack(side="left")
        ttk.Button(actions,text="KOPIJA A4",command=lambda:make_copy("A4")).pack(side="right",padx=5)
        ttk.Button(actions,text="KOPIJA 80 mm",command=lambda:make_copy("80")).pack(side="right")
        ttk.Button(actions,text="REKLAMACIJA",command=claim_selected).pack(side="right",padx=5)
        if self._can_storno():ttk.Button(actions,text="STORNO",command=storno_selected).pack(side="right")
        refresh_history();search.focus_set()

    def _manage_central_issued_invoices(self):
        dialog = tk.Toplevel(self.win)
        dialog.title("MikroFix – centralni izdani računi")
        dialog.transient(self.win)
        maximize_work_window(dialog, "1180x720")
        query = tk.StringVar()
        ttk.Label(
            dialog,
            text="Išči po številki, datumu, kupcu, artiklu, barkodi ali servisu",
        ).pack(anchor="w", padx=14, pady=(14, 3))
        search = ttk.Entry(dialog, textvariable=query)
        search.pack(fill="x", padx=14, pady=(0, 8))
        columns = ("no", "date", "type", "customer", "amount", "status")
        tree = ttk.Treeview(
            dialog, columns=columns, show="headings", selectmode="browse"
        )
        for key, label, width in (
            ("no", "Številka", 165), ("date", "Datum", 155),
            ("type", "Vrsta", 135), ("customer", "Kupec", 280),
            ("amount", "Znesek", 110), ("status", "Stanje", 170),
        ):
            tree.heading(key, text=label)
            tree.column(
                key, width=width,
                anchor="e" if key == "amount" else "w",
                stretch=key == "customer",
            )
        tree.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        rows_by_id = {}

        def refresh(*_args):
            try:
                rows = self.central_cashier.invoices(query.get(), limit=500)
            except Exception as exc:
                messagebox.showerror("Izdani računi", str(exc), parent=dialog)
                return
            rows_by_id.clear()
            for iid in tree.get_children():
                tree.delete(iid)
            for row in rows:
                identity = str(row["invoice_id"])
                rows_by_id[identity] = row
                customer = dict(row.get("snapshot") or {}).get(
                    "billing_customer", {}
                )
                document_type = (
                    "STORNO RAČUNA"
                    if row.get("source_type") == "REVERSAL" else "RAČUN"
                )
                tree.insert("", "end", iid=identity, values=(
                    row["invoice_number"],
                    str(row.get("issued_at") or "").replace("T", " ")[:19],
                    document_type, customer.get("name") or "Končni kupec",
                    f"{dec(row['total_amount']):.2f} EUR", row["status"],
                ))

        def selected():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(
                    "Izdani računi", "Izberi račun.", parent=dialog
                )
                return None
            try:
                return self.central_cashier.transport.get_invoice(selection[0])
            except Exception as exc:
                messagebox.showerror("Izdani računi", str(exc), parent=dialog)
                return None

        def open_original(receipt=False):
            invoice = selected()
            if not invoice:
                return
            printable_statuses = (
                {"ISSUED"} if invoice.get("source_type") == "REVERSAL"
                else {"ISSUED", "REVERSAL_PENDING", "REVERSED"}
            )
            if invoice.get("status") not in printable_statuses:
                messagebox.showwarning(
                    "Izdani računi",
                    "Dokument še ni dokončno fiskaliziran in zato še nima "
                    "končnega originalnega PDF-ja.", parent=dialog,
                )
                return
            try:
                a4, narrow = generate_central_invoice_pdfs(
                    invoice=invoice, output_dir=self.pdf_dir,
                    company=self._company_data(),
                    paper_width_mm=self._paper_width(),
                    test_mode=self.runtime.is_test,
                )
                open_local_file(narrow if receipt else a4)
            except Exception as exc:
                messagebox.showerror("Izdani računi", str(exc), parent=dialog)

        def storno_selected():
            invoice = selected()
            if invoice:
                self._storno_central_invoice(invoice, parent=dialog, on_success=refresh)

        query.trace_add("write", refresh)
        footer = ttk.Frame(dialog, padding=(14, 0, 14, 14))
        footer.pack(fill="x")
        ttk.Button(footer, text="ZAPRI", command=dialog.destroy).pack(side="left")
        ttk.Button(
            footer, text="ODPRI A4", command=lambda: open_original(False)
        ).pack(side="left", padx=5)
        ttk.Button(
            footer, text=f"ODPRI {self._paper_width()} mm",
            command=lambda: open_original(True),
        ).pack(side="left")
        if self._can_storno():
            ttk.Button(
                footer, text="STORNO RAČUNA", command=storno_selected
            ).pack(side="right")
        refresh()
        search.focus_set()

    def _storno_central_invoice(self, invoice, *, parent, on_success=None):
        if not self._require_storno_permission():
            return
        if invoice.get("source_type") == "REVERSAL":
            messagebox.showwarning(
                "Storno računa", "Storno dokumenta ni mogoče ponovno stornirati.",
                parent=parent,
            )
            return
        if invoice.get("status") != "ISSUED":
            messagebox.showwarning(
                "Storno računa",
                "Stornirati je mogoče samo dokončno izdani račun.", parent=parent,
            )
            return
        reason = simpledialog.askstring(
            "Storno računa", "Razlog popolnega storna:", parent=parent
        )
        if not reason:
            return
        if not messagebox.askyesno(
            "Dokončno potrdi storno",
            f"Račun {invoice['invoice_number']} bo finančno storniran z novim "
            "dokumentom.\n\nStorno NE vrne delov na zalogo in NE odpre "
            "servisa. Fizično vračilo in ponovno odprtje sta ločena "
            "postopka.\n\nNadaljujem?", parent=parent,
        ):
            return
        try:
            result = self.central_cashier.storno_invoice(
                invoice_id=str(invoice["invoice_id"]),
                expected_version=int(invoice["row_version"]), reason=reason,
            )
        except Exception as exc:
            messagebox.showerror("Storno računa", str(exc), parent=parent)
            return
        reversal = result.invoice
        if reversal.get("status") == "ISSUED":
            try:
                a4, _receipt = generate_central_invoice_pdfs(
                    invoice=reversal, output_dir=self.pdf_dir,
                    company=self._company_data(),
                    paper_width_mm=self._paper_width(),
                    test_mode=self.runtime.is_test,
                )
                open_local_file(a4)
                note = " Končni storno PDF je pripravljen."
            except Exception as exc:
                note = f" PDF-ja ni bilo mogoče pripraviti: {exc}"
        else:
            note = " Storno čaka na varno davčno potrditev FURS."
        messagebox.showinfo(
            "Storno računa",
            f"Ustvarjen je dokument {reversal['invoice_number']}.{note}",
            parent=parent,
        )
        if callable(on_success):
            on_success()

    def refresh_stock(self):
        if self.issued_id:
            return
        query = self.search_var.get()
        try:
            if self.central_mode:
                central_rows = self.central_cashier.inventory(query)
                rows = []
                for item in central_rows:
                    barcode = str(
                        item.get("supplier_barcode") or item.get("main_barcode") or ""
                    )
                    available = item.get(
                        "identity_quantity_available"
                        if item.get("supplier_barcode")
                        else "article_quantity_available"
                    )
                    rows.append({
                        "id": str(item.get("identity_id") or item["article_id"]),
                        "article_id": str(item["article_id"]),
                        "barcode": barcode, "article_name": item["description"],
                        "internal_code": "", "stock_qty": available,
                        "reserved_qty": "0", "price": item["current_sale_price"],
                        "acquisition_cost": "0", "source_kind": "CENTRAL",
                        "requested_lot_serial": "",
                        "track_mode": item.get("track_mode") or "QUANTITY",
                        "available_lot_serials": list(
                            item.get("available_lot_serials") or []
                        ),
                        "blocked": bool(item.get("blocked")),
                        "blocked_reason": str(item.get("blocked_reason") or ""),
                    })
            else:
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
        if self.central_service_draft_id:
            messagebox.showwarning(
                "Blagajna",
                "Servisni osnutek je zaklenjen. Neposredna prodaja mora biti ločen račun.",
                parent=self.win,
            )
            return
        if row.get("blocked"):
            messagebox.showwarning(
                "Blagajna", f"Artikel je blokiran: {row.get('blocked_reason') or 'pregled'}",
                parent=self.win,
            )
            return
        article_id = row.get("article_id") if self.central_mode else int(row["id"])
        requested_serial = str(row.get("requested_lot_serial") or "")
        if self.central_mode and row.get("track_mode") == "SERIAL":
            serials = list(row.get("available_lot_serials") or [])
            if len(serials) == 1:
                requested_serial = serials[0]
            else:
                requested_serial = simpledialog.askstring(
                    "Blagajna – serijska identiteta",
                    "Skeniraj ali vpiši točno serijsko številko kosa:", parent=self.win,
                ) or ""
                if requested_serial not in serials:
                    messagebox.showwarning(
                        "Blagajna", "Vpisana serijska številka ni na razpoložljivi zalogi.",
                        parent=self.win,
                    )
                    return
        for index, line in enumerate(self.lines):
            if (
                line.line_kind == "ARTICLE" and line.article_id == article_id
                and not line.source_locked
                and line.requested_lot_serial == requested_serial
            ):
                available = dec(row["stock_qty"]) - dec(row["reserved_qty"])
                if dec(line.quantity) + Decimal("1") > available:
                    messagebox.showwarning("Blagajna", "Na zalogi ni dovolj kosov.", parent=self.win)
                    return
                self.lines[index] = replace(line, quantity=dec(line.quantity) + Decimal("1"))
                if str(row.get("source_kind") or "") == "BUYBACK_DEVICE":
                    self.used_device_ids.add(article_id)
                self.recalc()
                return
        self.lines.append(PosLine(
            line_kind="ARTICLE", article_id=article_id, barcode=str(row["barcode"] or ""),
            name=str(row["article_name"]), code=str(row["internal_code"] or ""),
            quantity=Decimal("1.00"), base_unit_price=dec(row["price"]),
            acquisition_cost=dec(row["acquisition_cost"]),
            requested_lot_serial=requested_serial,
        ))
        if str(row.get("source_kind") or "") == "BUYBACK_DEVICE":
            self.used_device_ids.add(article_id)
        self.search_var.set("")
        self.recalc()

    def add_service(self, tier):
        if self.issued_id:
            return
        if self.central_service_draft_id:
            messagebox.showwarning(
                "Blagajna",
                "Servisni osnutek je zaklenjen. Dodatno delo mora biti na ločenem računu.",
                parent=self.win,
            )
            return
        minutes = simpledialog.askinteger(
            f"Blagajna – {tier}",
            "Vpiši dejanske minute opravljenega dela:",
            initialvalue=60, minvalue=1, maxvalue=24 * 60, parent=self.win,
        )
        if minutes is None:
            return
        tariff = service_labor_for_tier_minutes(
            tier, minutes, self.runtime.settings,
            description=default_service_description(tier),
        )
        price = tariff["price"]
        description = default_service_description(tier)
        self.lines.append(PosLine(
            line_kind="SERVICE", name=description, code=tier,
            service_tier=tier, base_unit_price=price,
            service_billable_minutes=tariff["billable_minutes"],
            service_elapsed_minutes=tariff["elapsed_minutes"],
            service_tariff_version=tariff["tariff_version"],
            service_first_rate=tariff["first_rate"],
            service_extra_half_hour_rate=tariff["extra_half_hour_rate"],
        ))
        self.recalc()
        self.state_var.set(
            f"Dodano {tier}: {minutes} dejanskih min → "
            f"{tariff['billable_minutes']} obračunskih min → {price:.2f} EUR."
        )

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
        if self.central_mode:
            candidates = self.central_cashier.inventory(line.barcode, limit=20)
            row = next((r for r in candidates if str(r["article_id"]) == str(line.article_id)), None)
            if row:
                row = {
                    "stock_qty": row.get("identity_quantity_available")
                    if row.get("supplier_barcode") else row.get("article_quantity_available"),
                    "reserved_qty": "0",
                }
        else:
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

    def edit_line_dialog(self):
        """Majhno namensko okno: rabat ali nova, sledljiva +/- postavka."""
        idx = self.selected_line_index()
        if idx is None or self.issued_id:
            return
        line = self.lines[idx]
        dialog = tk.Toplevel(self.win)
        dialog.title("Blagajna – postavka")
        dialog.transient(self.win)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=line.name, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        discount_var = tk.StringVar(value=str(line.requested_discount_pct))
        ttk.Label(frame, text="Rabat %").grid(row=1, column=0, sticky="w", pady=4)
        discount_entry = ttk.Entry(frame, textvariable=discount_var, width=12)
        discount_entry.grid(row=1, column=1, sticky="ew", pady=4)

        def save_discount():
            try:
                value = dec(discount_var.get())
                if value < 0 or value > 100:
                    raise PosError("Rabat mora biti med 0 in 100 %.")
                self.lines[idx] = replace(line, requested_discount_pct=value)
                dialog.destroy()
                self.recalc()
            except Exception as exc:
                messagebox.showwarning("Blagajna", str(exc), parent=dialog)

        def append_quantity(sign):
            if line.source_locked or line.line_kind != "ARTICLE":
                messagebox.showinfo(
                    "Blagajna",
                    "Količinska korekcija je dovoljena samo za dodatno prodajo artiklov.",
                    parent=dialog,
                )
                return
            amount = simpledialog.askinteger(
                "Blagajna – količina",
                "Vpiši število kosov (najmanj 1):",
                initialvalue=1, minvalue=1, parent=dialog,
            )
            if amount is None:
                return
            quantity = Decimal(amount * sign)
            current_quantity = sum(
                (dec(item.quantity) for item in self.lines
                 if item.line_kind == "ARTICLE" and item.article_id == line.article_id),
                Decimal("0"),
            )
            if current_quantity + quantity < 0:
                messagebox.showwarning(
                    "Blagajna",
                    "Korekcija ne sme preseči doslej dodane količine artikla.",
                    parent=dialog,
                )
                return
            self.lines.append(replace(
                line,
                quantity=quantity,
                origin_kind="POS_CORRECTION" if sign < 0 else "POS_ADDED",
                origin_id=idx,
                source_locked=False,
            ))
            if sign < 0 and line.article_id:
                remaining = sum(
                    (dec(item.quantity) for item in self.lines
                     if item.line_kind == "ARTICLE" and item.article_id == line.article_id),
                    Decimal("0"),
                )
                if remaining <= 0:
                    self.used_device_ids.discard(int(line.article_id))
                    self.used_agreement_id = None
                    self.used_agreement_path = None
                    self.used_signed.set(False)
            dialog.destroy()
            self.recalc()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="DODAJ + KOLIČINO", command=lambda: append_quantity(1)).pack(side="left")
        ttk.Button(buttons, text="KOREKCIJA − KOLIČINE", command=lambda: append_quantity(-1)).pack(side="left", padx=6)
        ttk.Button(buttons, text="SHRANI RABAT", command=save_discount).pack(side="right")
        frame.columnconfigure(1, weight=1)
        discount_entry.focus_set()

    def public_invoice_lines(self):
        """Združi interne +/- dogodke; javni račun nikoli ne kaže korekcijskih vrstic."""
        return net_public_pos_lines(self.lines)

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
                    "SERVIS – ZAKL." if line.source_locked
                    else ("KOREKCIJA" if line.origin_kind == "POS_CORRECTION" else "DODATNA PRODAJA")
                )
                displayed_quantity = service_line_public_quantity(line)
                if line.line_kind == "SERVICE" and line.service_billable_minutes > 0:
                    hours = line.service_billable_minutes / 60
                    displayed_quantity = (
                        f"{line.service_billable_minutes} min / {hours:g} h"
                    ).replace(".", ",")
                self.cart_tree.insert(
                    "", "end", iid=str(index),
                    values=(
                        source_label, line.name, displayed_quantity,
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

    def refresh_central_quote(self):
        """Replace the provisional UI total with the server-owned direct-sale quote."""
        if not self.central_mode or self.central_service_draft_id:
            return None
        public_lines = self.public_invoice_lines()
        quote = self.central_cashier.quote_direct_sale(
            direct_sale_lines_from_pos(public_lines)
        )
        self.central_quote = quote
        self.total = dec(quote["total_amount"])
        self.total_var.set(f"SKUPAJ: {self.total:.2f} EUR".replace(".", ","))
        self.payment_changed()
        return quote

    def central_preview_lines(self):
        quote = self.central_quote or self.refresh_central_quote()
        result = []
        for line in quote["lines"]:
            kind = str(line["line_kind"])
            result.append(PosLine(
                line_kind=kind, name=str(line["description"]),
                barcode=str(line.get("barcode") or ""),
                article_id=line.get("article_id"),
                quantity=dec(line.get("quantity")),
                base_unit_price=dec(line.get("base_unit_price")),
                acquisition_cost=Decimal("0"),
                requested_discount_pct=dec(
                    line.get("applied_discount_percent")
                ),
                service_tier=str(line.get("service_tier") or ""),
                service_elapsed_minutes=int(line.get("elapsed_minutes") or 0),
                service_billable_minutes=int(line.get("billable_minutes") or 0),
                service_tariff_version=str(line.get("tariff_version") or ""),
                service_first_rate=dec(line.get("first_hour_rate")),
                service_extra_half_hour_rate=dec(
                    line.get("extra_half_hour_rate")
                ),
                requested_lot_serial=str(line.get("lot_serial") or ""),
                central_line_id=str(line["line_id"]),
            ))
        return result

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
        elif mode == "CARD":
            self.bank_received.set(False)
            self.cash_amount.set("0.00")
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
        if mode == "CARD":
            return {"CARD": self.total}
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
                    "requested_lot_serial": line.requested_lot_serial,
                    "service_elapsed_minutes": line.service_elapsed_minutes,
                    "central_line_id": line.central_line_id,
                }
                for line in self.lines
            ],
            "payments": {key: str(value) for key, value in self.payment_values().items()},
            "cash_received": str(dec(self.cash_received.get())),
            "bank_received": bool(self.bank_received.get()),
            "customer": self.customer_values(),
            "customer_partner_id": self.customer_partner_id,
            "customer_partner_version": self.customer_partner_version,
            "premise": self.selected_premise(),
            "device": self.runtime.electronic_device,
            "used_signed": bool(self.used_signed.get()),
            "service_id": self.service_id,
            "payment_reference": self.payment_reference,
            "replacement_of_invoice_id": self.replacement_of_invoice_id,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def review(self, *, ask_confirmation=True):
        if self.issued_id:
            return False
        try:
            if not self.lines:
                raise PosError("Račun nima nobene postavke.")
            if self.central_mode and not self.central_service_draft_id:
                self.refresh_central_quote()
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
            public_lines = (
                self.central_preview_lines()
                if self.central_mode and not self.central_service_draft_id
                else self.public_invoice_lines()
            )
            calculated = [calculate_line(x, self.labor_floor) for x in public_lines]
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
            if ask_confirmation and not messagebox.askyesno(
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
                return False
            self.preview_a4_path, self.preview_receipt_path = generate_pos_preview_pdfs(
                lines=public_lines, payments=payments, customer=customer,
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
            return True
        except Exception as exc:
            messagebox.showwarning("Blagajna", str(exc), parent=self.win)
            return False

    def open_customer_dialog(self, _event=None):
        """F9 izbere/ustvari partnerja ter ga veže na osnutek in dogovor."""
        if self.issued_id:
            return "break"
        if self.central_mode:
            return self.open_central_customer_dialog()
        if self.customer_dialog is not None:
            try:
                self.customer_dialog.lift()
                self.customer_dialog.focus_force()
                return "break"
            except Exception:
                self.customer_dialog = None

        dialog = tk.Toplevel(self.win)
        self.customer_dialog = dialog
        dialog.title("MikroFIX – stranka")
        dialog.geometry("760x560")
        dialog.minsize(680, 500)
        dialog.configure(bg=BG)
        dialog.transient(self.win)
        dialog.grab_set()

        def close_dialog():
            self.customer_dialog = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        tk.Label(
            dialog, text="STRANKA – SAMOSTOJNI VNOS (F9)",
            bg=DARK, fg="white", font=("Segoe UI", 16, "bold"), padx=18, pady=13,
        ).pack(fill="x")
        tk.Label(
            dialog,
            text=("Izberi obstoječega partnerja ali uporabi AJPES/ročni vnos. "
                  "Shranjeni kupec se pripne trenutnemu računu in 12-mesečnemu dogovoru."),
            bg=BG, fg=GRAY, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(12, 4))

        partners = list_customer_partners(self.office_db)
        partner_by_display = {}
        displays = []
        current_display = ""
        for partner in partners:
            suffix = partner["tax_number"] or partner["address"] or "brez dodatne oznake"
            display = f"{partner['name']}  |  {suffix}  |  ID {partner['id']}"
            displays.append(display)
            partner_by_display[display] = partner
            if self.customer_partner_id == partner["id"]:
                current_display = display
        selected_partner_id = {"value": self.customer_partner_id}
        filling = {"active": False}
        partner_var = tk.StringVar(value=current_display)
        local_vars = {
            "name": tk.StringVar(value=self.customer_name.get()),
            "address": tk.StringVar(value=self.customer_address.get()),
            "phone": tk.StringVar(value=self.customer_phone.get()),
            "email": tk.StringVar(value=self.customer_email.get()),
            "tax_number": tk.StringVar(value=self.customer_tax.get()),
        }

        form = ttk.Frame(dialog, padding=12)
        form.pack(fill="both", expand=True, padx=14, pady=6)
        ttk.Label(form, text="Obstoječi partner").grid(row=0, column=0, sticky="w", pady=5)
        partner_combo = ttk.Combobox(
            form, textvariable=partner_var, values=displays, state="readonly",
        )
        partner_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)

        def fill_local(customer, partner_id=None):
            filling["active"] = True
            try:
                for key, variable in local_vars.items():
                    variable.set(str(customer.get(key) or ""))
                selected_partner_id["value"] = partner_id
            finally:
                filling["active"] = False

        def choose_partner(_event=None):
            partner = partner_by_display.get(partner_var.get())
            if partner:
                fill_local(partner, partner["id"])

        def manual_mode():
            partner_var.set("")
            fill_local({"name": "", "address": "", "phone": "", "email": "", "tax_number": ""})
            entries[0].focus_set()

        def local_edited(*_args):
            if not filling["active"]:
                selected_partner_id["value"] = None
                partner_var.set("")

        entries = []
        for row, (label, key) in enumerate((
            ("Naziv / ime", "name"), ("Naslov", "address"),
            ("Telefon", "phone"), ("E-pošta", "email"),
            ("Davčna številka", "tax_number"),
        ), start=1):
            variable = local_vars[key]
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(form, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 6), pady=4)
            entries.append(entry)
            variable.trace_add("write", local_edited)
        form.columnconfigure(1, weight=1)

        def lookup_ajpes():
            tax = "".join(ch for ch in local_vars["tax_number"].get() if ch.isdigit())
            if not tax:
                tax = simpledialog.askstring(
                    "AJPES – kupec", "Vpiši davčno številko:", parent=dialog
                ) or ""
                tax = "".join(ch for ch in tax if ch.isdigit())
            if not tax:
                return
            try:
                found = find_partner_by_tax_number(
                    tax, test_mode=self.runtime.is_test, timeout=15
                )
            except AjpesError as exc:
                messagebox.showwarning(
                    "AJPES – kupec", f"{exc}\n\nRočni vnos ostaja omogočen.",
                    parent=dialog,
                )
                return
            address = ", ".join(
                value for value in (found.address, found.post_city) if str(value or "").strip()
            )
            partner_var.set("")
            fill_local({
                "name": found.name, "address": address,
                "phone": local_vars["phone"].get(),
                "email": local_vars["email"].get(),
                "tax_number": found.tax_number,
            })

        ttk.Button(
            form, text="AJPES PO DAVČNI ŠTEVILKI",
            command=lookup_ajpes,
        ).grid(row=5, column=2, padx=(4, 0))
        ttk.Button(
            form, text="NOV / ROČNI VNOS", command=manual_mode,
        ).grid(row=1, column=2, padx=(4, 0))
        partner_combo.bind("<<ComboboxSelected>>", choose_partner)

        def save_customer(_event=None):
            customer = {key: variable.get().strip() for key, variable in local_vars.items()}
            try:
                partner_id = save_customer_partner(
                    self.office_db, customer,
                    preferred_partner_id=selected_partner_id["value"],
                )
                saved = customer_partner_by_id(self.office_db, partner_id)
                if not saved:
                    raise PosError("Shranjene stranke ni mogoče ponovno prebrati.")
                self._set_customer(saved, partner_id=partner_id)
                self._save_draft_now()
            except Exception as exc:
                messagebox.showerror("F9 – stranka", str(exc), parent=dialog)
                return "break"
            close_dialog()
            self.state_var.set(
                f"Kupec »{saved['name']}« (partner ID {partner_id}) je pripet neizdanemu računu."
            )
            return "break"

        actions = tk.Frame(dialog, bg=BG)
        actions.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(actions, text="PREKLIČI", command=close_dialog).pack(side="left")
        ttk.Button(
            actions, text="SHRANI STRANKO  F9", command=save_customer,
            style="Blue.TButton",
        ).pack(side="right")
        dialog.bind("<F9>", save_customer)
        (partner_combo if displays else entries[0]).focus_set()
        return "break"

    def open_central_customer_dialog(self):
        if self.customer_dialog is not None:
            try:
                self.customer_dialog.lift()
                return "break"
            except Exception:
                self.customer_dialog = None
        dialog = tk.Toplevel(self.win)
        self.customer_dialog = dialog
        dialog.title("MikroFIX – centralna stranka")
        dialog.geometry("850x620")
        dialog.transient(self.win)
        dialog.grab_set()

        def close_dialog():
            self.customer_dialog = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        search_var = tk.StringVar()
        top = ttk.Frame(dialog, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Išči centralnega partnerja").pack(side="left")
        search = ttk.Entry(top, textvariable=search_var, width=35)
        search.pack(side="left", padx=8)
        tree = ttk.Treeview(
            dialog, columns=("name", "tax", "address"), show="headings",
            height=8, selectmode="browse",
        )
        for key, label, width in (
            ("name", "Naziv / ime", 280), ("tax", "Davčna", 110),
            ("address", "Naslov", 330),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.pack(fill="x", padx=12)
        partner_rows = {}

        def refresh_partners():
            try:
                rows = self.central_cashier.partners(search_var.get())
            except Exception as exc:
                messagebox.showwarning("Centralne stranke", str(exc), parent=dialog)
                return
            partner_rows.clear()
            tree.delete(*tree.get_children())
            for row in rows:
                identity = str(row["partner_id"])
                partner_rows[identity] = row
                tree.insert("", "end", iid=identity, values=(
                    row["name"], row.get("tax_number") or "",
                    ", ".join(value for value in (
                        row.get("address"), row.get("post_city")
                    ) if value),
                ))

        ttk.Button(top, text="IŠČI", command=refresh_partners).pack(side="left")
        search.bind("<Return>", lambda _event: refresh_partners())

        def choose_partner():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("Centralne stranke", "Izberi partnerja.", parent=dialog)
                return
            row = partner_rows[selection[0]]
            self._set_customer(
                row, partner_id=row["partner_id"],
                partner_version=row["row_version"],
            )
            self._save_draft_now()
            close_dialog()

        form = ttk.LabelFrame(dialog, text="Nov pregledan partner", padding=10)
        form.pack(fill="both", expand=True, padx=12, pady=12)
        variables = {
            key: tk.StringVar() for key in (
                "name", "address", "post_city", "tax_number", "registration_number",
                "phone", "email",
            )
        }
        for row_no, (label, key) in enumerate((
            ("Naziv / ime", "name"), ("Naslov", "address"),
            ("Pošta in kraj", "post_city"), ("Davčna številka", "tax_number"),
            ("Matična številka", "registration_number"),
            ("Telefon", "phone"), ("E-pošta", "email"),
        )):
            ttk.Label(form, text=label).grid(row=row_no, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=variables[key]).grid(
                row=row_no, column=1, sticky="ew", padx=(8, 0), pady=3
            )
        form.columnconfigure(1, weight=1)
        verification = {"source": "MANUAL_REVIEW", "reference": "F9"}

        def ajpes_lookup():
            tax = "".join(ch for ch in variables["tax_number"].get() if ch.isdigit())
            if not tax:
                return
            try:
                suggestion = self.central_cashier.transport.suggest_partner_from_ajpes(tax)
            except Exception as exc:
                messagebox.showwarning("AJPES", str(exc), parent=dialog)
                return
            for key in variables:
                if key in suggestion:
                    variables[key].set(str(suggestion.get(key) or ""))
            verification.update(source="AJPES_REVIEW", reference=f"AJPES:{tax}")

        def create_partner():
            values = {key: variable.get().strip() for key, variable in variables.items()}
            if not messagebox.askyesno(
                "Potrditev pregleda",
                "Potrjuješ, da si podatke novega partnerja pregledal?",
                parent=dialog,
            ):
                return
            partner_id = str(uuid.uuid4())
            try:
                result = self.central_cashier.create_partner({
                    "partner_id": partner_id,
                    "verification_source": verification["source"],
                    "review_confirmed": True, **values, "country": "Slovenija",
                    "source_reference": verification["reference"],
                })
            except Exception as exc:
                messagebox.showwarning("Centralne stranke", str(exc), parent=dialog)
                return
            self._set_customer(
                values, partner_id=result["partner_id"],
                partner_version=result["row_version"],
            )
            self._save_draft_now()
            close_dialog()

        actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        actions.pack(fill="x")
        ttk.Button(actions, text="ZAPRI", command=close_dialog).pack(side="left")
        ttk.Button(actions, text="IZBERI PARTNERJA", command=choose_partner).pack(side="right")
        ttk.Button(actions, text="USTVARI PREGLEDANEGA", command=create_partner).pack(side="right", padx=6)
        ttk.Button(actions, text="AJPES PREDLOG", command=ajpes_lookup).pack(side="right")
        tree.bind("<Double-1>", lambda _event: choose_partner())
        refresh_partners()
        search.focus_set()
        return "break"

    def open_payment_dialog(self, _event=None, *, focus_customer=False):
        if self.issued_id:
            self.print_copy()
            return "break"
        if not self.lines:
            messagebox.showwarning("Blagajna", "Najprej dodaj vsaj eno postavko.", parent=self.win)
            return "break"
        if self.central_mode and not self.central_service_draft_id:
            try:
                self.refresh_central_quote()
            except Exception as exc:
                messagebox.showwarning(
                    "Blagajna",
                    f"Centralnega končnega zneska ni mogoče potrditi:\n{exc}",
                    parent=self.win,
                )
                return "break"
        if self.payment_dialog is not None:
            try:
                self.payment_dialog.lift()
                self.payment_dialog.focus_force()
                return "break"
            except Exception:
                self.payment_dialog = None

        dialog = tk.Toplevel(self.win)
        self.payment_dialog = dialog
        dialog.title("MikroFIX - stranka, plačilo in potrditev")
        dialog.geometry("760x650")
        dialog.minsize(680, 560)
        dialog.configure(bg=BG)
        dialog.transient(self.win)
        dialog.grab_set()

        def close_dialog():
            self.payment_dialog = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        tk.Label(
            dialog, text=f"ZA PLAČILO: {self.total:.2f} EUR".replace(".", ","),
            bg=DARK, fg="white", font=("Segoe UI", 21, "bold"), padx=18, pady=14,
        ).pack(fill="x")

        customer = ttk.LabelFrame(dialog, text="STRANKA (samostojni vnos: F9)", padding=12)
        customer.pack(fill="x", padx=14, pady=(14, 8))
        customer_entries = []
        for row, (label, variable) in enumerate((
            ("Naziv / ime", self.customer_name), ("Naslov", self.customer_address),
            ("Telefon", self.customer_phone), ("E-pošta", self.customer_email),
            ("Davčna številka", self.customer_tax),
        )):
            ttk.Label(customer, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(customer, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=(10, 6), pady=3)
            if self.central_mode:
                entry.configure(state="readonly")
            customer_entries.append(entry)
        customer.columnconfigure(1, weight=1)
        ttk.Button(
            customer, text="IZBERI / SHRANI PREK F9",
            command=lambda: (close_dialog(), self.open_customer_dialog()),
        ).grid(row=4, column=2, padx=(4, 0))

        pay = ttk.LabelFrame(dialog, text="NAČIN PLAČILA", padding=12)
        pay.pack(fill="x", padx=14, pady=8)
        payment_choices = []
        if "CASH" in self.enabled_payment_methods:
            payment_choices.append(("GOTOVINA", "CASH"))
        if "CARD" in self.enabled_payment_methods:
            payment_choices.append(("KARTICA", "CARD"))
        if "BANK_TRANSFER" in self.enabled_payment_methods:
            payment_choices.append(("TRR / UPN", "BANK_TRANSFER"))
        if {"CASH", "BANK_TRANSFER"}.issubset(self.enabled_payment_methods):
            payment_choices.append(("GOTOVINA + TRR", "SPLIT"))
        for col, (label, value) in enumerate(payment_choices):
            ttk.Radiobutton(
                pay, text=label, variable=self.payment_mode, value=value,
                command=self.payment_changed,
            ).grid(row=0, column=col, padx=7, pady=4, sticky="w")
        ttk.Checkbutton(
            pay, text="TRR del je dejansko prejet (obvezno pri razdeljenem plačilu)",
            variable=self.bank_received, command=self.invalidate_review,
        ).grid(row=1, column=0, columnspan=4, padx=7, pady=(5, 0), sticky="w")

        cash = tk.Frame(pay, bg="#F7F9FA", highlightthickness=1, highlightbackground="#D8DEE3")
        cash.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 4))
        tk.Label(cash, text="PREJETI BANKOVEC / GOTOVINA", bg="#F7F9FA", fg=GRAY).grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")
        received_entry = ttk.Entry(cash, textvariable=self.cash_received, font=("Segoe UI", 17, "bold"), width=15)
        received_entry.grid(row=1, column=0, padx=10, pady=(0, 9), sticky="w")
        tk.Label(cash, textvariable=self.change_var, bg="#F7F9FA", fg=GREEN, font=("Segoe UI", 17, "bold")).grid(row=1, column=1, padx=16, sticky="e")
        for idx, value in enumerate((5, 10, 20, 50, 100, 200)):
            ttk.Button(cash, text=f"{value} EUR", command=lambda v=value: self.cash_received.set(str(v))).grid(row=2 + idx // 3, column=idx % 3, padx=6, pady=5, sticky="ew")
        for col in range(3):
            cash.columnconfigure(col, weight=1)

        upn = ttk.LabelFrame(dialog, text="UPN QR – SKENIRAJ V MOBILNI BANKI (NE Z NAVADNO KAMERO)", padding=10)
        upn.pack(fill="x", padx=14, pady=8)
        upn_status = tk.StringVar(value="Pripravljam plačilni nalog ...")
        ttk.Label(upn, textvariable=upn_status).pack(side="left", fill="x", expand=True)

        def prepare_dialog_upn(*, open_after=False):
            try:
                self.payment_upn_path = generate_upnqr_pdf(
                    output_dir=self.pdf_dir, amount=self.total,
                    reference=self.payment_reference, payer=self.customer_values(),
                    company=self._company_data(), paper_width_mm=self._paper_width(),
                )
                upn_status.set(f"UPN QR pripravljen: {self.payment_reference}")
                if open_after:
                    open_local_file(self.payment_upn_path)
                return True
            except Exception as exc:
                self.payment_upn_path = None
                upn_status.set(f"UPN QR ni pripravljen: {exc}")
                return False

        ttk.Button(
            upn, text="ODPRI QR ZA SKENIRANJE V BANKI",
            command=lambda: prepare_dialog_upn(open_after=True),
        ).pack(side="right")
        prepare_dialog_upn()

        actions = tk.Frame(dialog, bg=BG)
        actions.pack(side="bottom", fill="x", padx=14, pady=14)
        ttk.Button(actions, text="NAZAJ", command=close_dialog).pack(side="left")

        def confirm_and_issue(_key_event=None):
            # Kupec se lahko vpiše šele v tem oknu; QR zato tik pred izdajo
            # ponovno ustvarimo z zadnjimi vnesenimi podatki.
            prepare_dialog_upn()
            close_dialog()
            if self.review(ask_confirmation=False):
                self.on_f10()
            return "break"

        ttk.Button(
            actions, text="POTRDI, IZDAJ IN NATISNI  F10",
            command=confirm_and_issue, style="Blue.TButton",
        ).pack(side="right")
        dialog.bind("<F10>", confirm_and_issue)
        (customer_entries[0] if focus_customer else received_entry).focus_set()
        return "break"

    def customer_values(self):
        return {
            "name": self.customer_name.get(), "address": self.customer_address.get(),
            "phone": self.customer_phone.get(), "email": self.customer_email.get(),
            "tax_number": self.customer_tax.get(),
        }

    def _fiscalizer_for_issue(self):
        furs = dict(self.runtime.settings.get("furs") or {})
        production = (
            self.runtime.settings.get("environment") == "PRODUCTION"
            and furs.get("environment") == "PRODUCTION"
            and bool(furs.get("enabled"))
        )
        if not production:
            return None
        if "FURS_PRODUCTION" not in self.action_permissions:
            raise PosError("Uporabnik nima dovoljenja za produkcijsko davčno potrjevanje.")
        if self._production_fiscalizer is not None:
            return self._production_fiscalizer
        cert_path = Path(str(furs.get("certificate_path") or "")).expanduser()
        if not cert_path.is_absolute():
            cert_path = self.runtime.app_dir / cert_path
        password = simpledialog.askstring(
            "FURS produkcija", "Geslo namenskega potrdila davčne blagajne:",
            show="•", parent=self.win,
        )
        if password is None:
            raise PosError("Izdaja je preklicana: geslo FURS potrdila ni bilo vneseno.")
        bundle = load_production_certificate(
            cert_path, password,
            expected_tax_number=self.runtime.settings["business"]["tax_number"],
        )
        ca_path = str(furs.get("production_ca_bundle_path") or "").strip()
        if ca_path:
            ca = Path(ca_path)
            if not ca.is_absolute():
                ca = self.runtime.app_dir / ca
        else:
            ca = self.runtime.app_dir / "certs" / "furs-test-ca-bundle.pem"
        client = FursProductionClient(
            timeout_seconds=int(furs.get("production_timeout_seconds", 15)),
            ca_bundle_path=ca,
        )
        self._production_certificate_password = password
        self._production_fiscalizer = ProductionFiscalizer(bundle, client)
        return self._production_fiscalizer

    def open_preview(self):
        target = self.preview_a4_path or self.preview_receipt_path
        if target and Path(target).is_file():
            open_local_file(target)
        else:
            messagebox.showinfo("Blagajna", "Predogled še ni pripravljen.", parent=self.win)

    def on_f10(self, _event=None):
        if self.issued_id:
            if self.print_confirmed:
                return "break"
            if self.central_mode:
                try:
                    self.central_invoice = self.central_cashier.transport.get_invoice(
                        str(self.issued_id)
                    )
                    if self.central_invoice.get("status") != "ISSUED":
                        messagebox.showinfo(
                            "Blagajna – račun še ni dokončen",
                            "Centralni račun še čaka na fiskalizacijo oziroma uskladitev. "
                            "F10 ga ni izdal ponovno. Poskusi znova po potrditvi EOR.",
                            parent=self.win,
                        )
                        return "break"
                except Exception as exc:
                    messagebox.showwarning(
                        "Blagajna", f"Stanja centralnega računa ni mogoče preveriti:\n{exc}",
                        parent=self.win,
                    )
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
            self.open_payment_dialog()
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
        if self.central_mode:
            return self._issue_central_invoice()
        try:
            fiscalizer = self._fiscalizer_for_issue()
            furs_production = fiscalizer is not None
            self.issued_id, self.issued_no, self.total = issue_invoice(
                office_db=self.office_db, inventory_db=self.inventory_db,
                service_db=self.service_db, lines=self.public_invoice_lines(),
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
                customer_partner_id=self.customer_partner_id,
                replacement_of_invoice_id=self.replacement_of_invoice_id,
                authenticated_username=self.authenticated_username,
                action_permissions=self.action_permissions,
                fiscalization_required=furs_production,
                fiscalizer=fiscalizer,
                numbering_structure=str(
                    self.runtime.settings.get("business", {}).get("numbering_structure") or "B"
                ),
                test_mode=self.runtime.is_test,
            )
            connection = sqlite3.connect(str(self.office_db))
            row = connection.execute(
                "SELECT fiscal_status,COALESCE(furs_eor,'') FROM documents WHERE id=?",
                (self.issued_id,),
            ).fetchone()
            connection.close()
            status_text = (
                f"FURS POTRJENO • EOR {row[1]}" if row and row[0] == "CONFIRMED"
                else ("FURS ČAKA NA USKLADITEV" if row and row[0] == "PENDING_RECONCILIATION"
                      else "TRR – FURS ob izdaji ni potreben")
            )
            if self.runtime.is_test:
                status_text = "TEST/FURS izklopljen"
            self.header_status.set(f"Račun {self.issued_no}  •  {self.total:.2f} EUR  •  {status_text}")
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

    def _central_payment_payload(self):
        result = []
        for method, amount in self.payment_values().items():
            confirmed = method in {"CASH", "CARD"}
            if method == "BANK_TRANSFER":
                confirmed = bool(self.bank_received.get())
            result.append({
                "method": method, "amount": f"{dec(amount):.2f}",
                "confirmed": confirmed,
            })
        if any(item["method"] == "CASH" for item in result):
            session = self.central_cashier.current_cash_session(
                self.selected_premise(), self.runtime.electronic_device
            )
            if session is None:
                raise PosError(
                    "Gotovinsko plačilo zahteva odprt centralni blagajniški dnevnik."
                )
        return result

    def _central_billing_customer(self):
        if self.customer_partner_id is not None:
            if self.customer_partner_version is None:
                raise PosError("Centralni partner nima potrjene različice.")
            return {
                "source": "CENTRAL_PARTNER", "confirmed": True,
                "partner_id": str(self.customer_partner_id),
                "expected_version": int(self.customer_partner_version),
            }
        if self.central_service_draft_id:
            return {"source": "SERVICE_CUSTOMER", "confirmed": True}
        return {"source": "WALK_IN", "confirmed": True}

    def _issue_central_invoice(self):
        try:
            if self.central_operation_id:
                issued = self.central_cashier.retry(self.central_operation_id)
            elif self.central_service_draft_id:
                issued = self.central_cashier.issue_service_invoice(
                    draft_id=self.central_service_draft_id,
                    premise_id=self.selected_premise(),
                    electronic_device_id=self.runtime.electronic_device,
                    billing_customer=self._central_billing_customer(),
                    payments=self._central_payment_payload(),
                )
            else:
                issued = self.central_cashier.issue_direct_sale(
                    sale_id=self.issue_token,
                    premise_id=self.selected_premise(),
                    electronic_device_id=self.runtime.electronic_device,
                    billing_customer=self._central_billing_customer(),
                    payments=self._central_payment_payload(),
                    lines=direct_sale_lines_from_pos(self.public_invoice_lines()),
                )
            self.central_operation_id = issued.operation_id
            self.central_invoice = issued.invoice
            self.issued_id = str(issued.invoice["invoice_id"])
            self.issued_no = str(issued.invoice["invoice_number"])
            self.total = dec(issued.invoice["total_amount"])
            self.header_status.set(
                f"Račun {self.issued_no}  •  {self.total:.2f} EUR  •  "
                f"{issued.invoice['status']}"
            )
            self.premise_combo.configure(state="disabled")
            self.refresh_stock()
        except CashierResultUncertain as exc:
            self.central_operation_id = exc.operation_id
            self.review_ready = True
            self.state_var.set(
                "NEJASEN CENTRALNI REZULTAT – F10 ponovi isti ukaz; novega računa ne izdajaj."
            )
            messagebox.showwarning("Blagajna – nejasen rezultat", str(exc), parent=self.win)
            return "break"
        except CashierCommandRejected as exc:
            self.central_operation_id = None
            self.invalidate_review()
            messagebox.showwarning(
                "Blagajna – izdaja zavrnjena", f"{exc.code}: {exc}", parent=self.win
            )
            return "break"
        except Exception as exc:
            messagebox.showerror(
                "Blagajna",
                f"Centralni račun ni bil potrjeno izdan.\n\n{exc}", parent=self.win,
            )
            return "break"
        if self.central_invoice.get("status") != "ISSUED":
            self.state_var.set(
                f"Račun {self.issued_no} čaka na centralno fiskalizacijo; "
                "novega računa ni dovoljeno izdati."
            )
            messagebox.showinfo(
                "Blagajna – fiskalizacija",
                "Račun je centralno evidentiran, vendar še nima potrjenega končnega "
                "stanja. F10 bo pozneje preveril isti račun.", parent=self.win,
            )
            return "break"
        try:
            self._make_original_pdfs_and_print()
        except Exception as exc:
            self.state_var.set(
                f"Račun {self.issued_no} JE IZDAN. Ponovi samo PDF oziroma tisk."
            )
            self._post_issue_controls(printed=False)
            messagebox.showerror(
                "Blagajna – račun je že izdan",
                f"Centralni račun je izdan; novega ne izdajaj.\n\n{exc}",
                parent=self.win,
            )
        return "break"

    def _company_data(self):
        company = dict(self.runtime.settings.get("business", {}))
        company.update({"short_name": "MikroFix", "address": "Paradiž 73, 2282 Cirkulane"})
        return company

    def prepare_used_agreement(self):
        customer = self.customer_values()
        if (
            str(customer.get("name") or "").strip() in {"", "Končni kupec"}
            or not str(customer.get("address") or "").strip()
            or self.customer_partner_id is None
        ):
            messagebox.showwarning(
                "Dogovor za rabljeno napravo",
                "Pred pripravo 12-mesečnega dogovora s F9 izberi ali shrani kupca "
                "ter potrdi njegovo ime in naslov. Račun se ne bo izdal.",
                parent=self.win,
            )
            self.open_customer_dialog()
            return
        try:
            self.used_signed.set(False)
            self.used_agreement_id, agreement_no, self.used_agreement_path = (
                prepare_used_goods_agreement(
                    office_db=self.office_db, inventory_db=self.inventory_db,
                    article_ids=self.used_device_ids, customer=self.customer_values(),
                    issue_token=self.issue_token, output_dir=self.pdf_dir,
                    company=self._company_data(), operator_code=self.operator_code,
                    customer_partner_id=self.customer_partner_id,
                )
            )
            open_local_file(self.used_agreement_path)
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
            printer = preferred_printer(
                str(
                    self.runtime.settings.get("printer", {}).get("printer_name")
                    or self.runtime.settings.get("printer", {}).get("windows_printer_name")
                    or ""
                )
            )
            if not printer:
                raise PosError("Sistem ni našel fizičnega termičnega tiskalnika.")
            result = send_pdf_to_printer(path, printer)
            self.state_var.set(
                f"UPN QR je natisnjen ({result.printer}, opravilo {result.job_id})."
            )
        except Exception as exc:
            if 'path' in locals() and Path(path).is_file():
                open_local_file(path)
            messagebox.showwarning(
                "UPN QR",
                f"Neposredni tisk UPN QR ni uspel. PDF je odprt za ročni izpis.\n\n{exc}",
                parent=self.win,
            )
        return "break"

    def _make_original_pdfs_and_print(self):
        if self.central_mode:
            if not self.central_invoice:
                self.central_invoice = self.central_cashier.transport.get_invoice(
                    str(self.issued_id)
                )
            if self.central_invoice.get("status") != "ISSUED":
                raise PosError("Centralni račun še ni uspešno fiskaliziran.")
            self.a4_path, self.receipt_path = generate_central_invoice_pdfs(
                invoice=self.central_invoice, output_dir=self.pdf_dir,
                company=self._company_data(), paper_width_mm=self._paper_width(),
                test_mode=self.runtime.is_test,
            )
            self.print_confirmed = True
            try:
                result = self._attempt_print(self.receipt_path)
            except Exception as exc:
                self.state_var.set(
                    f"Račun {self.issued_no} je izdan; PDF-ja sta varno izdelana."
                )
                self._post_issue_controls(printed=False, document_ready=True)
                messagebox.showwarning(
                    "Blagajna – PDF je pripravljen",
                    f"Fizičnega izpisa ni bilo mogoče potrditi.\n\n{exc}",
                    parent=self.win,
                )
                return
            self.state_var.set(
                f"Centralni račun je izdan in PDF je pripravljen"
                + (f" ({result.printer})." if result.completed else ".")
            )
            self._post_issue_controls(
                printed=bool(result.completed), document_ready=True
            )
            return
        self.a4_path, self.receipt_path = generate_pos_pdfs(
            office_db=self.office_db, document_id=self.issued_id,
            output_dir=self.pdf_dir, company=self._company_data(),
            tax_note=self.runtime.tax_invoice_note,
            premise=str(self.premise_var.get() or self.runtime.active_premise),
            device=self.runtime.electronic_device,
            paper_width_mm=self._paper_width(),
            test_mode=self.runtime.is_test,
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
            self.runtime.settings.get("printer", {}).get("printer_name")
            or self.runtime.settings.get("printer", {}).get("windows_printer_name")
            or ""
        ).strip()
        printer = preferred_printer(configured)
        try:
            if printer:
                return send_pdf_to_printer(path, printer)
            else:
                raise RuntimeError(
                    "Sistem ni našel izbranega fizičnega termičnega tiskalnika."
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
            b = ttk.Button(footer, text="ODPRI A4 PDF", command=lambda: open_local_file(self.a4_path))
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
        if self.receipt_path:
            paper_width = self._paper_width()
            b = ttk.Button(footer, text=f"ODPRI {paper_width}-MM", command=lambda: open_local_file(self.receipt_path))
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
        if ready and not self.central_mode:
            b = ttk.Button(footer, text="NATISNI KOPIJO", command=self.print_copy)
            b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
            if self._can_storno():
                b = ttk.Button(footer, text="STORNO RAČUNA", command=self.storno_current_invoice)
                b.pack(side="right", padx=4, pady=10); self.post_buttons.append(b)
            # Delovno območje je po izdaji prazno; dejanja za zadnji izdani račun
            # ostanejo na gumbih in v glavi, ne pa v naslednjem osnutku.
            self.lines.clear()
            self.used_device_ids.clear()
            for iid in self.cart_tree.get_children():
                self.cart_tree.delete(iid)
            self._set_customer({"name": "Končni kupec"}, partner_id=None)
            self.payment_mode.set("CASH")
            self.cash_amount.set("0.00")
            self.bank_amount.set("0.00")
            self.cash_received.set("0.00")
            self.bank_received.set(False)
            self.used_frame.pack_forget()
            self.total_var.set("SKUPAJ: 0,00 EUR")
            self.autosave_var.set("Prejšnji račun zaključen – delovno območje je prazno")
        elif ready and self.central_mode and self._can_storno():
            invoice = self.central_invoice or {}
            if (
                invoice.get("status") == "ISSUED"
                and invoice.get("source_type") != "REVERSAL"
            ):
                b = ttk.Button(
                    footer, text="STORNO RAČUNA",
                    command=self.storno_current_invoice,
                )
                b.pack(side="right", padx=4, pady=10)
                self.post_buttons.append(b)

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
        if not self._require_storno_permission():
            return
        if not self.issued_id or not self.print_confirmed:
            return
        if self.central_mode:
            try:
                invoice = self.central_cashier.transport.get_invoice(
                    str(self.issued_id)
                )
            except Exception as exc:
                messagebox.showerror("Storno računa", str(exc), parent=self.win)
                return
            return self._storno_central_invoice(invoice, parent=self.win)
        reason = simpledialog.askstring(
            "Storno računa", "Razlog za storno celotnega računa:", parent=self.win
        )
        if not reason:
            return
        if not messagebox.askyesno(
            "Storno računa",
            f"Račun {self.issued_no} bo v celoti storniran. Izvirnik ostane v evidenci, "
            "ustvari se nov storno dokument. Zaloga se ne vrne in servis se ne odpre; "
            "za to sta potrebna ločena potrjena postopka. Nadaljujem?",
            parent=self.win,
        ):
            return
        try:
            fiscalizer = self._fiscalizer_for_issue()
            storno_id, storno_no = storno_invoice(
                office_db=self.office_db, inventory_db=self.inventory_db,
                service_db=self.service_db, document_id=int(self.issued_id),
                reason=reason, operator_code=self.operator_code,
                authenticated_username=self.authenticated_username,
                action_permissions=self.action_permissions,
                fiscalization_required=fiscalizer is not None,
                fiscalizer=fiscalizer,
                numbering_structure=str(
                    self.runtime.settings.get("business", {}).get("numbering_structure") or "B"
                ),
                test_mode=self.runtime.is_test,
            )
            a4, receipt = generate_pos_pdfs(
                office_db=self.office_db, document_id=storno_id,
                output_dir=self.pdf_dir, company=self._company_data(),
                tax_note=self.runtime.tax_invoice_note,
                premise=str(self.premise_var.get() or self.runtime.active_premise),
                device=self.runtime.electronic_device,
                paper_width_mm=self._paper_width(),
                test_mode=self.runtime.is_test,
            )
            try:
                self._attempt_print(receipt)
                mark_printed(self.office_db, storno_id, self.operator_code)
                print_note = " Storno izpis je zaključen."
            except Exception:
                print_note = " Storno PDF je pripravljen za ročni izpis."
                open_local_file(a4)
            self.state_var.set(
                f"Račun {self.issued_no} je storniran z dokumentom {storno_no}.{print_note}"
            )
            self.refresh_stock()
            self.review_btn.configure(text="NOV RAČUN",command=self.new_invoice)
            for child in getattr(self,"post_buttons",[]):
                child.configure(state="disabled")
        except Exception as exc:
            messagebox.showerror("Storno računa",str(exc),parent=self.win)

    def manage_unpaid_bank_invoices(self):
        if self.central_mode:
            return self._manage_central_unpaid_bank_invoices()
        try:
            rows = unpaid_bank_invoices(self.office_db)
        except Exception as exc:
            messagebox.showerror("Neplačani TRR", str(exc), parent=self.win)
            return
        if not rows:
            messagebox.showinfo(
                "Neplačani TRR", "Ni izdanih TRR računov, ki bi čakali na plačilo.",
                parent=self.win,
            )
            return
        dialog = tk.Toplevel(self.win)
        dialog.title("Poznejše gotovinsko plačilo TRR računa")
        dialog.geometry("760x430")
        dialog.transient(self.win)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=(
                "Izberi prvotni TRR račun. Program bo evidentiral plačilo na istem "
                "računu in ga naknadno poslal FURS-u."
            ),
            wraplength=710,
        ).pack(fill="x", padx=14, pady=(14, 8))
        tree = ttk.Treeview(
            dialog, columns=("invoice", "date", "customer", "amount", "reference"),
            show="headings", selectmode="browse", height=10,
        )
        for key, title, width in (
            ("invoice", "Račun", 125), ("date", "Datum", 100),
            ("customer", "Kupec", 210), ("amount", "Znesek", 90),
            ("reference", "Sklic", 170),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="e" if key == "amount" else "w")
        row_map = {}
        for row in rows:
            iid = tree.insert(
                "", "end",
                values=(
                    row["invoice_no"], row["date"], row["customer"],
                    f"{dec(row['invoice_total']):.2f} EUR", row["payment_reference"],
                ),
            )
            row_map[iid] = row
        tree.pack(fill="both", expand=True, padx=14, pady=8)
        if tree.get_children():
            tree.selection_set(tree.get_children()[0])
        method = tk.StringVar(value="BANK_TRANSFER")
        methods = ttk.Frame(dialog)
        methods.pack(fill="x", padx=14, pady=6)
        ttk.Label(methods, text="Dejansko prejeto:").pack(side="left")
        ttk.Radiobutton(
            methods, text="Nakazilo na TRR", variable=method, value="BANK_TRANSFER"
        ).pack(side="left", padx=10)
        if "CASH" in self.enabled_payment_methods:
            ttk.Radiobutton(methods, text="Gotovina", variable=method, value="CASH").pack(side="left", padx=10)
        if "CARD" in self.enabled_payment_methods:
            ttk.Radiobutton(methods, text="Kartica", variable=method, value="CARD").pack(side="left", padx=10)

        def confirm():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("Neplačani TRR", "Izberi račun.", parent=dialog)
                return
            row = row_map[selected[0]]
            method_label = {
                "BANK_TRANSFER": "nakazilom na TRR", "CASH": "gotovino",
                "CARD": "kartico",
            }[method.get()]
            if not messagebox.askyesno(
                "Potrdi dejansko plačilo",
                f"Potrjuješ, da je račun {row['invoice_no']} v znesku "
                f"{dec(row['invoice_total']):.2f} EUR zdaj v celoti plačan z {method_label}?\n\n"
                + ("Račun ostane isti; FURS ni potreben."
                 if method.get() == "BANK_TRANSFER"
                 else "Račun ostane isti in bo takoj poslan FURS-u."),
                parent=dialog,
            ):
                return
            try:
                fiscalizer = (
                    None if method.get() == "BANK_TRANSFER"
                    else self._fiscalizer_for_issue()
                )
                result = settle_unpaid_bank_invoice(
                    office_db=self.office_db, document_id=int(row["id"]),
                    method=method.get(), operator_code=self.operator_code,
                    fiscalization_required=method.get() != "BANK_TRANSFER",
                    fiscalizer=fiscalizer,
                    authenticated_username=self.authenticated_username,
                    numbering_structure=str(
                        self.runtime.settings.get("business", {}).get("numbering_structure") or "B"
                    ),
                    test_mode=self.runtime.is_test,
                )
                if method.get() != "BANK_TRANSFER":
                    copy_no = register_copy(
                        self.office_db, int(row["id"]), self.operator_code
                    )
                    a4, receipt = generate_pos_pdfs(
                        office_db=self.office_db, document_id=int(row["id"]),
                        output_dir=self.pdf_dir, company=self._company_data(),
                        tax_note=self.runtime.tax_invoice_note,
                        premise=self.selected_premise(), device=self.runtime.electronic_device,
                        paper_width_mm=self._paper_width(), copy_no=copy_no,
                        test_mode=self.runtime.is_test,
                    )
                    try:
                        self._attempt_print(receipt)
                    except Exception:
                        open_local_file(a4)
                dialog.destroy()
                eor = str(result.get("eor") or "")
                messagebox.showinfo(
                    "Plačilo evidentirano",
                    (f"Račun {row['invoice_no']} je označen kot plačan na TRR."
                     if method.get() == "BANK_TRANSFER"
                     else f"Račun {row['invoice_no']} je plačan in poslan v davčno "
                          f"potrditev.\nEOR: {eor or 'čaka na potrditev'}"),
                    parent=self.win,
                )
            except Exception as exc:
                messagebox.showerror(
                    "Plačila ni bilo mogoče evidentirati", str(exc), parent=dialog,
                )

        footer = ttk.Frame(dialog)
        footer.pack(fill="x", padx=14, pady=(5, 14))
        ttk.Button(footer, text="PREKLIČI", command=dialog.destroy).pack(side="left")
        ttk.Button(footer, text="POTRDI PLAČILO", command=confirm).pack(side="right")

    def _manage_central_unpaid_bank_invoices(self):
        try:
            rows = self.central_cashier.invoices(payment_status="UNPAID", limit=500)
            rows = [
                row for row in rows
                if len(row.get("snapshot", {}).get("payments", [])) == 1
                and str(row["snapshot"]["payments"][0].get("method") or "").upper()
                == "BANK_TRANSFER"
                and not bool(row["snapshot"]["payments"][0].get("confirmed"))
            ]
        except Exception as exc:
            messagebox.showerror("Neplačani TRR", str(exc), parent=self.win)
            return
        if not rows:
            messagebox.showinfo(
                "Neplačani TRR", "Ni centralnih TRR računov, ki čakajo na plačilo.",
                parent=self.win,
            )
            return
        dialog = tk.Toplevel(self.win)
        dialog.title("Potrditev plačila neplačanega TRR računa")
        dialog.geometry("820x460")
        dialog.transient(self.win)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=(
                "Izberi isti izdani račun in potrdi, kako je bilo celotno plačilo "
                "dejansko prejeto. Prejem na TRR ne sproži FURS; gotovina ali "
                "kartica sprožita naknadno davčno potrditev."
            ), wraplength=770,
        ).pack(fill="x", padx=14, pady=(14, 8))
        tree = ttk.Treeview(
            dialog, columns=("invoice", "date", "customer", "amount"),
            show="headings", selectmode="browse", height=11,
        )
        for key, title, width in (
            ("invoice", "Račun", 170), ("date", "Datum", 170),
            ("customer", "Kupec", 300), ("amount", "Znesek", 120),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="e" if key == "amount" else "w")
        row_map = {}
        for row in rows:
            customer = row.get("snapshot", {}).get("billing_customer", {})
            iid = tree.insert("", "end", values=(
                row["invoice_number"], str(row["issued_at"]).replace("T", " ")[:19],
                customer.get("name") or "Končni kupec",
                f"{dec(row['total_amount']):.2f} EUR",
            ))
            row_map[iid] = row
        tree.pack(fill="both", expand=True, padx=14, pady=8)
        tree.selection_set(tree.get_children()[0])
        method = tk.StringVar(value="BANK_TRANSFER")
        methods = ttk.Frame(dialog)
        methods.pack(fill="x", padx=14, pady=6)
        ttk.Label(methods, text="Dejansko prejeto:").pack(side="left")
        ttk.Radiobutton(
            methods, text="Nakazilo na TRR", variable=method, value="BANK_TRANSFER"
        ).pack(side="left", padx=10)
        if "CASH" in self.enabled_payment_methods:
            ttk.Radiobutton(
                methods, text="Gotovina", variable=method, value="CASH"
            ).pack(side="left", padx=10)
        if "CARD" in self.enabled_payment_methods:
            ttk.Radiobutton(
                methods, text="Kartica", variable=method, value="CARD"
            ).pack(side="left", padx=10)

        def confirm():
            selected = tree.selection()
            if not selected:
                return
            row = row_map[selected[0]]
            label = {
                "BANK_TRANSFER": "nakazilo na TRR", "CASH": "gotovino",
                "CARD": "kartico",
            }[method.get()]
            if not messagebox.askyesno(
                "Potrdi dejansko plačilo",
                f"Potrjuješ, da je isti račun {row['invoice_number']} v znesku "
                f"{dec(row['total_amount']):.2f} EUR v celoti plačan z {label}?",
                parent=dialog,
            ):
                return
            try:
                result = self.central_cashier.settle_unpaid_bank_invoice(
                    invoice_id=str(row["invoice_id"]), method=method.get()
                )
            except Exception as exc:
                messagebox.showerror("Plačilo ni potrjeno", str(exc), parent=dialog)
                return
            dialog.destroy()
            fiscal_note = (
                " FURS ni potreben." if method.get() == "BANK_TRANSFER"
                else " Naknadna davčna potrditev je uvrščena v varno vrsto."
            )
            messagebox.showinfo(
                "Plačilo potrjeno",
                f"Račun {result.invoice['invoice_number']} je označen kot plačan."
                + fiscal_note,
                parent=self.win,
            )

        footer = ttk.Frame(dialog)
        footer.pack(fill="x", padx=14, pady=(5, 14))
        ttk.Button(footer, text="PREKLIČI", command=dialog.destroy).pack(side="left")
        ttk.Button(footer, text="POTRDI PLAČILO", command=confirm).pack(side="right")

    def manage_pending_furs(self):
        try:
            rows = pending_furs_documents(self.office_db)
        except Exception as exc:
            messagebox.showerror("FURS uskladitev", str(exc), parent=self.win)
            return
        if not rows:
            messagebox.showinfo(
                "FURS uskladitev", "Ni FURS zahtevkov z neznanim stanjem.",
                parent=self.win,
            )
            return
        dialog = tk.Toplevel(self.win)
        dialog.title("FURS – uskladitev neznanega stanja")
        dialog.geometry("820x420")
        dialog.transient(self.win)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=(
                "Zaradi prekinitve povezave ni znano, ali je FURS zahtevek prejel. "
                "Ponovitev uporabi popolnoma isti MessageID in ZOI; novega računa ne ustvari."
            ),
            wraplength=770,
        ).pack(fill="x", padx=14, pady=(14, 8))
        tree = ttk.Treeview(
            dialog, columns=("invoice", "type", "amount", "attempts", "error"),
            show="headings", selectmode="browse", height=10,
        )
        for key, title, width in (
            ("invoice", "Račun", 135), ("type", "Vrsta", 130),
            ("amount", "Znesek", 90), ("attempts", "Poskusi", 65),
            ("error", "Zadnja napaka", 350),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="e" if key == "amount" else "w")
        row_map = {}
        for row in rows:
            iid = tree.insert(
                "", "end",
                values=(
                    row["invoice_no"], row["doc_type"],
                    f"{dec(row['invoice_total']):.2f} EUR", row["attempt_count"],
                    row["error_message"] or row["error_code"],
                ),
            )
            row_map[iid] = row
        tree.pack(fill="both", expand=True, padx=14, pady=8)
        tree.selection_set(tree.get_children()[0])

        def retry_selected():
            selected = tree.selection()
            if not selected:
                return
            row = row_map[selected[0]]
            if not messagebox.askyesno(
                "Ponovi isti FURS zahtevek",
                f"Ponovno pošljem isti zahtevek za {row['invoice_no']}?",
                parent=dialog,
            ):
                return
            try:
                fiscalizer = self._fiscalizer_for_issue()
                if fiscalizer is None:
                    raise PosError("Produkcijska FURS povezava ni vključena.")
                result = reconcile_pending_furs_document(
                    office_db=self.office_db, document_id=int(row["id"]),
                    fiscalizer=fiscalizer, operator_code=self.operator_code,
                    authenticated_username=self.authenticated_username,
                )
                if result["status"] != "CONFIRMED":
                    messagebox.showwarning(
                        "FURS stanje ostaja neznano",
                        str(result.get("error_message") or "Povezava znova ni dala zanesljivega odgovora."),
                        parent=dialog,
                    )
                    dialog.destroy()
                    return
                dialog.destroy()
                messagebox.showinfo(
                    "FURS potrjeno",
                    f"Račun {row['invoice_no']} je usklajen.\nEOR: {result.get('eor') or '-'}",
                    parent=self.win,
                )
            except Exception as exc:
                messagebox.showerror(
                    "Uskladitev ni uspela",
                    f"Račun ostaja zaklenjen za ročno preverjanje.\n\n{exc}",
                    parent=dialog,
                )

        def retry_all():
            if not messagebox.askyesno(
                "Uskladi celotno vrsto",
                f"Po vrstnem redu ponovno pošljem vseh {len(rows)} shranjenih zahtevkov?",
                parent=dialog,
            ):
                return
            confirmed = 0
            try:
                fiscalizer = self._fiscalizer_for_issue()
                if fiscalizer is None:
                    raise PosError("Produkcijska FURS povezava ni vključena.")
                for row in rows:
                    result = reconcile_pending_furs_document(
                        office_db=self.office_db, document_id=int(row["id"]),
                        fiscalizer=fiscalizer, operator_code=self.operator_code,
                        authenticated_username=self.authenticated_username,
                    )
                    if result["status"] != "CONFIRMED":
                        break
                    confirmed += 1
                remaining = len(pending_furs_documents(self.office_db))
                dialog.destroy()
                messagebox.showinfo(
                    "FURS uskladitev",
                    f"Potrjenih: {confirmed}. V vrsti ostaja: {remaining}.",
                    parent=self.win,
                )
            except Exception as exc:
                messagebox.showerror(
                    "Uskladitev se je ustavila",
                    f"Uspešno potrjenih pred napako: {confirmed}.\n\n{exc}",
                    parent=dialog,
                )

        footer = ttk.Frame(dialog)
        footer.pack(fill="x", padx=14, pady=(5, 14))
        ttk.Button(footer, text="ZAPRI", command=dialog.destroy).pack(side="left")
        ttk.Button(
            footer, text="PONOVI ISTI ZAHTEVEK", command=retry_selected
        ).pack(side="right")
        ttk.Button(
            footer, text="USKLADI VSE PO VRSTI", command=retry_all
        ).pack(side="right", padx=(0, 8))

    def new_invoice(self):
        if self.issued_id and not self.print_confirmed:
            messagebox.showwarning(
                "Blagajna", "Najprej potrdi uspešen tisk že izdanega računa.", parent=self.win
            )
            return
        if not self.issued_id and self.lines and not messagebox.askyesno(
            "Nov račun",
            "Trenutni osnutek vsebuje sledljive postavke. Celoten osnutek bo "
            "označen kot OPUŠČEN; posamezne vrstice ne bodo izbrisane. Nadaljujem?",
            parent=self.win,
        ):
            return
        if self._draft_after_id is not None:
            try:
                self.win.after_cancel(self._draft_after_id)
            except Exception:
                pass
            self._draft_after_id = None
        abandon_pos_draft(
            self.office_db, self.issue_token,
            actor_username=self.authenticated_username,
            device_id=self.runtime.electronic_device,
        )
        self.lines.clear(); self.used_device_ids.clear(); self.issue_token=str(uuid.uuid4())
        self.payment_reference=self._reference_for_token(self.issue_token)
        self.used_agreement_id=None; self.used_agreement_path=None
        self.issued_id=None; self.issued_no=""; self.review_ready=False; self.service_id=None
        self.central_service_draft_id=None; self.central_invoice=None
        self.central_quote=None; self.central_operation_id=None
        self.print_confirmed=False
        self.preview_a4_path=None; self.preview_receipt_path=None; self.review_fingerprint=""
        self.replacement_of_invoice_id=None; self.replacement_of_invoice_no=""
        self.replacement_var.set("Nadomestni račun: ni povezave")
        self._draft_started=False
        self._set_customer({"name": "Končni kupec"}, partner_id=None)
        self.used_signed.set(False); self.payment_mode.set("CASH"); self.cash_received.set("0.00")
        self.bank_received.set(False)
        for child in getattr(self, "post_buttons", []): child.destroy()
        self.post_buttons=[]; self.f10_btn.configure(text="PREGLED / IZDAJA  F10")
        self.premise_combo.configure(state="readonly")
        self.review_btn.configure(text="KONČNI PREGLED", command=self.review)
        self.header_status.set(
            f"Uporabnik: {self.authenticated_username}  •  FURS operater: {self.operator_code}  •  "
            f"{self.premise_var.get()}/{self.runtime.electronic_device}  •  "
            "TEST – FURS izklopljen"
        )
        self.state_var.set("Dodaj postavke in odpri končni pregled.")
        pending = None if self.central_mode else latest_pending_print(self.office_db)
        if pending:
            self._resume_pending_print(pending)
            return
        self.recalc(); self.refresh_stock(); self.search_entry.focus_set()

    def choose_replacement_source(self):
        if not self._require_storno_permission():
            return
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
        if self.central_mode:
            result = []
            for draft in self.central_cashier.ready_service_drafts():
                payload = dict(draft.get("locked_payload") or {})
                customer = dict(payload.get("customer") or {})
                service = dict(payload.get("service") or {})
                result.append({
                    "id": str(draft["draft_id"]),
                    "service_no": str(draft["service_order_id"]),
                    "date": str(draft.get("created_at") or "")[:10],
                    "customer": str(customer.get("name") or ""),
                    "model": str(service.get("device_model") or ""),
                    "serial": str(service.get("device_serial") or ""),
                    "locked_payload": payload,
                })
            return result
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
            service_id = selection[0] if self.central_mode else int(selection[0])
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
            if self.central_mode:
                return self._load_central_service_draft(
                    str(service_id), replace_current=replace_current
                )
            if replace_current:
                self.lines.clear()
                self.used_device_ids.clear()
            self.service_id = int(service_id)
            c=sqlite3.connect(str(self.service_db))
            seed=self.service_loader(c, int(service_id), labor_floor_eur=float(self.labor_floor)); c.close()
            service_customer = {
                "name": seed.customer or "Končni kupec",
                "address": seed.customer_address or "",
                "phone": seed.customer_phone or "",
                "email": seed.customer_email or "",
                "tax_number": "",
            }
            service_partner_id = save_customer_partner(
                self.office_db, service_customer
            ) if seed.customer else None
            self._set_customer(service_customer, partner_id=service_partner_id)
            for line_id, (
                kind, description, quantity, price, stored_tier, minutes,
                tariff_version, first_rate, extra_rate,
            ) in zip(
                seed.manual_line_ids, seed.manual_lines
            ):
                tier="T2" if str(stored_tier or kind).upper()=="T2" else "T1"
                self.lines.append(PosLine(
                    line_kind="SERVICE", name=description, code=tier,
                    service_tier=tier, quantity=dec(quantity),
                    base_unit_price=dec(price), origin_kind="SERVICE_MANUAL",
                    service_billable_minutes=int(minutes or 0),
                    service_tariff_version=str(tariff_version or ""),
                    service_first_rate=dec(first_rate),
                    service_extra_half_hour_rate=dec(extra_rate),
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
                has_reservation = False
                if article_id is not None:
                    inv_c = sqlite3.connect(str(self.inventory_db))
                    try:
                        reservation = inv_c.execute(
                            """
                            SELECT COALESCE(SUM(quantity),0)
                            FROM inventory_lot_allocations_v30
                            WHERE allocation_type='SERVICE_RESERVATION'
                              AND source_id=? AND source_line_id=?
                              AND reversed_at IS NULL
                            """,
                            (int(service_id), int(line_id)),
                        ).fetchone()
                        has_reservation = dec(reservation[0] if reservation else 0) > 0
                    except sqlite3.Error:
                        has_reservation = False
                    finally:
                        inv_c.close()
                self.lines.append(PosLine(
                    line_kind="ARTICLE", article_id=article_id, barcode=barcode or "",
                    name=name or "Vgrajeni del", code=code or "", quantity=Decimal("1"),
                    base_unit_price=dec(price), acquisition_cost=dec(row.get("acquisition_cost",0)),
                    stock_already_consumed=not has_reservation, origin_kind="SERVICE_PART",
                    origin_id=int(line_id), source_locked=True,
                ))
                if row.get("source_kind")=="BUYBACK_DEVICE": self.used_device_ids.add(int(article_id))
            self.recalc()
            self.state_var.set(f"Prevzete so postavke zaključenega servisa {seed.service_no}.")
        except Exception as exc:
            messagebox.showerror("Blagajna", f"Servisa ni mogoče prenesti v blagajno:\n{exc}", parent=self.win)

    def _load_central_service_draft(self, draft_id, *, replace_current=False):
        drafts = self.central_cashier.ready_service_drafts()
        draft = next(
            (item for item in drafts if str(item["draft_id"]) == str(draft_id)), None
        )
        if draft is None:
            raise PosError("Centralni servisni osnutek ni več pripravljen za račun.")
        payload = dict(draft.get("locked_payload") or {})
        if replace_current:
            self.lines.clear()
            self.used_device_ids.clear()
        self.central_service_draft_id = str(draft["draft_id"])
        self.service_id = None
        self._set_customer(dict(payload.get("customer") or {}), partner_id=None)
        for line in payload.get("billing_lines", []):
            self.lines.append(PosLine(
                line_kind="SERVICE", name=str(line["description"]),
                code=str(line.get("line_kind") or "T1"),
                quantity=dec(line["quantity"]),
                base_unit_price=dec(line["unit_price"]),
                service_tier=str(line.get("line_kind") or "T1"),
                service_billable_minutes=int(line.get("billable_minutes") or 0),
                service_tariff_version=str(line.get("tariff_version") or ""),
                source_locked=True, origin_kind="CENTRAL_SERVICE",
                central_line_id=str(line["line_id"]),
            ))
        for line in payload.get("parts", []):
            self.lines.append(PosLine(
                line_kind="ARTICLE", name=str(line["description"]),
                barcode=str(line["barcode"]), article_id=str(line["article_id"]),
                quantity=dec(line["quantity"]),
                base_unit_price=dec(line["unit_price"]),
                stock_already_consumed=True, source_locked=True,
                origin_kind="CENTRAL_SERVICE",
                requested_lot_serial=str(line.get("lot_serial") or ""),
                central_line_id=str(line["line_id"]),
            ))
        self.recalc()
        self.state_var.set("Centralni servisni osnutek je prenesen in zaklenjen.")
        self._save_draft_now()

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
