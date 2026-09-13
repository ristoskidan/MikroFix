"""Explicit development bridge from the desktop shell to central Cashier."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from mikrofix_cashier_client import RequestsCashierTransport
from mikrofix_cashier_runtime import CentralCashierWorkflow


class CentralCashierBootstrapError(RuntimeError):
    pass


@dataclass(slots=True)
class CentralCashierHandle:
    workflow: CentralCashierWorkflow
    connection: sqlite3.Connection

    def close(self) -> None:
        self.connection.close()


def build_central_cashier(
    *, settings: Mapping[str, Any], office_db: Path | str,
    actor_id: str, device_id: str, token: str | None = None,
) -> CentralCashierHandle | None:
    """Return central mode only when it is explicitly and completely configured.

    The shared development token is intentionally not a production login.  A
    configured central URL must never silently fall back to local accounting.
    """
    server_url = str((settings.get("database") or {}).get("server_url") or "").strip()
    if not server_url:
        return None
    selected_token = str(
        token if token is not None else os.environ.get("MIKROFIX_SYNC_TOKEN") or ""
    ).strip()
    if not selected_token:
        raise CentralCashierBootstrapError(
            "Centralni strežnik je nastavljen, manjka pa razvojna skrivnost. "
            "Blagajna zaradi varnosti ne bo prešla na lokalno knjiženje."
        )
    if str(settings.get("environment") or "").strip().upper() == "PRODUCTION":
        raise CentralCashierBootstrapError(
            "Produkcijska centralna prijava Blagajne še ni priklopljena. "
            "Razvojne skrivnosti v produkciji ni dovoljeno uporabiti."
        )
    actor = str(actor_id or "").strip()
    device = str(device_id or "").strip()
    if not actor or not device:
        raise CentralCashierBootstrapError(
            "Centralna Blagajna zahteva prijavljenega uporabnika in identiteto postaje."
        )
    connection = sqlite3.connect(str(office_db), timeout=20)
    try:
        workflow = CentralCashierWorkflow(
            connection,
            RequestsCashierTransport(server_url, selected_token),
            actor_id=actor,
            device_id=device,
        )
    except Exception:
        connection.close()
        raise
    return CentralCashierHandle(workflow=workflow, connection=connection)
