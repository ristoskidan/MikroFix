"""Explicit development bridge for the central Office workflow."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from mikrofix_office_client import RequestsOfficeTransport
from mikrofix_office_runtime import CentralOfficeWorkflow


class CentralOfficeBootstrapError(RuntimeError):
    pass


@dataclass(slots=True)
class CentralOfficeHandle:
    workflow: CentralOfficeWorkflow
    connection: sqlite3.Connection

    def close(self) -> None:
        self.connection.close()


def build_central_office(
    *, settings: Mapping[str, Any], office_db: Path | str,
    actor_id: str, device_id: str, token: str | None = None,
) -> CentralOfficeHandle | None:
    """Build central Office only from an explicit complete configuration."""
    server_url = str((settings.get("database") or {}).get("server_url") or "").strip()
    if not server_url:
        return None
    selected_token = str(
        token if token is not None else os.environ.get("MIKROFIX_SYNC_TOKEN") or ""
    ).strip()
    if not selected_token:
        raise CentralOfficeBootstrapError(
            "Centralni strežnik je nastavljen, manjka pa razvojna skrivnost. "
            "Office zaradi varnosti ne bo prešel na lokalno knjiženje."
        )
    if str(settings.get("environment") or "").strip().upper() == "PRODUCTION":
        raise CentralOfficeBootstrapError(
            "Produkcijska centralna prijava Office še ni priklopljena. "
            "Razvojne skrivnosti v produkciji ni dovoljeno uporabiti."
        )
    actor = str(actor_id or "").strip()
    device = str(device_id or "").strip()
    if not actor or not device:
        raise CentralOfficeBootstrapError(
            "Centralni Office zahteva prijavljenega uporabnika in identiteto postaje."
        )
    connection = sqlite3.connect(str(office_db), timeout=20)
    try:
        workflow = CentralOfficeWorkflow(
            connection,
            RequestsOfficeTransport(server_url, selected_token),
            actor_id=actor,
            device_id=device,
        )
    except Exception:
        connection.close()
        raise
    return CentralOfficeHandle(workflow=workflow, connection=connection)
