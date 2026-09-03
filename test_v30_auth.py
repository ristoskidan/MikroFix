import tempfile
import unittest
from pathlib import Path

from mikrofix_auth import (
    ALL_PERMISSIONS,
    AuthStore,
    PERMISSION_CASHIER,
    PERMISSION_OFFICE,
    PERMISSION_SERVICE,
    required_office_entry_permission,
    validate_pin_format,
)


class V30AuthTests(unittest.TestCase):
    def make_store(self, folder: str) -> AuthStore:
        store = AuthStore(Path(folder) / "auth.db")
        store.init()
        return store

    def test_pin_length_and_trivial_values(self):
        self.assertFalse(validate_pin_format("1234"))
        self.assertFalse(validate_pin_format("123456"))
        self.assertFalse(validate_pin_format("111111"))
        self.assertTrue(validate_pin_format("482951"))
        self.assertTrue(validate_pin_format("48295173016428"))
        self.assertFalse(validate_pin_format("482951730164287"))

    def test_direct_office_entry_requires_the_selected_module_permission(self):
        self.assertEqual(
            required_office_entry_permission(pos_requested=False),
            PERMISSION_OFFICE,
        )
        self.assertEqual(
            required_office_entry_permission(pos_requested=True),
            PERMISSION_CASHIER,
        )

    def test_new_user_pin_is_temporary_and_permissions_are_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.make_store(folder)
            store.set_pin("482951")
            store.create_user(
                "servis2",
                "Serviser 2",
                "SERVICE",
                pin="729405",
            )
            login = store.verify_pin("729405", "servis2")
            self.assertTrue(login.ok)
            self.assertTrue(login.must_change_pin)
            self.assertTrue(
                store.has_permission("servis2", PERMISSION_SERVICE)
            )
            self.assertFalse(
                store.has_permission("servis2", PERMISSION_CASHIER)
            )

            store.set_pin(
                "850274",
                "servis2",
                event_type="PIN_CHANGED_FIRST_LOGIN",
                changed_by="servis2",
            )
            changed_login = store.verify_pin("850274", "servis2")
            self.assertTrue(changed_login.ok)
            self.assertFalse(changed_login.must_change_pin)

    def test_permissions_can_be_changed_but_daniel_keeps_all(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.make_store(folder)
            store.create_user(
                "blagajna",
                "Blagajna",
                "CASHIER",
                pin="640275",
            )
            store.set_permissions(
                "blagajna",
                {PERMISSION_CASHIER, PERMISSION_SERVICE},
            )
            self.assertEqual(
                store.permissions_for("blagajna"),
                {PERMISSION_CASHIER, PERMISSION_SERVICE},
            )
            with self.assertRaises(ValueError):
                store.set_permissions("daniel", {PERMISSION_SERVICE})
            self.assertEqual(store.permissions_for("daniel"), ALL_PERMISSIONS)

    def test_admin_recovery_hint_secret_and_lockout(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.make_store(folder)
            store.set_pin("482951")
            store.set_recovery(
                "Ime prve delovne mize",
                "Miza-modra-2026",
            )
            self.assertTrue(store.has_recovery())
            self.assertEqual(
                store.recovery_hint(), "Ime prve delovne mize"
            )
            self.assertTrue(store.verify_recovery("Miza-modra-2026").ok)
            for attempt in range(4):
                result = store.verify_recovery(f"napacno-{attempt}")
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "WRONG_SECRET")
            locked = store.verify_recovery("se-vedno-napacno")
            self.assertFalse(locked.ok)
            self.assertEqual(locked.reason, "LOCKED")
            self.assertIsNotNone(locked.locked_until)
            self.assertEqual(
                store.verify_recovery("Miza-modra-2026").reason,
                "LOCKED",
            )

            raw_database = (Path(folder) / "auth.db").read_bytes()
            self.assertNotIn(b"Miza-modra-2026", raw_database)


if __name__ == "__main__":
    unittest.main()
