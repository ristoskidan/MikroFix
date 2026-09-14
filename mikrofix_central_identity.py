"""In-memory central identity for online-only MikroFIX applications."""

from __future__ import annotations

from dataclasses import dataclass

from mikrofix_central_auth_client import CentralSession, RequestsCentralAuthClient


class CentralApplicationIdentityError(RuntimeError):
    pass


APPLICATION_PERMISSIONS = {
    "OFFICE": "MODULE_OFFICE",
    "CASHIER": "MODULE_CASHIER",
}


@dataclass(frozen=True, slots=True)
class OnlineApplicationIdentity:
    username: str
    user_id: str
    user_version: int
    workstation_code: str
    application_type: str


class CentralApplicationIdentityManager:
    """Own one online bearer session and never persist its token or PIN."""

    def __init__(
        self,
        auth_client: RequestsCentralAuthClient,
        *,
        workstation_code: str,
        workstation_secret: str,
        application_type: str,
    ) -> None:
        application = str(application_type or "").strip().upper()
        if application not in APPLICATION_PERMISSIONS:
            raise ValueError("Only OFFICE and CASHIER online identities are supported.")
        code = str(workstation_code or "").strip().upper()
        secret = str(workstation_secret or "").strip()
        if not code or not secret:
            raise ValueError("Workstation code and secret are required.")
        self.auth_client = auth_client
        self.workstation_code = code
        self.workstation_secret = secret
        self.application_type = application
        self._session: CentralSession | None = None

    @property
    def session(self) -> CentralSession | None:
        return self._session

    @property
    def access_token(self) -> str:
        return self._session.access_token if self._session is not None else ""

    @property
    def must_change_pin(self) -> bool:
        return bool(self._session is not None and self._session.must_change_pin)

    def login(self, *, username: str, pin: str) -> OnlineApplicationIdentity:
        normalized = str(username or "").strip().lower()
        previous_token = self.access_token
        session = self.auth_client.login(
            username=normalized,
            pin=str(pin),
            workstation_code=self.workstation_code,
            workstation_secret=self.workstation_secret,
            application_type=self.application_type,
        )
        try:
            self._validate_session(session, expected_username=normalized)
        except CentralApplicationIdentityError:
            self._revoke_quietly(session.access_token)
            raise
        self._session = session
        if previous_token and previous_token != session.access_token:
            self._revoke_quietly(previous_token)
        return OnlineApplicationIdentity(
            username=session.username,
            user_id=session.user_id,
            user_version=session.user_version,
            workstation_code=session.workstation_code,
            application_type=session.application_type,
        )

    def _validate_session(
        self, session: CentralSession, *, expected_username: str
    ) -> None:
        required_permission = APPLICATION_PERMISSIONS[self.application_type]
        if not session.access_token.strip():
            raise CentralApplicationIdentityError(
                "Centralni strežnik ni vrnil veljavnega dostopnega žetona."
            )
        if session.username.strip().lower() != expected_username:
            raise CentralApplicationIdentityError(
                "Centralni strežnik je vrnil drugega uporabnika."
            )
        if session.application_type.strip().upper() != self.application_type:
            raise CentralApplicationIdentityError(
                "Centralna seja je vezana na drug program."
            )
        if session.workstation_code.strip().upper() != self.workstation_code:
            raise CentralApplicationIdentityError(
                "Centralna seja je vezana na drugo delovno postajo."
            )
        if required_permission not in session.permissions:
            raise CentralApplicationIdentityError(
                "Centralna seja nima pravice za ta program."
            )

    def _revoke_quietly(self, access_token: str) -> None:
        token = str(access_token or "").strip()
        if not token:
            return
        try:
            self.auth_client.logout(token)
        except Exception:
            pass

    def change_pin_and_relogin(
        self, *, username: str, current_pin: str, new_pin: str
    ) -> OnlineApplicationIdentity:
        if self._session is None:
            raise CentralApplicationIdentityError(
                "PIN-a brez aktivne centralne seje ni mogoče spremeniti."
            )
        token = self._session.access_token
        self.auth_client.change_pin(
            token, current_pin=current_pin, new_pin=new_pin
        )
        self._session = None
        return self.login(username=username, pin=new_pin)

    def end_session(self) -> bool:
        token = self.access_token
        self._session = None
        if not token:
            return True
        try:
            self.auth_client.logout(token)
        except Exception:
            return False
        return True
