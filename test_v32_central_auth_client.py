import unittest
from unittest.mock import Mock, patch

from mikrofix_central_auth_client import RequestsCentralAuthClient


class CentralAuthClientTests(unittest.TestCase):
    @patch("requests.post")
    def test_login_returns_signed_operation_actor_metadata(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "access_token": "session-token",
            "expires_at": "2026-09-12T12:00:00+00:00",
            "user_id": "657e3890-a3e6-4fee-a493-1074be3fd4ac",
            "user_version": 4,
            "username": "serviser-a",
            "display_name": "Serviser A",
            "role": "SERVICE",
            "workstation_code": "SERVICE-PC-1",
            "application_type": "SERVICE",
            "permissions": ["MODULE_SERVICE"],
            "must_change_pin": False,
        }
        post.return_value = response

        session = RequestsCentralAuthClient(
            "https://central.example/", timeout_seconds=9
        ).login(
            username="serviser-a", pin="246810",
            workstation_code="SERVICE-PC-1", workstation_secret="x" * 48,
            application_type="SERVICE",
        )

        self.assertEqual(session.user_version, 4)
        self.assertEqual(session.user_id, "657e3890-a3e6-4fee-a493-1074be3fd4ac")
        self.assertEqual(session.permissions, frozenset({"MODULE_SERVICE"}))
        post.assert_called_once_with(
            "https://central.example/v1/auth/login",
            json={
                "username": "serviser-a", "pin": "246810",
                "workstation_code": "SERVICE-PC-1",
                "workstation_secret": "x" * 48,
                "application_type": "SERVICE",
            },
            timeout=9.0,
        )


if __name__ == "__main__":
    unittest.main()
