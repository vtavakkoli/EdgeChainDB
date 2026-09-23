from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any
import uuid

import httpx

from ..crypto import KeyPair
from ..device import DeviceClient
from ..device_node import DeviceStateError, _sync_and_flush
from ..models import ZERO_HASH
from ..observability import get_logger
from ..outbox import DurableOutbox, OutboxFullError
from .common import BenchmarkSpec


log = get_logger("resilience-boundaries")


def build_spec(
    gateway: Any,
    *,
    capacity: int = 1_000,
    overflow_attempts: int = 500,
    checkpoint_events: int = 20,
) -> BenchmarkSpec:
    if capacity < 2:
        raise ValueError("capacity must be at least 2")
    if overflow_attempts < 1 or checkpoint_events < 1:
        raise ValueError("overflow_attempts and checkpoint_events must be positive")

    def run() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="edgechain-resilience-boundary-") as raw_dir:
            root = Path(raw_dir)

            # Capacity boundary: the durable outbox itself rejects C+1 and never evicts
            # or overwrites older signed events.
            capacity_key = KeyPair.generate()
            capacity_device = DeviceClient("capacity-device", capacity_key)
            outbox_path = root / "capacity-outbox.json"
            outbox = DurableOutbox(outbox_path, max_items=capacity)
            for index in range(capacity):
                event = capacity_device.create_event(
                    "offline-capacity",
                    {"sample": index, "quality": 100},
                    device_time_ms=1_800_100_000_000 + index,
                )
                outbox.append(event.to_wire())
            if len(outbox) != capacity:
                raise AssertionError("outbox did not reach configured capacity")

            next_event = capacity_device.create_event(
                "offline-capacity",
                {"sample": capacity, "quality": 100},
                device_time_ms=1_800_100_000_000 + capacity,
            )
            overflow_rejected = 0
            for attempt in range(overflow_attempts):
                try:
                    outbox.append(next_event.to_wire())
                except OutboxFullError:
                    overflow_rejected += 1
                rows.append(
                    {
                        "kind": "outbox_capacity",
                        "attempt": attempt + 1,
                        "capacity": capacity,
                        "persisted": len(outbox),
                        "overflow_rejected": overflow_rejected,
                    }
                )
            if overflow_rejected != overflow_attempts or len(outbox) != capacity:
                raise AssertionError(
                    {
                        "overflow_rejected": overflow_rejected,
                        "overflow_attempts": overflow_attempts,
                        "persisted": len(outbox),
                        "capacity": capacity,
                    }
                )

            reloaded = DurableOutbox(outbox_path, max_items=capacity)
            if len(reloaded) != capacity:
                raise AssertionError("capacity-full outbox did not survive reload")
            first_sequence = int(reloaded.items()[0]["sequence"])
            last_sequence = int(reloaded.items()[-1]["sequence"])

            # Checkpoint-loss boundary: use the real device synchronization path.
            device_id = f"checkpoint-loss-{uuid.uuid4().hex[:10]}"
            key = KeyPair.generate()
            gateway.json(
                "POST",
                "/devices",
                expected=201,
                json={"device_id": device_id, "public_key": key.public_bytes.hex()},
            )
            producer = DeviceClient(device_id, key)
            for index in range(checkpoint_events):
                event = producer.create_event(
                    "checkpoint-boundary",
                    {"sample": index, "quality": 100},
                    device_time_ms=1_800_200_000_000 + index,
                )
                gateway.json("POST", "/events", expected=202, json=event.to_wire())

            checkpoint = gateway.json("GET", f"/devices/{device_id}/checkpoint")
            if int(checkpoint["last_sequence"]) != checkpoint_events:
                raise AssertionError(checkpoint)

            lost_state_path = root / "lost-state.json"
            lost_state = {
                "sequence": 0,
                "previous_event_hash": ZERO_HASH.hex(),
            }
            lost_state_path.write_text(json.dumps(lost_state), encoding="utf-8")
            lost_outbox = DurableOutbox(root / "lost-outbox.json", max_items=capacity)
            restarted_client = DeviceClient(device_id, key)
            checkpoint_loss_rejected = False
            checkpoint_loss_error = ""
            with httpx.Client(base_url=gateway.base_url, timeout=5.0) as client:
                try:
                    _sync_and_flush(
                        client=client,
                        device_id=device_id,
                        key=key,
                        client_state=restarted_client,
                        local_state=lost_state,
                        state_path=lost_state_path,
                        outbox=lost_outbox,
                        retries=1,
                        logger=log,
                    )
                except DeviceStateError as exc:
                    checkpoint_loss_rejected = True
                    checkpoint_loss_error = str(exc)
            if not checkpoint_loss_rejected:
                raise AssertionError(
                    "device checkpoint loss was not rejected by the synchronization path"
                )

            # Control: a retained checkpoint with an empty outbox is accepted and aligned.
            retained_state_path = root / "retained-state.json"
            retained_state = {
                "sequence": int(checkpoint["last_sequence"]),
                "previous_event_hash": str(checkpoint["last_event_hash"]),
            }
            retained_state_path.write_text(json.dumps(retained_state), encoding="utf-8")
            retained_outbox = DurableOutbox(
                root / "retained-outbox.json", max_items=capacity
            )
            retained_client = DeviceClient(device_id, key)
            retained_client.restore(
                int(checkpoint["last_sequence"]),
                bytes.fromhex(str(checkpoint["last_event_hash"])),
            )
            with httpx.Client(base_url=gateway.base_url, timeout=5.0) as client:
                delivered = _sync_and_flush(
                    client=client,
                    device_id=device_id,
                    key=key,
                    client_state=retained_client,
                    local_state=retained_state,
                    state_path=retained_state_path,
                    outbox=retained_outbox,
                    retries=1,
                    logger=log,
                )
            if delivered != 0 or retained_client.sequence != checkpoint_events:
                raise AssertionError(
                    {"delivered": delivered, "sequence": retained_client.sequence}
                )

            rows.extend(
                [
                    {
                        "kind": "checkpoint_loss",
                        "gateway_sequence": checkpoint_events,
                        "local_sequence": 0,
                        "outbox_events": 0,
                        "accepted": False,
                        "reason": checkpoint_loss_error,
                    },
                    {
                        "kind": "checkpoint_control",
                        "gateway_sequence": checkpoint_events,
                        "local_sequence": checkpoint_events,
                        "outbox_events": 0,
                        "accepted": True,
                        "delivered": delivered,
                    },
                ]
            )

            metrics = {
                "outbox_capacity_events": capacity,
                "overflow_attempts": overflow_attempts,
                "overflow_rejected": overflow_rejected,
                "overflow_rejection_rate": round(
                    overflow_rejected / overflow_attempts, 4
                ),
                "persisted_events_at_capacity": len(reloaded),
                "first_persisted_sequence": first_sequence,
                "last_persisted_sequence": last_sequence,
                "checkpoint_events": checkpoint_events,
                "checkpoint_loss_rejected": checkpoint_loss_rejected,
                "retained_checkpoint_control_passed": True,
            }
            return {
                "details": (
                    "Measured the configured offline-buffer boundary and exercised the "
                    "device checkpoint-loss safety boundary through the real reconnect path"
                ),
                "metrics": metrics,
                "notes": [
                    "Outbox overflow is fail-closed: C+1 is rejected; no oldest-event eviction or overwrite occurs.",
                    "In continuous device mode the existing producer guard pauses new event generation while the outbox is full; the outbox-level limit added here is defense in depth.",
                    "A device that loses its local checkpoint while the gateway is ahead is deliberately rejected rather than silently re-anchored, preventing unsafe rollback. Recovery requires restoring durable state or an explicit future re-anchoring protocol.",
                ],
                "rows": rows,
            }

    return BenchmarkSpec(
        "Offline durability and checkpoint-loss boundaries",
        "Resilience boundary",
        "resilience_boundaries",
        run,
    )
