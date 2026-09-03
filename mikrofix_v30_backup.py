"""Šifrirane in preverljive varnostne kopije celotnega MikroFIX v30."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from mikrofix_backup import backup_sqlite_file, verify_sqlite_file


MAGIC = b"MFBAK30\x00"
FORMAT_VERSION = 1
PBKDF2_ITERATIONS = 600_000
DAILY_PREFIX = "mikrofix_daily_"
RESCUE_PREFIX = "mikrofix_rescue_"


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupInspection:
    path: Path
    created_at: str
    app_version: str
    schema_version: int
    kind: str
    file_count: int
    plain_size: int


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if len(passphrase) < 12:
        raise BackupError("Obnovitveno geslo mora imeti najmanj 12 znakov.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_volume(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    if os.name == "nt":
        return first.drive.casefold() == second.drive.casefold()
    try:
        return first.stat().st_dev == second.stat().st_dev
    except FileNotFoundError:
        return first.anchor == second.anchor


def validate_target(
    source_dir: Path | str, target_dir: Path | str, *, require_distinct_volume: bool = True
) -> Path:
    source = Path(source_dir).resolve()
    target = Path(target_dir).resolve()
    if not source.is_dir():
        raise BackupError(f"Izvorna podatkovna mapa ne obstaja: {source}")
    target.mkdir(parents=True, exist_ok=True)
    if require_distinct_volume and _same_volume(source, target):
        raise BackupError(
            "Cilj varnostne kopije mora biti na drugem fizičnem nosilcu ali "
            "neodvisni lokaciji, ne v drugem imeniku istega diska."
        )
    probe = target / f".mikrofix_write_test_{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"Na cilj varnostne kopije ni mogoče pisati: {target}") from exc
    return target


def _stage_sources(
    data_dir: Path, staging: Path, extra_files: Sequence[Path | str]
) -> list[dict]:
    entries: list[dict] = []
    source_files = [p for p in data_dir.rglob("*") if p.is_file()]
    for source in source_files:
        if source.name.endswith(("-wal", "-shm", "-journal")):
            continue
        relative = Path("data") / source.relative_to(data_dir)
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".db":
            backup_sqlite_file(source, destination)
        else:
            destination.write_bytes(source.read_bytes())
        entries.append({
            "path": relative.as_posix(), "size": destination.stat().st_size,
            "sha256": _sha256(destination), "sqlite": source.suffix.lower() == ".db",
        })
    used_names: set[str] = set()
    for raw in extra_files:
        source = Path(raw).resolve()
        if not source.is_file():
            continue
        name = source.name
        if name in used_names:
            name = f"{source.stem}_{hashlib.sha256(str(source).encode()).hexdigest()[:8]}{source.suffix}"
        used_names.add(name)
        relative = Path("config") / name
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        entries.append({
            "path": relative.as_posix(), "size": destination.stat().st_size,
            "sha256": _sha256(destination), "sqlite": False,
        })
    return entries


def _build_zip(
    *, data_dir: Path, extra_files: Sequence[Path | str], app_version: str,
    schema_version: int, kind: str, zip_path: Path,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="mikrofix_backup_stage_") as raw_stage:
        stage = Path(raw_stage)
        entries = _stage_sources(data_dir, stage, extra_files)
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": app_version,
            "schema_version": int(schema_version),
            "kind": kind,
            "files": entries,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file_path in sorted(p for p in stage.rglob("*") if p.is_file()):
                archive.write(file_path, file_path.relative_to(stage).as_posix())
        return manifest


def _encrypt_zip(zip_path: Path, destination: Path, passphrase: str) -> None:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        "format": FORMAT_VERSION,
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "cipher": "AES-256-GCM",
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    key = _derive_key(passphrase, salt, PBKDF2_ITERATIONS)
    encrypted = AESGCM(key).encrypt(nonce, zip_path.read_bytes(), MAGIC + header_bytes)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(MAGIC)
        handle.write(struct.pack(">I", len(header_bytes)))
        handle.write(header_bytes)
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def _decrypt_to_zip(package: Path, passphrase: str, zip_path: Path) -> None:
    raw = package.read_bytes()
    if len(raw) < len(MAGIC) + 4 or raw[: len(MAGIC)] != MAGIC:
        raise BackupError("Datoteka ni veljaven paket MikroFIX v30.")
    header_size = struct.unpack(">I", raw[len(MAGIC): len(MAGIC) + 4])[0]
    start = len(MAGIC) + 4
    header_bytes = raw[start: start + header_size]
    try:
        header = json.loads(header_bytes.decode("utf-8"))
        salt = bytes.fromhex(header["salt"])
        nonce = bytes.fromhex(header["nonce"])
        iterations = int(header["iterations"])
    except Exception as exc:
        raise BackupError("Glava varnostne kopije je poškodovana.") from exc
    if iterations < PBKDF2_ITERATIONS:
        raise BackupError("Varnostna kopija uporablja nedovoljeno šibko izpeljavo ključa.")
    key = _derive_key(passphrase, salt, iterations)
    try:
        plain = AESGCM(key).decrypt(
            nonce, raw[start + header_size:], MAGIC + header_bytes
        )
    except Exception as exc:
        raise BackupError("Napačno obnovitveno geslo ali poškodovana kopija.") from exc
    zip_path.write_bytes(plain)


def _verify_zip(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise BackupError(f"Poškodovana datoteka v paketu: {bad}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            names = set(archive.namelist())
            for entry in manifest.get("files", []):
                name = entry["path"]
                if name not in names:
                    raise BackupError(f"V paketu manjka datoteka: {name}")
                content = archive.read(name)
                if len(content) != int(entry["size"]):
                    raise BackupError(f"Napačna velikost datoteke: {name}")
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise BackupError(f"Napačen kontrolni povzetek datoteke: {name}")
            return manifest
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError("Notranja vsebina varnostne kopije je poškodovana.") from exc


def inspect_backup(package: Path | str, passphrase: str) -> BackupInspection:
    package = Path(package).resolve()
    if not package.is_file():
        raise BackupError("Izbrana varnostna kopija ne obstaja.")
    with tempfile.TemporaryDirectory(prefix="mikrofix_backup_verify_") as raw_temp:
        zip_path = Path(raw_temp) / "payload.zip"
        _decrypt_to_zip(package, passphrase, zip_path)
        manifest = _verify_zip(zip_path)
        return BackupInspection(
            path=package, created_at=str(manifest["created_at"]),
            app_version=str(manifest["app_version"]),
            schema_version=int(manifest["schema_version"]), kind=str(manifest["kind"]),
            file_count=len(manifest.get("files", [])),
            plain_size=sum(int(item["size"]) for item in manifest.get("files", [])),
        )


def create_encrypted_backup(
    *, data_dir: Path | str, target_dir: Path | str, passphrase: str,
    app_version: str, schema_version: int, kind: str = "DAILY",
    extra_files: Sequence[Path | str] = (), require_distinct_volume: bool = True,
) -> Path:
    data = Path(data_dir).resolve()
    target = validate_target(data, target_dir, require_distinct_volume=require_distinct_volume)
    normalized_kind = str(kind).strip().upper()
    if normalized_kind not in {"DAILY", "RESCUE"}:
        raise BackupError("Vrsta kopije mora biti DAILY ali RESCUE.")
    if normalized_kind == "DAILY":
        existing = sorted(target.glob(f"{DAILY_PREFIX}{date.today().isoformat()}_*.mfbak"))
        if existing:
            inspect_backup(existing[-1], passphrase)
            return existing[-1]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    prefix = DAILY_PREFIX if normalized_kind == "DAILY" else RESCUE_PREFIX
    destination = target / f"{prefix}{stamp}.mfbak"
    with tempfile.TemporaryDirectory(prefix="mikrofix_backup_build_") as raw_temp:
        zip_path = Path(raw_temp) / "payload.zip"
        _build_zip(
            data_dir=data, extra_files=extra_files, app_version=app_version,
            schema_version=schema_version, kind=normalized_kind, zip_path=zip_path,
        )
        _encrypt_zip(zip_path, destination, passphrase)
    inspect_backup(destination, passphrase)
    if normalized_kind == "DAILY":
        rotate_daily_backups(target, keep=30)
    return destination


def rotate_daily_backups(target_dir: Path | str, *, keep: int = 30) -> list[Path]:
    if keep < 1:
        raise BackupError("Ohraniti je treba najmanj eno dnevno kopijo.")
    target = Path(target_dir).resolve()
    backups = sorted(target.glob(f"{DAILY_PREFIX}*.mfbak"), key=lambda p: p.name, reverse=True)
    removed: list[Path] = []
    for old in backups[keep:]:
        old.unlink()
        removed.append(old)
    return removed


def latest_daily_backup(target_dir: Path | str) -> Path | None:
    target = Path(target_dir)
    values = sorted(target.glob(f"{DAILY_PREFIX}*.mfbak"), key=lambda p: p.name)
    return values[-1] if values else None


def backup_is_older_than(target_dir: Path | str, hours: int = 24) -> bool:
    latest = latest_daily_backup(target_dir)
    if latest is None:
        return True
    modified = datetime.fromtimestamp(latest.stat().st_mtime)
    return datetime.now() - modified > timedelta(hours=hours)


def extract_verified_backup(
    package: Path | str, passphrase: str, destination: Path | str,
    *, expected_schema_version: int | None = None,
) -> BackupInspection:
    """Izvleče preverjen paket samo v prazno ciljno mapo; ne menja žive zbirke."""
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("Ciljna obnovitvena mapa mora biti prazna.")
    destination.mkdir(parents=True, exist_ok=True)
    package = Path(package).resolve()
    with tempfile.TemporaryDirectory(prefix="mikrofix_backup_restore_") as raw_temp:
        zip_path = Path(raw_temp) / "payload.zip"
        _decrypt_to_zip(package, passphrase, zip_path)
        manifest = _verify_zip(zip_path)
        if expected_schema_version is not None and int(manifest["schema_version"]) != int(expected_schema_version):
            raise BackupError("Različica podatkovne sheme ni združljiva z obnovo.")
        with zipfile.ZipFile(zip_path, "r") as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if destination not in target.parents and target != destination:
                    raise BackupError("Paket vsebuje nevarno pot in ga ni dovoljeno obnoviti.")
            archive.extractall(destination)
    for db_path in (destination / "data").rglob("*.db"):
        verify_sqlite_file(db_path)
    return BackupInspection(
        path=package, created_at=str(manifest["created_at"]),
        app_version=str(manifest["app_version"]),
        schema_version=int(manifest["schema_version"]), kind=str(manifest["kind"]),
        file_count=len(manifest.get("files", [])),
        plain_size=sum(int(item["size"]) for item in manifest.get("files", [])),
    )
