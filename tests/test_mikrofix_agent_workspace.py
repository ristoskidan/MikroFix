import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_agent_workspace.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_agent_workspace", MODULE_PATH)
workspace_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = workspace_module
SPEC.loader.exec_module(workspace_module)


class TrackingServices:
    def __init__(self, active=(), *, fail_start_at=None, unhealthy=()):
        self.active = set(active)
        self.fail_start_at = fail_start_at
        self.unhealthy = set(unhealthy)
        self.starts = []
        self.stops = []

    def is_active(self, name):
        return name in self.active

    def stop(self, name):
        self.stops.append(name)
        self.active.discard(name)

    def start(self, name):
        self.starts.append(name)
        if self.fail_start_at == len(self.starts):
            raise RuntimeError(f"start failed: {name}")
        self.active.add(name)

    def health(self, name):
        return name in self.active and name not in self.unhealthy


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.workspace = self.root / "workspace"
        self.source.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        required = {
            "AGENTS.md": "rules\n",
            "docs/kanon/00_KAZALO.md": "index\n",
            "docs/kanon/01_POSLOVNA_PRAVILA.md": "business\n",
            "docs/kanon/02_CILJNA_ARHITEKTURA.md": "architecture\n",
            "docs/kanon/03_SPREJEMNI_TESTI.md": "acceptance\n",
            "docs/kanon/04_STANJE_IZVEDBE.md": "state\n",
            "docs/kanon/05_ZGODOVINA_ODLOCITEV.md": "history\n",
            "docs/kanon/06_LOKALNI_AGENTI.md": "agents\n",
            "docs/local_agents/README.md": "readme\n",
            "docs/local_agents/PRAVILNIK_VERIGE.md": "chain\n",
            "docs/local_agents/pravila/00_AVTORITETA.md": "core\n",
            "docs/local_agents/roles/EXECUTOR_SYSTEM.md": "executor\n",
            "app.py": "print('baseline')\n",
        }
        for relative, content in required.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.source), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def rotation_journal(self, *, phase, archive, old_inventory, new_checkpoint=None):
        rotation_id = f"test-{phase.lower()}"
        journal_path = workspace_module.rotation_root(self.workspace) / f"{rotation_id}.json"
        journal = {
            "schema_version": 2,
            "rotation_id": rotation_id,
            "workspace": str(self.workspace),
            "archive": str(archive),
            "failed_candidate": str(self.root / f"failed-{phase.lower()}"),
            "old_checkpoint": self.git("rev-parse", "HEAD"),
            "new_checkpoint": new_checkpoint or self.git("rev-parse", "HEAD"),
            "old_inventory": old_inventory,
            "services_before": {},
            "service_recovery": {},
        }
        workspace_module.update_rotation(journal_path, journal, phase)
        return rotation_id, journal_path, journal

    def test_init_creates_isolated_committed_clone_and_frozen_knowledge(self):
        (self.source / "app.py").write_text("dirty\n", encoding="utf-8")
        result = workspace_module.init_workspace(self.source, self.workspace)
        self.assertEqual("OK", result["status"])
        self.assertEqual("print('baseline')\n", (
            self.workspace / "repository" / "app.py"
        ).read_text(encoding="utf-8"))
        self.assertEqual(0, (self.workspace / "knowledge" / "AGENTS.md").stat().st_mode & 0o222)
        self.assertEqual(0, (self.workspace / "knowledge").stat().st_mode & 0o222)
        self.assertEqual("owner_only", result["notification_policy"])
        dirty_report = self.workspace / "reports" / "SOURCE_DIRTY_AT_CREATION.txt"
        self.assertIn("app.py", dirty_report.read_text(encoding="utf-8"))
        config = json.loads((self.workspace / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(str(self.source.resolve()), config["trusted_control_root"])
        manifest = json.loads((self.workspace / "KNOWLEDGE_MANIFEST.json").read_text())
        self.assertIn("docs/kanon/06_LOKALNI_AGENTI.md", manifest["files"])

    def test_init_fsyncs_parent_after_installing_workspace(self):
        original_fsync = workspace_module.fsync_directory
        synced = []

        def record(path):
            synced.append(Path(path))
            return original_fsync(path)

        with mock.patch.object(workspace_module, "fsync_directory", side_effect=record):
            workspace_module.init_workspace(self.source, self.workspace)
        self.assertIn(self.workspace.parent, synced)

    def test_verify_detects_knowledge_tampering(self):
        workspace_module.init_workspace(self.source, self.workspace)
        knowledge = self.workspace / "knowledge" / "AGENTS.md"
        knowledge.chmod(0o600)
        knowledge.write_text("changed\n", encoding="utf-8")
        result = workspace_module.verify_workspace(self.workspace)
        self.assertEqual("INVALID", result["status"])
        self.assertEqual(["AGENTS.md"], result["knowledge_mismatches"])

    def test_verify_detects_unexpected_knowledge_file_and_writable_root(self):
        workspace_module.init_workspace(self.source, self.workspace)
        knowledge = self.workspace / "knowledge"
        knowledge.chmod(0o700)
        (knowledge / "injected.md").write_text("not canonical\n", encoding="utf-8")
        result = workspace_module.verify_workspace(self.workspace)
        self.assertEqual("INVALID", result["status"])
        self.assertEqual(
            ["UNEXPECTED:injected.md", "WRITABLE:."],
            result["knowledge_mismatches"],
        )

    def test_init_refuses_existing_workspace(self):
        self.workspace.mkdir()
        with self.assertRaises(workspace_module.WorkspaceError):
            workspace_module.init_workspace(self.source, self.workspace)

    def test_inventory_includes_empty_directories_files_and_symlinks(self):
        root = self.root / "inventory"
        (root / "empty").mkdir(parents=True)
        (root / "value.txt").write_text("value", encoding="utf-8")
        (root / "link").symlink_to("value.txt")
        first = workspace_module.workspace_inventory(root)
        self.assertEqual(1, first["directories"])
        self.assertEqual(1, first["files"])
        self.assertEqual(1, first["symlinks"])
        self.assertEqual(first, workspace_module.workspace_inventory(root))

    def test_rotation_lock_refuses_exclusive_access_while_writer_holds_shared_lock(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_writer_lock():
            with workspace_module.workspace_lock(self.workspace):
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_writer_lock)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            with self.assertRaisesRegex(RuntimeError, "workspace is busy"):
                with workspace_module.workspace_lock(
                    self.workspace, exclusive=True, blocking=False,
                ):
                    self.fail("exclusive lock must not be acquired")
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_rotation_preserves_old_workspace_and_uses_new_commit(self):
        workspace_module.init_workspace(self.source, self.workspace)
        evidence = self.workspace / "jobs" / "MF-OLD" / "audit.json"
        evidence.parent.mkdir()
        evidence.write_text("old evidence\n", encoding="utf-8")
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        (self.source / "app.py").write_text("print('new')\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-m", "new checkpoint")

        class Services:
            def is_active(self, _name): return False
            def stop(self, _name): raise AssertionError("inactive service stopped")
            def start(self, _name): raise AssertionError("inactive service started")

        result = workspace_module.rotate_workspace(
            self.source, self.workspace, services=Services(), process_checker=lambda _path: [],
        )
        self.assertEqual("OK", result["status"])
        rotation = result["rotation"]
        archive = Path(rotation["archive"])
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(archive))
        self.assertEqual("old evidence\n", (archive / "jobs/MF-OLD/audit.json").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"), workspace_module.verify_workspace(
            self.workspace
        )["baseline_commit"])

    def test_failed_rotation_restores_old_workspace_without_deletion(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)

        class Services:
            def is_active(self, _name): return False
            def stop(self, _name): pass
            def start(self, _name): pass

        original = workspace_module.init_workspace
        with mock.patch.object(
            workspace_module, "init_workspace", side_effect=RuntimeError("injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                workspace_module.rotate_workspace(
                    self.source, self.workspace, services=Services(),
                    process_checker=lambda _path: [],
                )
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))
        journals = list(workspace_module.rotation_root(self.workspace).glob("*.json"))
        self.assertEqual(1, len(journals))
        self.assertEqual("ROLLED_BACK", json.loads(journals[0].read_text())["phase"])

    def test_failed_workspace_archive_rename_keeps_original_at_canonical_path(self):
        workspace_module.init_workspace(self.source, self.workspace)
        original_inventory = workspace_module.workspace_inventory(self.workspace)
        original_replace = workspace_module.os.replace

        def fail_archive_rename(source, destination):
            if Path(source) == self.workspace and ".archive-" in Path(destination).name:
                raise OSError("injected archive rename failure")
            return original_replace(source, destination)

        with mock.patch.object(workspace_module.os, "replace", side_effect=fail_archive_rename):
            with self.assertRaisesRegex(OSError, "archive rename failure"):
                workspace_module.rotate_workspace(
                    self.source, self.workspace, services=TrackingServices(),
                    process_checker=lambda _path: [],
                )
        self.assertEqual(original_inventory, workspace_module.workspace_inventory(self.workspace))
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual("ROLLED_BACK", journal["phase"])
        self.assertFalse(Path(journal["failed_candidate"]).exists())

    def test_failure_after_archive_rename_before_phase_write_restores_old_workspace(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        original_fsync = workspace_module.fsync_directory
        failed = False

        def fail_first_parent_fsync(path):
            nonlocal failed
            if Path(path) == self.workspace.parent and not failed:
                failed = True
                raise OSError("injected post-rename fsync failure")
            return original_fsync(path)

        with mock.patch.object(
            workspace_module, "fsync_directory", side_effect=fail_first_parent_fsync,
        ):
            with self.assertRaisesRegex(OSError, "post-rename"):
                workspace_module.rotate_workspace(
                    self.source, self.workspace, services=TrackingServices(),
                    process_checker=lambda _path: [],
                )
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        self.assertEqual("ROLLED_BACK", json.loads(journal_path.read_text())["phase"])

    def test_rollback_quarantines_interrupted_staging_without_deleting_it(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "archive"
        os.replace(self.workspace, archive)
        staging = self.root / f".{self.workspace.name}.init-interrupted"
        staging.mkdir()
        (staging / "partial.txt").write_text("partial\n", encoding="utf-8")
        rotation_id, _, _ = self.rotation_journal(
            phase="OLD_ARCHIVED", archive=archive, old_inventory=old_inventory,
        )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False,
            services=TrackingServices(), process_checker=lambda _path: [],
        )
        self.assertEqual("ROLLED_BACK", result["rotation"]["phase"])
        self.assertFalse(staging.exists())
        quarantined = result["rotation"]["quarantined"]
        self.assertEqual(1, len(quarantined))
        self.assertEqual("partial\n", (Path(quarantined[0]) / "partial.txt").read_text())

    def test_valid_new_workspace_with_old_archived_phase_can_complete(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "archive"
        os.replace(self.workspace, archive)
        (self.source / "app.py").write_text("print('new')\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-m", "new")
        workspace_module.init_workspace(self.source, self.workspace)
        rotation_id, _, _ = self.rotation_journal(
            phase="OLD_ARCHIVED", archive=archive, old_inventory=old_inventory,
            new_checkpoint=self.git("rev-parse", "HEAD"),
        )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=True,
            services=TrackingServices(), process_checker=lambda _path: [],
        )
        self.assertEqual("COMPLETED", result["rotation"]["phase"])
        self.assertTrue(archive.is_dir())

    def test_prepared_recovery_rollback_is_a_verified_no_op(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "not-created-archive"
        rotation_id, _, _ = self.rotation_journal(
            phase="PREPARED", archive=archive, old_inventory=old_inventory,
        )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False,
            services=TrackingServices(), process_checker=lambda _path: [],
        )
        self.assertEqual("ROLLED_BACK", result["rotation"]["phase"])
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))
        self.assertFalse(archive.exists())

    def test_forward_recovery_resumes_partial_service_activation_and_forbids_rollback(self):
        workspace_module.init_workspace(self.source, self.workspace)
        services = TrackingServices(
            active=workspace_module.WRITER_SERVICES[:2], fail_start_at=2,
        )
        with self.assertRaises(RuntimeError):
            workspace_module.rotate_workspace(
                self.source, self.workspace, services=services,
                process_checker=lambda _path: [],
            )
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        with self.assertRaisesRegex(workspace_module.WorkspaceError, "rollback is forbidden"):
            workspace_module.recover_rotation(
                self.workspace, journal["rotation_id"], complete=False,
                services=services, process_checker=lambda _path: [],
            )
        services.fail_start_at = None
        result = workspace_module.recover_rotation(
            self.workspace, journal["rotation_id"], complete=True,
            services=services, process_checker=lambda _path: [],
        )
        self.assertEqual("COMPLETED", result["rotation"]["phase"])
        for name in workspace_module.WRITER_SERVICES[:2]:
            self.assertTrue(result["rotation"]["service_recovery"][name]["health_ok"])

    def test_rollback_service_recovery_retries_health_before_final_phase(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "unused-archive"
        service_name = workspace_module.WRITER_SERVICES[0]
        rotation_id, journal_path, journal = self.rotation_journal(
            phase="ROLLBACK_SERVICES_RECOVERY_REQUIRED",
            archive=archive, old_inventory=old_inventory,
        )
        journal["services_before"] = {service_name: True}
        journal["service_recovery"] = {
            service_name: {"was_active": True, "state": "STARTING"},
        }
        workspace_module.update_rotation(
            journal_path, journal, "ROLLBACK_SERVICES_RECOVERY_REQUIRED",
        )
        services = TrackingServices()
        with self.assertRaisesRegex(workspace_module.WorkspaceError, "completion is forbidden"):
            workspace_module.recover_rotation(
                self.workspace, rotation_id, complete=True, services=services,
            )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False, services=services,
        )
        self.assertEqual("ROLLED_BACK", result["rotation"]["phase"])
        self.assertTrue(result["rotation"]["service_recovery"][service_name]["health_ok"])

    def test_first_service_start_failure_is_forward_only(self):
        workspace_module.init_workspace(self.source, self.workspace)
        services = TrackingServices(
            active=workspace_module.WRITER_SERVICES[:2], fail_start_at=1,
        )
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            workspace_module.rotate_workspace(
                self.source, self.workspace, services=services,
                process_checker=lambda _path: [],
            )
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual("SERVICES_RECOVERY_REQUIRED", journal["phase"])
        self.assertEqual("NEW", workspace_module.workspace_identity(self.workspace, journal))
        self.assertEqual("OLD", workspace_module.workspace_identity(Path(journal["archive"]), journal))

    def test_second_service_start_failure_keeps_first_service_and_new_workspace(self):
        workspace_module.init_workspace(self.source, self.workspace)
        services = TrackingServices(
            active=workspace_module.WRITER_SERVICES[:2], fail_start_at=2,
        )
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            workspace_module.rotate_workspace(
                self.source, self.workspace, services=services,
                process_checker=lambda _path: [],
            )
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual("SERVICES_RECOVERY_REQUIRED", journal["phase"])
        self.assertIn(workspace_module.WRITER_SERVICES[0], services.active)
        self.assertEqual("NEW", workspace_module.workspace_identity(self.workspace, journal))

    def test_service_health_failure_is_recorded_for_forward_recovery(self):
        service_name = workspace_module.WRITER_SERVICES[0]
        workspace_module.init_workspace(self.source, self.workspace)
        services = TrackingServices(active=(service_name,), unhealthy=(service_name,))
        with self.assertRaisesRegex(workspace_module.WorkspaceError, "health-check failed"):
            workspace_module.rotate_workspace(
                self.source, self.workspace, services=services,
                process_checker=lambda _path: [],
            )
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual("SERVICES_RECOVERY_REQUIRED", journal["phase"])
        self.assertFalse(journal["service_recovery"][service_name]["health_ok"])

    def test_journal_failure_at_services_restored_does_not_roll_back_files(self):
        self._assert_service_phase_write_failure("SERVICES_RESTORED")

    def test_journal_failure_at_completed_does_not_roll_back_files(self):
        self._assert_service_phase_write_failure("COMPLETED")

    def _assert_service_phase_write_failure(self, failing_phase):
        service_name = workspace_module.WRITER_SERVICES[0]
        workspace_module.init_workspace(self.source, self.workspace)
        services = TrackingServices(active=(service_name,))
        original_update = workspace_module.update_rotation
        failed = False

        def injected(journal_path, journal, phase, **extra):
            nonlocal failed
            if phase == failing_phase and not failed:
                failed = True
                raise OSError(f"injected {phase}")
            return original_update(journal_path, journal, phase, **extra)

        with mock.patch.object(workspace_module, "update_rotation", side_effect=injected):
            with self.assertRaisesRegex(OSError, failing_phase):
                workspace_module.rotate_workspace(
                    self.source, self.workspace, services=services,
                    process_checker=lambda _path: [],
                )
        journal_path = next(workspace_module.rotation_root(self.workspace).glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual("SERVICES_RECOVERY_REQUIRED", journal["phase"])
        self.assertEqual("NEW", workspace_module.workspace_identity(self.workspace, journal))
        self.assertEqual("OLD", workspace_module.workspace_identity(Path(journal["archive"]), journal))

    def test_recovery_rollback_holds_exclusive_lock_against_new_cooperative_writer(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "archive"
        os.replace(self.workspace, archive)
        workspace_module.init_workspace(self.source, self.workspace)
        rotation_id, _, _ = self.rotation_journal(
            phase="NEW_INITIALIZED", archive=archive, old_inventory=old_inventory,
        )
        writer_blocked = []

        def check_writer(_path):
            def try_writer():
                try:
                    with workspace_module.workspace_lock(self.workspace, blocking=False):
                        writer_blocked.append(False)
                except RuntimeError:
                    writer_blocked.append(True)
            thread = threading.Thread(target=try_writer)
            thread.start()
            thread.join(timeout=5)
            return []

        workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False,
            services=TrackingServices(), process_checker=check_writer,
        )
        self.assertEqual([True], writer_blocked)

    def test_unfinished_rotation_blocks_new_rotation_and_requires_explicit_recovery(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "archived-workspace"
        os.replace(self.workspace, archive)
        workspace_module.init_workspace(self.source, self.workspace)
        rotation_id = "interrupted-test"
        journal_path = workspace_module.rotation_root(self.workspace) / f"{rotation_id}.json"
        journal = {
            "schema_version": 1,
            "rotation_id": rotation_id,
            "workspace": str(self.workspace),
            "archive": str(archive),
            "failed_candidate": str(self.root / "failed-candidate"),
            "old_checkpoint": self.git("rev-parse", "HEAD"),
            "new_checkpoint": self.git("rev-parse", "HEAD"),
            "old_inventory": old_inventory,
            "services_before": {},
        }
        workspace_module.update_rotation(journal_path, journal, "NEW_INITIALIZED")

        with self.assertRaisesRegex(workspace_module.WorkspaceError, "requires recovery"):
            workspace_module.rotate_workspace(
                self.source, self.workspace, process_checker=lambda _path: [],
            )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False,
            process_checker=lambda _path: [],
        )
        self.assertEqual("ROLLED_BACK", result["rotation"]["phase"])
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))

    def test_recovery_refuses_to_move_candidate_when_archive_hash_changed(self):
        workspace_module.init_workspace(self.source, self.workspace)
        archive = self.root / "archived-workspace"
        shutil.copytree(self.workspace, archive)
        expected = workspace_module.workspace_inventory(archive)
        (archive / "changed.txt").write_text("changed\n", encoding="utf-8")
        rotation_id = "damaged-archive"
        journal_path = workspace_module.rotation_root(self.workspace) / f"{rotation_id}.json"
        journal = {
            "schema_version": 1,
            "rotation_id": rotation_id,
            "workspace": str(self.workspace),
            "archive": str(archive),
            "failed_candidate": str(self.root / "failed-candidate"),
            "old_checkpoint": self.git("rev-parse", "HEAD"),
            "new_checkpoint": self.git("rev-parse", "HEAD"),
            "old_inventory": expected,
            "services_before": {},
        }
        workspace_module.update_rotation(journal_path, journal, "OLD_ARCHIVED")
        candidate_before = workspace_module.workspace_inventory(self.workspace)

        with self.assertRaisesRegex(workspace_module.WorkspaceError, "archive changed"):
            workspace_module.recover_rotation(
                self.workspace, rotation_id, complete=False,
                process_checker=lambda _path: [],
            )
        self.assertEqual(candidate_before, workspace_module.workspace_inventory(self.workspace))


if __name__ == "__main__":
    unittest.main()
