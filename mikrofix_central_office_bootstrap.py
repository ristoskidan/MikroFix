"""Explicit development bridge for the central Office workflow."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from mikrofix_office_client import RequestsOfficeTransport
from mikrofix_office_runtime import CentralOfficeWorkflow
from mikrofix_central_auth_client import CentralSession


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
    session: CentralSession | None = None,
    on_session_expired: Callable[[], None] | None = None,
) -> CentralOfficeHandle | None:
    """Build central Office only from an explicit complete configuration."""
    server_url = str((settings.get("database") or {}).get("server_url") or "").strip()
    if not server_url:
        return None
    if session is not None:
        if session.application_type != "OFFICE" or "MODULE_OFFICE" not in session.permissions:
            raise CentralOfficeBootstrapError(
                "Centralna seja ni veljavna za program Office."
            )
        if str(actor_id).strip().lower() != session.username.strip().lower():
            raise CentralOfficeBootstrapError(
                "Uporabnik Office se ne ujema s centralno sejo."
            )
        if str(device_id).strip().upper() != session.workstation_code.strip().upper():
            raise CentralOfficeBootstrapError(
                "Delovna postaja Office se ne ujema s centralno sejo."
            )
        selected_token = str(session.access_token).strip()
    else:
        selected_token = str(
            token if token is not None else os.environ.get("MIKROFIX_SYNC_TOKEN") or ""
        ).strip()
    if not selected_token:
        raise CentralOfficeBootstrapError(
            "Centralni strežnik je nastavljen, manjka pa razvojna skrivnost. "
            "Office zaradi varnosti ne bo prešel na lokalno knjiženje."
        )
    if (
        session is None
        and str(settings.get("environment") or "").strip().upper() == "PRODUCTION"
    ):
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
            RequestsOfficeTransport(
                server_url,
                selected_token,
                on_session_expired=on_session_expired,
            ),
            actor_id=actor,
            device_id=device,
        )
    except Exception:
        connection.close()
        raise
    return CentralOfficeHandle(workflow=workflow, connection=connection)
