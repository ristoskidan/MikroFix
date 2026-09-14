"""Safe first desktop view for the online central Office application."""

from __future__ import annotations

import json
import tkinter as tk
from decimal import Decimal
from tkinter import messagebox, simpledialog, ttk
from typing import Any
from uuid import uuid4

from mikrofix_office_runtime import CentralOfficeWorkflow
from mikrofix_central_receipt_ui import open_attach_invoice, open_new_receipt
from mikrofix_tk_bootstrap import maximize_work_window


TAB_KEYS = ("inventory", "receipts", "invoices", "partners")


def normalized_central_office_tab(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    aliases = {"stock": "inventory", "documents": "receipts"}
    return aliases.get(cleaned, cleaned) if aliases.get(cleaned, cleaned) in TAB_KEYS else "inventory"


def central_office_rows(kind: str, rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    """Convert central read models to stable, presentation-only table rows."""
    if kind == "inventory":
        return [(
            str(row.get("article_id") or ""), str(row.get("description") or ""),
            str(row.get("main_barcode") or ""),
            str(row.get("supplier_barcode") or ""),
            str(row.get("article_quantity_available") or "0"),
            f"{Decimal(str(row.get('current_sale_price') or '0')):.2f} €",
            "BLOKIRAN" if row.get("blocked") else "AKTIVEN",
        ) for row in rows]
    if kind == "receipts":
        return [(
            str(row.get("receipt_id") or ""), str(row.get("receipt_number") or ""),
            str(row.get("received_date") or ""),
            str((row.get("supplier") or {}).get("name") or ""),
            str(row.get("external_document_no") or ""),
            f"{Decimal(str(row.get('document_total') or '0')):.2f} €",
            "ČAKA NA RAČUN" if row.get("status") == "PENDING_INVOICE" else "SPROŠČENO",
        ) for row in rows]
    if kind == "invoices":
        return [(
            str(row.get("invoice_id") or ""), str(row.get("invoice_number") or ""),
            str(row.get("issued_at") or "").replace("T", " ")[:19],
            str((row.get("snapshot") or {}).get("billing_customer", {}).get("name")
                or "Končni kupec"),
            f"{Decimal(str(row.get('total_amount') or '0')):.2f} €",
            "PLAČAN" if row.get("payment_status") == "PAID" else "NEPLAČAN",
            str(row.get("status") or ""),
        ) for row in rows]
    if kind == "partners":
        return [(
            str(row.get("partner_id") or ""), str(row.get("name") or ""),
            str(row.get("tax_number") or ""), str(row.get("post_city") or ""),
            str(row.get("phone") or ""), str(row.get("email") or ""),
            "AKTIVEN" if row.get("active", True) else "NEAKTIVEN",
        ) for row in rows]
    raise ValueError("Unsupported central Office table.")


def central_invoice_detail_text(invoice: dict[str, Any]) -> str:
    snapshot = dict(invoice.get("snapshot") or {})
    customer = dict(snapshot.get("billing_customer") or {})
    lines = list(snapshot.get("lines") or [])
    payments = list(snapshot.get("payments") or [])
    result = [
        f"RAČUN: {invoice.get('invoice_number') or snapshot.get('invoice_number') or '—'}",
        f"Izdano: {str(invoice.get('issued_at') or snapshot.get('issued_at') or '—').replace('T', ' ')[:19]}",
        f"Kupec: {customer.get('name') or 'Končni kupec'}",
    ]
    address = " ".join(
        value for value in (
            str(customer.get("address") or "").strip(),
            str(customer.get("post_city") or "").strip(),
        ) if value
    )
    if address:
        result.append(f"Naslov: {address}")
    if customer.get("tax_number"):
        result.append(f"Davčna številka: {customer['tax_number']}")
    result.extend(("", "POSTAVKE"))
    for line in lines:
        result.append(
            f"• {line.get('description') or 'Postavka'}  "
            f"{line.get('quantity') or '0'} × {line.get('unit_price') or '0.00'} €"
            f"  = {line.get('line_total') or '0.00'} €"
        )
    result.extend((
        "",
        f"SKUPAJ: {snapshot.get('total_amount') or invoice.get('total_amount') or '0.00'} €",
        "PLAČILO: " + ", ".join(
            f"{item.get('method') or '—'} {item.get('amount') or '0.00'} €"
            for item in payments
        ),
        f"Stanje računa: {invoice.get('status') or '—'}",
        f"Stanje plačila: {invoice.get('payment_status') or '—'}",
    ))
    if invoice.get("furs_eor"):
        result.append(f"EOR: {invoice['furs_eor']}")
    return "\n".join(result)


def central_partner_payload(
    values: dict[str, Any], *, existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(existing or {})
    result = {
        "partner_id": str(current.get("partner_id") or uuid4()),
        "verification_source": "MANUAL_REVIEW",
        "review_confirmed": True,
        "name": str(values.get("name") or "").strip(),
        "address": str(values.get("address") or "").strip(),
        "post_city": str(values.get("post_city") or "").strip(),
        "country": str(values.get("country") or "Slovenija").strip(),
        "tax_number": str(values.get("tax_number") or "").strip(),
        "registration_number": str(values.get("registration_number") or "").strip(),
        "phone": str(values.get("phone") or "").strip(),
        "email": str(values.get("email") or "").strip(),
        "source_reference": "CENTRAL_OFFICE_UI",
    }
    if current:
        result["expected_version"] = int(current["row_version"])
    return result


class CentralOfficeWindow:
    def __init__(
        self, parent, *, workflow: CentralOfficeWorkflow, username: str,
        initial_tab: str = "inventory", on_close=None,
    ) -> None:
        self.parent = parent
        self.workflow = workflow
        self.username = str(username or "")
        self.on_close_callback = on_close
        self.kind = normalized_central_office_tab(initial_tab)
        self.rows: list[dict[str, Any]] = []
        self.win = tk.Toplevel(parent)
        self.win.title("MikroFIX – Office (centralni razvojni način)")
        self.win.configure(bg="#F2F4F6")
        self.win.minsize(980, 620)
        maximize_work_window(self.win, "1280x760")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.select_kind(self.kind)

    def _build(self) -> None:
        header = tk.Frame(self.win, bg="#F2F4F6")
        header.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(
            header, text="MikroFIX  OFFICE", bg="#F2F4F6", fg="#2E3439",
            font=("Segoe UI", 22, "bold"),
        ).pack(side="left")
        ttk.Button(header, text="ZAPRI", command=self.close).pack(side="right")
        tk.Label(
            header, text=f"CENTRALNI RAZVOJNI NAČIN  •  {self.username}",
            bg="#F2F4F6", fg="#A63A3A", font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=16)

        nav = tk.Frame(self.win, bg="#FFFFFF")
        nav.pack(fill="x", padx=18, pady=(0, 8))
        for key, label in (
            ("inventory", "ZALOGA"), ("receipts", "PREJEMI"),
            ("invoices", "IZDANI RAČUNI"), ("partners", "PARTNERJI"),
        ):
            ttk.Button(nav, text=label, command=lambda item=key: self.select_kind(item)).pack(
                side="left", padx=4, pady=8
            )

        search = tk.Frame(self.win, bg="#F2F4F6")
        search.pack(fill="x", padx=18, pady=(0, 8))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _event: self.refresh())
        ttk.Button(search, text="IŠČI / OSVEŽI", command=self.refresh).pack(side="left", padx=(8, 0))
        ttk.Button(
            search, text="USKLADI NEDOKONČANO", command=self.reconcile_pending
        ).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar()
        tk.Label(search, textvariable=self.status_var, bg="#F2F4F6", fg="#606A73").pack(
            side="right", padx=12
        )

        self.table_frame = tk.Frame(self.win, bg="#FFFFFF")
        self.table_frame.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.context_actions = tk.Frame(self.win, bg="#F2F4F6")
        self.context_actions.pack(fill="x", padx=18, pady=(0, 8))
        self.notice_var = tk.StringVar(
            value="Centralni pregled je aktiven. Prejemi in partnerji se knjižijo prek centralnega strežnika; nedokončani vnosi se hranijo na tej postaji."
        )
        tk.Label(
            self.win, textvariable=self.notice_var, bg="#FFF4DC", fg="#805B13",
            anchor="w", padx=12, pady=8,
        ).pack(fill="x", padx=18, pady=(0, 14))

    def select_kind(self, kind: str) -> None:
        self.kind = normalized_central_office_tab(kind)
        self.search_var.set("")
        self._build_table()
        self.refresh()

    def _build_table(self) -> None:
        for child in self.table_frame.winfo_children():
            child.destroy()
        definitions = {
            "inventory": (("id", "article", "main", "supplier", "qty", "price", "status"),
                          ("ID", "Artikel", "Glavna barkoda", "Dobaviteljeva barkoda", "Na voljo", "Cena", "Stanje")),
            "receipts": (("id", "number", "date", "supplier", "external", "total", "status"),
                         ("ID", "Prejem", "Datum", "Dobavitelj", "Dokument", "Znesek", "Stanje")),
            "invoices": (("id", "number", "date", "customer", "total", "payment", "status"),
                         ("ID", "Račun", "Izdano", "Kupec", "Znesek", "Plačilo", "Stanje")),
            "partners": (("id", "name", "tax", "city", "phone", "email", "status"),
                         ("ID", "Naziv", "Davčna", "Kraj", "Telefon", "E-pošta", "Stanje")),
        }
        columns, labels = definitions[self.kind]
        self.tree = ttk.Treeview(self.table_frame, columns=columns, show="headings")
        for key, label in zip(columns, labels):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=0 if key == "id" else 130,
                             minwidth=0 if key == "id" else 60,
                             stretch=key in {"article", "supplier", "customer", "name"})
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(self.table_frame, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        if self.kind == "invoices":
            self.tree.bind("<Double-1>", lambda _event: self.open_invoice())
        for child in self.context_actions.winfo_children():
            child.destroy()
        if self.kind == "partners":
            ttk.Button(
                self.context_actions, text="NOV PARTNER", command=self.new_partner
            ).pack(side="left")
            ttk.Button(
                self.context_actions, text="UREDI IZBRANEGA", command=self.edit_partner
            ).pack(side="left", padx=6)
            ttk.Button(
                self.context_actions, text="AKTIVIRAJ / DEAKTIVIRAJ",
                command=self.toggle_partner,
            ).pack(side="left")
            self.tree.bind("<Double-1>", lambda _event: self.edit_partner())
        elif self.kind == "receipts":
            ttk.Button(
                self.context_actions, text="NOV PREJEM", command=self.new_receipt
            ).pack(side="left")
            ttk.Button(
                self.context_actions, text="DODAJ PREJETI RAČUN",
                command=self.attach_receipt_invoice,
            ).pack(side="left", padx=6)
            ttk.Button(
                self.context_actions, text="NEDOKONČANI VNOSI",
                command=self.resume_receipt_draft,
            ).pack(side="left")

    def refresh(self) -> None:
        query = self.search_var.get().strip()
        try:
            if self.kind == "inventory":
                rows = self.workflow.inventory(query, limit=300)
            elif self.kind == "receipts":
                rows = self.workflow.purchase_receipts(limit=300)
                if query:
                    needle = query.casefold()
                    rows = [row for row in rows if needle in json.dumps(row, ensure_ascii=False, default=str).casefold()]
            elif self.kind == "invoices":
                rows = self.workflow.invoices(query, limit=300)
            else:
                rows = self.workflow.partners(query, limit=200)
        except Exception as exc:
            self.status_var.set("CENTRALNI STREŽNIK NI DOSEGLJIV")
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.win)
            return
        self.rows = list(rows)
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            display_rows = central_office_rows(self.kind, self.rows)
        except Exception as exc:
            self.status_var.set("CENTRALNI ODGOVOR NI VELJAVEN")
            messagebox.showerror("MikroFIX Office", str(exc), parent=self.win)
            return
        for values in display_rows:
            self.tree.insert("", "end", values=values)
        self.status_var.set(f"{len(self.rows)} zapisov • centralni podatki")

    def open_invoice(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        invoice_id = str(self.tree.item(selected[0], "values")[0])
        try:
            invoice = self.workflow.invoice(invoice_id)
        except Exception as exc:
            messagebox.showerror("Izdani račun", str(exc), parent=self.win)
            return
        text = central_invoice_detail_text(invoice)
        dialog = tk.Toplevel(self.win)
        dialog.title(str(invoice.get("invoice_number") or "Izdani račun"))
        maximize_work_window(dialog, "900x650")
        body = tk.Text(dialog, wrap="word", font=("Segoe UI", 10))
        body.insert("1.0", text)
        body.configure(state="disabled")
        body.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Button(dialog, text="ZAPRI", command=dialog.destroy).pack(pady=(0, 12))

    def selected_record(self) -> dict[str, Any] | None:
        selected = self.tree.selection()
        if not selected:
            return None
        identity = str(self.tree.item(selected[0], "values")[0])
        return next(
            (row for row in self.rows if str(
                row.get("partner_id") or row.get("invoice_id")
                or row.get("receipt_id") or row.get("article_id") or ""
            ) == identity),
            None,
        )

    def new_partner(self) -> None:
        self._partner_dialog(None)

    def new_receipt(self) -> None:
        try:
            drafts = [
                row for row in self.workflow.active_work_drafts()
                if row["draft_kind"] == "PURCHASE_RECEIPT"
            ]
        except Exception as exc:
            messagebox.showerror("Nov prejem", str(exc), parent=self.win)
            return
        if drafts and messagebox.askyesno(
            "Nedokončan prejem",
            "Na tej postaji že obstaja nedokončan prejem. Ali želiš najprej nadaljevati zadnjega?",
            parent=self.win,
        ):
            open_new_receipt(
                self.win, workflow=self.workflow, draft=drafts[0],
                on_posted=self.refresh,
            )
            return
        open_new_receipt(self.win, workflow=self.workflow, on_posted=self.refresh)

    def attach_receipt_invoice(self) -> None:
        receipt = self.selected_record()
        if not receipt:
            messagebox.showwarning("Prejemi", "Izberi prejem.", parent=self.win)
            return
        if receipt.get("status") != "PENDING_INVOICE":
            messagebox.showwarning(
                "Prejemi", "Izbrani prejem ne čaka na prejeti račun.", parent=self.win
            )
            return
        try:
            existing = next((
                row for row in self.workflow.active_work_drafts()
                if row["draft_kind"] == "ATTACH_PURCHASE_INVOICE"
                and str(row.get("source_id")) == str(receipt.get("receipt_id"))
            ), None)
        except Exception as exc:
            messagebox.showerror("Prejeti račun", str(exc), parent=self.win)
            return
        if existing:
            open_attach_invoice(
                self.win, workflow=self.workflow, receipt=receipt,
                draft=existing, on_posted=self.refresh,
            )
            return
        open_attach_invoice(
            self.win, workflow=self.workflow, receipt=receipt, on_posted=self.refresh,
        )

    def resume_receipt_draft(self) -> None:
        try:
            drafts = self.workflow.active_work_drafts()
        except Exception as exc:
            messagebox.showerror("Nedokončani vnosi", str(exc), parent=self.win)
            return
        if not drafts:
            messagebox.showinfo(
                "Nedokončani vnosi", "Ni shranjenih nedokončanih vnosov.", parent=self.win
            )
            return
        labels = [
            f"{index + 1}. {('Prejeti račun' if row['draft_kind'] == 'ATTACH_PURCHASE_INVOICE' else 'Prejem')}"
            f" • {row['updated_at'].replace('T', ' ')[:19]}"
            for index, row in enumerate(drafts)
        ]
        chosen = simpledialog.askinteger(
            "Nedokončani vnosi",
            "Izberi številko vnosa:\n\n" + "\n".join(labels),
            parent=self.win, minvalue=1, maxvalue=len(drafts),
        )
        if chosen is None:
            return
        draft = drafts[chosen - 1]
        if draft["draft_kind"] == "PURCHASE_RECEIPT":
            open_new_receipt(
                self.win, workflow=self.workflow, draft=draft, on_posted=self.refresh,
            )
            return
        receipt = next(
            (row for row in self.workflow.purchase_receipts(status="PENDING_INVOICE", limit=500)
             if str(row.get("receipt_id")) == str(draft.get("source_id"))),
            None,
        )
        if not receipt:
            messagebox.showerror(
                "Nedokončani vnosi",
                "Povezani prejem ne čaka več na račun. Osnutek ni bil spremenjen.",
                parent=self.win,
            )
            return
        open_attach_invoice(
            self.win, workflow=self.workflow, receipt=receipt, draft=draft,
            on_posted=self.refresh,
        )

    def edit_partner(self) -> None:
        partner = self.selected_record()
        if not partner:
            messagebox.showwarning("Partnerji", "Izberi partnerja.", parent=self.win)
            return
        self._partner_dialog(partner)

    def _partner_dialog(self, existing: dict[str, Any] | None) -> None:
        dialog = tk.Toplevel(self.win)
        dialog.title("Uredi partnerja" if existing else "Nov partner")
        dialog.transient(self.win)
        dialog.grab_set()
        frame = tk.Frame(dialog, bg="#F2F4F6")
        frame.pack(fill="both", expand=True, padx=16, pady=14)
        fields = (
            ("name", "Naziv"), ("address", "Naslov"),
            ("post_city", "Pošta in kraj"), ("country", "Država"),
            ("tax_number", "Davčna številka"),
            ("registration_number", "Matična številka"),
            ("phone", "Telefon"), ("email", "E-pošta"),
        )
        variables: dict[str, tk.StringVar] = {}
        source = dict(existing or {})
        for row_no, (key, label) in enumerate(fields):
            tk.Label(frame, text=label, bg="#F2F4F6", anchor="w").grid(
                row=row_no, column=0, sticky="w", padx=(0, 10), pady=4
            )
            variable = tk.StringVar(
                value=str(source.get(key) or ("Slovenija" if key == "country" else ""))
            )
            variables[key] = variable
            ttk.Entry(frame, textvariable=variable, width=48).grid(
                row=row_no, column=1, sticky="ew", pady=4
            )
        frame.grid_columnconfigure(1, weight=1)

        def save():
            payload = central_partner_payload(
                {key: value.get() for key, value in variables.items()},
                existing=existing,
            )
            try:
                if existing:
                    self.workflow.update_partner(payload)
                else:
                    self.workflow.create_partner(payload)
            except Exception as exc:
                messagebox.showerror("Partnerji", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.refresh()

        actions = tk.Frame(frame, bg="#F2F4F6")
        actions.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="PREKLIČI", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="SHRANI", command=save).pack(side="right", padx=6)

    def toggle_partner(self) -> None:
        partner = self.selected_record()
        if not partner:
            messagebox.showwarning("Partnerji", "Izberi partnerja.", parent=self.win)
            return
        target = not bool(partner.get("active", True))
        reason = simpledialog.askstring(
            "Sprememba stanja partnerja",
            "Vpiši razlog aktivacije:" if target else "Vpiši razlog deaktivacije:",
            parent=self.win,
        )
        if not str(reason or "").strip():
            return
        try:
            self.workflow.set_partner_active({
                "partner_id": str(partner["partner_id"]),
                "expected_version": int(partner["row_version"]),
                "active": target, "reason": str(reason).strip(),
            })
        except Exception as exc:
            messagebox.showerror("Partnerji", str(exc), parent=self.win)
            self.refresh()
            return
        self.refresh()

    def reconcile_pending(self) -> None:
        pending = self.workflow.unresolved_commands(limit=1)
        if not pending:
            messagebox.showinfo(
                "Centralni Office", "Ni nedokončanih centralnih ukazov.", parent=self.win
            )
            return
        operation = pending[0]
        try:
            self.workflow.retry_command(str(operation["operation_id"]))
        except Exception as exc:
            messagebox.showerror(
                "Centralni Office",
                "Nedokončanega ukaza še ni mogoče uskladiti:\n" + str(exc),
                parent=self.win,
            )
            return
        messagebox.showinfo(
            "Centralni Office",
            "Isti nedokončani ukaz je bil uspešno usklajen.",
            parent=self.win,
        )
        self.refresh()

    def close(self) -> None:
        self.win.destroy()
        if self.on_close_callback:
            self.on_close_callback()


def open_central_office(parent, **kwargs):
    return CentralOfficeWindow(parent, **kwargs)
