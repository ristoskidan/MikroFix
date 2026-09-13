"""UI-neutral online Cashier workflow over the central API.

The local SQLite connection is only an idempotency journal. PostgreSQL remains
the sole source of invoices, stock, prices, partners, and fiscal state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

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
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.transport.list_invoices(
            query, invoice_status=invoice_status, limit=limit
        )

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
        dispatched = self.journal.dispatch(identity)
        if dispatched.uncertain:
            raise CashierResultUncertain(identity)
        receipt = dict(dispatched.receipt or {})
        result = dict(receipt.get("result") or {})
        if receipt.get("result_code") != "ACCEPTED":
            raise CashierCommandRejected(
                str(receipt.get("error_code") or "REJECTED"),
                str(receipt.get("error_message") or "Partner je zavrnjen."), result,
            )
        return result

    def unresolved_commands(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return list_dispatchable_operations(
            self.connection, self.journal.device_id, limit=limit
        )

    def retry(self, operation_id: str) -> CentralInvoiceResult:
        self._assert_command_order(operation_id)
        return self._dispatch_invoice(operation_id)

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
        invoice_id = str(result.get("invoice_id") or "")
        if not invoice_id:
            raise CashierWorkflowError("Centralni odgovor nima identitete računa.")
        invoice = self.transport.get_invoice(invoice_id)
        return CentralInvoiceResult(operation_id=operation_id, invoice=invoice)


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
