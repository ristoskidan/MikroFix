"""Preverljive SQLite varnostne kopije za MikroFIX."""

from __future__ import annotations

import sqlite3
import os
import uuid
from pathlib import Path


def verify_sqlite_file(path: Path) -> None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Varnostna kopija ne obstaja ali je prazna: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError(f"SQLite preverjanje celovitosti ni uspelo: {path}")


def backup_sqlite_file(source: Path, destination: Path) -> Path:
    """Izvede konsistentno, preverjeno in atomsko SQLite kopijo.

    Ciljna datoteka se zamenja šele po uspešnem preverjanju začasne kopije.
    Prekinitev zato ne more prepisati zadnje veljavne kopije z delno datoteko.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True
    )
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    try:
        verify_sqlite_file(temporary)
        os.replace(temporary, destination)
        verify_sqlite_file(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def verify_restore_roundtrip(backup_path: Path, restored_path: Path) -> Path:
    """Obnovi samo v podano testno datoteko in preveri njeno celovitost."""
    restored_path = Path(restored_path)
    if restored_path.exists():
        raise FileExistsError(
            f"Testna obnovitvena datoteka že obstaja: {restored_path}"
        )
    backup_sqlite_file(Path(backup_path), restored_path)
    return restored_path
