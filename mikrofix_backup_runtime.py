"""Windows shramba obnovitvenega gesla in samodejna zunanja kopija MikroFIX."""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mikrofix_v30_backup import (
    BackupError,
    backup_is_older_than,
    create_encrypted_backup,
)


SECRET_FILE = ".mikrofix_backup_secret.dpapi"
DPAPI_DESCRIPTION = "MikroFIX external backup password"


class BackupRuntimeError(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    return (
        DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def _protect_windows(data: bytes) -> bytes:
    if os.name != "nt":
        raise BackupRuntimeError("Varna shramba gesla je trenutno podprta samo v Windows.")
    input_blob, input_buffer = _blob(data)
    output_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob), ctypes.c_wchar_p(DPAPI_DESCRIPTION), None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def _unprotect_windows(data: bytes) -> bytes:
    if os.name != "nt":
        raise BackupRuntimeError("Varna shramba gesla je trenutno podprta samo v Windows.")
    input_blob, input_buffer = _blob(data)
    output_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def secret_path(data_dir: Path | str) -> Path:
    return Path(data_dir).resolve() / SECRET_FILE


def save_backup_passphrase(data_dir: Path | str, passphrase: str) -> Path:
    if len(str(passphrase)) < 12:
        raise BackupRuntimeError("Obnovitveno geslo mora imeti najmanj 12 znakov.")
    destination = secret_path(data_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(_protect_windows(str(passphrase).encode("utf-8")))
    os.replace(temporary, destination)
    return destination


def load_backup_passphrase(data_dir: Path | str) -> str:
    source = secret_path(data_dir)
    if not source.is_file():
        raise BackupRuntimeError("Obnovitveno geslo še ni nastavljeno.")
    try:
        return _unprotect_windows(source.read_bytes()).decode("utf-8")
    except Exception as exc:
        raise BackupRuntimeError(
            "Obnovitvenega gesla ni mogoče odpreti v tem Windows uporabniškem računu."
        ) from exc


def update_backup_settings(
    settings_path: Path | str,
    *,
    target_dir: str,
    enabled: bool,
    passphrase_configured: bool,
) -> None:
    path = Path(settings_path)
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    backup = raw.setdefault("backup", {})
    backup.update(
        {
            "enabled": bool(enabled),
            "target_dir": str(target_dir),
            "retention_days": 30,
            "max_age_hours": 24,
            "require_distinct_volume": True,
            "passphrase_configured": bool(passphrase_configured),
        }
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


@dataclass(frozen=True)
class ExternalBackupStatus:
    code: str
    message: str
    due: bool
    target: Path | None


def external_backup_status(settings: dict[str, Any], data_dir: Path | str) -> ExternalBackupStatus:
    rule = dict(settings.get("backup") or {})
    raw_target = str(rule.get("target_dir") or "").strip()
    target = Path(raw_target) if raw_target else None
    if not rule.get("enabled"):
        return ExternalBackupStatus(
            "SETUP_REQUIRED",
            "Kingston E: je izbran, obnovitveno geslo pa še ni nastavljeno.",
            False,
            target,
        )
    if not rule.get("passphrase_configured") or not secret_path(data_dir).is_file():
        return ExternalBackupStatus(
            "PASSWORD_REQUIRED", "Zunanja kopija čaka na obnovitveno geslo.", False, target
        )
    if target is None:
        return ExternalBackupStatus("TARGET_MISSING", "Cilj zunanje kopije ni nastavljen.", False, None)
    if not target.anchor or not Path(target.anchor).exists():
        return ExternalBackupStatus(
            "DRIVE_MISSING", f"Zunanji nosilec {target.anchor or target} ni priključen.", True, target
        )
    due = backup_is_older_than(target, int(rule.get("max_age_hours", 24)))
    return ExternalBackupStatus(
        "DUE" if due else "OK",
        "Zunanja kopija je potrebna." if due else "Zunanja kopija je veljavna in mlajša od 24 ur.",
        due,
        target,
    )


def create_due_external_backup(runtime, *, force: bool = False) -> Path | None:
    status = external_backup_status(runtime.settings, runtime.data_dir)
    if status.code not in {"DUE", "OK"}:
        raise BackupRuntimeError(status.message)
    if not force and not status.due:
        return None
    passphrase = load_backup_passphrase(runtime.data_dir)
    rule = runtime.settings["backup"]
    try:
        return create_encrypted_backup(
            data_dir=runtime.data_dir,
            target_dir=status.target,
            passphrase=passphrase,
            app_version=runtime.version_label,
            schema_version=int(runtime.settings.get("schema_version", 1)),
            kind="DAILY",
            extra_files=(runtime.settings_path,),
            require_distinct_volume=bool(rule.get("require_distinct_volume", True)),
        )
    except BackupError as exc:
        raise BackupRuntimeError(str(exc)) from exc
