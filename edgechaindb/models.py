from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import dumps
from .crypto import sha256


ZERO_HASH = b"\x00" * 32


@dataclass(frozen=True)
class SignedEvent:
    device_id: str
    sequence: int
    device_time_ms: int
    event_type: str
    payload: dict[str, Any]
    previous_event_hash: bytes
    signature: bytes
    version: int = 1

    def unsigned_map(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_time_ms": self.device_time_ms,
            "event_type": self.event_type,
            "payload": self.payload,
            "previous_event_hash": self.previous_event_hash,
            "sequence": self.sequence,
            "version": self.version,
        }

    @property
    def signing_bytes(self) -> bytes:
        return dumps(self.unsigned_map())

    @property
    def event_hash(self) -> bytes:
        return sha256(b"edgechaindb:event:v1\x00" + self.signing_bytes + self.signature)

    def to_wire(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "sequence": self.sequence,
            "device_time_ms": self.device_time_ms,
            "event_type": self.event_type,
            "payload": self.payload,
            "previous_event_hash": self.previous_event_hash.hex(),
            "signature": self.signature.hex(),
            "event_hash": self.event_hash.hex(),
            "version": self.version,
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "SignedEvent":
        try:
            previous = bytes.fromhex(value["previous_event_hash"])
            signature = bytes.fromhex(value["signature"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid hexadecimal event field") from exc

        if len(previous) != 32:
            raise ValueError("previous_event_hash must be 32 bytes")
        if len(signature) != 64:
            raise ValueError("Ed25519 signature must be 64 bytes")

        return cls(
            device_id=str(value["device_id"]),
            sequence=int(value["sequence"]),
            device_time_ms=int(value["device_time_ms"]),
            event_type=str(value["event_type"]),
            payload=dict(value["payload"]),
            previous_event_hash=previous,
            signature=signature,
            version=int(value.get("version", 1)),
        )


@dataclass(frozen=True)
class BlockHeader:
    height: int
    previous_hash: bytes
    created_at_ms: int
    merkle_root: bytes
    event_count: int
    proposer_id: str
    authority_set_hash: bytes
    quorum_threshold: int
    policy_hash: bytes
    version: int = 1

    def as_map(self) -> dict[str, Any]:
        return {
            "authority_set_hash": self.authority_set_hash,
            "created_at_ms": self.created_at_ms,
            "event_count": self.event_count,
            "height": self.height,
            "merkle_root": self.merkle_root,
            "policy_hash": self.policy_hash,
            "previous_hash": self.previous_hash,
            "proposer_id": self.proposer_id,
            "quorum_threshold": self.quorum_threshold,
            "version": self.version,
        }

    @property
    def encoded(self) -> bytes:
        return dumps(self.as_map())

    @property
    def block_hash(self) -> bytes:
        return sha256(b"edgechaindb:block:v1\x00" + self.encoded)
