import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mikrofix_central_cashier_bootstrap import (
    CentralCashierBootstrapError,
    build_central_cashier,
)
from mikrofix_v30_pos import PosError
from mikrofix_v30_pos_ui import PosWindow


class CentralCashierBootstrapTests(unittest.TestCase):
    def test_empty_url_keeps_explicit_legacy_mode(self):
        self.assertIsNone(build_central_cashier(
            settings={"environment": "TEST", "database": {"server_url": ""}},
            office_db=":memory:", actor_id="blagajnik", device_id="POS-1",
            token="",
        ))

    def test_configured_url_without_secret_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                CentralCashierBootstrapError, "ne bo prešla na lokalno"
            ):
                build_central_cashier(
                    settings={
                        "environment": "TEST",
                        "database": {"server_url": "http://127.0.0.1:8765"},
                    },
                    office_db=":memory:", actor_id="blagajnik", device_id="POS-1",
                )

    def test_development_configuration_builds_and_closes_durable_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "office.sqlite"
            handle = build_central_cashier(
                settings={
                    "environment": "TEST",
                    "database": {"server_url": "http://127.0.0.1:8765/"},
                },
                office_db=database, actor_id="blagajnik", device_id="POS-1",
                token="development-token-with-24-chars",
            )
            self.assertIsNotNone(handle)
            self.assertEqual(handle.workflow.transport.base_url, "http://127.0.0.1:8765")
            self.assertEqual(handle.workflow.journal.device_id, "POS-1")
            handle.close()

    def test_production_never_uses_shared_development_secret(self):
        with self.assertRaisesRegex(CentralCashierBootstrapError, "Produkcijska"):
            build_central_cashier(
                settings={
                    "environment": "PRODUCTION",
                    "database": {"server_url": "https://central.example"},
                },
                office_db=":memory:", actor_id="blagajnik", device_id="POS-1",
                token="development-token-with-24-chars",
            )

    def test_central_window_refuses_legacy_local_draft_without_mutating_it(self):
        window = PosWindow.__new__(PosWindow)
        window.central_mode = True
        window.office_db = Path("unused.sqlite")
        window.authenticated_username = "blagajnik"
        window.runtime = SimpleNamespace(electronic_device="PIS")
        stored = {"payload": {"lines": [{"name": "stari lokalni osnutek"}]}}
        with (
            patch("mikrofix_v30_pos_ui.latest_pending_print", return_value=None),
            patch("mikrofix_v30_pos_ui.load_active_pos_draft", return_value=stored),
        ):
            with self.assertRaisesRegex(PosError, "lokalnega načina"):
                window._assert_recovery_mode_compatible()

    def test_central_window_refuses_pending_local_original_print(self):
        window = PosWindow.__new__(PosWindow)
        window.central_mode = True
        window.office_db = Path("unused.sqlite")
        window.authenticated_username = "blagajnik"
        window.runtime = SimpleNamespace(electronic_device="PIS")
        with patch(
            "mikrofix_v30_pos_ui.latest_pending_print", return_value={"id": 17}
        ):
            with self.assertRaisesRegex(PosError, "čaka na prvotni izpis"):
                window._assert_recovery_mode_compatible()


if __name__ == "__main__":
    unittest.main()
