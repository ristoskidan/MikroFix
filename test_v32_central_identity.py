import unittest
from unittest.mock import Mock
from uuid import uuid4

from mikrofix_central_auth_client import CentralSession
from mikrofix_central_identity import (
    CentralApplicationIdentityError,
    CentralApplicationIdentityManager,
)


def central_session(**changes):
    values = {
        "access_token": "memory-only-token",
        "expires_at": "2026-09-14T12:00:00+00:00",
        "user_id": str(uuid4()), "user_version": 2,
        "username": "office-a", "display_name": "Office A", "role": "OFFICE",
        "workstation_code": "OFFICE-PC-A", "application_type": "OFFICE",
        "permissions": frozenset({"MODULE_OFFICE"}), "must_change_pin": False,
    }
    values.update(changes)
    return CentralSession(**values)


class CentralApplicationIdentityTests(unittest.TestCase):
    def manager(self, application="OFFICE", code="OFFICE-PC-A"):
        self.client = Mock()
        return CentralApplicationIdentityManager(
            self.client, workstation_code=code, workstation_secret="secret",
            application_type=application,
        )

    def test_office_login_keeps_bearer_only_in_memory(self):
        manager = self.manager()
        self.client.login.return_value = central_session()

        identity = manager.login(username="OFFICE-A", pin="246810")

        self.assertEqual(identity.application_type, "OFFICE")
        self.assertEqual(manager.access_token, "memory-only-token")
        self.client.login.assert_called_once_with(
            username="office-a", pin="246810", workstation_code="OFFICE-PC-A",
            workstation_secret="secret", application_type="OFFICE",
        )

    def test_cashier_rejects_office_session(self):
        manager = self.manager("CASHIER", "CASHIER-PC-A")
        self.client.login.return_value = central_session()

        with self.assertRaisesRegex(CentralApplicationIdentityError, "drug program"):
            manager.login(username="office-a", pin="246810")
        self.client.logout.assert_called_once_with("memory-only-token")
        self.assertIsNone(manager.session)

    def test_workstation_mismatch_is_rejected(self):
        manager = self.manager()
        self.client.login.return_value = central_session(
            workstation_code="OFFICE-PC-B"
        )

        with self.assertRaisesRegex(CentralApplicationIdentityError, "drugo delovno"):
            manager.login(username="office-a", pin="246810")

    def test_missing_required_permission_is_rejected_and_revoked(self):
        manager = self.manager()
        self.client.login.return_value = central_session(permissions=frozenset())

        with self.assertRaisesRegex(CentralApplicationIdentityError, "nima pravice"):
            manager.login(username="office-a", pin="246810")

        self.client.logout.assert_called_once_with("memory-only-token")
        self.assertEqual(manager.access_token, "")

    def test_end_session_forgets_token_when_logout_is_offline(self):
        manager = self.manager()
        self.client.login.return_value = central_session()
        manager.login(username="office-a", pin="246810")
        self.client.logout.side_effect = RuntimeError("offline")

        self.assertFalse(manager.end_session())
        self.assertEqual(manager.access_token, "")
        self.assertIsNone(manager.session)


if __name__ == "__main__":
    unittest.main()
