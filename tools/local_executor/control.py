#!/usr/bin/env python3
"""Grant, revoke, or inspect the local executor's short write lease."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time


STATE_DIR = Path.home() / ".local" / "state" / "mikrofix"
LEASE_FILE = STATE_DIR / "executor-lease.json"


def atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def confirm_with_zenity(minutes: int) -> bool:
    message = (
        "Lokalnemu izvajalcu Qwen dovoli spreminjanje datotek projekta in "
        f"zagon omejenih testov za {minutes} minut?\n\n"
        "Izvajalec nima terminala, root dostopa, dostopa do certifikatov, "
        "gesel ali poslovne baze. Vsaka sprememba se zapiše v dnevnik."
    )
    result = subprocess.run(
        ["zenity", "--question", "--title=MikroFIX – dovoljenje izvajalcu", f"--text={message}", "--width=520"],
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("grant", "revoke", "status"))
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--yes", action="store_true", help="Skip the GUI confirmation; intended for tests only.")
    args = parser.parse_args()

    if args.action == "revoke":
        LEASE_FILE.unlink(missing_ok=True)
        print("MikroFIX local executor: writing is locked.")
        return 0
    if args.action == "status":
        try:
            lease = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
            remaining = max(0, int(float(lease.get("expires_at", 0)) - time.time()))
        except (OSError, json.JSONDecodeError):
            remaining = 0
        print(f"MikroFIX local executor: write lease has {remaining} seconds remaining.")
        return 0

    if not 1 <= args.minutes <= 30:
        parser.error("--minutes must be between 1 and 30")
    if not args.yes and not confirm_with_zenity(args.minutes):
        print("Permission was not granted.")
        return 1
    now = time.time()
    atomic_write(
        LEASE_FILE,
        {
            "lease_id": secrets.token_hex(12),
            "granted_at": now,
            "expires_at": now + args.minutes * 60,
            "granted_by_uid": os.getuid(),
        },
    )
    print(f"MikroFIX local executor: writing is enabled for {args.minutes} minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
