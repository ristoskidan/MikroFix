"""Verified one-time conversion of the known MikroFIX Kingston stick to ext4."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SOURCE = Path.home() / "KINGSTON_PRE_FORMAT_2026-09-04"
MANIFEST = Path.home() / "KINGSTON_PRE_FORMAT_2026-09-04.sha256"
TARGET = Path("/media/mikrofix/KINGSTON")
HELPER = APP_DIR / "tools/format-kingston-ext4-root"


def hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def backup_payload_hashes(source: Path) -> dict[str, str]:
    """Select the only payload that belongs on the dedicated backup drive."""
    backup_source = source / "MikroFix_Backup"
    if not backup_source.is_dir():
        raise RuntimeError("V predformatni kopiji manjka mapa MikroFix_Backup.")
    selected = hashes(backup_source)
    if not selected:
        raise RuntimeError("Mapa MikroFix_Backup je prazna; formatiranje je ustavljeno.")
    return selected


def main() -> int:
    if not SOURCE.is_dir() or not MANIFEST.is_file():
        raise RuntimeError("Preverjena predformatna kopija manjka; formatiranje je ustavljeno.")
    source_hashes = hashes(SOURCE)
    manifest_hashes: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        manifest_hashes[name.removeprefix("./")] = digest
    if source_hashes != manifest_hashes:
        raise RuntimeError("Kontrolne vsote predformatne kopije se ne ujemajo.")
    backup_source = SOURCE / "MikroFix_Backup"
    backup_hashes = backup_payload_hashes(SOURCE)

    completed = subprocess.run(
        ["pkexec", str(HELPER)], text=True, capture_output=True, timeout=180, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Formatiranje ni uspelo.")
    if not TARGET.is_dir():
        raise RuntimeError("Ext4 Kingston po formatiranju ni priklopljen.")

    backup_dir = TARGET / "MikroFix_Backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--delete", str(backup_source) + "/", str(backup_dir) + "/"],
        timeout=180, check=True,
    )
    os.sync()
    if hashes(backup_dir) != backup_hashes:
        raise RuntimeError("Povrnjeni backup nima enakih kontrolnih vsot.")
    backup_dir.chmod(0o700)
    for package in backup_dir.glob("*.mfbak"):
        package.chmod(0o600)
    filesystem = subprocess.run(
        ["findmnt", "-T", str(TARGET), "-n", "-o", "FSTYPE"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()
    if filesystem != "ext4":
        raise RuntimeError(f"Pričakovan ext4, dobljen {filesystem or 'neznan datotečni sistem'}.")
    print("KINGSTON JE EXT4; POVRNJENA IN PREVERJENA JE SAMO MAPA MikroFix_Backup.")
    print(f"Celoten stari arhiv ostaja na lokalnem disku: {SOURCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
