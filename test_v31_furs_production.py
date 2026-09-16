from __future__ import annotations

import unittest
from datetime import datetime

from mikrofix_furs import (
    FursProductionClient,
    FursTestError,
    build_fiscal_qr_payload,
)


class FursProductionRulesTests(unittest.TestCase):
    def test_official_qr_examples_are_exact(self):
        issued = datetime(2015, 8, 15, 10, 13, 32)
        self.assertEqual(
            build_fiscal_qr_payload(
                zoi="a7e5f55e1dbb48b799268e1a6d8618a3",
                tax_number="12345678", issue_datetime=issued,
            ),
            "223175087923687075112234402528973166755123456781508151013321",
        )
        self.assertEqual(
            build_fiscal_qr_payload(
                zoi="3024e56bf1ddd2e7eeb5715c6859a913",
                tax_number="12345678", issue_datetime=issued,
            ),
            "063994519708649896901260100447252359443123456781508151013320",
        )

    def test_production_client_is_locked_to_official_host_and_port(self):
        client = FursProductionClient()
        client._assert_url("https://blagajne.fu.gov.si:9003/v1/cash_registers/invoices")
        for bad in (
            "https://blagajne-test.fu.gov.si:9002/v1/cash_registers/invoices",
            "https://blagajne.fu.gov.si/v1/cash_registers/invoices",
            "https://example.invalid:9003/v1/cash_registers/invoices",
        ):
            with self.assertRaises(FursTestError):
                client._assert_url(bad)


if __name__ == "__main__":
    unittest.main()
