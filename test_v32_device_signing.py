import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from mikrofix_device_signing import (
    generate_private_key_file,
    load_private_key,
    sign_operation,
    verify_operation_signature,
)
from mikrofix_sync_contracts import SyncOperationEnvelope


def envelope():
    return SyncOperationEnvelope(
        operation_id=str(uuid4()), operation_type="CreateServiceOrder",
        aggregate_type="SERVICE_ORDER", aggregate_id=str(uuid4()),
        expected_version=0, actor_id="serviser-a",
        actor_user_id=str(uuid4()), actor_authorization_version=3,
        device_id="SERVICE-PC-1", device_sequence=7,
        occurred_at="2026-09-12T08:00:00+02:00",
        payload={"customer_name": "Test"},
    )


class DeviceSigningTests(unittest.TestCase):
    def test_private_key_is_mode_0600_and_signature_covers_every_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-signing.key"
            public_key = generate_private_key_file(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            signed = sign_operation(envelope(), load_private_key(path))
            self.assertTrue(verify_operation_signature(signed, public_key))
            self.assertFalse(verify_operation_signature(
                replace(signed, device_sequence=8), public_key
            ))
            with self.assertRaises(FileExistsError):
                generate_private_key_file(path)

    def test_loading_key_with_broad_permissions_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-signing.key"
            generate_private_key_file(path)
            os.chmod(path, 0o644)
            with self.assertRaises(PermissionError):
                load_private_key(path)


if __name__ == "__main__":
    unittest.main()
