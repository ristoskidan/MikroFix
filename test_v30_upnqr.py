import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mikrofix_upnqr import UpnQrError, build_upnqr_payload, generate_upnqr_pdf


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
        self.assertEqual(fields[15], "SI001234567890123456")
        self.assertNotIn(" ", fields[15])
        first_19 = "".join(f"{value}\n" for value in fields[:19]).encode("iso-8859-2")
        self.assertEqual(fields[19], f"{len(first_19):03d}")

    def test_invalid_iban_is_rejected(self):
        with self.assertRaises(UpnQrError):
            build_upnqr_payload(
                amount=1, recipient_iban="SI56BAD", recipient_reference="SI00 1",
                recipient_name="MikroFix", recipient_street="Paradiž 73",
                recipient_city="2282 Cirkulane", purpose="Test",
            )

    @unittest.skipUnless(
        shutil.which("pdftoppm") and shutil.which("zbarimg"),
        "PDF/QR decoder ni nameščen",
    )
    def test_printed_pdf_decodes_to_complete_upn_not_only_amount(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            output = Path(raw_dir)
            pdf = generate_upnqr_pdf(
                output_dir=output, amount="123.45",
                reference="SI0012345678901234",
                payer={"name": "Testni kupec", "address": "Cesta 1"},
                company={
                    "legal_name": "MikroFix, Ristoski Daniel s.p.",
                    "iban": "SI56040000283154872",
                    "street": "Paradiž 73", "city": "2282 Cirkulane",
                },
                paper_width_mm=80,
            )
            image = output / "render"
            subprocess.run(
                ["pdftoppm", "-png", "-r", "300", "-singlefile", str(pdf), str(image)],
                check=True, capture_output=True,
            )
            decoded = subprocess.run(
                ["zbarimg", "--raw", str(image.with_suffix(".png"))],
                check=True, capture_output=True,
            ).stdout.decode("utf-8")
            fields = decoded.splitlines()
            self.assertEqual(fields[0], "UPNQR")
            self.assertEqual(fields[8], "00000012345")
            self.assertEqual(fields[14], "SI56040000283154872")
            self.assertEqual(fields[15], "SI0012345678901234")


if __name__ == "__main__":
    unittest.main()
