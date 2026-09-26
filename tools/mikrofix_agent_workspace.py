#!/usr/bin/env python3
"""Create and verify the isolated workspace used by MikroFix local agents.

The workspace contains an agent-owned Git clone, immutable project knowledge,
per-run worktrees and candidate artifacts.  It deliberately does not copy
ignored files, secrets, databases or the trusted supervisor control plane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
import uuid

try:
    from tools.mikrofix_workspace_lock import workspace_lock
except ModuleNotFoundError:
    from mikrofix_workspace_lock import workspace_lock  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
WORKSPACE_DIRS = (
    "knowledge", "inbox", "jobs", "worktrees", "runs", "artifacts",
    "reports", "proposals", "archive", "completion-outbox",
)
KNOWLEDGE_PATHS = (
    "AGENTS.md",
    "docs/kanon/00_KAZALO.md",
    "docs/kanon/01_POSLOVNA_PRAVILA.md",
    "docs/kanon/02_CILJNA_ARHITEKTURA.md",
    "docs/kanon/03_SPREJEMNI_TESTI.md",
    "docs/kanon/04_STANJE_IZVEDBE.md",
    "docs/kanon/05_ZGODOVINA_ODLOCITEV.md",
    "docs/kanon/06_LOKALNI_AGENTI.md",
    "docs/local_agents/README.md",
    "docs/local_agents/PRAVILNIK_VERIGE.md",
    "docs/local_agents/pravila",
    "docs/local_agents/roles",
)


class WorkspaceError(RuntimeError):
    pass


WRITER_SERVICES = (
    "mikrofix-local-web.service",
    "mikrofix-openwebui-gateway.service",
    "mikrofix-bridge.service",
    "mikrofix-local-executor.service",
)
WRITER_MARKERS = (
    "mikrofix_local_manager.py", "mikrofix_local_manager_ui.py",
    "mikrofix_supervisor.py", "mikrofix_bridge.py", "mikrofix_local_agent.py",
    "mikrofix_codex_review.py", "mikrofix_completion.py",
    "mikrofix_owner_commands.py", "tools.local_executor.server",
    "tools/local_executor/server.py", "tools/local_executor/start.sh",
    "tools/local_executor/control.py",
)
FINAL_ROTATION_PHASES = {"COMPLETED", "ROLLED_BACK"}
FORWARD_ONLY_PHASES = {
    "SERVICES_ACTIVATING", "SERVICES_RECOVERY_REQUIRED", "SERVICES_RESTORED",
}
ROLLBACK_SERVICE_PHASES = {
    "ROLLBACK_SERVICES_ACTIVATING", "ROLLBACK_SERVICES_RECOVERY_REQUIRED",
    "ROLLBACK_SERVICES_RESTORED",
}


def run(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, check=False,
    )
    if result.returncode:
        raise WorkspaceError(f"Command failed ({args[0]}): {result.stdout[-4000:]}")
    return result.stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def durable_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def workspace_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve()
    records: list[bytes] = []
    counts = {"directories": 0, "files": 0, "symlinks": 0, "bytes": 0}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        names = sorted(directories + files, key=lambda value: value.encode("utf-8"))
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = format(stat.S_IMODE(metadata.st_mode), "04o")
            encoded_path = relative.encode("utf-8")
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path).encode("utf-8")
                digest = hashlib.sha256(target).hexdigest().encode("ascii")
                records.append(b"L\0" + encoded_path + b"\0" + mode.encode() + b"\0" + digest + b"\n")
                counts["symlinks"] += 1
                if name in directories:
                    directories.remove(name)
            elif stat.S_ISDIR(metadata.st_mode):
                records.append(b"D\0" + encoded_path + b"\0" + mode.encode() + b"\n")
                counts["directories"] += 1
            elif stat.S_ISREG(metadata.st_mode):
                digest = sha256(path).encode("ascii")
                records.append(
                    b"F\0" + encoded_path + b"\0" + mode.encode() + b"\0"
                    + str(metadata.st_size).encode("ascii") + b"\0" + digest + b"\n"
                )
                counts["files"] += 1
                counts["bytes"] += metadata.st_size
    records.sort(key=lambda record: record.split(b"\0", 2)[1])
    digest = hashlib.sha256(b"".join(records)).hexdigest()
    return {"algorithm": "sha256-dfl-v1", "tree_sha256": digest, **counts}


class UserServices:
    def is_active(self, name: str) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", name], check=False,
        )
        return result.returncode == 0

    def stop(self, name: str) -> None:
        run(["systemctl", "--user", "stop", name])

    def start(self, name: str) -> None:
        run(["systemctl", "--user", "start", name])

    def health(self, name: str) -> bool:
        return self.is_active(name)


def workspace_processes(workspace: Path) -> list[str]:
    own_pid = os.getpid()
    matches = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            cwd = (entry / "cwd").resolve()
        except (OSError, PermissionError):
            continue
        in_workspace = cwd == workspace or workspace in cwd.parents
        marked = any(marker in command for marker in WRITER_MARKERS)
        if in_workspace or marked:
            matches.append(f"{entry.name}:{command.strip()}")
    return sorted(matches)


def rotation_root(workspace: Path) -> Path:
    return workspace.parent / ".mikrofix-workspace-rotations"


def unfinished_rotations(workspace: Path) -> list[Path]:
    root = rotation_root(workspace)
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob("*.json")):
        try:
            phase = json.loads(path.read_text(encoding="utf-8")).get("phase")
        except (OSError, json.JSONDecodeError):
            phase = "INVALID"
        if phase not in FINAL_ROTATION_PHASES:
            result.append(path)
    return result


def update_rotation(journal_path: Path, journal: dict[str, Any], phase: str, **extra: Any) -> None:
    journal.update(extra)
    journal["phase"] = phase
    journal["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    durable_json(journal_path, journal)


def workspace_staging_paths(workspace: Path) -> list[Path]:
    return sorted(workspace.parent.glob(f".{workspace.name}.init-*"))


def workspace_identity(path: Path, journal: dict[str, Any]) -> str:
    if not path.exists():
        return "MISSING"
    if not path.is_dir():
        return "INVALID"
    try:
        inventory = workspace_inventory(path)
    except (OSError, WorkspaceError):
        return "INVALID"
    if inventory == journal.get("old_inventory"):
        return "OLD"
    try:
        verification = verify_workspace(path)
    except (OSError, KeyError, json.JSONDecodeError, WorkspaceError):
        return "INVALID"
    if (
        verification["status"] == "OK"
        and verification["baseline_commit"] == journal.get("new_checkpoint")
    ):
        expected_new = journal.get("new_inventory")
        if expected_new is None or inventory == expected_new:
            return "NEW"
    return "INVALID"


def rotation_topology(workspace: Path, journal: dict[str, Any]) -> dict[str, Any]:
    archive = Path(journal["archive"])
    failed = Path(journal["failed_candidate"])
    return {
        "workspace": workspace_identity(workspace, journal),
        "archive": workspace_identity(archive, journal),
        "failed": workspace_identity(failed, journal),
        "staging": [str(path) for path in workspace_staging_paths(workspace)],
    }


def unique_failed_path(preferred: Path) -> Path:
    if not preferred.exists():
        return preferred
    return Path(str(preferred) + f"-{uuid.uuid4().hex[:8]}")


def quarantine_staging(workspace: Path, failed: Path) -> list[str]:
    quarantined = []
    for index, staging in enumerate(workspace_staging_paths(workspace), start=1):
        destination = unique_failed_path(Path(f"{failed}-staging-{index}"))
        os.replace(staging, destination)
        fsync_directory(workspace.parent)
        quarantined.append(str(destination))
    return quarantined


def service_health(services: Any, name: str) -> bool:
    health = getattr(services, "health", None)
    return bool(health(name) if health is not None else services.is_active(name))


def activate_services(
    journal_path: Path, journal: dict[str, Any], services: Any, *,
    activating_phase: str, restored_phase: str, final_phase: str,
    failure_phase: str,
) -> None:
    records = journal.setdefault("service_recovery", {})
    try:
        for name, was_active in journal.get("services_before", {}).items():
            record = records.setdefault(name, {"was_active": bool(was_active)})
            if not was_active:
                record["state"] = "NOT_REQUIRED"
                continue
            if service_health(services, name):
                record.update({"state": "HEALTHY", "command_succeeded": True,
                               "health_ok": True})
                update_rotation(journal_path, journal, activating_phase)
                continue
            record.update({
                "state": "STARTING",
                "start_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "command_succeeded": False,
                "health_ok": False,
            })
            update_rotation(journal_path, journal, activating_phase)
            services.start(name)
            record.update({"state": "COMMAND_OK", "command_succeeded": True})
            update_rotation(journal_path, journal, activating_phase)
            healthy = service_health(services, name)
            record.update({"state": "HEALTHY" if healthy else "UNHEALTHY",
                           "health_ok": healthy})
            update_rotation(journal_path, journal, activating_phase)
            if not healthy:
                raise WorkspaceError(f"service health-check failed: {name}")
        update_rotation(journal_path, journal, restored_phase)
        update_rotation(journal_path, journal, final_phase)
    except BaseException as exc:
        try:
            update_rotation(journal_path, journal, failure_phase, service_error=str(exc))
        except BaseException:
            pass
        raise


def copy_knowledge(source: Path, destination: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in KNOWLEDGE_PATHS:
        origin = source / relative
        if not origin.exists():
            raise WorkspaceError(f"Required project knowledge is missing: {origin}")
        target = destination / relative
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            path.chmod(0o400)
            hashes[path.relative_to(destination).as_posix()] = sha256(path)
        elif path.is_dir():
            path.chmod(0o500)
    destination.chmod(0o500)
    return hashes


def policy_text() -> str:
    return """# Varnostna meja lokalne ekipe MikroFix

1. `repository/`, `worktrees/` in ostale mape tega delovnega območja niso
   kanonični original. So prostor za pripravo kandidatov.
2. Lokalni agenti nikoli ne pišejo v zaščiten original ali na varnostni USB.
3. `knowledge/` je samo za branje. Nova spoznanja se zapišejo v `proposals/`.
4. Kandidat brez lokalnih testov, dokazov in pregleda GPT-OSS ni pripravljen.
5. Lokalna oznaka READY ne pomeni odobritve. Merge, push in deploy so prepovedani.
6. Codex kandidat oceni kot APPROVE, REVISE ali REJECT. Samo APPROVE sme postati
   nov zaščiten checkpoint in varnostna kopija.
7. Naloga, odvisna od nepotrjenega kandidata, mora navesti njegov `depends_on`.
   Zavrnitev starša zadrži vse njegove potomce.
8. Zaključek se privzeto sporoči lastniku. Codex se ne kliče samodejno.
"""


def init_workspace(
    source: Path, workspace: Path, *, failed_destination: Path | None = None,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    if workspace.exists():
        raise WorkspaceError(f"Workspace already exists: {workspace}")
    if not (source / ".git").exists():
        raise WorkspaceError(f"Source is not a Git working tree: {source}")
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"])
    branch = run(["git", "-C", str(source), "branch", "--show-current"])
    dirty = run(["git", "-C", str(source), "status", "--short"]).splitlines()

    workspace.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{workspace.name}.init-", dir=workspace.parent
    ))
    try:
        staging.chmod(0o700)
        for name in WORKSPACE_DIRS:
            (staging / name).mkdir(mode=0o700)
        repository = staging / "repository"
        run(["git", "clone", "--no-hardlinks", "--no-checkout",
             str(source), str(repository)])
        run(["git", "checkout", "--detach", head], cwd=repository)

        knowledge = staging / "knowledge"
        hashes = copy_knowledge(source, knowledge)
        policy = staging / "WORKSPACE_POLICY.md"
        policy.write_text(policy_text(), encoding="utf-8")
        policy.chmod(0o400)

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        checkpoint = {
            "schema_version": 1,
            "commit": head,
            "branch_at_creation": branch,
            "created_at": created_at,
            "disposition": "LOCAL_AGENT_BASELINE_NOT_CODEX_APPROVED",
        }
        write_json(staging / "CURRENT_CHECKPOINT.json", checkpoint, 0o400)
        config = {
            "schema_version": 1,
            "workspace": str(workspace),
            "repository": str(workspace / "repository"),
            "trusted_control_root": str(source),
            "notification_policy": "owner_only",
            "codex_review_policy": "manual_batch",
            "knowledge_manifest": str(workspace / "KNOWLEDGE_MANIFEST.json"),
        }
        write_json(staging / "workspace.json", config, 0o400)
        write_json(staging / "KNOWLEDGE_MANIFEST.json", {
            "schema_version": 1,
            "created_at": created_at,
            "source_commit": head,
            "files": hashes,
        }, 0o400)
        if dirty:
            (staging / "reports" / "SOURCE_DIRTY_AT_CREATION.txt").write_text(
                "Uncommitted source paths were deliberately excluded:\n"
                + "\n".join(dirty) + "\n",
                encoding="utf-8",
            )
        os.replace(staging, workspace)
        fsync_directory(workspace.parent)
    except BaseException:
        if failed_destination is not None and staging.exists():
            os.replace(staging, failed_destination)
        else:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_workspace(workspace)


def verify_workspace(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    config_path = workspace / "workspace.json"
    manifest_path = workspace / "KNOWLEDGE_MANIFEST.json"
    if not config_path.is_file() or not manifest_path.is_file():
        raise WorkspaceError("Workspace config or knowledge manifest is missing")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches = []
    knowledge = workspace / "knowledge"
    expected_files = manifest.get("files", {})
    for relative, expected in expected_files.items():
        path = knowledge / relative
        if not path.is_file() or sha256(path) != expected:
            mismatches.append(relative)
    actual_files = {
        path.relative_to(knowledge).as_posix()
        for path in knowledge.rglob("*") if path.is_file()
    }
    mismatches.extend(
        f"UNEXPECTED:{relative}"
        for relative in sorted(actual_files - set(expected_files))
    )
    if knowledge.stat().st_mode & 0o222:
        mismatches.append("WRITABLE:.")
    mismatches = sorted(set(mismatches))
    repository = Path(config["repository"])
    repository_head = run(["git", "-C", str(repository), "rev-parse", "HEAD"])
    checkpoint = json.loads(
        (workspace / "CURRENT_CHECKPOINT.json").read_text(encoding="utf-8")
    )
    return {
        "status": "OK" if not mismatches else "INVALID",
        "workspace": str(workspace),
        "repository_head": repository_head,
        "baseline_commit": checkpoint["commit"],
        "knowledge_files": len(manifest.get("files", {})),
        "knowledge_mismatches": mismatches,
        "notification_policy": config.get("notification_policy"),
    }


def rotate_workspace(
    source: Path, workspace: Path, *, services: Any | None = None,
    process_checker: Any = workspace_processes,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    services = services or UserServices()
    pending = unfinished_rotations(workspace)
    if pending:
        raise WorkspaceError(f"unfinished rotation requires recovery: {pending[0]}")
    if run(["git", "-C", str(source), "status", "--short"]):
        raise WorkspaceError("source repository must be clean before rotation")
    old_check = verify_workspace(workspace)
    if old_check["status"] != "OK":
        raise WorkspaceError("existing workspace failed verification")
    active = {name: services.is_active(name) for name in WRITER_SERVICES}
    stopped = []
    try:
        for name, was_active in active.items():
            if was_active:
                services.stop(name)
                stopped.append(name)
    except BaseException:
        for name in stopped:
            services.start(name)
        raise
    rotation_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"
    archive = workspace.parent / f"{workspace.name}.archive-{old_check['baseline_commit'][:8]}-{rotation_id}"
    failed = workspace.parent / f"{workspace.name}.failed-{rotation_id}"
    journal_path = rotation_root(workspace) / f"{rotation_id}.json"
    journal: dict[str, Any] | None = None
    original_error: BaseException | None = None
    try:
        with workspace_lock(workspace, exclusive=True, blocking=False):
            processes = process_checker(workspace)
            if processes:
                raise WorkspaceError("workspace writers are still active: " + "; ".join(processes))
            old_inventory = workspace_inventory(workspace)
            new_commit = run(["git", "-C", str(source), "rev-parse", "HEAD"])
            journal = {
                "schema_version": 2, "rotation_id": rotation_id,
                "workspace": str(workspace), "archive": str(archive),
                "failed_candidate": str(failed),
                "old_checkpoint": old_check["baseline_commit"],
                "new_checkpoint": new_commit,
                "verified_ancestor": "b0d6e25cc31191c4589f00f7b6f5e2d49983ba10",
                "old_inventory": old_inventory, "services_before": active,
                "service_recovery": {
                    name: {"was_active": was_active, "state": "PENDING"}
                    for name, was_active in active.items()
                },
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            update_rotation(journal_path, journal, "PREPARED")
            try:
                os.replace(workspace, archive)
                fsync_directory(workspace.parent)
                update_rotation(journal_path, journal, "OLD_ARCHIVED")
                init_workspace(source, workspace, failed_destination=failed)
                update_rotation(journal_path, journal, "NEW_INITIALIZED")
                new_check = verify_workspace(workspace)
                if new_check["status"] != "OK" or new_check["baseline_commit"] != new_commit:
                    raise WorkspaceError("new workspace failed checkpoint verification")
                if workspace_inventory(archive) != old_inventory:
                    raise WorkspaceError("archived workspace inventory changed")
                new_inventory = workspace_inventory(workspace)
                update_rotation(
                    journal_path, journal, "NEW_VERIFIED",
                    new_verification=new_check, new_inventory=new_inventory,
                )
                # This durable boundary makes every later failure forward-only.
                update_rotation(journal_path, journal, "SERVICES_ACTIVATING")
            except BaseException as exc:
                original_error = exc
                update_rotation(journal_path, journal, "ROLLBACK_REQUIRED", error=str(exc))
                rollback_paths_locked(
                    workspace, journal_path, journal, process_checker=process_checker,
                )
    except BaseException:
        if journal is None:
            for name, was_active in active.items():
                if was_active:
                    services.start(name)
        raise

    if original_error is not None:
        activate_services(
            journal_path, journal, services,
            activating_phase="ROLLBACK_SERVICES_ACTIVATING",
            restored_phase="ROLLBACK_SERVICES_RESTORED",
            final_phase="ROLLED_BACK",
            failure_phase="ROLLBACK_SERVICES_RECOVERY_REQUIRED",
        )
        raise original_error

    activate_services(
        journal_path, journal, services,
        activating_phase="SERVICES_ACTIVATING",
        restored_phase="SERVICES_RESTORED",
        final_phase="COMPLETED",
        failure_phase="SERVICES_RECOVERY_REQUIRED",
    )
    return {"status": "OK", "rotation": journal}


def rollback_paths_locked(
    workspace: Path, journal_path: Path, journal: dict[str, Any], *,
    process_checker: Any = workspace_processes,
) -> dict[str, Any]:
    processes = process_checker(workspace)
    if processes:
        raise WorkspaceError("workspace writers are still active: " + "; ".join(processes))
    archive = Path(journal["archive"])
    failed = Path(journal["failed_candidate"])
    topology = rotation_topology(workspace, journal)
    quarantined: list[str] = []
    if topology["workspace"] == "OLD" and topology["archive"] == "MISSING":
        # PREPARED or a failed W -> A rename: the original is already authoritative.
        pass
    elif topology["workspace"] == "OLD":
        raise WorkspaceError(f"old archive changed or is ambiguous; rollback refused: {topology}")
    elif topology["archive"] == "OLD":
        if topology["workspace"] != "MISSING":
            quarantine = unique_failed_path(failed)
            os.replace(workspace, quarantine)
            fsync_directory(workspace.parent)
            quarantined.append(str(quarantine))
        os.replace(archive, workspace)
        fsync_directory(workspace.parent)
    else:
        raise WorkspaceError(f"no unambiguous old workspace for rollback: {topology}")
    quarantined.extend(quarantine_staging(workspace, failed))
    if workspace_identity(workspace, journal) != "OLD":
        raise WorkspaceError("restored workspace does not match the original tree hash")
    restored = verify_workspace(workspace)
    update_rotation(
        journal_path, journal, "ROLLBACK_SERVICES_ACTIVATING",
        rollback_verification=restored, quarantined=quarantined,
    )
    return restored


def recover_rotation(
    workspace: Path, rotation_id: str, *, complete: bool,
    services: Any | None = None, process_checker: Any = workspace_processes,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    services = services or UserServices()
    journal_path = rotation_root(workspace) / f"{rotation_id}.json"
    if not journal_path.is_file():
        raise WorkspaceError(f"rotation journal does not exist: {journal_path}")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if Path(journal.get("workspace", "")).resolve() != workspace:
        raise WorkspaceError("rotation journal belongs to a different workspace")
    phase = str(journal.get("phase"))
    if phase in FINAL_ROTATION_PHASES:
        raise WorkspaceError(f"rotation is already final: {phase}")

    if phase in FORWARD_ONLY_PHASES:
        if not complete:
            raise WorkspaceError(f"rollback is forbidden after service activation began: {phase}")
        verify_forward_topology(workspace, journal)
        activate_services(
            journal_path, journal, services,
            activating_phase="SERVICES_ACTIVATING",
            restored_phase="SERVICES_RESTORED",
            final_phase="COMPLETED",
            failure_phase="SERVICES_RECOVERY_REQUIRED",
        )
        return {"status": "OK", "rotation": journal}

    if phase in ROLLBACK_SERVICE_PHASES:
        if complete:
            raise WorkspaceError(f"completion is forbidden after rollback began: {phase}")
        try:
            old_result = verify_workspace(workspace)
        except (OSError, KeyError, json.JSONDecodeError, WorkspaceError) as exc:
            raise WorkspaceError("rollback service recovery has no valid old workspace") from exc
        if (
            old_result["status"] != "OK"
            or old_result["baseline_commit"] != journal["old_checkpoint"]
        ):
            raise WorkspaceError("rollback service recovery has no valid old workspace")
        activate_services(
            journal_path, journal, services,
            activating_phase="ROLLBACK_SERVICES_ACTIVATING",
            restored_phase="ROLLBACK_SERVICES_RESTORED",
            final_phase="ROLLED_BACK",
            failure_phase="ROLLBACK_SERVICES_RECOVERY_REQUIRED",
        )
        return {"status": "OK", "rotation": journal}

    with workspace_lock(workspace, exclusive=True, blocking=False):
        topology = rotation_topology(workspace, journal)
        if complete:
            if topology["workspace"] != "NEW" or topology["archive"] != "OLD":
                raise WorkspaceError(f"new workspace cannot be completed safely: {topology}")
            if topology["staging"]:
                raise WorkspaceError(f"unfinished staging prevents completion: {topology['staging']}")
            result = verify_workspace(workspace)
            new_inventory = workspace_inventory(workspace)
            update_rotation(
                journal_path, journal, "NEW_VERIFIED",
                new_verification=result, new_inventory=new_inventory,
            )
            update_rotation(journal_path, journal, "SERVICES_ACTIVATING")
        else:
            result = rollback_paths_locked(
                workspace, journal_path, journal, process_checker=process_checker,
            )

    if complete:
        activate_services(
            journal_path, journal, services,
            activating_phase="SERVICES_ACTIVATING",
            restored_phase="SERVICES_RESTORED",
            final_phase="COMPLETED",
            failure_phase="SERVICES_RECOVERY_REQUIRED",
        )
    else:
        activate_services(
            journal_path, journal, services,
            activating_phase="ROLLBACK_SERVICES_ACTIVATING",
            restored_phase="ROLLBACK_SERVICES_RESTORED",
            final_phase="ROLLED_BACK",
            failure_phase="ROLLBACK_SERVICES_RECOVERY_REQUIRED",
        )
    return {"status": "OK", "rotation": journal}


def verify_forward_topology(workspace: Path, journal: dict[str, Any]) -> None:
    archive = Path(journal["archive"])
    result = verify_workspace(workspace)
    if result["status"] != "OK" or result["baseline_commit"] != journal["new_checkpoint"]:
        raise WorkspaceError("forward recovery has no valid new workspace")
    if workspace_identity(archive, journal) != "OLD":
        raise WorkspaceError("forward recovery has no valid old archive")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MikroFix local-agent workspace")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--source", type=Path, default=PROJECT_ROOT)
    sub.add_parser("verify")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("--source", type=Path, default=PROJECT_ROOT)
    recover = sub.add_parser("recover")
    recover.add_argument("--rotation-id", required=True)
    action = recover.add_mutually_exclusive_group(required=True)
    action.add_argument("--rollback", action="store_true")
    action.add_argument("--complete", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = (
        init_workspace(args.source, args.workspace)
        if args.command == "init" else
        rotate_workspace(args.source, args.workspace)
        if args.command == "rotate" else
        recover_rotation(args.workspace, args.rotation_id, complete=args.complete)
        if args.command == "recover" else verify_workspace(args.workspace)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
