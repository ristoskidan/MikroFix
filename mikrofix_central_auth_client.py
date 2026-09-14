"""Framework-neutral client for a central MikroFIX user session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mikrofix_sync_client import SyncTransportError


class CentralAuthUnavailable(SyncTransportError):
    """The central server could not be reached."""


class CentralAuthRejected(SyncTransportError):
    """The central server explicitly rejected the credentials or workstation."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class CentralAuthProtocolError(SyncTransportError):
    """The central server replied, but not with the agreed auth contract."""


@dataclass(frozen=True, slots=True)
class CentralSession:
    access_token: str
    expires_at: str
    user_id: str
    user_version: int
    username: str
    display_name: str
    role: str
    workstation_code: str
    application_type: str
    permissions: frozenset[str]
    must_change_pin: bool


class RequestsCentralAuthClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 15) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _json(response) -> dict[str, Any]:
        if response.status_code != 200:
            try:
                detail = str(response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = ""
            raise CentralAuthRejected(
                detail or f"Central authentication returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise CentralAuthProtocolError(
                "Central authentication returned invalid JSON."
            ) from exc
        if not isinstance(result, dict):
            raise CentralAuthProtocolError(
                "Central authentication response is invalid."
            )
        return result

    def login(
        self,
        *,
        username: str,
        pin: str,
        workstation_code: str,
        workstation_secret: str,
        application_type: str,
    ) -> CentralSession:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/v1/auth/login",
                json={
                    "username": username,
                    "pin": pin,
                    "workstation_code": workstation_code,
                    "workstation_secret": workstation_secret,
                    "application_type": application_type,
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise CentralAuthUnavailable(str(exc)) from exc
        result = self._json(response)
        required = {
            "access_token", "expires_at", "user_id", "user_version", "username",
            "display_name", "role", "workstation_code", "application_type",
            "permissions", "must_change_pin",
        }
        if not required.issubset(result) or not isinstance(result["permissions"], list):
            raise CentralAuthProtocolError(
                "Central authentication response is incomplete."
            )
        return CentralSession(
            access_token=str(result["access_token"]),
            expires_at=str(result["expires_at"]), user_id=str(result["user_id"]),
            user_version=int(result["user_version"]), username=str(result["username"]),
            display_name=str(result["display_name"]), role=str(result["role"]),
            workstation_code=str(result["workstation_code"]),
            application_type=str(result["application_type"]),
            permissions=frozenset(str(value) for value in result["permissions"]),
            must_change_pin=bool(result["must_change_pin"]),
        )

    def logout(self, access_token: str) -> None:
        """Revoke one in-memory central session token."""
        import requests

        token = str(access_token or "").strip()
        if not token:
            return
        try:
            response = requests.post(
                f"{self.base_url}/v1/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise CentralAuthUnavailable(str(exc)) from exc
        if response.status_code != 204:
            try:
                detail = str(response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = ""
            raise CentralAuthRejected(
                detail or f"Central logout returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )

    def change_pin(self, access_token: str, *, current_pin: str, new_pin: str) -> None:
        """Change the central PIN; the server revokes the current session."""
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/v1/auth/change-pin",
                json={"current_pin": current_pin, "new_pin": new_pin},
                headers={"Authorization": f"Bearer {str(access_token).strip()}"},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise CentralAuthUnavailable(str(exc)) from exc
        if response.status_code != 204:
            try:
                detail = str(response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = ""
            raise CentralAuthRejected(
                detail or f"Central PIN change returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
