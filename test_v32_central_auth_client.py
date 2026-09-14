import unittest
from unittest.mock import Mock, patch

import requests

from mikrofix_central_auth_client import (
    CentralAuthProtocolError,
    CentralAuthRejected,
    CentralAuthUnavailable,
    RequestsCentralAuthClient,
)


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

    @patch("requests.post")
    def test_explicit_login_rejection_is_not_reported_as_offline(self, post):
        response = Mock(status_code=403)
        response.json.return_value = {"detail": "Workstation is disabled."}
        post.return_value = response

        with self.assertRaises(CentralAuthRejected) as caught:
            RequestsCentralAuthClient("https://central.example").login(
                username="serviser-a", pin="246810",
                workstation_code="SERVICE-PC-1", workstation_secret="x" * 48,
                application_type="SERVICE",
            )

        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("disabled", str(caught.exception))

    @patch("requests.post", side_effect=requests.ConnectionError("offline"))
    def test_network_failure_has_distinct_type(self, _post):
        with self.assertRaises(CentralAuthUnavailable):
            RequestsCentralAuthClient("https://central.example").login(
                username="serviser-a", pin="246810",
                workstation_code="SERVICE-PC-1", workstation_secret="x" * 48,
                application_type="SERVICE",
            )

    @patch("requests.post")
    def test_malformed_success_is_not_treated_as_offline(self, post):
        post.return_value = Mock(status_code=200, json=Mock(return_value={}))

        with self.assertRaises(CentralAuthProtocolError):
            RequestsCentralAuthClient("https://central.example").login(
                username="serviser-a", pin="246810",
                workstation_code="SERVICE-PC-1", workstation_secret="x" * 48,
                application_type="SERVICE",
            )

    @patch("requests.post")
    def test_logout_revokes_bearer_session(self, post):
        post.return_value = Mock(status_code=204)

        RequestsCentralAuthClient(
            "https://central.example/", timeout_seconds=7
        ).logout("session-token")

        post.assert_called_once_with(
            "https://central.example/v1/auth/logout",
            headers={"Authorization": "Bearer session-token"},
            timeout=7.0,
        )

    @patch("requests.post")
    def test_change_pin_uses_current_bearer_session(self, post):
        post.return_value = Mock(status_code=204)

        RequestsCentralAuthClient(
            "https://central.example/", timeout_seconds=8
        ).change_pin(
            "session-token", current_pin="246810", new_pin="864209"
        )

        post.assert_called_once_with(
            "https://central.example/v1/auth/change-pin",
            json={"current_pin": "246810", "new_pin": "864209"},
            headers={"Authorization": "Bearer session-token"},
            timeout=8.0,
        )


if __name__ == "__main__":
    unittest.main()
