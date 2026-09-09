"""Linux gocryptfs mount and Secret Service integration for production data."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


KEYRING_APPLICATION = "MikroFIX"
KEYRING_PURPOSE = "production-data"


class DataEncryptionError(RuntimeError):
    pass


def _secret_tool() -> str:
    executable = shutil.which("secret-tool")
    if not executable:
        raise DataEncryptionError(
            "Manjka Linux Secret Service (secret-tool); šifriranih podatkov ni mogoče odkleniti."
        )
    return executable


def _gocryptfs() -> str:
    executable = shutil.which("gocryptfs")
    if not executable:
        raise DataEncryptionError("Manjka gocryptfs; produkcijskih podatkov ni mogoče odkleniti.")
    return executable


def _keyring_id(cipher_dir: Path | str) -> str:
    value = str(Path(cipher_dir).expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def save_data_passphrase(cipher_dir: Path | str, passphrase: str) -> None:
    if len(str(passphrase)) < 12:
        raise DataEncryptionError("Geslo za podatke mora imeti najmanj 12 znakov.")
    completed = subprocess.run(
        [
            _secret_tool(), "store", "--label=MikroFIX produkcijski podatki",
            "application", KEYRING_APPLICATION,
            "purpose", KEYRING_PURPOSE,
            "cipher-dir-hash", _keyring_id(cipher_dir),
        ],
        input=str(passphrase), text=True, capture_output=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise DataEncryptionError(
            completed.stderr.strip() or "Gesla za podatke ni bilo mogoče shraniti v keyring."
        )


def load_data_passphrase(cipher_dir: Path | str) -> str:
    completed = subprocess.run(
        [
            _secret_tool(), "lookup",
            "application", KEYRING_APPLICATION,
            "purpose", KEYRING_PURPOSE,
            "cipher-dir-hash", _keyring_id(cipher_dir),
        ],
        text=True, capture_output=True, timeout=15, check=False,
    )
    value = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not value:
        raise DataEncryptionError(
            completed.stderr.strip()
            or "Gesla za produkcijske podatke ni v odklenjenem Linux keyringu."
        )
    return value


def _mount_info(mountpoint: Path | str) -> tuple[str, str] | None:
    path = Path(mountpoint).expanduser().resolve()
    completed = subprocess.run(
        ["findmnt", "-T", str(path), "-n", "-o", "TARGET,FSTYPE"],
        text=True, capture_output=True, timeout=10, check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    fields = completed.stdout.strip().split(None, 1)
    if len(fields) != 2:
        return None
    return fields[0], fields[1]


def is_gocryptfs_mount(mountpoint: Path | str) -> bool:
    path = Path(mountpoint).expanduser().resolve()
    info = _mount_info(path)
    if info is None:
        return False
    target, filesystem = info
    return Path(target).resolve() == path and filesystem == "fuse.gocryptfs"


def mount_encrypted_data(
    cipher_dir: Path | str, mountpoint: Path | str, passphrase: str,
) -> None:
    cipher = Path(cipher_dir).expanduser().resolve()
    plain = Path(mountpoint).expanduser().resolve()
    if not (cipher / "gocryptfs.conf").is_file():
        raise DataEncryptionError(f"Šifrirana mapa ni inicializirana: {cipher}")
    plain.mkdir(parents=True, exist_ok=True)
    if is_gocryptfs_mount(plain):
        return
    if any(plain.iterdir()):
        raise DataEncryptionError(
            f"Odklep je ustavljen: nezaščitena priklopna mapa ni prazna ({plain})."
        )
    completed = subprocess.run(
        [_gocryptfs(), "-q", "-passfile", "/dev/stdin", str(cipher), str(plain)],
        input=str(passphrase) + "\n", text=True, capture_output=True,
        timeout=30, check=False,
    )
    if completed.returncode != 0 or not is_gocryptfs_mount(plain):
        raise DataEncryptionError(
            completed.stderr.strip() or "Šifrirane produkcijske mape ni bilo mogoče odkleniti."
        )


def unmount_encrypted_data(mountpoint: Path | str) -> None:
    plain = Path(mountpoint).expanduser().resolve()
    if not is_gocryptfs_mount(plain):
        return
    executable = shutil.which("fusermount3") or shutil.which("fusermount")
    if not executable:
        raise DataEncryptionError("Manjka fusermount; šifrirane mape ni mogoče zakleniti.")
    completed = subprocess.run(
        [executable, "-u", str(plain)], capture_output=True, text=True,
        timeout=30, check=False,
    )
    if completed.returncode != 0 or is_gocryptfs_mount(plain):
        raise DataEncryptionError(
            completed.stderr.strip() or "Produkcijske podatkovne mape ni bilo mogoče zakleniti."
        )


def encryption_rule(settings: dict[str, Any]) -> dict[str, Any]:
    return dict(settings.get("data_encryption") or {})


def require_encrypted_data_ready(settings: dict[str, Any], data_dir: Path | str) -> None:
    rule = encryption_rule(settings)
    if not rule.get("enabled"):
        return
    if os.name == "nt":
        raise DataEncryptionError("gocryptfs šifriranje produkcijske mape je podprto samo v Linuxu.")
    if not is_gocryptfs_mount(data_dir):
        raise DataEncryptionError(
            "Produkcijska podatkovna mapa ni odklenjena. Program zaradi zaščite ne bo ustvaril navadne baze."
        )


def ensure_encrypted_data_mounted(settings: dict[str, Any], data_dir: Path | str) -> bool:
    rule = encryption_rule(settings)
    if not rule.get("enabled"):
        return False
    cipher_dir = str(rule.get("cipher_dir") or "").strip()
    if not cipher_dir:
        raise DataEncryptionError("Manjka pot šifrirane produkcijske mape.")
    mount_encrypted_data(cipher_dir, data_dir, load_data_passphrase(cipher_dir))
    require_encrypted_data_ready(settings, data_dir)
    return True
