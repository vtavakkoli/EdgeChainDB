from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:24]


def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True)
class KeyPair:
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "KeyPair":
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def public_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def private_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)

    @classmethod
    def load_or_create(cls, path: str | Path) -> "KeyPair":
        target = Path(path)
        if target.exists():
            raw = target.read_bytes()
            if len(raw) != 32:
                raise ValueError(f"invalid Ed25519 private key file: {target}")
            return cls.from_private_bytes(raw)

        target.parent.mkdir(parents=True, exist_ok=True)
        pair = cls.generate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pair.private_bytes)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return pair
