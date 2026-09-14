"""Durable, UI-neutral central boundary for the MikroFIX Office application.

Office is online for business posting.  Its local SQLite journal only preserves
an immutable command across a timeout, lost response, or application restart;
it is not a second stock database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import uuid4

from mikrofix_sync_client import RequestsSyncTransport, SyncTransportError, _wire_operation
from mikrofix_sync_contracts import (
    SyncOperationEnvelope,
    validated_attach_receipt_invoice_payload,
    validated_confirm_receipt_payload,
    validated_create_inventory_item_payload,
    validated_create_partner_payload,
    validated_set_partner_active_payload,
    validated_update_inventory_price_payload,
    validated_update_partner_payload,
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


class OfficeCommandTransport(Protocol):
    def submit_operation(self, operation: dict[str, Any]) -> dict[str, Any]: ...


class RequestsOfficeTransport(RequestsSyncTransport):
    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}{path}", params=params,
                headers=self.headers, timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)

    def search_partners(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        payload = self._get("/v1/office/partners", q=query, limit=limit)
        return self._list(payload, "partners")

    def search_inventory(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._get("/v1/office/inventory", q=query, limit=limit)
        return self._list(payload, "inventory")

    def list_purchase_receipts(
        self, *, status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        payload = self._get(
            "/v1/office/purchase-receipts", status=status, limit=limit,
        )
        return self._list(payload, "receipts")

    def list_issued_invoices(
        self, query: str = "", *, payment_status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if payment_status:
            params["payment_status"] = payment_status
        payload = self._get("/v1/office/invoices", **params)
        return self._list(payload, "invoices")

    def get_issued_invoice(self, invoice_id: str) -> dict[str, Any]:
        payload = self._get(f"/v1/office/invoices/{invoice_id}")
        if not payload.get("invoice_id") or not isinstance(payload.get("snapshot"), dict):
            raise SyncTransportError("Central API returned an invalid invoice.")
        return payload

    def list_custom_document_types(self) -> list[dict[str, Any]]:
        payload = self._get("/v1/office/receipt-document-types")
        return self._list(payload, "custom_types")

    @staticmethod
    def _list(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
        result = payload.get(field)
        if not isinstance(result, list):
            raise SyncTransportError(f"Central API returned an invalid {field} list.")
        return result


@dataclass(frozen=True, slots=True)
class OfficeCommandResult:
    operation_id: str
    status: str
    receipt: dict[str, Any] | None
    uncertain: bool


class OfficeCommandJournal:
    """Prepare once and retry the exact same immutable Office operation."""

    def __init__(
        self, connection: sqlite3.Connection, transport: OfficeCommandTransport,
        *, actor_id: str, device_id: str,
    ) -> None:
        self.connection = connection
        self.transport = transport
        self.actor_id = str(actor_id or "").strip()
        self.device_id = str(device_id or "").strip()
        if not self.actor_id or not self.device_id:
            raise ValueError("actor_id and device_id are required.")
        with self.connection:
            ensure_sync_store_schema(self.connection)

    def prepare_confirm_purchase_receipt(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_confirm_receipt_payload(payload)
        return self._prepare(
            operation_type="ConfirmPurchaseReceipt",
            aggregate_id=normalized["receipt_id"], expected_version=0,
            payload=normalized, operation_id=operation_id,
        )

    def prepare_create_inventory_item(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_create_inventory_item_payload(payload)
        return self._prepare(
            operation_type="CreateInventoryItem",
            aggregate_id=normalized["article_id"], expected_version=0,
            payload=normalized, operation_id=operation_id,
            aggregate_type="INVENTORY_ITEM",
        )

    def prepare_update_inventory_price(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_update_inventory_price_payload(payload)
        return self._prepare(
            operation_type="UpdateInventorySalePrice",
            aggregate_id=normalized["article_id"],
            expected_version=normalized["expected_version"],
            payload=normalized, operation_id=operation_id,
            aggregate_type="INVENTORY_ITEM",
        )

    def prepare_create_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_create_partner_payload(payload)
        return self._prepare_partner(
            operation_type="CreateOfficePartner", normalized=normalized,
            expected_version=0, operation_id=operation_id,
        )

    def prepare_update_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_update_partner_payload(payload)
        return self._prepare_partner(
            operation_type="UpdatePartner", normalized=normalized,
            expected_version=normalized["expected_version"],
            operation_id=operation_id,
        )

    def prepare_set_partner_active(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_set_partner_active_payload(payload)
        return self._prepare_partner(
            operation_type="SetPartnerActive", normalized=normalized,
            expected_version=normalized["expected_version"],
            operation_id=operation_id,
        )

    def _prepare_partner(
        self, *, operation_type: str, normalized: Mapping[str, Any],
        expected_version: int, operation_id: str | None,
    ) -> str:
        return self._prepare(
            operation_type=operation_type,
            aggregate_id=str(normalized["partner_id"]),
            expected_version=expected_version,
            payload=normalized,
            operation_id=operation_id,
            aggregate_type="PARTNER",
        )

    def prepare_attach_purchase_invoice(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> str:
        normalized = validated_attach_receipt_invoice_payload(payload)
        return self._prepare(
            operation_type="AttachPurchaseInvoice",
            aggregate_id=normalized["receipt_id"],
            expected_version=normalized["expected_version"],
            payload=normalized, operation_id=operation_id,
        )

    def _prepare(
        self, *, operation_type: str, aggregate_id: str,
        expected_version: int, payload: Mapping[str, Any],
        operation_id: str | None, aggregate_type: str = "PURCHASE_RECEIPT",
    ) -> str:
        identity = str(operation_id or uuid4())
        existing = outbox_operation(self.connection, identity)
        with self.connection:
            if existing:
                envelope = SyncOperationEnvelope(
                    operation_id=identity, operation_type=operation_type,
                    aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                    expected_version=expected_version, actor_id=self.actor_id,
                    device_id=self.device_id,
                    device_sequence=int(existing["device_sequence"]),
                    occurred_at=str(existing["occurred_at"]), payload=payload,
                )
            else:
                envelope = SyncOperationEnvelope(
                    operation_id=identity, operation_type=operation_type,
                    aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                    expected_version=expected_version, actor_id=self.actor_id,
                    device_id=self.device_id,
                    device_sequence=next_device_sequence(
                        self.connection, self.device_id
                    ),
                    occurred_at=now_text(), payload=payload,
                )
            enqueue_sync_operation(self.connection, envelope)
        return identity

    def dispatch(self, operation_id: str) -> OfficeCommandResult:
        row = outbox_operation(self.connection, operation_id)
        if not row:
            raise ValueError("Office command does not exist in the local journal.")
        if row["device_id"] != self.device_id:
            raise ValueError("Office command belongs to another workstation.")
        if row["status"] == "ACKNOWLEDGED":
            return OfficeCommandResult(
                operation_id=str(operation_id), status="ACKNOWLEDGED",
                receipt=operation_receipt(self.connection, operation_id),
                uncertain=False,
            )
        if row["status"] not in {"PENDING", "UNCERTAIN"}:
            raise ValueError(f"Office command is not dispatchable: {row['status']}.")
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
                    self.connection, operation_id,
                    result_code=str(receipt["result_code"]), response=receipt,
                    central_sequence=receipt.get("central_sequence"),
                )
            return OfficeCommandResult(
                operation_id=str(operation_id), status="ACKNOWLEDGED",
                receipt=receipt, uncertain=False,
            )
        except Exception as exc:
            with self.connection:
                mark_operation_uncertain(self.connection, operation_id, exc)
            return OfficeCommandResult(
                operation_id=str(operation_id), status="UNCERTAIN",
                receipt=None, uncertain=True,
            )
