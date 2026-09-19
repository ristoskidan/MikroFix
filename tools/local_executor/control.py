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


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "mikrofix"


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


def confirm_with_zenity(minutes: int, run_id: str = "") -> bool:
    scope = f" za nadzorovani run {run_id}" if run_id else ""
    message = (
        f"Lokalnemu izvajalcu Qwen{scope} dovoli spreminjanje datotek projekta in "
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
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--plan-sha256", default="")
    parser.add_argument("--allowed-files-sha256", default="")
    parser.add_argument("--yes", action="store_true", help="Skip the GUI confirmation; intended for tests only.")
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser().resolve()
    lease_file = state_dir / "executor-lease.json"

    if args.action == "revoke":
        lease_file.unlink(missing_ok=True)
        print("MikroFIX local executor: writing is locked.")
        return 0
    if args.action == "status":
        try:
            lease = json.loads(lease_file.read_text(encoding="utf-8"))
            remaining = max(0, int(float(lease.get("expires_at", 0)) - time.time()))
        except (OSError, json.JSONDecodeError):
            remaining = 0
        print(f"MikroFIX local executor: write lease has {remaining} seconds remaining.")
        return 0

    if not 1 <= args.minutes <= 30:
        parser.error("--minutes must be between 1 and 30")
    binding_values = (
        args.run_id, args.project_root, args.plan_sha256,
        args.allowed_files_sha256,
    )
    if any(binding_values) and not all(binding_values):
        parser.error(
            "run-scoped grants require --run-id, --project-root, "
            "--plan-sha256 and --allowed-files-sha256"
        )
    if not args.yes and not confirm_with_zenity(args.minutes, args.run_id):
        print("Permission was not granted.")
        return 1
    now = time.time()
    atomic_write(
        lease_file,
        {
            "lease_id": secrets.token_hex(12),
            "granted_at": now,
            "expires_at": now + args.minutes * 60,
            "granted_by_uid": os.getuid(),
            **({
                "run_id": args.run_id,
                "project_root": str(args.project_root.expanduser().resolve()),
                "plan_sha256": args.plan_sha256,
                "allowed_files_sha256": args.allowed_files_sha256,
            } if args.run_id else {}),
        },
    )
    print(f"MikroFIX local executor: writing is enabled for {args.minutes} minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
