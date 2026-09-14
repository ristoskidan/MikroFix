"""UI-neutral online Cashier workflow over the central API.

The local SQLite connection is only an idempotency journal. PostgreSQL remains
the sole source of invoices, stock, prices, partners, and fiscal state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from mikrofix_cashier_client import CashierCommandJournal, RequestsCashierTransport
from mikrofix_sync_store import list_dispatchable_operations, recover_stale_sending


class CashierWorkflowError(RuntimeError):
    pass


class CashierResultUncertain(CashierWorkflowError):
    def __init__(self, operation_id: str) -> None:
        super().__init__(
            "Rezultat centralne operacije ni jasen. Ne izdajaj novega računa; "
            "ponovi isto operacijo po obnovitvi povezave."
        )
        self.operation_id = operation_id


class CashierCommandRejected(CashierWorkflowError):
    def __init__(self, code: str, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message or code)
        self.code = code
        self.result = dict(result)


class PendingCashierCommand(CashierWorkflowError):
    def __init__(self, operation_id: str, operation_type: str) -> None:
        super().__init__(
            "Delovna postaja ima starejši nedokončan centralni zahtevek. "
            "Najprej uskladi istega; novega računa ni dovoljeno ustvariti."
        )
        self.operation_id = operation_id
        self.operation_type = operation_type


@dataclass(frozen=True, slots=True)
class CentralInvoiceResult:
    operation_id: str
    invoice: dict[str, Any]


class CentralCashierWorkflow:
    """One online Cashier session with durable command identities."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        transport: RequestsCashierTransport,
        *,
        actor_id: str,
        device_id: str,
    ) -> None:
        self.connection = connection
        self.transport = transport
        self.journal = CashierCommandJournal(
            connection, transport, actor_id=actor_id, device_id=device_id
        )
        with self.connection:
            recover_stale_sending(self.connection, stale_after_seconds=1)

    def inventory(self, query: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        return self.transport.search_inventory(query, limit=limit)

    def ready_service_drafts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.transport.list_ready_service_drafts(limit=limit)

    def invoices(
        self, query: str = "", *, invoice_status: str | None = None,
        payment_status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.transport.list_invoices(
            query, invoice_status=invoice_status,
            payment_status=payment_status, limit=limit
        )

    def current_cash_session(
        self, premise_id: str, electronic_device_id: str
    ) -> dict[str, Any] | None:
        return self.transport.current_cash_session(premise_id, electronic_device_id)

    def cash_sessions(
        self, *, premise_id: str = "", electronic_device_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.transport.list_cash_sessions(
            premise_id=premise_id, electronic_device_id=electronic_device_id,
            limit=limit,
        )

    def cash_session(self, session_id: str) -> dict[str, Any]:
        return self.transport.get_cash_session(session_id)

    def partners(self, query: str = "", *, limit: int = 30) -> list[dict[str, Any]]:
        return self.transport.search_partners(query, limit=limit)

    def quote_direct_sale(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return self.transport.quote_direct_sale(lines)

    def issue_direct_sale(
        self,
        *,
        sale_id: str,
        premise_id: str,
        electronic_device_id: str,
        billing_customer: Mapping[str, Any],
        payments: list[dict[str, Any]],
        lines: list[dict[str, Any]],
        operation_id: str | None = None,
    ) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_issue_direct_sale_invoice({
            "sale_id": sale_id,
            "expected_version": 0,
            "premise_id": premise_id,
            "electronic_device_id": electronic_device_id,
            "billing_customer": dict(billing_customer),
            "payments": payments,
            "lines": lines,
        }, operation_id=operation_id)
        return self._dispatch_invoice(identity)

    def issue_service_invoice(
        self,
        *,
        draft_id: str,
        premise_id: str,
        electronic_device_id: str,
        billing_customer: Mapping[str, Any],
        payments: list[dict[str, Any]],
        operation_id: str | None = None,
    ) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_issue_service_invoice({
            "draft_id": draft_id,
            "expected_version": 1,
            "premise_id": premise_id,
            "electronic_device_id": electronic_device_id,
            "billing_customer": dict(billing_customer),
            "payments": payments,
        }, operation_id=operation_id)
        return self._dispatch_invoice(identity)

    def create_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_create_partner(
            payload, operation_id=operation_id
        )
        return self._dispatch_command(identity)

    def open_cash_session(
        self, *, premise_id: str, electronic_device_id: str, opening_cash: Any,
        session_id: str | None = None, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_open_cash_session({
            "session_id": str(session_id or uuid4()), "premise_id": premise_id,
            "electronic_device_id": electronic_device_id,
            "opening_cash": str(opening_cash),
        }, operation_id=operation_id)
        return self._dispatch_command(identity)

    def close_cash_session(
        self, *, session_id: str, counted_cash: Any, note: str = "",
        expected_version: int = 1, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_close_cash_session({
            "session_id": session_id, "expected_version": expected_version,
            "counted_cash": str(counted_cash), "note": note,
        }, operation_id=operation_id)
        return self._dispatch_command(identity)

    def record_manual_cash_entry(
        self, *, session_id: str, direction: str, amount: Any,
        reference_no: str, description: str, entry_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_manual_cash_entry({
            "entry_id": str(entry_id or uuid4()), "session_id": session_id,
            "direction": direction, "amount": str(amount),
            "reference_no": reference_no, "description": description,
        }, operation_id=operation_id)
        return self._dispatch_command(identity)

    def settle_unpaid_bank_invoice(
        self, *, invoice_id: str, method: str,
        operation_id: str | None = None,
    ) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_settle_unpaid_bank_invoice({
            "invoice_id": invoice_id, "expected_version": 1, "method": method,
        }, operation_id=operation_id)
        return self._dispatch_invoice(identity)

    def storno_invoice(
        self, *, invoice_id: str, expected_version: int, reason: str,
        operation_id: str | None = None,
    ) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_storno_invoice({
            "invoice_id": invoice_id, "expected_version": expected_version,
            "reason": reason, "confirmed": True,
        }, operation_id=operation_id)
        result = self._dispatch_command(identity)
        reversal_id = str(result.get("reversal_invoice_id") or "")
        if not reversal_id:
            raise CashierWorkflowError(
                "Centralni odgovor nima identitete storno dokumenta."
            )
        reversal = self.transport.get_invoice(reversal_id)
        return CentralInvoiceResult(operation_id=identity, invoice=reversal)

    def unresolved_commands(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return list_dispatchable_operations(
            self.connection, self.journal.device_id, limit=limit
        )

    def retry(self, operation_id: str) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        return self._dispatch_invoice(operation_id)

    def retry_command(self, operation_id: str) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        return self._dispatch_command(operation_id)

    def _assert_command_order(self, operation_id: str | None) -> None:
        pending = self.unresolved_commands(limit=1)
        if not pending:
            return
        oldest = pending[0]
        if operation_id and str(oldest["operation_id"]) == str(operation_id):
            return
        raise PendingCashierCommand(
            str(oldest["operation_id"]), str(oldest["operation_type"])
        )

    def _dispatch_invoice(self, operation_id: str) -> CentralInvoiceResult:
        result = self._dispatch_command(operation_id)
        invoice_id = str(result.get("invoice_id") or "")
        if not invoice_id:
            raise CashierWorkflowError("Centralni odgovor nima identitete računa.")
        invoice = self.transport.get_invoice(invoice_id)
        return CentralInvoiceResult(operation_id=operation_id, invoice=invoice)

    def _dispatch_command(self, operation_id: str) -> dict[str, Any]:
        dispatched = self.journal.dispatch(operation_id)
        if dispatched.uncertain:
            raise CashierResultUncertain(operation_id)
        receipt = dict(dispatched.receipt or {})
        result = dict(receipt.get("result") or {})
        if receipt.get("result_code") != "ACCEPTED":
            raise CashierCommandRejected(
                str(receipt.get("error_code") or "REJECTED"),
                str(receipt.get("error_message") or "Centralna operacija je zavrnjena."),
                result,
            )
        return result


def direct_sale_lines_from_pos(lines) -> list[dict[str, Any]]:
    """Reduce POS view models to the only fields accepted by the central API."""
    result: list[dict[str, Any]] = []
    for line in lines:
        kind = str(getattr(line, "line_kind", "") or "").upper()
        common = {
            "line_id": str(getattr(line, "central_line_id", "") or ""),
            "line_kind": kind,
            "quantity": str(getattr(line, "quantity", "1")),
            "requested_discount_percent": str(
                getattr(line, "requested_discount_pct", "0")
            ),
        }
        if kind == "ARTICLE":
            result.append({
                **common,
                "barcode": str(getattr(line, "barcode", "") or ""),
                "lot_serial": str(
                    getattr(line, "requested_lot_serial", "") or ""
                ),
                "service_tier": "", "elapsed_minutes": 0,
            })
        elif kind == "SERVICE":
            result.append({
                **common, "barcode": "", "lot_serial": "",
                "service_tier": str(
                    getattr(line, "service_tier", "") or ""
                ),
                "elapsed_minutes": int(
                    getattr(line, "service_elapsed_minutes", 0) or 0
                ),
            })
        else:
            raise CashierWorkflowError(f"Nepodprta vrsta prodajne vrstice: {kind}")
    return result
