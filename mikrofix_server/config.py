"""Explicit configuration for the central MikroFIX API."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerSettings:
    database_url: str
    sync_token: str
    environment: str = "development"
    session_minutes: int = 30

    @classmethod
    def from_environment(cls) -> "ServerSettings":
        database_url = os.environ.get(
            "MIKROFIX_DATABASE_URL", "postgresql+psycopg:///mikrofix_dev"
        ).strip()
        sync_token = os.environ.get("MIKROFIX_DEV_SYNC_TOKEN", "").strip()
        environment = os.environ.get(
            "MIKROFIX_SERVER_ENV", "development"
        ).strip()
        session_minutes = int(os.environ.get("MIKROFIX_SESSION_MINUTES", "30"))
        if not database_url:
            raise RuntimeError("MIKROFIX_DATABASE_URL must not be empty.")
        if environment.lower() != "production" and len(sync_token) < 24:
            raise RuntimeError(
                "MIKROFIX_DEV_SYNC_TOKEN must contain at least 24 characters."
            )
        if environment.lower() == "production" and sync_token:
            raise RuntimeError(
                "MIKROFIX_DEV_SYNC_TOKEN must be empty in production."
            )
        if not 5 <= session_minutes <= 480:
            raise RuntimeError("MIKROFIX_SESSION_MINUTES must be between 5 and 480.")
        return cls(
            database_url=database_url,
            sync_token=sync_token,
            environment=environment or "development",
            session_minutes=session_minutes,
        )
