"""Read-only Service availability derived from central stock plus local overlay.

The central snapshot is the last confirmed server truth.  A Service completion
that has already happened locally must remain deducted until the corresponding
central stock event has also reached this computer.  This module deliberately
does not write inventory movements and never invents positive stock.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID


FOUR = Decimal("0.0001")


class ServiceInventoryProjectionError(RuntimeError):
    """The local central-stock projection is internally inconsistent."""


def _quantity(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(FOUR)
    except (InvalidOperation, ValueError) as exc:
        raise ServiceInventoryProjectionError(
            "Central inventory contains an invalid quantity."
        ) from exc


@dataclass(frozen=True, slots=True)
class ServiceInventoryIdentity:
    article_uuid: str
    identity_uuid: str
    barcode: str
    description: str
    track_mode: str
    quantity_central: Decimal
    quantity_pending_local: Decimal
    quantity_available: Decimal
    current_sale_price: Decimal
    central_sequence: int
    blocked: bool
    blocked_reason: str

    @property
    def local_article_id(self) -> int:
        """Stable negative compatibility key; never collides with legacy IDs."""
        value = UUID(self.identity_uuid).int & ((1 << 62) - 1)
        return -(value or 1)


def _unreflected_completion_parts(
    connection: sqlite3.Connection,
) -> list[tuple[dict[str, Any], int | None, str]]:
    """Return local physical uses not yet represented by a pulled stock event."""
    rows = connection.execute(
        """
        SELECT o.payload_json,r.central_sequence,COALESCE(r.result_code,'')
        FROM sync_outbox_v32 o
        LEFT JOIN sync_receipts_v32 r ON r.operation_id=o.operation_id
        WHERE o.operation_type='CompleteServiceTechnically'
          AND o.status NOT IN ('SUPERSEDED')
        ORDER BY o.device_sequence
        """
    ).fetchall()
    result: list[tuple[dict[str, Any], int | None, str]] = []
    for payload_json, receipt_sequence, result_code in rows:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError) as exc:
            raise ServiceInventoryProjectionError(
                "A Service completion has an invalid persisted payload."
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceInventoryProjectionError(
                "A Service completion payload must be an object."
            )
        result.append((
            payload,
            int(receipt_sequence) if receipt_sequence is not None else None,
            str(result_code),
        ))
    return result


def service_inventory_identities(
    connection: sqlite3.Connection,
) -> list[ServiceInventoryIdentity]:
    """Build the safe Service view without mutating the central snapshot.

    An accepted local completion stops being an overlay only after this device
    has applied a stock snapshot whose central sequence is at least the receipt
    sequence.  Rejected completions remain deducted because the part physically
    left Service; their separately recorded sync conflict needs resolution.
    """
    snapshot_rows = connection.execute(
        """
        SELECT article_uuid,main_barcode,description,track_mode,
               quantity_available,current_sale_price,central_sequence,
               blocked,blocked_reason,identities_json
        FROM central_inventory_snapshot_v32
        ORDER BY description COLLATE NOCASE,article_uuid
        """
    ).fetchall()
    completions = _unreflected_completion_parts(connection)
    result: list[ServiceInventoryIdentity] = []
    local_keys: dict[int, str] = {}

    for row in snapshot_rows:
        article_uuid = str(row[0])
        article_sequence = int(row[6])
        try:
            identity_document = json.loads(str(row[9]))
            identities = identity_document["identities"]
        except (TypeError, ValueError, KeyError) as exc:
            raise ServiceInventoryProjectionError(
                f"Central identities are invalid for article {article_uuid}."
            ) from exc
        if not isinstance(identities, list):
            raise ServiceInventoryProjectionError(
                f"Central identities are invalid for article {article_uuid}."
            )

        for identity in identities:
            if not isinstance(identity, dict):
                raise ServiceInventoryProjectionError(
                    f"Central identity is invalid for article {article_uuid}."
                )
            barcode = str(identity.get("supplier_barcode") or "").strip()
            identity_uuid = str(identity.get("identity_id") or "").strip()
            if not barcode or not identity_uuid:
                raise ServiceInventoryProjectionError(
                    f"Central identity is incomplete for article {article_uuid}."
                )
            pending = Decimal("0.0000")
            for payload, receipt_sequence, result_code in completions:
                # Only an ACCEPTED receipt can prove that the server processed
                # the use.  It is reflected locally only after its event was
                # pulled into this article's snapshot.
                reflected = (
                    result_code == "ACCEPTED"
                    and receipt_sequence is not None
                    and article_sequence >= receipt_sequence
                )
                if reflected:
                    continue
                for part in payload.get("parts", []):
                    if not isinstance(part, dict):
                        raise ServiceInventoryProjectionError(
                            "A Service completion contains an invalid part line."
                        )
                    if str(part.get("barcode") or "").strip() != barcode:
                        continue
                    pending += _quantity(part.get("quantity"))

            central = _quantity(identity.get("quantity_available"))
            # A negative result is a visible shortage/conflict, never stock that
            # another Service may consume.  The conflict journal retains detail.
            available = max(Decimal("0.0000"), central - pending)
            projected = ServiceInventoryIdentity(
                article_uuid=article_uuid,
                identity_uuid=identity_uuid,
                barcode=barcode,
                description=str(row[2] or ""),
                track_mode=str(row[3] or ""),
                quantity_central=central,
                quantity_pending_local=pending.quantize(FOUR),
                quantity_available=available.quantize(FOUR),
                current_sale_price=_quantity(row[5]),
                central_sequence=article_sequence,
                blocked=bool(row[7]),
                blocked_reason=str(row[8] or ""),
            )
            previous_identity = local_keys.setdefault(
                projected.local_article_id, identity_uuid
            )
            if previous_identity != identity_uuid:
                raise ServiceInventoryProjectionError(
                    "Two central identities map to the same local compatibility key."
                )
            result.append(projected)
    return result


def service_inventory_by_barcode(
    connection: sqlite3.Connection, barcode: str,
) -> ServiceInventoryIdentity | None:
    wanted = str(barcode or "").strip()
    if not wanted:
        return None
    return next(
        (item for item in service_inventory_identities(connection)
         if item.barcode == wanted),
        None,
    )


def service_inventory_by_local_id(
    connection: sqlite3.Connection, article_id: int,
) -> ServiceInventoryIdentity | None:
    wanted = int(article_id)
    return next(
        (item for item in service_inventory_identities(connection)
         if item.local_article_id == wanted),
        None,
    )
