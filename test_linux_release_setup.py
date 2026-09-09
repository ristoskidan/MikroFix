import json
import tempfile
import unittest
from pathlib import Path

from formatiraj_kingston_ext4 import backup_payload_hashes


APP_DIR = Path(__file__).resolve().parent


class LinuxReleaseSetupTests(unittest.TestCase):
    def test_kingston_payload_contains_only_dedicated_backup_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "old-source-code.txt").write_text("keep locally", encoding="utf-8")
            backup = source / "MikroFix_Backup"
            backup.mkdir()
            (backup / "daily.mfbak").write_bytes(b"encrypted")

            self.assertEqual(backup_payload_hashes(source).keys(), {"daily.mfbak"})

    def test_empty_backup_payload_blocks_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "MikroFix_Backup").mkdir()
            with self.assertRaises(RuntimeError):
                backup_payload_hashes(Path(temporary))

    def test_manual_sandbox_is_isolated_from_production(self):
        production = json.loads((APP_DIR / "mikrofix_settings.json").read_text(encoding="utf-8"))
        sandbox = json.loads((APP_DIR / "mikrofix_settings_sandbox.json").read_text(encoding="utf-8"))

        self.assertEqual(production["environment"], "PRODUCTION")
        self.assertTrue(production["data_encryption"]["enabled"])
        self.assertEqual(sandbox["environment"], "TEST")
        self.assertFalse(sandbox["furs"]["enabled"])
        self.assertEqual(sandbox["furs"]["environment"], "TEST")
        self.assertFalse(sandbox["data_encryption"]["enabled"])
        self.assertNotEqual(
            Path(production["database"]["data_dir"]).expanduser().resolve(),
            Path(sandbox["database"]["data_dir"]).expanduser().resolve(),
        )


if __name__ == "__main__":
    unittest.main()
