import tempfile
import unittest
from pathlib import Path

from PIL import Image

from mikrofix_attachments import (
    ServiceImageError,
    archive_service_image,
    load_service_image,
    resolve_service_image_path,
)


class ServicePhotoSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (32, 24), "red").save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_reencodes_to_canonical_jpeg(self):
        destination = archive_service_image(self.source, self.root / "photos", "01_PRED")
        self.assertEqual(destination.suffix, ".jpg")
        self.assertTrue(destination.is_file())
        with Image.open(destination) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (32, 24))

    def test_executable_extension_is_rejected_even_with_image_content(self):
        disguised = self.root / "photo.exe"
        disguised.write_bytes(self.source.read_bytes())
        with self.assertRaises(ServiceImageError):
            load_service_image(disguised)

    def test_invalid_image_content_is_rejected(self):
        invalid = self.root / "photo.jpg"
        invalid.write_bytes(b"not-an-image")
        with self.assertRaises(ServiceImageError):
            load_service_image(invalid)

    def test_path_cannot_escape_data_roots(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(self.source.read_bytes())
        data_dir = self.root / "data"
        data_dir.mkdir()
        with self.assertRaises(ServiceImageError):
            resolve_service_image_path(str(outside), data_dir=data_dir)

    def test_new_data_relative_path_is_resolved(self):
        data_dir = self.root / "data"
        photo = data_dir / "service" / "photos" / "MF-1" / "01_PRED.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(self.source.read_bytes())
        resolved = resolve_service_image_path(
            "service/photos/MF-1/01_PRED.jpg",
            data_dir=data_dir,
        )
        self.assertEqual(resolved, photo.resolve())

    def test_other_image_inside_application_root_is_not_exposed(self):
        app_dir = self.root / "app"
        image = app_dir / "private.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(self.source.read_bytes())
        with self.assertRaises(ServiceImageError):
            resolve_service_image_path(
                "private.jpg",
                data_dir=self.root / "data",
                app_dir=app_dir,
            )


if __name__ == "__main__":
    unittest.main()
