"""Ed25519 identity for immutable offline Service operations."""

from __future__ import annotations

import base64
import os
from dataclasses import replace
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from mikrofix_sync_contracts import SyncOperationEnvelope


def generate_private_key_file(path: Path) -> bytes:
    """Create a non-overwriting mode-0600 key and return its public key."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, base64.b64encode(raw_private))
    finally:
        os.close(descriptor)
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def load_private_key(path: Path) -> Ed25519PrivateKey:
    target = Path(path)
    mode = target.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("Service signing key must not be readable by other users.")
    raw = base64.b64decode(target.read_bytes(), validate=True)
    if len(raw) != 32:
        raise ValueError("Service signing key is invalid.")
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_operation(
    envelope: SyncOperationEnvelope, private_key: Ed25519PrivateKey
) -> SyncOperationEnvelope:
    if not envelope.actor_user_id:
        raise ValueError("Signed Service operation requires a central actor identity.")
    signature = base64.b64encode(
        private_key.sign(envelope.signing_bytes)
    ).decode("ascii")
    return replace(envelope, workstation_signature=signature)


def verify_operation_signature(
    envelope: SyncOperationEnvelope, public_key: bytes
) -> bool:
    try:
        signature = base64.b64decode(envelope.workstation_signature, validate=True)
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(
            signature, envelope.signing_bytes
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
