"""Central user, workstation and opaque-session authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import Connection, and_, insert, select, update

from mikrofix_auth import (
    ALL_PERMISSIONS,
    MAX_FAILED_ATTEMPTS,
    ROLE_DEFAULT_PERMISSIONS,
    hash_pin,
    validate_pin_format,
)

from .models import (
    central_audit_events,
    central_sessions,
    central_user_permissions,
    central_user_permission_history,
    central_users,
    workstations,
)


APPLICATION_PERMISSION = {
    "SERVICE": "MODULE_SERVICE",
    "CASHIER": "MODULE_CASHIER",
    "OFFICE": "MODULE_OFFICE",
}


class AuthenticationError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    user_uuid: UUID | None
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    workstation_uuid: UUID | None
    workstation_code: str
    application_type: str
    operator_tax_number: str
    user_version: int
    signing_public_key: bytes | None
    must_change_pin: bool
    legacy_development: bool = False

    def require(self, permission: str) -> None:
        if not self.legacy_development and permission not in self.permissions:
            raise AuthorizationError(f"Missing permission: {permission}.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _secret_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _audit(
    connection: Connection,
    event_type: str,
    success: bool,
    *,
    actor_uuid: UUID | None = None,
    workstation_uuid: UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(insert(central_audit_events).values(
        event_type=event_type,
        success=success,
        actor_uuid=actor_uuid,
        workstation_uuid=workstation_uuid,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=details or {},
    ))


def create_central_user(
    connection: Connection,
    *,
    username: str,
    display_name: str,
    role: str,
    pin: str,
    permissions: Iterable[str] | None = None,
    operator_tax_number: str = "",
    created_by: UUID | None = None,
    must_change_pin: bool = True,
) -> UUID:
    normalized = str(username or "").strip().lower()
    display = str(display_name or "").strip()
    selected_role = str(role or "").strip().upper()
    if not normalized or not all(ch.isalnum() or ch in "._-" for ch in normalized):
        raise ValueError("Invalid username.")
    if not display or selected_role not in ROLE_DEFAULT_PERMISSIONS:
        raise ValueError("Display name and supported role are required.")
    if not validate_pin_format(pin):
        raise ValueError("PIN must contain 6-14 non-trivial digits.")
    tax_number = str(operator_tax_number or "").strip()
    if tax_number and (len(tax_number) != 8 or not tax_number.isdigit()):
        raise ValueError("Operator tax number must contain exactly 8 digits.")
    selected = set(
        ROLE_DEFAULT_PERMISSIONS[selected_role]
        if permissions is None else permissions
    )
    if not selected.issubset(ALL_PERMISSIONS):
        raise ValueError("Unsupported permission.")
    salt = secrets.token_bytes(16)
    user_uuid = uuid4()
    connection.execute(insert(central_users).values(
        user_uuid=user_uuid,
        username=normalized,
        username_normalized=normalized,
        display_name=display,
        role=selected_role,
        pin_salt=salt,
        pin_hash=hash_pin(pin, salt),
        operator_tax_number=tax_number or None,
        must_change_pin=must_change_pin,
    ))
    if selected:
        connection.execute(insert(central_user_permissions), [
            {"user_uuid": user_uuid, "permission": permission, "granted_by": created_by}
            for permission in sorted(selected)
        ])
        connection.execute(insert(central_user_permission_history), [
            {
                "user_uuid": user_uuid, "permission": permission,
                "valid_from_version": 1, "recorded_by": created_by,
            }
            for permission in sorted(selected)
        ])
    _audit(
        connection, "USER_CREATED", True, actor_uuid=created_by,
        entity_type="USER", entity_id=str(user_uuid),
        details={"username": normalized, "role": selected_role},
    )
    return user_uuid


def register_workstation(
    connection: Connection,
    *,
    workstation_code: str,
    display_name: str,
    application_type: str,
    approved_by: UUID,
    signing_public_key: bytes | None = None,
) -> tuple[UUID, str]:
    code = str(workstation_code or "").strip().upper()
    display = str(display_name or "").strip()
    application = str(application_type or "").strip().upper()
    if not code or not display or application not in APPLICATION_PERMISSION:
        raise ValueError("Valid workstation code, name and application are required.")
    public_key = bytes(signing_public_key) if signing_public_key is not None else None
    if application == "SERVICE" and (public_key is None or len(public_key) != 32):
        raise ValueError("A Service workstation requires an Ed25519 public key.")
    if application != "SERVICE" and public_key is not None:
        raise ValueError("Only a Service workstation may register a signing key.")
    raw_secret = secrets.token_urlsafe(48)
    workstation_uuid = uuid4()
    connection.execute(insert(workstations).values(
        workstation_uuid=workstation_uuid,
        workstation_code=code,
        display_name=display,
        application_type=application,
        credential_hash=_secret_hash(raw_secret),
        signing_public_key=public_key,
        approved_by=approved_by,
        approved_at=_utcnow(),
    ))
    _audit(
        connection, "WORKSTATION_REGISTERED", True, actor_uuid=approved_by,
        workstation_uuid=workstation_uuid, entity_type="WORKSTATION",
        entity_id=str(workstation_uuid), details={"code": code, "application": application},
    )
    return workstation_uuid, raw_secret


def update_user_access(
    connection: Connection,
    *,
    user_uuid: UUID,
    display_name: str,
    role: str,
    permissions: Iterable[str],
    is_active: bool,
    operator_tax_number: str,
    changed_by: UUID,
    expected_version: int,
) -> int:
    display = str(display_name or "").strip()
    selected_role = str(role or "").strip().upper()
    selected = {str(value).strip().upper() for value in permissions}
    tax_number = str(operator_tax_number or "").strip()
    if not display or selected_role not in ROLE_DEFAULT_PERMISSIONS:
        raise ValueError("Display name and supported role are required.")
    if not selected.issubset(ALL_PERMISSIONS):
        raise ValueError("Unsupported permission.")
    if tax_number and (len(tax_number) != 8 or not tax_number.isdigit()):
        raise ValueError("Operator tax number must contain exactly 8 digits.")
    row = connection.execute(
        select(central_users).where(
            central_users.c.user_uuid == user_uuid
        ).with_for_update()
    ).mappings().one_or_none()
    if not row:
        raise ValueError("User does not exist.")
    if int(row["row_version"]) != int(expected_version):
        raise ValueError("User was changed; reload it before saving access.")
    if row["username_normalized"] == "daniel" and (
        not is_active or selected_role != "ADMIN" or selected != set(ALL_PERMISSIONS)
    ):
        raise ValueError("The initial administrator must remain active with all rights.")
    new_version = int(row["row_version"]) + 1
    connection.execute(
        update(central_users).where(
            central_users.c.user_uuid == user_uuid
        ).values(
            display_name=display, role=selected_role, is_active=bool(is_active),
            operator_tax_number=tax_number or None, row_version=new_version,
            updated_at=_utcnow(),
        )
    )
    connection.execute(
        update(central_user_permission_history).where(
            central_user_permission_history.c.user_uuid == user_uuid,
            central_user_permission_history.c.valid_until_version.is_(None),
        ).values(valid_until_version=new_version - 1)
    )
    connection.execute(
        central_user_permissions.delete().where(
            central_user_permissions.c.user_uuid == user_uuid
        )
    )
    if selected:
        connection.execute(insert(central_user_permissions), [
            {"user_uuid": user_uuid, "permission": permission, "granted_by": changed_by}
            for permission in sorted(selected)
        ])
        connection.execute(insert(central_user_permission_history), [
            {
                "user_uuid": user_uuid, "permission": permission,
                "valid_from_version": new_version, "recorded_by": changed_by,
            }
            for permission in sorted(selected)
        ])
    connection.execute(
        update(central_sessions).where(
            central_sessions.c.user_uuid == user_uuid,
            central_sessions.c.revoked_at.is_(None),
        ).values(revoked_at=_utcnow())
    )
    _audit(
        connection, "USER_ACCESS_CHANGED", True, actor_uuid=changed_by,
        entity_type="USER", entity_id=str(user_uuid),
        details={
            "role": selected_role, "permissions": sorted(selected),
            "is_active": bool(is_active), "row_version": new_version,
        },
    )
    return new_version


def set_workstation_active(
    connection: Connection,
    *,
    workstation_uuid: UUID,
    is_active: bool,
    changed_by: UUID,
) -> None:
    changed = connection.execute(
        update(workstations).where(
            workstations.c.workstation_uuid == workstation_uuid
        ).values(is_active=bool(is_active), updated_at=_utcnow())
    )
    if changed.rowcount != 1:
        raise ValueError("Workstation does not exist.")
    if not is_active:
        connection.execute(
            update(central_sessions).where(
                central_sessions.c.workstation_uuid == workstation_uuid,
                central_sessions.c.revoked_at.is_(None),
            ).values(revoked_at=_utcnow())
        )
    _audit(
        connection, "WORKSTATION_ACCESS_CHANGED", True, actor_uuid=changed_by,
        workstation_uuid=workstation_uuid, entity_type="WORKSTATION",
        entity_id=str(workstation_uuid), details={"is_active": bool(is_active)},
    )


def change_own_pin(
    connection: Connection,
    *,
    user_uuid: UUID,
    current_pin: str,
    new_pin: str,
    workstation_uuid: UUID,
) -> None:
    if not validate_pin_format(new_pin):
        raise ValueError("New PIN must contain 6-14 non-trivial digits.")
    row = connection.execute(
        select(central_users).where(
            central_users.c.user_uuid == user_uuid
        ).with_for_update()
    ).mappings().one_or_none()
    if not row or not hmac.compare_digest(
        hash_pin(current_pin, bytes(row["pin_salt"])), bytes(row["pin_hash"])
    ):
        _audit(
            connection, "PIN_CHANGE_REJECTED", False,
            actor_uuid=user_uuid, workstation_uuid=workstation_uuid,
        )
        raise AuthenticationError("Current PIN is invalid.")
    salt = secrets.token_bytes(16)
    connection.execute(
        update(central_users).where(
            central_users.c.user_uuid == user_uuid
        ).values(
            pin_salt=salt, pin_hash=hash_pin(new_pin, salt),
            must_change_pin=False, row_version=int(row["row_version"]) + 1,
            failed_attempts=0, locked_until=None, updated_at=_utcnow(),
        )
    )
    connection.execute(
        update(central_sessions).where(
            central_sessions.c.user_uuid == user_uuid,
            central_sessions.c.revoked_at.is_(None),
        ).values(revoked_at=_utcnow())
    )
    _audit(
        connection, "PIN_CHANGED", True, actor_uuid=user_uuid,
        workstation_uuid=workstation_uuid,
    )


def login(
    connection: Connection,
    *,
    username: str,
    pin: str,
    workstation_code: str,
    workstation_secret: str,
    application_type: str,
    session_minutes: int,
) -> tuple[str, RequestIdentity, datetime, bool]:
    now = _utcnow()
    normalized = str(username or "").strip().lower()
    code = str(workstation_code or "").strip().upper()
    application = str(application_type or "").strip().upper()
    station = connection.execute(
        select(workstations).where(workstations.c.workstation_code == code).with_for_update()
    ).mappings().one_or_none()
    station_uuid = station["workstation_uuid"] if station else None
    if (
        not station
        or not station["is_active"]
        or station["application_type"] != application
        or not hmac.compare_digest(
            str(station["credential_hash"]), _secret_hash(workstation_secret)
        )
    ):
        _audit(
            connection, "LOGIN_REJECTED_WORKSTATION", False,
            workstation_uuid=station_uuid, details={"code": code, "application": application},
        )
        raise AuthenticationError("Invalid login credentials.")

    user = connection.execute(
        select(central_users)
        .where(central_users.c.username_normalized == normalized)
        .with_for_update()
    ).mappings().one_or_none()
    if not user or not user["is_active"]:
        _audit(
            connection, "LOGIN_REJECTED_USER", False,
            workstation_uuid=station_uuid, details={"username": normalized},
        )
        raise AuthenticationError("Invalid login credentials.")
    if user["locked_until"] and user["locked_until"] > now:
        _audit(
            connection, "LOGIN_REJECTED_LOCKED", False,
            actor_uuid=user["user_uuid"], workstation_uuid=station_uuid,
        )
        raise AuthenticationError("User is temporarily locked.")

    actual_hash = hash_pin(pin, bytes(user["pin_salt"]))
    if not hmac.compare_digest(actual_hash, bytes(user["pin_hash"])):
        failures = int(user["failed_attempts"]) + 1
        locked_until = now + timedelta(minutes=5) if failures >= MAX_FAILED_ATTEMPTS else None
        connection.execute(
            update(central_users).where(
                central_users.c.user_uuid == user["user_uuid"]
            ).values(failed_attempts=failures, locked_until=locked_until, updated_at=now)
        )
        _audit(
            connection, "LOGIN_REJECTED_PIN", False,
            actor_uuid=user["user_uuid"], workstation_uuid=station_uuid,
            details={"failed_attempts": failures},
        )
        raise AuthenticationError("Invalid login credentials.")

    permission_rows = connection.execute(
        select(central_user_permissions.c.permission).where(
            central_user_permissions.c.user_uuid == user["user_uuid"]
        )
    ).scalars().all()
    permissions = frozenset(str(value) for value in permission_rows)
    required = APPLICATION_PERMISSION[application]
    if required not in permissions:
        _audit(
            connection, "LOGIN_REJECTED_PERMISSION", False,
            actor_uuid=user["user_uuid"], workstation_uuid=station_uuid,
            details={"required_permission": required},
        )
        raise AuthorizationError(f"Missing permission: {required}.")

    token = secrets.token_urlsafe(48)
    expires_at = now + timedelta(minutes=int(session_minutes))
    connection.execute(insert(central_sessions).values(
        session_uuid=uuid4(), user_uuid=user["user_uuid"],
        workstation_uuid=station_uuid, token_hash=_secret_hash(token),
        expires_at=expires_at, last_used_at=now,
    ))
    connection.execute(
        update(central_users).where(
            central_users.c.user_uuid == user["user_uuid"]
        ).values(failed_attempts=0, locked_until=None, last_login_at=now, updated_at=now)
    )
    connection.execute(
        update(workstations).where(
            workstations.c.workstation_uuid == station_uuid
        ).values(last_seen_at=now, updated_at=now)
    )
    _audit(
        connection, "LOGIN_SUCCEEDED", True,
        actor_uuid=user["user_uuid"], workstation_uuid=station_uuid,
    )
    identity = RequestIdentity(
        user_uuid=user["user_uuid"], username=str(user["username"]),
        display_name=str(user["display_name"]), role=str(user["role"]),
        permissions=permissions, workstation_uuid=station_uuid,
        workstation_code=code, application_type=application,
        operator_tax_number=str(user["operator_tax_number"] or ""),
        user_version=int(user["row_version"]),
        signing_public_key=bytes(station["signing_public_key"])
        if station["signing_public_key"] is not None else None,
        must_change_pin=bool(user["must_change_pin"]),
    )
    return token, identity, expires_at, bool(user["must_change_pin"])


def resolve_session(connection: Connection, token: str) -> RequestIdentity:
    now = _utcnow()
    row = connection.execute(
        select(
            central_sessions.c.session_uuid,
            central_sessions.c.expires_at,
            central_sessions.c.revoked_at,
            central_users.c.user_uuid,
            central_users.c.username,
            central_users.c.display_name,
            central_users.c.role,
            central_users.c.operator_tax_number,
            central_users.c.row_version,
            central_users.c.must_change_pin,
            central_users.c.is_active.label("user_active"),
            workstations.c.workstation_uuid,
            workstations.c.workstation_code,
            workstations.c.application_type,
            workstations.c.signing_public_key,
            workstations.c.is_active.label("workstation_active"),
        )
        .join(central_users, central_users.c.user_uuid == central_sessions.c.user_uuid)
        .join(workstations, workstations.c.workstation_uuid == central_sessions.c.workstation_uuid)
        .where(central_sessions.c.token_hash == _secret_hash(token))
    ).mappings().one_or_none()
    if (
        not row or row["revoked_at"] is not None or row["expires_at"] <= now
        or not row["user_active"] or not row["workstation_active"]
    ):
        raise AuthenticationError("Session is invalid or expired.")
    permissions = frozenset(connection.execute(
        select(central_user_permissions.c.permission).where(
            central_user_permissions.c.user_uuid == row["user_uuid"]
        )
    ).scalars().all())
    connection.execute(
        update(central_sessions).where(
            central_sessions.c.session_uuid == row["session_uuid"]
        ).values(last_used_at=now)
    )
    return RequestIdentity(
        user_uuid=row["user_uuid"], username=str(row["username"]),
        display_name=str(row["display_name"]), role=str(row["role"]),
        permissions=permissions, workstation_uuid=row["workstation_uuid"],
        workstation_code=str(row["workstation_code"]),
        application_type=str(row["application_type"]),
        operator_tax_number=str(row["operator_tax_number"] or ""),
        user_version=int(row["row_version"]),
        signing_public_key=bytes(row["signing_public_key"])
        if row["signing_public_key"] is not None else None,
        must_change_pin=bool(row["must_change_pin"]),
    )


def assert_identity_current(
    connection: Connection,
    identity: RequestIdentity,
    *,
    permission: str,
    application_type: str,
) -> None:
    """Recheck mutable access state inside the business transaction."""
    if identity.legacy_development:
        return
    row = connection.execute(
        select(
            central_users.c.is_active.label("user_active"),
            central_users.c.row_version,
            workstations.c.is_active.label("workstation_active"),
            workstations.c.application_type,
        ).select_from(
            central_users.join(
                workstations,
                workstations.c.workstation_uuid == identity.workstation_uuid,
            )
        ).where(
            central_users.c.user_uuid == identity.user_uuid
        )
    ).one_or_none()
    has_permission = connection.execute(
        select(central_user_permissions.c.permission).where(
            central_user_permissions.c.user_uuid == identity.user_uuid,
            central_user_permissions.c.permission == permission,
        )
    ).scalar_one_or_none()
    if (
        not row or not row.user_active or not row.workstation_active
        or int(row.row_version) != identity.user_version
        or row.application_type != application_type
        or has_permission is None
    ):
        raise AuthorizationError("User or workstation access changed; login again.")


def revoke_session(connection: Connection, token: str, identity: RequestIdentity) -> None:
    connection.execute(
        update(central_sessions).where(
            and_(
                central_sessions.c.token_hash == _secret_hash(token),
                central_sessions.c.revoked_at.is_(None),
            )
        ).values(revoked_at=_utcnow())
    )
    _audit(
        connection, "LOGOUT", True, actor_uuid=identity.user_uuid,
        workstation_uuid=identity.workstation_uuid,
    )
