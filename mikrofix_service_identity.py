"""Central Service identity with an offline-safe, non-secret local cache."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from mikrofix_central_auth_client import (
    CentralAuthUnavailable,
    CentralSession,
    RequestsCentralAuthClient,
)


class ServiceIdentityError(RuntimeError):
    pass


class OfflineIdentityUnavailable(ServiceIdentityError):
    pass


@dataclass(frozen=True, slots=True)
class CachedServiceIdentity:
    username: str
    user_id: str
    user_version: int
    verified_at: str


@dataclass(frozen=True, slots=True)
class ServiceLoginIdentity:
    username: str
    user_id: str
    user_version: int
    online: bool


class ServiceIdentityCache:
    """Stores actor metadata only; never a PIN, workstation secret or token."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_central_identities(
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    user_id TEXT NOT NULL,
                    user_version INTEGER NOT NULL CHECK(user_version >= 1),
                    verified_at TEXT NOT NULL
                )
                """
            )

    def remember(self, session: CentralSession) -> CachedServiceIdentity:
        identity = CachedServiceIdentity(
            username=str(session.username).strip().lower(),
            user_id=str(UUID(str(session.user_id))),
            user_version=int(session.user_version),
            verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if identity.user_version < 1:
            raise ValueError("Central user version must be positive.")
        self.init()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO service_central_identities(
                    username,user_id,user_version,verified_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    user_id=excluded.user_id,
                    user_version=excluded.user_version,
                    verified_at=excluded.verified_at
                """,
                (
                    identity.username, identity.user_id,
                    identity.user_version, identity.verified_at,
                ),
            )
        return identity

    def get(self, username: str) -> CachedServiceIdentity | None:
        self.init()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT username,user_id,user_version,verified_at
                FROM service_central_identities WHERE username=?
                """,
                (str(username or "").strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        return CachedServiceIdentity(
            username=str(row[0]), user_id=str(UUID(str(row[1]))),
            user_version=int(row[2]), verified_at=str(row[3]),
        )

    def forget(self, username: str) -> None:
        self.init()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM service_central_identities WHERE username=?",
                (str(username or "").strip().lower(),),
            )


class ServiceCentralIdentityManager:
    """Keeps the bearer token in memory and supports verified offline actors."""

    def __init__(
        self,
        auth_client: RequestsCentralAuthClient,
        cache: ServiceIdentityCache,
        *,
        workstation_code: str,
        workstation_secret: str,
    ) -> None:
        self.auth_client = auth_client
        self.cache = cache
        self.workstation_code = str(workstation_code).strip()
        self.workstation_secret = str(workstation_secret).strip()
        self._session: CentralSession | None = None
        self._identity: ServiceLoginIdentity | None = None

    @property
    def access_token(self) -> str:
        return self._session.access_token if self._session is not None else ""

    @property
    def identity(self) -> ServiceLoginIdentity | None:
        return self._identity

    @property
    def must_change_pin(self) -> bool:
        return bool(self._session is not None and self._session.must_change_pin)

    def login(self, *, username: str, pin: str) -> ServiceLoginIdentity:
        normalized = str(username or "").strip().lower()
        previous_token = self.access_token
        try:
            session = self.auth_client.login(
                username=normalized,
                pin=pin,
                workstation_code=self.workstation_code,
                workstation_secret=self.workstation_secret,
                application_type="SERVICE",
            )
        except CentralAuthUnavailable as exc:
            cached = self.cache.get(normalized)
            if cached is None:
                raise OfflineIdentityUnavailable(
                    "Za tega uporabnika še ni varno potrjene centralne identitete; "
                    "prva prijava zahteva povezavo s strežnikom."
                ) from exc
            self._session = None
            self._identity = ServiceLoginIdentity(
                username=cached.username, user_id=cached.user_id,
                user_version=cached.user_version, online=False,
            )
            return self._identity

        if session.username.strip().lower() != normalized:
            raise ServiceIdentityError("Centralni strežnik je vrnil drugega uporabnika.")
        if session.application_type != "SERVICE" or "MODULE_SERVICE" not in session.permissions:
            raise ServiceIdentityError("Centralna seja nima pravice za program Servis.")
        self._session = session
        if previous_token and previous_token != session.access_token:
            try:
                self.auth_client.logout(previous_token)
            except Exception:
                # The replacement session is valid; an old/expired token will
                # disappear on the server even if explicit revocation fails.
                pass
        if session.must_change_pin:
            # This identity is not authorized for application operations yet.
            self.cache.forget(normalized)
            self._identity = ServiceLoginIdentity(
                username=normalized, user_id=str(UUID(str(session.user_id))),
                user_version=int(session.user_version), online=True,
            )
            return self._identity
        cached = self.cache.remember(session)
        self._identity = ServiceLoginIdentity(
            username=cached.username, user_id=cached.user_id,
            user_version=cached.user_version, online=True,
        )
        return self._identity

    def change_pin_and_relogin(
        self, *, username: str, current_pin: str, new_pin: str
    ) -> ServiceLoginIdentity:
        """Complete the mandatory change and obtain a fresh usable session."""
        if self._session is None:
            raise ServiceIdentityError(
                "Centralnega PIN-a brez povezane aktivne seje ni mogoče spremeniti."
            )
        token = self._session.access_token
        self.auth_client.change_pin(
            token, current_pin=current_pin, new_pin=new_pin
        )
        # A successful change revokes every old session on the server.
        self._session = None
        self._identity = None
        return self.login(username=username, pin=new_pin)

    def end_session(self) -> bool:
        """Forget locally first; central revocation is best effort on shutdown."""
        token = self.access_token
        self._session = None
        self._identity = None
        if not token:
            return True
        try:
            self.auth_client.logout(token)
        except Exception:
            return False
        return True
