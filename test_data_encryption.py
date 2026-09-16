from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mikrofix_data_encryption import (
    DataEncryptionError,
    is_gocryptfs_mount,
    mount_encrypted_data,
    require_encrypted_data_ready,
)


class DataEncryptionTests(unittest.TestCase):
    def test_mount_detection_requires_exact_gocryptfs_mountpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw).resolve()
            with patch("mikrofix_data_encryption._mount_info", return_value=(str(path), "fuse.gocryptfs")):
                self.assertTrue(is_gocryptfs_mount(path))
            with patch("mikrofix_data_encryption._mount_info", return_value=("/home", "ext4")):
                self.assertFalse(is_gocryptfs_mount(path))

    def test_enabled_encryption_fails_closed_when_mount_is_missing(self):
        settings = {"data_encryption": {"enabled": True, "provider": "gocryptfs"}}
        with patch("mikrofix_data_encryption.is_gocryptfs_mount", return_value=False):
            with self.assertRaisesRegex(DataEncryptionError, "ne bo ustvaril navadne baze"):
                require_encrypted_data_ready(settings, "/tmp/not-mounted")

    def test_mount_refuses_nonempty_plain_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            cipher = base / "cipher"
            plain = base / "plain"
            cipher.mkdir()
            plain.mkdir()
            (cipher / "gocryptfs.conf").write_text("test", encoding="utf-8")
            (plain / "must-not-overwrite").write_text("data", encoding="utf-8")
            with patch("mikrofix_data_encryption.is_gocryptfs_mount", return_value=False):
                with self.assertRaisesRegex(DataEncryptionError, "ni prazna"):
                    mount_encrypted_data(cipher, plain, "long-enough-test-passphrase")


if __name__ == "__main__":
    unittest.main()
