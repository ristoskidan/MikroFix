import unittest

from mikrofix_central_desktop import (
    CentralDesktopConfigurationError,
    central_desktop_configuration,
)
from zazeni_mikrofix import APPLICATION_TARGETS


class CentralDesktopConfigurationTests(unittest.TestCase):
    def test_office_requires_central_server(self):
        with self.assertRaisesRegex(CentralDesktopConfigurationError, "ni nastavljen"):
            central_desktop_configuration(
                {"database": {"server_url": ""}}, "OFFICE", environment={}
            )

    def test_cashier_requires_its_own_registered_identity(self):
        with self.assertRaisesRegex(
            CentralDesktopConfigurationError,
            "MIKROFIX_CASHIER_WORKSTATION_SECRET",
        ):
            central_desktop_configuration(
                {"database": {"server_url": "https://central.example"}},
                "CASHIER",
                environment={"MIKROFIX_CASHIER_WORKSTATION_CODE": "MFD-PIS-CASHIER"},
            )

    def test_office_configuration_is_explicit_and_normalized(self):
        configuration = central_desktop_configuration(
            {"database": {"server_url": "https://central.example/"}},
            "office",
            environment={
                "MIKROFIX_OFFICE_WORKSTATION_CODE": "mfd-pis-office",
                "MIKROFIX_OFFICE_WORKSTATION_SECRET": "secret",
            },
        )

        self.assertEqual(configuration.application_type, "OFFICE")
        self.assertEqual(configuration.server_url, "https://central.example")
        self.assertEqual(configuration.workstation_code, "MFD-PIS-OFFICE")

    def test_production_entrypoints_do_not_open_legacy_office(self):
        self.assertEqual(
            APPLICATION_TARGETS["office"],
            ("mikrofix_central_desktop.py", "--application", "OFFICE"),
        )
        self.assertEqual(
            APPLICATION_TARGETS["cashier"],
            ("mikrofix_central_desktop.py", "--application", "CASHIER"),
        )


if __name__ == "__main__":
    unittest.main()
