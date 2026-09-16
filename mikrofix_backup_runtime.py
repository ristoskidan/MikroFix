"""Varna shramba obnovitvenega gesla in samodejna zunanja kopija MikroFIX."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mikrofix_v30_backup import (
    BackupError,
    backup_is_older_than,
    create_encrypted_backup,
)


SECRET_FILE_WINDOWS = ".mikrofix_backup_secret.dpapi"
SECRET_FILE_LINUX = ".mikrofix_backup_secret.keyring"
DPAPI_DESCRIPTION = "MikroFIX external backup password"
KEYRING_APPLICATION = "MikroFIX"
KEYRING_PURPOSE = "external-backup"


class BackupRuntimeError(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


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


def _linux_keyring_id(data_dir: Path | str) -> str:
    normalized = str(Path(data_dir).expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _secret_tool() -> str:
    executable = shutil.which("secret-tool")
    if not executable:
        raise BackupRuntimeError(
            "Manjka varna shramba gesel. Namesti paket libsecret-tools in se prijavi v namizno sejo."
        )
    return executable


def _save_linux_secret(data_dir: Path | str, passphrase: str) -> None:
    completed = subprocess.run(
        [
            _secret_tool(), "store", "--label=MikroFIX zunanja varnostna kopija",
            "application", KEYRING_APPLICATION,
            "purpose", KEYRING_PURPOSE,
            "data-dir-hash", _linux_keyring_id(data_dir),
        ],
        input=str(passphrase),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise BackupRuntimeError(
            completed.stderr.strip() or "Gesla ni bilo mogoče shraniti v Linux keyring."
        )


def _load_linux_secret(data_dir: Path | str) -> str:
    completed = subprocess.run(
        [
            _secret_tool(), "lookup",
            "application", KEYRING_APPLICATION,
            "purpose", KEYRING_PURPOSE,
            "data-dir-hash", _linux_keyring_id(data_dir),
        ],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    value = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not value:
        raise BackupRuntimeError(
            completed.stderr.strip() or "Obnovitvenega gesla ni v Linux keyringu."
        )
    return value


def secret_path(data_dir: Path | str) -> Path:
    filename = SECRET_FILE_WINDOWS if _is_windows() else SECRET_FILE_LINUX
    return Path(data_dir).resolve() / filename


def save_backup_passphrase(data_dir: Path | str, passphrase: str) -> Path:
    if len(str(passphrase)) < 12:
        raise BackupRuntimeError("Obnovitveno geslo mora imeti najmanj 12 znakov.")
    destination = secret_path(data_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not _is_windows():
        _save_linux_secret(data_dir, str(passphrase))
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"provider": "Secret Service", "data_dir_hash": _linux_keyring_id(data_dir)},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(_protect_windows(str(passphrase).encode("utf-8")))
    os.replace(temporary, destination)
    return destination


def load_backup_passphrase(data_dir: Path | str) -> str:
    source = secret_path(data_dir)
    if not source.is_file():
        raise BackupRuntimeError("Obnovitveno geslo še ni nastavljeno.")
    if not _is_windows():
        return _load_linux_secret(data_dir)
    try:
        return _unprotect_windows(source.read_bytes()).decode("utf-8")
    except Exception as exc:
        raise BackupRuntimeError(
            "Obnovitvenega gesla ni mogoče odpreti v tem uporabniškem računu."
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
            "Zunanji nosilec in obnovitveno geslo še nista dokončno nastavljena.",
            False,
            target,
        )
    if not rule.get("passphrase_configured") or not secret_path(data_dir).is_file():
        return ExternalBackupStatus(
            "PASSWORD_REQUIRED", "Zunanja kopija čaka na obnovitveno geslo.", False, target
        )
    if target is None:
        return ExternalBackupStatus("TARGET_MISSING", "Cilj zunanje kopije ni nastavljen.", False, None)
    if os.name == "nt":
        target_available = bool(target.anchor and Path(target.anchor).exists())
    else:
        probe = target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        target_available = probe != Path(probe.anchor) and any(
            os.path.ismount(candidate)
            for candidate in (probe, *probe.parents)
            if candidate != Path(candidate.anchor)
        )
    if not target_available:
        return ExternalBackupStatus(
            "DRIVE_MISSING", f"Zunanji nosilec za {target} ni priključen.", True, target
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
    extra_files = [runtime.settings_path]
    release_manifest = runtime.app_dir / "production-release.json"
    if release_manifest.is_file():
        extra_files.append(release_manifest)
    try:
        return create_encrypted_backup(
            data_dir=runtime.data_dir,
            target_dir=status.target,
            passphrase=passphrase,
            app_version=runtime.version_label,
            schema_version=int(runtime.settings.get("schema_version", 1)),
            kind="DAILY",
            extra_files=tuple(extra_files),
            require_distinct_volume=bool(rule.get("require_distinct_volume", True)),
        )
    except BackupError as exc:
        raise BackupRuntimeError(str(exc)) from exc
