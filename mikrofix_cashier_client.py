"""Durable, UI-neutral command boundary for MikroFIX Cashier.

Cashier remains an online application.  The local SQLite journal is not an
offline business database: it only preserves the exact identity and body of a
command across a timeout, lost HTTP response, or application restart.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import uuid4

from mikrofix_sync_client import RequestsSyncTransport, SyncTransportError, _wire_operation
from mikrofix_sync_contracts import (
    SyncOperationEnvelope,
    validated_create_partner_payload,
    validated_close_cash_session_payload,
    validated_issue_service_invoice_payload,
    validated_issue_direct_sale_invoice_payload,
    validated_manual_cash_entry_payload,
    validated_open_cash_session_payload,
    validated_settle_unpaid_bank_invoice_payload,
)
from mikrofix_sync_store import (
    acknowledge_operation,
    enqueue_sync_operation,
    ensure_sync_store_schema,
    mark_operation_sending,
    mark_operation_uncertain,
    next_device_sequence,
    now_text,
    operation_receipt,
    outbox_operation,
)


class CashierCommandTransport(Protocol):
    def submit_operation(self, operation: dict[str, Any]) -> dict[str, Any]: ...


class RequestsCashierTransport(RequestsSyncTransport):
    """HTTP reads for Cashier plus the shared idempotent command endpoint."""

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)

    def list_ready_service_drafts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        response = self._get("/v1/cashier/service-invoice-drafts", limit=limit)
        drafts = response.get("drafts")
        if not isinstance(drafts, list):
            raise SyncTransportError("Central API returned an invalid draft list.")
        return drafts

    def search_partners(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
        response = self._get("/v1/cashier/partners", q=query, limit=limit)
        partners = response.get("partners")
        if not isinstance(partners, list):
            raise SyncTransportError("Central API returned an invalid partner list.")
        return partners

    def search_inventory(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        response = self._get("/v1/cashier/inventory", q=query, limit=limit)
        inventory = response.get("inventory")
        if not isinstance(inventory, list):
            raise SyncTransportError("Central API returned an invalid inventory list.")
        return inventory

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        response = self._get(f"/v1/cashier/invoices/{invoice_id}")
        snapshot = response.get("snapshot")
        if not isinstance(snapshot, dict) or not response.get("invoice_number"):
            raise SyncTransportError("Central API returned an invalid invoice.")
        return response

    def list_invoices(
        self, query: str = "", *, invoice_status: str | None = None,
        payment_status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if invoice_status:
            params["invoice_status"] = invoice_status
        if payment_status:
            params["payment_status"] = payment_status
        response = self._get("/v1/cashier/invoices", **params)
        rows = response.get("invoices")
        if not isinstance(rows, list):
            raise SyncTransportError("Central API returned an invalid invoice list.")
        return rows

    def current_cash_session(
        self, premise_id: str, electronic_device_id: str
    ) -> dict[str, Any] | None:
        response = self._get(
            "/v1/cashier/cash-sessions/current", premise_id=premise_id,
            electronic_device_id=electronic_device_id,
        )
        session = response.get("session")
        if session is not None and not isinstance(session, dict):
            raise SyncTransportError("Central API returned an invalid cash session.")
        return session

    def list_cash_sessions(
        self, *, premise_id: str = "", electronic_device_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        response = self._get(
            "/v1/cashier/cash-sessions", premise_id=premise_id,
            electronic_device_id=electronic_device_id, limit=limit,
        )
        rows = response.get("sessions")
        if not isinstance(rows, list):
            raise SyncTransportError("Central API returned an invalid cash-session list.")
        return rows

    def get_cash_session(self, session_id: str) -> dict[str, Any]:
        response = self._get(f"/v1/cashier/cash-sessions/{session_id}")
        if not response.get("session_id") or not isinstance(response.get("entries"), list):
            raise SyncTransportError("Central API returned an invalid cash journal.")
        return response

    def quote_direct_sale(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/v1/cashier/direct-sale-quote",
                json={"lines": lines}, headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        quote = self._json_response(response)
        if not isinstance(quote.get("lines"), list) or not quote.get("total_amount"):
            raise SyncTransportError("Central API returned an invalid direct-sale quote.")
        return quote

    def suggest_partner_from_ajpes(self, tax_number: str) -> dict[str, Any]:
        response = self._get(f"/v1/cashier/partners/ajpes/{tax_number}")
        suggestion = response.get("suggestion")
        if not isinstance(suggestion, dict) or suggestion.get("review_confirmed") is not False:
            raise SyncTransportError("Central API returned an invalid AJPES suggestion.")
        return suggestion


@dataclass(frozen=True, slots=True)
class CashierCommandResult:
    operation_id: str
    status: str
    receipt: dict[str, Any] | None
    uncertain: bool


class CashierCommandJournal:
    """Prepare first, then dispatch the same immutable central command."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        transport: CashierCommandTransport,
        *,
        actor_id: str,
        device_id: str,
    ) -> None:
        self.connection = connection
        self.transport = transport
        self.actor_id = str(actor_id or "").strip()
        self.device_id = str(device_id or "").strip()
        if not self.actor_id or not self.device_id:
            raise ValueError("actor_id and device_id are required.")
        with self.connection:
            ensure_sync_store_schema(self.connection)

    def prepare_create_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_create_partner_payload(payload)
        return self._prepare(
            operation_type="CreatePartner",
            aggregate_type="PARTNER",
            aggregate_id=normalized["partner_id"],
            expected_version=0,
            payload=normalized,
            operation_id=operation_id,
        )

    def prepare_open_cash_session(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_open_cash_session_payload(payload)
        return self._prepare(
            operation_type="OpenCashSession", aggregate_type="CASH_SESSION",
            aggregate_id=normalized["session_id"], expected_version=0,
            payload=normalized, operation_id=operation_id,
        )

    def prepare_close_cash_session(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_close_cash_session_payload(payload)
        return self._prepare(
            operation_type="CloseCashSession", aggregate_type="CASH_SESSION",
            aggregate_id=normalized["session_id"],
            expected_version=normalized["expected_version"],
            payload=normalized, operation_id=operation_id,
        )

    def prepare_manual_cash_entry(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_manual_cash_entry_payload(payload)
        return self._prepare(
            operation_type="RecordManualCashEntry", aggregate_type="CASH_ENTRY",
            aggregate_id=normalized["entry_id"], expected_version=0,
            payload=normalized, operation_id=operation_id,
        )

    def prepare_settle_unpaid_bank_invoice(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_settle_unpaid_bank_invoice_payload(payload)
        return self._prepare(
            operation_type="SettleUnpaidBankInvoice", aggregate_type="INVOICE",
            aggregate_id=normalized["invoice_id"],
            expected_version=normalized["expected_version"],
            payload=normalized, operation_id=operation_id,
        )

    def prepare_issue_service_invoice(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_issue_service_invoice_payload(payload)
        return self._prepare(
            operation_type="IssueServiceInvoice",
            aggregate_type="INVOICE_DRAFT",
            aggregate_id=normalized["draft_id"],
            expected_version=normalized["expected_version"],
            payload=normalized,
            operation_id=operation_id,
        )

    def prepare_issue_direct_sale_invoice(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None
    ) -> str:
        normalized = validated_issue_direct_sale_invoice_payload(payload)
        return self._prepare(
            operation_type="IssueDirectSaleInvoice",
            aggregate_type="DIRECT_SALE",
            aggregate_id=normalized["sale_id"],
            expected_version=0,
            payload=normalized,
            operation_id=operation_id,
        )

    def _prepare(
        self,
        *,
        operation_type: str,
        aggregate_type: str,
        aggregate_id: str,
        expected_version: int,
        payload: Mapping[str, Any],
        operation_id: str | None,
    ) -> str:
        identity = str(operation_id or uuid4())
        existing = outbox_operation(self.connection, identity)
        with self.connection:
            if existing:
                envelope = SyncOperationEnvelope(
                    operation_id=identity,
                    operation_type=operation_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    expected_version=expected_version,
                    actor_id=self.actor_id,
                    device_id=self.device_id,
                    device_sequence=int(existing["device_sequence"]),
                    occurred_at=str(existing["occurred_at"]),
                    payload=payload,
                )
            else:
                envelope = SyncOperationEnvelope(
                    operation_id=identity,
                    operation_type=operation_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    expected_version=expected_version,
                    actor_id=self.actor_id,
                    device_id=self.device_id,
                    device_sequence=next_device_sequence(
                        self.connection, self.device_id
                    ),
                    occurred_at=now_text(),
                    payload=payload,
                )
            enqueue_sync_operation(self.connection, envelope)
        return identity

    def dispatch(self, operation_id: str) -> CashierCommandResult:
        row = outbox_operation(self.connection, operation_id)
        if not row:
            raise ValueError("Cashier command does not exist in the local journal.")
        if row["device_id"] != self.device_id:
            raise ValueError("Cashier command belongs to another workstation.")
        if row["status"] == "ACKNOWLEDGED":
            return CashierCommandResult(
                operation_id=str(operation_id),
                status="ACKNOWLEDGED",
                receipt=operation_receipt(self.connection, operation_id),
                uncertain=False,
            )
        if row["status"] not in {"PENDING", "UNCERTAIN"}:
            raise ValueError(f"Cashier command is not dispatchable: {row['status']}.")

        with self.connection:
            mark_operation_sending(self.connection, operation_id)
        try:
            receipt = self.transport.submit_operation(_wire_operation(row))
            if receipt.get("operation_id") != operation_id:
                raise SyncTransportError("Receipt operation_id does not match request.")
            if receipt.get("result_code") not in {"ACCEPTED", "REJECTED"}:
                raise SyncTransportError("Receipt result_code is not supported.")
            with self.connection:
                acknowledge_operation(
                    self.connection,
                    operation_id,
                    result_code=str(receipt["result_code"]),
                    response=receipt,
                    central_sequence=receipt.get("central_sequence"),
                )
            return CashierCommandResult(
                operation_id=str(operation_id),
                status="ACKNOWLEDGED",
                receipt=receipt,
                uncertain=False,
            )
        except Exception as exc:
            with self.connection:
                mark_operation_uncertain(self.connection, operation_id, exc)
            return CashierCommandResult(
                operation_id=str(operation_id),
                status="UNCERTAIN",
                receipt=None,
                uncertain=True,
            )
