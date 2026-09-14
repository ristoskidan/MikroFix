import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from mikrofix_central_auth_client import (
    CentralAuthRejected,
    CentralAuthUnavailable,
    CentralSession,
)
from mikrofix_service_identity import (
    OfflineIdentityUnavailable,
    ServiceCentralIdentityManager,
    ServiceIdentityCache,
)


def central_session(**changes):
    values = {
        "access_token": "memory-only-token",
        "expires_at": "2026-09-14T12:00:00+00:00",
        "user_id": str(uuid4()),
        "user_version": 3,
        "username": "serviser-a",
        "display_name": "Serviser A",
        "role": "SERVICE",
        "workstation_code": "SERVICE-PC-1",
        "application_type": "SERVICE",
        "permissions": frozenset({"MODULE_SERVICE"}),
        "must_change_pin": False,
    }
    values.update(changes)
    return CentralSession(**values)


class ServiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp.name) / "identity.sqlite"
        self.cache = ServiceIdentityCache(self.cache_path)
        self.client = Mock()
        self.manager = ServiceCentralIdentityManager(
            self.client, self.cache,
            workstation_code="SERVICE-PC-1", workstation_secret="secret",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_online_login_caches_only_actor_metadata(self):
        session = central_session()
        self.client.login.return_value = session

        identity = self.manager.login(username="Serviser-A", pin="246810")

        self.assertTrue(identity.online)
        self.assertEqual(identity.user_id, session.user_id)
        self.assertEqual(self.manager.access_token, "memory-only-token")
        with sqlite3.connect(self.cache_path) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(service_central_identities)"
                )
            }
            stored = " ".join(
                str(value) for row in connection.execute(
                    "SELECT * FROM service_central_identities"
                ) for value in row
            )
        self.assertNotIn("access_token", columns)
        self.assertNotIn("pin", columns)
        self.assertNotIn("secret", columns)
        self.assertNotIn("memory-only-token", stored)
        self.assertNotIn("246810", stored)

    def test_network_failure_uses_previously_verified_identity(self):
        session = central_session()
        self.cache.remember(session)
        self.client.login.side_effect = CentralAuthUnavailable("offline")

        identity = self.manager.login(username="serviser-a", pin="246810")

        self.assertFalse(identity.online)
        self.assertEqual(identity.user_version, 3)
        self.assertEqual(self.manager.access_token, "")

    def test_first_offline_login_is_refused_without_cached_identity(self):
        self.client.login.side_effect = CentralAuthUnavailable("offline")

        with self.assertRaises(OfflineIdentityUnavailable):
            self.manager.login(username="serviser-a", pin="246810")

    def test_explicit_rejection_never_falls_back_to_cached_identity(self):
        self.cache.remember(central_session())
        self.client.login.side_effect = CentralAuthRejected(
            "User is disabled.", status_code=401
        )

        with self.assertRaises(CentralAuthRejected):
            self.manager.login(username="serviser-a", pin="246810")

    def test_end_session_forgets_token_even_when_server_is_offline(self):
        self.client.login.return_value = central_session()
        self.manager.login(username="serviser-a", pin="246810")
        self.client.logout.side_effect = CentralAuthUnavailable("offline")

        self.assertFalse(self.manager.end_session())
        self.assertEqual(self.manager.access_token, "")
        self.assertIsNone(self.manager.identity)

    def test_mandatory_pin_change_relogs_and_caches_new_user_version(self):
        first = central_session(must_change_pin=True, user_version=3)
        second = central_session(
            access_token="new-token", must_change_pin=False, user_version=4
        )
        self.client.login.side_effect = [first, second]
        self.manager.login(username="serviser-a", pin="246810")

        identity = self.manager.change_pin_and_relogin(
            username="serviser-a", current_pin="246810", new_pin="864209"
        )

        self.client.change_pin.assert_called_once_with(
            "memory-only-token", current_pin="246810", new_pin="864209"
        )
        self.assertFalse(self.manager.must_change_pin)
        self.assertEqual(self.manager.access_token, "new-token")
        self.assertEqual(identity.user_version, 4)
        self.assertEqual(self.cache.get("serviser-a").user_version, 4)


if __name__ == "__main__":
    unittest.main()
