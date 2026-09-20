from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from fastapi import HTTPException

from tools.local_executor import server


class LocalExecutorSecurityTests(unittest.TestCase):
    def test_safe_path_rejects_escape_and_sensitive_files(self):
        with self.assertRaises(HTTPException) as escape:
            server.safe_path("../../etc/passwd")
        self.assertEqual(escape.exception.status_code, 403)

        with self.assertRaises(HTTPException) as certificate:
            server.safe_path("certs/example.p12")
        self.assertEqual(certificate.exception.status_code, 403)

    def test_patch_rejects_binary_and_deletion(self):
        with self.assertRaises(HTTPException):
            server.validate_patch("GIT binary patch\n")

        deletion = """diff --git a/example.py b/example.py
deleted file mode 100644
--- a/example.py
+++ /dev/null
@@ -1 +0,0 @@
-print('x')
"""
        with self.assertRaises(HTTPException):
            server.validate_patch(deletion)

    def test_patch_accepts_bounded_python_edit(self):
        patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-print('old')
+print('new')
"""
        self.assertEqual(server.validate_patch(patch), ["example.py"])

    def test_patch_manifest_rejects_file_outside_approved_plan(self):
        patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-print('old')
+print('new')
"""
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "allowed.json"
            manifest.write_text(
                json.dumps({"allowed_files": ["other.py"]}), encoding="utf-8"
            )
            with mock.patch.object(
                server, "ALLOWED_FILES_MANIFEST", str(manifest)
            ):
                with self.assertRaises(HTTPException) as error:
                    server.validate_patch(patch)
        self.assertEqual(error.exception.status_code, 403)

    def test_patch_manifest_accepts_exact_approved_file(self):
        patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-print('old')
+print('new')
"""
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "allowed.json"
            manifest.write_text(
                json.dumps({"allowed_files": ["example.py"]}), encoding="utf-8"
            )
            with mock.patch.object(
                server, "ALLOWED_FILES_MANIFEST", str(manifest)
            ):
                self.assertEqual(server.validate_patch(patch), ["example.py"])

    def test_create_file_accepts_only_exact_approved_new_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "allowed.json"
            manifest.write_text(
                json.dumps({"allowed_files": ["docs/local_agents/OWNER_WORKFLOW.md"]}),
                encoding="utf-8",
            )
            request = server.CreateRequest(
                path="docs/local_agents/OWNER_WORKFLOW.md",
                content="# Owner workflow\n",
                summary="create approved workflow documentation",
            )
            with (
                mock.patch.object(server, "PROJECT_ROOT", root),
                mock.patch.object(server, "ALLOWED_FILES_MANIFEST", str(manifest)),
                mock.patch.object(
                    server, "require_lease", return_value={"lease_id": "test"}
                ),
                mock.patch.object(server, "_audit"),
            ):
                result = server.project_create(request)
            target = root / "docs/local_agents/OWNER_WORKFLOW.md"
            self.assertTrue(result["created"])
            self.assertEqual("# Owner workflow\n", target.read_text(encoding="utf-8"))

    def test_create_file_rejects_outside_manifest_and_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "allowed.json"
            manifest.write_text(
                json.dumps({"allowed_files": ["approved.md"]}), encoding="utf-8"
            )
            with (
                mock.patch.object(server, "PROJECT_ROOT", root),
                mock.patch.object(server, "ALLOWED_FILES_MANIFEST", str(manifest)),
                mock.patch.object(
                    server, "require_lease", return_value={"lease_id": "test"}
                ),
            ):
                with self.assertRaises(HTTPException) as outside:
                    server.project_create(server.CreateRequest(
                        path="other.md", content="no\n", summary="reject other path"
                    ))
                self.assertEqual(403, outside.exception.status_code)
                (root / "approved.md").write_text("keep\n", encoding="utf-8")
                with self.assertRaises(HTTPException) as existing:
                    server.project_create(server.CreateRequest(
                        path="approved.md", content="replace\n", summary="reject overwrite"
                    ))
                self.assertEqual(409, existing.exception.status_code)
                self.assertEqual("keep\n", (root / "approved.md").read_text(encoding="utf-8"))

    def test_exact_replace_requires_one_match_and_preserves_other_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.py"
            target.write_text("before\nold block\nafter\n", encoding="utf-8")
            request = server.ReplaceRequest(
                path="example.py", old="old block", new="new block",
                summary="replace one block",
            )
            with (
                mock.patch.object(server, "PROJECT_ROOT", root),
                mock.patch.object(server, "ALLOWED_FILES_MANIFEST", ""),
                mock.patch.object(
                    server, "require_lease", return_value={"lease_id": "test"}
                ),
                mock.patch.object(server, "_audit"),
            ):
                result = server.project_replace(request)
            self.assertTrue(result["replaced"])
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "before\nnew block\nafter\n",
            )

    def test_exact_replace_rejects_ambiguous_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.py"
            target.write_text("same\nsame\n", encoding="utf-8")
            request = server.ReplaceRequest(
                path="example.py", old="same", new="new",
                summary="ambiguous replacement",
            )
            with (
                mock.patch.object(server, "PROJECT_ROOT", root),
                mock.patch.object(server, "ALLOWED_FILES_MANIFEST", ""),
                mock.patch.object(
                    server, "require_lease", return_value={"lease_id": "test"}
                ),
            ):
                with self.assertRaises(HTTPException) as error:
                    server.project_replace(request)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")

    def test_expired_lease_is_rejected_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            lease_file.write_text(
                json.dumps({"expires_at": time.time() - 1}), encoding="utf-8"
            )
            with mock.patch.object(server, "LEASE_FILE", lease_file):
                with self.assertRaises(HTTPException) as error:
                    server.require_lease()
            self.assertEqual(error.exception.status_code, 423)
            self.assertFalse(lease_file.exists())

    def test_run_scoped_lease_must_match_executor_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            lease_file.write_text(
                json.dumps({
                    "expires_at": time.time() + 60,
                    "run_id": "other-run",
                    "project_root": str(server.PROJECT_ROOT),
                    "plan_sha256": "plan",
                    "allowed_files_sha256": "allowed",
                }),
                encoding="utf-8",
            )
            with (
                mock.patch.object(server, "LEASE_FILE", lease_file),
                mock.patch.object(server, "EXPECTED_RUN_ID", "expected-run"),
                mock.patch.object(server, "EXPECTED_PLAN_SHA256", "plan"),
                mock.patch.object(
                    server, "EXPECTED_ALLOWED_FILES_SHA256", "allowed"
                ),
            ):
                with self.assertRaises(HTTPException) as error:
                    server.require_lease()
            self.assertEqual(error.exception.status_code, 423)

    def test_matching_run_scoped_lease_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            lease = {
                "expires_at": time.time() + 60,
                "run_id": "expected-run",
                "project_root": str(server.PROJECT_ROOT),
                "plan_sha256": "plan",
                "allowed_files_sha256": "allowed",
            }
            lease_file.write_text(json.dumps(lease), encoding="utf-8")
            with (
                mock.patch.object(server, "LEASE_FILE", lease_file),
                mock.patch.object(server, "EXPECTED_RUN_ID", "expected-run"),
                mock.patch.object(server, "EXPECTED_PLAN_SHA256", "plan"),
                mock.patch.object(
                    server, "EXPECTED_ALLOWED_FILES_SHA256", "allowed"
                ),
            ):
                self.assertEqual(server.require_lease(), lease)

    def test_authentication_rejects_wrong_token(self):
        with self.assertRaises(HTTPException) as error:
            server.authenticate("Bearer wrong")
        self.assertEqual(error.exception.status_code, 401)

    def test_supervised_run_rejects_system_command_before_lease_or_shell(self):
        request = server.CommandRequest(
            command="true", purpose="must remain disabled"
        )
        with (
            mock.patch.object(server, "EXPECTED_RUN_ID", "supervised-run"),
            mock.patch.object(server, "require_lease") as require_lease,
            mock.patch.object(server.subprocess, "run") as run,
        ):
            with self.assertRaises(HTTPException) as error:
                server.system_command(request)
        self.assertEqual(error.exception.status_code, 403)
        require_lease.assert_not_called()
        run.assert_not_called()

    def test_supervised_run_rejects_executor_check_before_lease_or_python(self):
        request = server.CheckRequest(profile="syntax_changed_python")
        with (
            mock.patch.object(server, "EXPECTED_RUN_ID", "supervised-run"),
            mock.patch.object(server, "require_lease") as require_lease,
            mock.patch.object(server, "run_command") as run_command,
        ):
            with self.assertRaises(HTTPException) as error:
                server.project_run_check(request)
        self.assertEqual(error.exception.status_code, 403)
        require_lease.assert_not_called()
        run_command.assert_not_called()

    def test_supervised_run_rejects_desktop_action_before_lease_or_xdotool(self):
        request = server.DesktopActionRequest(action="press_key", key="F1")
        with (
            mock.patch.object(server, "EXPECTED_RUN_ID", "supervised-run"),
            mock.patch.object(server, "require_lease") as require_lease,
            mock.patch.object(server, "_screen_geometry") as geometry,
            mock.patch.object(server.subprocess, "run") as run,
        ):
            with self.assertRaises(HTTPException) as error:
                server.desktop_action(request)
        self.assertEqual(error.exception.status_code, 403)
        require_lease.assert_not_called()
        geometry.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
