"""Skupna prijava, uporabniki in dovoljenja za MikroFIX v30."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


DEFAULT_USERNAME = "daniel"
DEFAULT_DISPLAY_NAME = "Daniel"
DEFAULT_ROLE = "ADMIN"
ALLOWED_ROLES = {"ADMIN", "SERVICE", "OFFICE", "BUYBACK", "CASHIER"}
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 5
MAX_RECOVERY_ATTEMPTS = 5
RECOVERY_LOCK_MINUTES = 30

PERMISSION_OFFICE = "MODULE_OFFICE"
PERMISSION_SERVICE = "MODULE_SERVICE"
PERMISSION_BUYBACK = "MODULE_BUYBACK"
PERMISSION_CASHIER = "MODULE_CASHIER"
PERMISSION_CONFIRM_DOCUMENT = "CONFIRM_DOCUMENT"
PERMISSION_STORNO_DOCUMENT = "STORNO_DOCUMENT"
PERMISSION_MANAGE_PRICES = "MANAGE_PRICES"
PERMISSION_MANAGE_INVENTORY = "MANAGE_INVENTORY"
PERMISSION_FURS_PRODUCTION = "FURS_PRODUCTION"
PERMISSION_BACKUP_RESTORE = "BACKUP_RESTORE"
PERMISSION_MANAGE_USERS = "MANAGE_USERS"

ALL_PERMISSIONS = {
    PERMISSION_OFFICE,
    PERMISSION_SERVICE,
    PERMISSION_BUYBACK,
    PERMISSION_CASHIER,
    PERMISSION_CONFIRM_DOCUMENT,
    PERMISSION_STORNO_DOCUMENT,
    PERMISSION_MANAGE_PRICES,
    PERMISSION_MANAGE_INVENTORY,
    PERMISSION_FURS_PRODUCTION,
    PERMISSION_BACKUP_RESTORE,
    PERMISSION_MANAGE_USERS,
}

ROLE_DEFAULT_PERMISSIONS = {
    "ADMIN": set(ALL_PERMISSIONS),
    "SERVICE": {PERMISSION_SERVICE},
    "OFFICE": {
        PERMISSION_OFFICE,
        PERMISSION_CONFIRM_DOCUMENT,
        PERMISSION_STORNO_DOCUMENT,
        PERMISSION_MANAGE_PRICES,
        PERMISSION_MANAGE_INVENTORY,
    },
    "BUYBACK": {PERMISSION_BUYBACK},
    "CASHIER": {PERMISSION_CASHIER},
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def validate_pin_format(pin: str) -> bool:
    pin = str(pin or "")
    return pin.isdigit() and 6 <= len(pin) <= 14 and not is_trivial_pin(pin)


def is_trivial_pin(pin: str) -> bool:
    """Prepozna očitno predvidljive PIN-e brez beleženja njihove vrednosti."""
    pin = str(pin or "")
    if not pin:
        return True
    if len(set(pin)) == 1:
        return True
    ascending = "01234567890123456789"
    descending = "98765432109876543210"
    if pin in ascending or pin in descending:
        return True
    if len(pin) % 2 == 0 and pin == pin[: len(pin) // 2] * 2:
        return True
    return pin in {"123456", "654321", "121212", "112233", "123123"}


def hash_pin(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        pin.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str = ""
    remaining_attempts: int = MAX_FAILED_ATTEMPTS
    locked_until: str | None = None
    must_change_pin: bool = False


@dataclass(frozen=True)
class RecoveryResult:
    ok: bool
    reason: str = ""
    remaining_attempts: int = MAX_RECOVERY_ATTEMPTS
    locked_until: str | None = None


class AuthStore:
    def __init__(
        self,
        db_path: Path,
        protect_permissions: Callable[[Path], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.protect_permissions = protect_permissions

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def init(self) -> None:
        connection = self.connect()
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'SERVICE',
                salt BLOB,
                pin_hash BLOB,
                is_active INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                must_change_pin INTEGER NOT NULL DEFAULT 0,
                recovery_hint TEXT,
                recovery_salt BLOB,
                recovery_hash BLOB,
                recovery_failed_attempts INTEGER NOT NULL DEFAULT 0,
                recovery_locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
        """)
        user_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "must_change_pin" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN must_change_pin "
                "INTEGER NOT NULL DEFAULT 0"
            )
        recovery_columns = {
            "recovery_hint": "TEXT",
            "recovery_salt": "BLOB",
            "recovery_hash": "BLOB",
            "recovery_failed_attempts": "INTEGER NOT NULL DEFAULT 0",
            "recovery_locked_until": "TEXT",
        }
        for column, declaration in recovery_columns.items():
            if column not in user_columns:
                connection.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {declaration}"
                )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS audit_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                username TEXT,
                event_type TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                details_json TEXT
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(occurred_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_entity "
            "ON audit_events(entity_type, entity_id)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS session_handoffs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                target_context TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY(username) REFERENCES users(username)
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_handoff_expiry "
            "ON session_handoffs(expires_at, consumed_at)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_permissions(
                username TEXT NOT NULL,
                permission TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                granted_by TEXT,
                PRIMARY KEY(username, permission),
                FOREIGN KEY(username) REFERENCES users(username)
            )
        """)

        timestamp = now_text()
        connection.execute("""
            INSERT INTO users(username, display_name, role, created_at, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(username) DO NOTHING
        """, (
            DEFAULT_USERNAME,
            DEFAULT_DISPLAY_NAME,
            DEFAULT_ROLE,
            timestamp,
            timestamp,
        ))

        self._migrate_legacy_pin(connection)
        for user in connection.execute(
            "SELECT username, role FROM users"
        ).fetchall():
            existing = connection.execute(
                "SELECT COUNT(*) FROM user_permissions WHERE username=?",
                (user["username"],),
            ).fetchone()[0]
            if not existing:
                self._replace_permissions(
                    connection,
                    str(user["username"]),
                    ROLE_DEFAULT_PERMISSIONS.get(str(user["role"]), set()),
                    granted_by="system-migration",
                )
        connection.commit()
        connection.close()
        if self.protect_permissions:
            self.protect_permissions(self.db_path)

    @staticmethod
    def _handoff_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def issue_session_handoff(
        self,
        username: str,
        target_context: str,
        *,
        ttl_seconds: int = 60,
    ) -> str:
        """Izda kratkoživo enkratno predajo že potrjene lokalne seje."""
        username = str(username or "").strip().lower()
        target_context = str(target_context or "").strip().upper()
        if not target_context:
            raise ValueError("Cilj predaje seje je obvezen.")
        ttl_seconds = max(5, min(int(ttl_seconds), 300))
        token = secrets.token_urlsafe(32)
        timestamp = datetime.now()
        expires_at = (timestamp + timedelta(seconds=ttl_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        connection = self.connect()
        user = connection.execute(
            "SELECT username FROM users WHERE username=? AND is_active=1",
            (username,),
        ).fetchone()
        if not user:
            connection.close()
            raise ValueError("Prijavljeni uporabnik ni več aktiven.")
        connection.execute(
            "DELETE FROM session_handoffs "
            "WHERE consumed_at IS NOT NULL OR expires_at < ?",
            (now_text(),),
        )
        connection.execute("""
            INSERT INTO session_handoffs(
                token_hash, username, target_context, created_at, expires_at
            ) VALUES(?,?,?,?,?)
        """, (
            self._handoff_hash(token),
            username,
            target_context,
            timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at,
        ))
        self._record(
            connection,
            username,
            "SESSION_HANDOFF_ISSUED",
            "session",
            target_context,
            {"expires_at": expires_at},
        )
        connection.commit()
        connection.close()
        if self.protect_permissions:
            self.protect_permissions(self.db_path)
        return token

    def consume_session_handoff(
        self,
        token: str,
        target_context: str,
    ) -> dict[str, Any] | None:
        """Atomsko porabi predajo; istega žetona ni mogoče uporabiti dvakrat."""
        token = str(token or "").strip()
        target_context = str(target_context or "").strip().upper()
        if not token or not target_context:
            return None

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT h.id, h.username, h.expires_at,
                       u.display_name, u.role, u.is_active
                FROM session_handoffs h
                JOIN users u ON u.username=h.username
                WHERE h.token_hash=? AND h.target_context=?
                  AND h.consumed_at IS NULL
            """, (self._handoff_hash(token), target_context)).fetchone()
            if not row or not bool(row["is_active"]):
                connection.rollback()
                return None

            consumed_at = now_text()
            if str(row["expires_at"]) < consumed_at:
                connection.execute(
                    "UPDATE session_handoffs SET consumed_at=? WHERE id=?",
                    (consumed_at, row["id"]),
                )
                self._record(
                    connection,
                    str(row["username"]),
                    "SESSION_HANDOFF_EXPIRED",
                    "session",
                    target_context,
                    None,
                )
                connection.commit()
                return None

            changed = connection.execute("""
                UPDATE session_handoffs
                SET consumed_at=?
                WHERE id=? AND consumed_at IS NULL
            """, (consumed_at, row["id"]))
            if changed.rowcount != 1:
                connection.rollback()
                return None
            self._record(
                connection,
                str(row["username"]),
                "SESSION_HANDOFF_CONSUMED",
                "session",
                target_context,
                None,
            )
            connection.commit()
            return {
                "username": str(row["username"]),
                "display_name": str(row["display_name"]),
                "role": str(row["role"]),
                "is_active": int(row["is_active"]),
            }
        finally:
            connection.close()

    def _migrate_legacy_pin(self, connection: sqlite3.Connection) -> None:
        legacy_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pin_auth'"
        ).fetchone()
        if not legacy_exists:
            return

        user = connection.execute(
            "SELECT salt, pin_hash FROM users WHERE username=?",
            (DEFAULT_USERNAME,),
        ).fetchone()
        if user and user["pin_hash"]:
            return

        legacy = connection.execute(
            "SELECT salt, pin_hash FROM pin_auth WHERE id=1"
        ).fetchone()
        if not legacy:
            return

        connection.execute("""
            UPDATE users
            SET salt=?, pin_hash=?, updated_at=?
            WHERE username=?
        """, (legacy["salt"], legacy["pin_hash"], now_text(), DEFAULT_USERNAME))
        self._record(
            connection,
            DEFAULT_USERNAME,
            "LEGACY_PIN_MIGRATED",
            "user",
            DEFAULT_USERNAME,
            None,
        )

    def has_pin(self, username: str = DEFAULT_USERNAME) -> bool:
        connection = self.connect()
        row = connection.execute(
            "SELECT pin_hash FROM users WHERE username=? AND is_active=1",
            (username,),
        ).fetchone()
        connection.close()
        return bool(row and row["pin_hash"])

    def list_users(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        connection = self.connect()
        query = """
            SELECT id, username, display_name, role, is_active, must_change_pin,
                   created_at, updated_at, last_login_at
            FROM users
        """
        if active_only:
            query += " WHERE is_active=1"
        query += " ORDER BY LOWER(display_name), id"
        rows = connection.execute(query).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def create_user(
        self,
        username: str,
        display_name: str,
        role: str = "SERVICE",
        *,
        pin: str | None = None,
        permissions: set[str] | None = None,
        created_by: str = DEFAULT_USERNAME,
    ) -> int:
        username = str(username or "").strip().lower()
        display_name = str(display_name or "").strip()
        role = str(role or "").strip().upper()
        if not username or not all(ch.isalnum() or ch in "._-" for ch in username):
            raise ValueError("Uporabniško ime vsebuje nedovoljene znake.")
        if not display_name:
            raise ValueError("Prikazno ime je obvezno.")
        if role not in ALLOWED_ROLES:
            raise ValueError("Neveljavna uporabniška vloga.")
        if pin is not None and not validate_pin_format(pin):
            raise ValueError(
                "PIN mora vsebovati 6 do 14 številk in ne sme biti preprost."
            )
        selected_permissions = (
            ROLE_DEFAULT_PERMISSIONS[role]
            if permissions is None
            else self._validated_permissions(permissions)
        )

        timestamp = now_text()
        salt = secrets.token_bytes(16) if pin is not None else None
        pin_hash_value = hash_pin(pin, salt) if pin is not None else None
        connection = self.connect()
        try:
            cur = connection.execute("""
                INSERT INTO users(
                    username, display_name, role, salt, pin_hash,
                    must_change_pin, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (
                username, display_name, role, salt, pin_hash_value,
                1 if pin is not None else 0, timestamp, timestamp,
            ))
            user_id = int(cur.lastrowid)
            self._replace_permissions(
                connection,
                username,
                selected_permissions,
                granted_by=created_by,
            )
            self._record(
                connection,
                created_by,
                "USER_CREATED",
                "user",
                username,
                {"display_name": display_name, "role": role},
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            connection.close()
            raise ValueError("Uporabniško ime že obstaja.") from exc
        connection.close()
        if self.protect_permissions:
            self.protect_permissions(self.db_path)
        return user_id

    def set_user_active(
        self,
        username: str,
        active: bool,
        *,
        changed_by: str = DEFAULT_USERNAME,
    ) -> None:
        username = str(username or "").strip().lower()
        if username == DEFAULT_USERNAME and not active:
            raise ValueError("Začetnega skrbnika Daniel ni mogoče izključiti.")
        connection = self.connect()
        result = connection.execute("""
            UPDATE users SET is_active=?, updated_at=? WHERE username=?
        """, (1 if active else 0, now_text(), username))
        if result.rowcount != 1:
            connection.close()
            raise ValueError("Uporabnik ne obstaja.")
        self._record(
            connection,
            changed_by,
            "USER_ACTIVATED" if active else "USER_DEACTIVATED",
            "user",
            username,
            None,
        )
        connection.commit()
        connection.close()

    def set_pin(
        self,
        pin: str,
        username: str = DEFAULT_USERNAME,
        *,
        event_type: str = "PIN_SET",
        changed_by: str | None = None,
        temporary: bool = False,
    ) -> None:
        if not validate_pin_format(pin):
            raise ValueError(
                "PIN mora vsebovati 6 do 14 številk in ne sme biti preprost."
            )

        salt = secrets.token_bytes(16)
        pin_hash_value = hash_pin(pin, salt)
        connection = self.connect()
        result = connection.execute("""
            UPDATE users
            SET salt=?, pin_hash=?, failed_attempts=0, locked_until=NULL,
                must_change_pin=?, updated_at=?
            WHERE username=? AND is_active=1
        """, (salt, pin_hash_value, 1 if temporary else 0, now_text(), username))
        if result.rowcount != 1:
            connection.close()
            raise ValueError("Uporabnik ne obstaja ali ni aktiven.")
        self._record(
            connection,
            changed_by or username,
            event_type,
            "user",
            username,
            None,
        )
        connection.commit()
        connection.close()
        if self.protect_permissions:
            self.protect_permissions(self.db_path)

    def has_recovery(self, username: str = DEFAULT_USERNAME) -> bool:
        connection = self.connect()
        row = connection.execute(
            "SELECT recovery_hash FROM users WHERE username=? AND is_active=1",
            (str(username or "").strip().lower(),),
        ).fetchone()
        connection.close()
        return bool(row and row["recovery_hash"])

    def recovery_hint(self, username: str = DEFAULT_USERNAME) -> str:
        connection = self.connect()
        row = connection.execute(
            "SELECT recovery_hint FROM users WHERE username=? AND is_active=1",
            (str(username or "").strip().lower(),),
        ).fetchone()
        connection.close()
        return str(row["recovery_hint"] or "") if row else ""

    def set_recovery(
        self,
        hint: str,
        password: str,
        username: str = DEFAULT_USERNAME,
        *,
        changed_by: str | None = None,
    ) -> None:
        username = str(username or "").strip().lower()
        hint = str(hint or "").strip()
        password = str(password or "")
        if username != DEFAULT_USERNAME:
            raise ValueError("Obnovitveno geslo je dovoljeno samo skrbniku Daniel.")
        if not hint:
            raise ValueError("Obnovitveni namig je obvezen.")
        if len(password) < 8:
            raise ValueError("Obnovitveno geslo mora imeti najmanj 8 znakov.")
        if password.casefold() in hint.casefold():
            raise ValueError("Namig ne sme vsebovati obnovitvenega gesla.")
        salt = secrets.token_bytes(16)
        secret_hash = hash_pin(password, salt)
        connection = self.connect()
        result = connection.execute("""
            UPDATE users
            SET recovery_hint=?, recovery_salt=?, recovery_hash=?,
                recovery_failed_attempts=0, recovery_locked_until=NULL,
                updated_at=?
            WHERE username=? AND is_active=1 AND role='ADMIN'
        """, (hint, salt, secret_hash, now_text(), username))
        if result.rowcount != 1:
            connection.close()
            raise ValueError("Skrbniški uporabnik ne obstaja ali ni aktiven.")
        self._record(
            connection,
            changed_by or username,
            "RECOVERY_SECRET_SET",
            "user",
            username,
            {"hint_changed": True},
        )
        connection.commit()
        connection.close()
        if self.protect_permissions:
            self.protect_permissions(self.db_path)

    def verify_recovery(
        self,
        password: str,
        username: str = DEFAULT_USERNAME,
    ) -> RecoveryResult:
        username = str(username or "").strip().lower()
        connection = self.connect()
        row = connection.execute(
            "SELECT * FROM users WHERE username=? AND is_active=1 AND role='ADMIN'",
            (username,),
        ).fetchone()
        if not row or not row["recovery_hash"]:
            connection.close()
            return RecoveryResult(False, "NOT_CONFIGURED", 0)

        locked_until = row["recovery_locked_until"]
        if locked_until:
            try:
                locked_dt = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                locked_dt = datetime.min
            if locked_dt > datetime.now():
                connection.close()
                return RecoveryResult(False, "LOCKED", 0, locked_until)

        candidate = hash_pin(str(password or ""), row["recovery_salt"])
        if hmac.compare_digest(candidate, row["recovery_hash"]):
            connection.execute("""
                UPDATE users
                SET recovery_failed_attempts=0, recovery_locked_until=NULL,
                    updated_at=?
                WHERE username=?
            """, (now_text(), username))
            self._record(
                connection,
                username,
                "RECOVERY_SECRET_ACCEPTED",
                "user",
                username,
                None,
            )
            connection.commit()
            connection.close()
            return RecoveryResult(True)

        attempts = int(row["recovery_failed_attempts"] or 0) + 1
        new_locked_until = None
        if attempts >= MAX_RECOVERY_ATTEMPTS:
            new_locked_until = (
                datetime.now() + timedelta(minutes=RECOVERY_LOCK_MINUTES)
            ).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("""
            UPDATE users
            SET recovery_failed_attempts=?, recovery_locked_until=?, updated_at=?
            WHERE username=?
        """, (attempts, new_locked_until, now_text(), username))
        self._record(
            connection,
            username,
            "RECOVERY_SECRET_REJECTED",
            "user",
            username,
            {"failed_attempts": attempts},
        )
        connection.commit()
        connection.close()
        remaining = max(0, MAX_RECOVERY_ATTEMPTS - attempts)
        return RecoveryResult(
            False,
            "LOCKED" if new_locked_until else "WRONG_SECRET",
            remaining,
            new_locked_until,
        )

    def verify_pin(
        self,
        pin: str,
        username: str = DEFAULT_USERNAME,
    ) -> LoginResult:
        if not validate_pin_format(pin):
            return LoginResult(False, "INVALID_FORMAT", 0)

        connection = self.connect()
        row = connection.execute(
            "SELECT * FROM users WHERE username=? AND is_active=1",
            (username,),
        ).fetchone()
        if not row or not row["pin_hash"]:
            connection.close()
            return LoginResult(False, "NO_PIN", 0)

        locked_until = row["locked_until"]
        if locked_until:
            try:
                locked_dt = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                locked_dt = datetime.min
            if locked_dt > datetime.now():
                connection.close()
                return LoginResult(False, "LOCKED", 0, locked_until)

        candidate = hash_pin(pin, row["salt"])
        if hmac.compare_digest(candidate, row["pin_hash"]):
            timestamp = now_text()
            connection.execute("""
                UPDATE users
                SET failed_attempts=0, locked_until=NULL, last_login_at=?, updated_at=?
                WHERE username=?
            """, (timestamp, timestamp, username))
            self._record(connection, username, "LOGIN_SUCCESS", "user", username, None)
            connection.commit()
            connection.close()
            return LoginResult(True, must_change_pin=bool(row["must_change_pin"]))

        failed_attempts = int(row["failed_attempts"] or 0) + 1
        new_locked_until = None
        if failed_attempts >= MAX_FAILED_ATTEMPTS:
            new_locked_until = (
                datetime.now() + timedelta(minutes=LOCK_MINUTES)
            ).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("""
            UPDATE users
            SET failed_attempts=?, locked_until=?, updated_at=?
            WHERE username=?
        """, (failed_attempts, new_locked_until, now_text(), username))
        self._record(
            connection,
            username,
            "LOGIN_FAILED",
            "user",
            username,
            {"failed_attempts": failed_attempts},
        )
        connection.commit()
        connection.close()
        remaining = max(0, MAX_FAILED_ATTEMPTS - failed_attempts)
        reason = "LOCKED" if new_locked_until else "WRONG_PIN"
        return LoginResult(False, reason, remaining, new_locked_until)

    @staticmethod
    def _validated_permissions(permissions: set[str] | list[str]) -> set[str]:
        selected = {str(permission).strip().upper() for permission in permissions}
        unknown = selected - ALL_PERMISSIONS
        if unknown:
            raise ValueError(
                "Neveljavna dovoljenja: " + ", ".join(sorted(unknown))
            )
        return selected

    @staticmethod
    def _replace_permissions(
        connection: sqlite3.Connection,
        username: str,
        permissions: set[str],
        *,
        granted_by: str,
    ) -> None:
        connection.execute(
            "DELETE FROM user_permissions WHERE username=?", (username,)
        )
        timestamp = now_text()
        connection.executemany(
            "INSERT INTO user_permissions(username, permission, granted_at, granted_by) "
            "VALUES(?,?,?,?)",
            [
                (username, permission, timestamp, granted_by)
                for permission in sorted(permissions)
            ],
        )

    def permissions_for(self, username: str) -> set[str]:
        connection = self.connect()
        rows = connection.execute(
            "SELECT permission FROM user_permissions WHERE username=?",
            (str(username or "").strip().lower(),),
        ).fetchall()
        connection.close()
        return {str(row["permission"]) for row in rows}

    def has_permission(self, username: str, permission: str) -> bool:
        permission = str(permission or "").strip().upper()
        if permission not in ALL_PERMISSIONS:
            return False
        connection = self.connect()
        row = connection.execute("""
            SELECT 1
            FROM users u
            JOIN user_permissions p ON p.username=u.username
            WHERE u.username=? AND u.is_active=1 AND p.permission=?
        """, (str(username or "").strip().lower(), permission)).fetchone()
        connection.close()
        return bool(row)

    def set_permissions(
        self,
        username: str,
        permissions: set[str] | list[str],
        *,
        changed_by: str = DEFAULT_USERNAME,
    ) -> None:
        username = str(username or "").strip().lower()
        selected = self._validated_permissions(permissions)
        if username == DEFAULT_USERNAME and selected != ALL_PERMISSIONS:
            raise ValueError("Začetnemu skrbniku Daniel ni mogoče odvzeti pravic.")
        connection = self.connect()
        user = connection.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone()
        if not user:
            connection.close()
            raise ValueError("Uporabnik ne obstaja.")
        self._replace_permissions(
            connection, username, selected, granted_by=changed_by
        )
        self._record(
            connection,
            changed_by,
            "USER_PERMISSIONS_CHANGED",
            "user",
            username,
            {"permissions": sorted(selected)},
        )
        connection.commit()
        connection.close()

    def record_event(
        self,
        event_type: str,
        *,
        username: str = DEFAULT_USERNAME,
        entity_type: str | None = None,
        entity_id: Any = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection = self.connect()
        self._record(
            connection,
            username,
            event_type,
            entity_type,
            entity_id,
            details,
        )
        connection.commit()
        connection.close()

    @staticmethod
    def _record(
        connection: sqlite3.Connection,
        username: str | None,
        event_type: str,
        entity_type: str | None,
        entity_id: Any,
        details: dict[str, Any] | None,
    ) -> None:
        connection.execute("""
            INSERT INTO audit_events(
                occurred_at, username, event_type,
                entity_type, entity_id, details_json
            ) VALUES(?,?,?,?,?,?)
        """, (
            now_text(),
            username,
            event_type,
            entity_type,
            None if entity_id is None else str(entity_id),
            None if details is None else json.dumps(
                details, ensure_ascii=False, sort_keys=True
            ),
        ))
