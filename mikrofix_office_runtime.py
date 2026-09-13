"""UI-neutral online Office workflow over the central API.

The SQLite connection is only a durable idempotency journal. PostgreSQL is the
sole authority for partners, purchase receipts, inventory and issued invoices.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from mikrofix_office_client import OfficeCommandJournal, RequestsOfficeTransport
from mikrofix_sync_store import list_dispatchable_operations, recover_stale_sending


class OfficeWorkflowError(RuntimeError):
    pass


class OfficeResultUncertain(OfficeWorkflowError):
    def __init__(self, operation_id: str) -> None:
        super().__init__(
            "Rezultat centralne Office operacije ni jasen. Ukaza ne ponavljaj kot "
            "novo dejanje; po obnovitvi povezave uskladi isti zahtevek."
        )
        self.operation_id = operation_id


class OfficeCommandRejected(OfficeWorkflowError):
    def __init__(self, code: str, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message or code)
        self.code = code
        self.result = dict(result)


class PendingOfficeCommand(OfficeWorkflowError):
    def __init__(self, operation_id: str, operation_type: str) -> None:
        super().__init__(
            "Delovna postaja ima starejši nedokončan centralni Office zahtevek. "
            "Najprej uskladi njega; nove poslovne spremembe niso dovoljene."
        )
        self.operation_id = operation_id
        self.operation_type = operation_type


class CentralOfficeWorkflow:
    """One online Office session with ordered, durable command identities."""

    def __init__(
        self, connection: sqlite3.Connection, transport: RequestsOfficeTransport,
        *, actor_id: str, device_id: str,
    ) -> None:
        self.connection = connection
        self.transport = transport
        self.journal = OfficeCommandJournal(
            connection, transport, actor_id=actor_id, device_id=device_id
        )
        with self.connection:
            recover_stale_sending(self.connection, stale_after_seconds=1)

    def partners(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        return self.transport.search_partners(query, limit=limit)

    def inventory(self, query: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        return self.transport.search_inventory(query, limit=limit)

    def purchase_receipts(
        self, *, status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.transport.list_purchase_receipts(status=status, limit=limit)

    def invoices(
        self, query: str = "", *, payment_status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.transport.list_issued_invoices(
            query, payment_status=payment_status, limit=limit
        )

    def invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.transport.get_issued_invoice(invoice_id)

    def receipt_document_types(self) -> list[dict[str, Any]]:
        return self.transport.list_custom_document_types()

    def confirm_purchase_receipt(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_confirm_purchase_receipt(
            payload, operation_id=operation_id
        )
        return self._dispatch(identity)

    def attach_purchase_invoice(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_attach_purchase_invoice(
            payload, operation_id=operation_id
        )
        return self._dispatch(identity)

    def create_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_create_partner(payload, operation_id=operation_id)
        return self._dispatch(identity)

    def update_partner(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_update_partner(payload, operation_id=operation_id)
        return self._dispatch(identity)

    def set_partner_active(
        self, payload: Mapping[str, Any], *, operation_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        identity = self.journal.prepare_set_partner_active(
            payload, operation_id=operation_id
        )
        return self._dispatch(identity)

    def unresolved_commands(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return list_dispatchable_operations(
            self.connection, self.journal.device_id, limit=limit
        )

    def retry_command(self, operation_id: str) -> dict[str, Any]:
        self._assert_command_order(operation_id)
        return self._dispatch(operation_id)

    def _assert_command_order(self, operation_id: str | None) -> None:
        pending = self.unresolved_commands(limit=1)
        if not pending:
            return
        oldest = pending[0]
        if operation_id and str(oldest["operation_id"]) == str(operation_id):
            return
        raise PendingOfficeCommand(
            str(oldest["operation_id"]), str(oldest["operation_type"])
        )

    def _dispatch(self, operation_id: str) -> dict[str, Any]:
        dispatched = self.journal.dispatch(operation_id)
        if dispatched.uncertain:
            raise OfficeResultUncertain(operation_id)
        receipt = dict(dispatched.receipt or {})
        result = dict(receipt.get("result") or {})
        if receipt.get("result_code") != "ACCEPTED":
            raise OfficeCommandRejected(
                str(receipt.get("error_code") or "REJECTED"),
                str(receipt.get("error_message") or "Centralna operacija je zavrnjena."),
                result,
            )
        return result
