import unittest

from mikrofix_upnqr import UpnQrError, build_upnqr_payload


class V30UpnQrTests(unittest.TestCase):
    def test_payload_has_twenty_lines_exact_cents_and_control_length(self):
        payload = build_upnqr_payload(
            amount="32.82", recipient_iban="SI56040000283154872",
            recipient_reference="SI00 1234567890123456",
            recipient_name="MikroFix, Ristoski Daniel s.p.",
            recipient_street="Paradiž 73", recipient_city="2282 Cirkulane",
            purpose="Plačilo računa TEST", payer_name="Testni kupec",
        )
        fields = payload.decode("iso-8859-2").splitlines()
        self.assertEqual(len(fields), 20)
        self.assertEqual(fields[0], "UPNQR")
        self.assertEqual(fields[8], "00000003282")
        self.assertEqual(fields[14], "SI56040000283154872")
        first_19 = "".join(f"{value}\n" for value in fields[:19]).encode("iso-8859-2")
        self.assertEqual(fields[19], f"{len(first_19):03d}")

    def test_invalid_iban_is_rejected(self):
        with self.assertRaises(UpnQrError):
            build_upnqr_payload(
                amount=1, recipient_iban="SI56BAD", recipient_reference="SI00 1",
                recipient_name="MikroFix", recipient_street="Paradiž 73",
                recipient_city="2282 Cirkulane", purpose="Test",
            )


if __name__ == "__main__":
    unittest.main()
