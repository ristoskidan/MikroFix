"""One-time graphical setup for encrypted production data and daily backups."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from mikrofix_backup_runtime import save_backup_passphrase
from mikrofix_data_encryption import (
    DataEncryptionError,
    ensure_encrypted_data_mounted,
    mount_encrypted_data,
    save_data_passphrase,
    unmount_encrypted_data,
)
from mikrofix_v30_backup import create_encrypted_backup


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "mikrofix_settings.json"
DEFAULT_CIPHER_DIR = Path.home() / ".local/share/mikrofix/data-v30.encrypted"
DEFAULT_BACKUP_DIR = Path("/media/mikrofix/KINGSTON/MikroFix_Backup")


class SetupError(RuntimeError):
    pass


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    path.chmod(0o600)


def _init_cipher(cipher_dir: Path, passphrase: str) -> None:
    cipher_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    completed = subprocess.run(
        ["gocryptfs", "-q", "-init", "-passfile", "/dev/stdin", str(cipher_dir)],
        input=passphrase + "\n", text=True, capture_output=True, timeout=30, check=False,
    )
    if completed.returncode != 0 or not (cipher_dir / "gocryptfs.conf").is_file():
        raise SetupError(completed.stderr.strip() or "Inicializacija gocryptfs ni uspela.")


def _copy_tree(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        if item.is_symlink():
            raise SetupError(f"Selitev je ustavljena zaradi simbolne povezave: {item}")
    shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=shutil.copy2)


def _file_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _verify_databases(root: Path) -> None:
    databases = sorted((root / "db").glob("*.db"))
    if not databases:
        raise SetupError("Produkcijska mapa ne vsebuje nobene baze.")
    for database in databases:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            status = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if status != "ok":
            raise SetupError(f"Preverjanje baze {database.name} ni uspelo: {status}")


def configure(passphrase: str) -> tuple[Path, Path]:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    original_settings = json.loads(json.dumps(settings))
    if settings.get("environment") != "PRODUCTION":
        raise SetupError("Zaščita se sme vključiti samo nad produkcijsko konfiguracijo.")
    if (settings.get("data_encryption") or {}).get("enabled"):
        raise SetupError("Šifriranje produkcijskih podatkov je že vključeno.")

    data_dir = Path(settings["database"]["data_dir"]).expanduser().resolve()
    cipher_dir = DEFAULT_CIPHER_DIR.resolve()
    backup_dir = DEFAULT_BACKUP_DIR.resolve()
    if not data_dir.is_dir():
        raise SetupError(f"Produkcijska mapa ne obstaja: {data_dir}")
    if cipher_dir.exists():
        raise SetupError(f"Ciljna šifrirana mapa že obstaja: {cipher_dir}")
    if not backup_dir.parent.is_dir() or not os.path.ismount(backup_dir.parent):
        raise SetupError("Zunanji nosilec KINGSTON ni priključen.")

    _verify_databases(data_dir)
    original_hashes = _file_hashes(data_dir)
    if not original_hashes:
        raise SetupError("Produkcijska mapa je prazna.")

    cipher_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _init_cipher(cipher_dir, passphrase)
    save_data_passphrase(cipher_dir, passphrase)

    with tempfile.TemporaryDirectory(prefix="mikrofix-encryption-setup-") as raw_mount:
        staging_mount = Path(raw_mount)
        mount_encrypted_data(cipher_dir, staging_mount, passphrase)
        try:
            _copy_tree(data_dir, staging_mount)
            _verify_databases(staging_mount)
            if _file_hashes(staging_mount) != original_hashes:
                raise SetupError("Kontrolne vsote po šifrirani kopiji se ne ujemajo.")
        finally:
            unmount_encrypted_data(staging_mount)

    rescue = data_dir.with_name(
        data_dir.name + ".plaintext-rescue-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    data_dir.rename(rescue)
    data_dir.mkdir(mode=0o700)
    try:
        mount_encrypted_data(cipher_dir, data_dir, passphrase)
        _verify_databases(data_dir)
        if _file_hashes(data_dir) != original_hashes:
            raise SetupError("Kontrolne vsote po končnem priklopu se ne ujemajo.")

        save_backup_passphrase(data_dir, passphrase)
        settings["data_encryption"] = {
            "enabled": True,
            "provider": "gocryptfs",
            "cipher_dir": str(cipher_dir),
        }
        settings["backup"] = {
            "enabled": True,
            "target_dir": str(backup_dir),
            "retention_days": 30,
            "max_age_hours": 24,
            "require_distinct_volume": True,
            "passphrase_configured": True,
        }
        _atomic_json(SETTINGS_PATH, settings)
        create_encrypted_backup(
            data_dir=data_dir,
            target_dir=backup_dir,
            passphrase=passphrase,
            app_version=str(settings.get("app_version") or "30.0"),
            schema_version=int(settings.get("schema_version", 1)),
            kind="DAILY",
            extra_files=(SETTINGS_PATH,),
            require_distinct_volume=True,
        )
    except Exception:
        _atomic_json(SETTINGS_PATH, original_settings)
        try:
            unmount_encrypted_data(data_dir)
        except Exception:
            pass
        if not any(data_dir.iterdir()):
            data_dir.rmdir()
            rescue.rename(data_dir)
        if cipher_dir.is_dir():
            shutil.rmtree(cipher_dir)
        raise

    unmount_encrypted_data(data_dir)
    ensure_encrypted_data_mounted(settings, data_dir)
    _verify_databases(data_dir)
    unmount_encrypted_data(data_dir)

    # Source contains only the blank, verified pre-production databases. The encrypted
    # copy and a separately verified encrypted backup now both exist.
    shutil.rmtree(rescue)
    return cipher_dir, backup_dir


def ask_passphrase() -> str | None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("MikroFIX - končna zaščita produkcije")
    root.resizable(False, False)
    result: dict[str, str | None] = {"value": None}

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack()
    tk.Label(frame, text="Šifriranje podatkov in zunanjega backupa", font=("Sans", 13, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
    )
    tk.Label(
        frame,
        text=(
            "Vpiši eno močno obnovitveno geslo. Uporabljeno bo za lokalno šifrirano mapo\n"
            "in za backup na KINGSTON. Shrani ga tudi izven računalnika; brez njega obnova ni mogoča."
        ),
        justify="left",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
    first = tk.StringVar()
    second = tk.StringVar()
    tk.Label(frame, text="Geslo (najmanj 12 znakov):").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=5)
    first_entry = tk.Entry(frame, textvariable=first, show="*", width=34)
    first_entry.grid(row=2, column=1, pady=5)
    tk.Label(frame, text="Ponovi geslo:").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=5)
    tk.Entry(frame, textvariable=second, show="*", width=34).grid(row=3, column=1, pady=5)

    def accept() -> None:
        if len(first.get()) < 12:
            messagebox.showerror("MikroFIX", "Geslo mora imeti najmanj 12 znakov.", parent=root)
            return
        if first.get() != second.get():
            messagebox.showerror("MikroFIX", "Gesli se ne ujemata.", parent=root)
            return
        result["value"] = first.get()
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
    tk.Button(buttons, text="PREKLIČI", command=root.destroy).pack(side="right", padx=(8, 0))
    tk.Button(buttons, text="ŠIFRIRAJ IN PREVERI", command=accept).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    first_entry.focus_set()
    root.mainloop()
    return result["value"]


def main() -> int:
    passphrase = ask_passphrase()
    if passphrase is None:
        print("Nastavitev je bila preklicana.")
        return 1
    try:
        cipher_dir, backup_dir = configure(passphrase)
    except Exception as exc:
        import tkinter.messagebox as messagebox
        messagebox.showerror("MikroFIX - nastavitev ni uspela", str(exc))
        return 1
    import tkinter.messagebox as messagebox
    messagebox.showinfo(
        "MikroFIX - zaščita je vključena",
        f"Šifrirani podatki: {cipher_dir}\nPreverjen zunanji backup: {backup_dir}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
