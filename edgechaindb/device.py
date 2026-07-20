from __future__ import annotations

import time
from typing import Any

from .crypto import KeyPair
from .models import SignedEvent, ZERO_HASH


class DeviceClient:
    """Reference client for a device-side signed event chain."""

    def __init__(self, device_id: str, key_pair: KeyPair) -> None:
        if not device_id or len(device_id) > 128:
            raise ValueError("device_id must contain 1 to 128 characters")
        self.device_id = device_id
        self.key_pair = key_pair
        self.sequence = 0
        self.previous_event_hash = ZERO_HASH

    def restore(self, sequence: int, previous_event_hash: bytes) -> None:
        if sequence < 0 or len(previous_event_hash) != 32:
            raise ValueError("invalid device-chain checkpoint")
        self.sequence = sequence
        self.previous_event_hash = previous_event_hash

    def create_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        device_time_ms: int | None = None,
    ) -> SignedEvent:
        sequence = self.sequence + 1
        unsigned = SignedEvent(
            device_id=self.device_id,
            sequence=sequence,
            device_time_ms=device_time_ms or int(time.time() * 1000),
            event_type=event_type,
            payload=payload,
            previous_event_hash=self.previous_event_hash,
            signature=b"",
        )
        signature = self.key_pair.sign(unsigned.signing_bytes)
        event = SignedEvent(
            device_id=unsigned.device_id,
            sequence=unsigned.sequence,
            device_time_ms=unsigned.device_time_ms,
            event_type=unsigned.event_type,
            payload=unsigned.payload,
            previous_event_hash=unsigned.previous_event_hash,
            signature=signature,
            version=unsigned.version,
        )
        self.sequence = sequence
        self.previous_event_hash = event.event_hash
        return event
