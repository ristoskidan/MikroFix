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

    def test_authentication_rejects_wrong_token(self):
        with self.assertRaises(HTTPException) as error:
            server.authenticate("Bearer wrong")
        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
