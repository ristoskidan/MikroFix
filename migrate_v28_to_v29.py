"""Enkraten, povrnljiv prenos uporabniških podatkov iz v28 v v29."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SOURCE_DIR = (
    APP_DIR.parent
    / "MikroFIX_Service_v28_MicroDETECT_TEST_2026-08-26"
    / "MikroFixData_TEST"
)
TARGET_DIR = APP_DIR / "MikroFixData_TEST"
MARKER = TARGET_DIR / ".v28_to_v29_complete.json"
DATABASES = (
    Path("db/mikrofix.db"),
    Path("db/mikrofix_auth.db"),
    Path("db/mikrofix_inventory.db"),
    Path("db/mikrofix_office.db"),
)


def _integrity(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError(f"Baza ni prestala pregleda: {path.name}")
    finally:
        connection.close()


def _copy_database(source: Path, destination: Path) -> None:
    """SQLite backup naredi konsistentno kopijo tudi, če je vir nedavno uporabljen."""
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True, timeout=20
    )
    destination_connection = sqlite3.connect(str(destination), timeout=20)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _backup_target() -> Path | None:
    existing = [path for path in DATABASES if (TARGET_DIR / path).exists()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET_DIR / "migration_backup" / stamp
    for relative in existing:
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TARGET_DIR / relative, destination)
    return backup


def migrate() -> bool:
    if MARKER.exists():
        print("Prenos podatkov v28 -> v29 je že zaključen.")
        return False
    if not SOURCE_DIR.exists():
        raise RuntimeError(
            "Ne najdem mape v28. Mapi v28 in v29 morata ostati druga ob drugi."
        )
    missing = [str(path) for path in DATABASES if not (SOURCE_DIR / path).exists()]
    if missing:
        raise RuntimeError("V v28 manjkajo baze: " + ", ".join(missing))

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    backup = _backup_target()
    copied: list[str] = []
    try:
        for relative in DATABASES:
            source = SOURCE_DIR / relative
            destination = TARGET_DIR / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".v29copy")
            if temporary.exists():
                temporary.unlink()
            _copy_database(source, temporary)
            _integrity(temporary)
            os.replace(temporary, destination)
            copied.append(relative.as_posix())

        # Fotografije in že ustvarjeni PDF-ji se samo dopolnijo; ničesar ne brišemo.
        for folder_name in ("service", "office"):
            source_folder = SOURCE_DIR / folder_name
            if source_folder.exists():
                shutil.copytree(
                    source_folder,
                    TARGET_DIR / folder_name,
                    dirs_exist_ok=True,
                    copy_function=shutil.copy2,
                )
    except Exception:
        # Če je prenos prekinjen, vrni vse obstoječe ciljne baze iz varnostne kopije.
        if backup:
            for relative in DATABASES:
                saved = backup / relative
                if saved.exists():
                    destination = TARGET_DIR / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, destination)
        else:
            for relative_text in copied:
                destination = TARGET_DIR / Path(relative_text)
                if destination.exists():
                    destination.unlink()
        raise

    payload = {
        "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(SOURCE_DIR),
        "copied_databases": copied,
        "backup": str(backup) if backup else "",
    }
    temporary_marker = MARKER.with_suffix(".tmp")
    temporary_marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary_marker, MARKER)
    print("Podatki v28 so varno preneseni v v29.")
    if backup:
        print(f"Predhodno stanje v29 je shranjeno v: {backup}")
    return True


if __name__ == "__main__":
    migrate()
