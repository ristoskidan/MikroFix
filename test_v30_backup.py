import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_v30_backup import (
    BackupError,
    backup_is_older_than,
    create_encrypted_backup,
    extract_verified_backup,
    inspect_backup,
    rotate_daily_backups,
    validate_target,
)


class V30BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.target = self.root / "external"
        (self.data / "db").mkdir(parents=True)
        (self.data / "office" / "documents").mkdir(parents=True)
        connection = sqlite3.connect(self.data / "db" / "mikrofix.db")
        connection.execute("CREATE TABLE test(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO test(value) VALUES('sledljivo')")
        connection.commit()
        connection.close()
        (self.data / "office" / "documents" / "document.pdf").write_bytes(
            b"%PDF-1.4\nMikroFIX\n"
        )
        self.settings = self.root / "mikrofix_settings.json"
        self.settings.write_text('{"environment":"TEST"}', encoding="utf-8")
        self.password = "MikroFIX-obnova-2026!"

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return create_encrypted_backup(
            data_dir=self.data, target_dir=self.target, passphrase=self.password,
            app_version="30-test", schema_version=30, extra_files=[self.settings],
            require_distinct_volume=False,
        )

    def test_encrypted_daily_backup_is_verified_and_only_once_per_day(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.target.glob("mikrofix_daily_*.mfbak"))), 1)
        self.assertNotIn(b"sledljivo", first.read_bytes())
        info = inspect_backup(first, self.password)
        self.assertEqual(info.app_version, "30-test")
        self.assertEqual(info.schema_version, 30)
        self.assertEqual(info.kind, "DAILY")
        self.assertGreaterEqual(info.file_count, 3)
        self.assertFalse(backup_is_older_than(self.target, 24))

    def test_wrong_password_or_tampering_is_blocked(self):
        package = self.create()
        with self.assertRaisesRegex(BackupError, "Napačno obnovitveno geslo"):
            inspect_backup(package, "To-je-napacno-geslo!")
        damaged = self.target / "damaged.mfbak"
        raw = bytearray(package.read_bytes())
        raw[-1] ^= 0x01
        damaged.write_bytes(raw)
        with self.assertRaisesRegex(BackupError, "poškodovana"):
            inspect_backup(damaged, self.password)

    def test_verified_extract_keeps_database_and_configuration(self):
        package = self.create()
        destination = self.root / "restore"
        info = extract_verified_backup(
            package, self.password, destination, expected_schema_version=30
        )
        self.assertEqual(info.kind, "DAILY")
        connection = sqlite3.connect(destination / "data" / "db" / "mikrofix.db")
        value = connection.execute("SELECT value FROM test").fetchone()[0]
        connection.close()
        self.assertEqual(value, "sledljivo")
        self.assertTrue((destination / "config" / "mikrofix_settings.json").is_file())

    def test_same_volume_is_not_valid_production_target(self):
        with self.assertRaisesRegex(BackupError, "drugem fizičnem nosilcu"):
            validate_target(self.data, self.target, require_distinct_volume=True)

    def test_rotation_keeps_latest_thirty_daily_files(self):
        self.target.mkdir()
        for day in range(1, 32):
            (self.target / f"mikrofix_daily_2026-07-{day:02d}_120000.mfbak").write_bytes(b"x")
        removed = rotate_daily_backups(self.target, keep=30)
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(list(self.target.glob("mikrofix_daily_*.mfbak"))), 30)
        self.assertFalse((self.target / "mikrofix_daily_2026-07-01_120000.mfbak").exists())


if __name__ == "__main__":
    unittest.main()
