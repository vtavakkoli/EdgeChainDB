from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from typing import Any
import uuid

from ..crypto import KeyPair
from ..device import DeviceClient
from ..outbox import DurableOutbox
from .common import BenchmarkSpec


def build_spec(gateway: Any, *, buffered_events: int = 50) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        device_id = f"offline-{uuid.uuid4().hex[:10]}"
        key = KeyPair.generate()
        gateway.json(
            "POST", "/devices", expected=201,
            json={"device_id": device_id, "public_key": key.public_bytes.hex()},
        )
        device = DeviceClient(device_id, key)
        with tempfile.TemporaryDirectory(prefix="edgechain-outbox-") as raw_dir:
            outbox_path = Path(raw_dir) / "outbox.json"
            outbox = DurableOutbox(outbox_path)
            for index in range(buffered_events):
                event = device.create_event(
                    "offline", {"sample": index, "temperature_milli_celsius": 21000 + index}
                )
                outbox.append(event.to_wire())
            peak_outbox_file_bytes = outbox_path.stat().st_size
            persisted = DurableOutbox(outbox_path)
            if len(persisted) != buffered_events:
                raise AssertionError("durable outbox did not survive reload")

            pending = persisted.items()
            out_of_order = gateway.request("POST", "/events", json=pending[1])
            out_of_order_rejected = out_of_order.status_code == 400
            if not out_of_order_rejected:
                raise AssertionError("out-of-order reconnect delivery was accepted")

            started = time.perf_counter()
            duplicate_retries = 0
            rows: list[dict[str, Any]] = []
            while persisted.items():
                wire = persisted.items()[0]
                response = gateway.json("POST", "/events", expected=202, json=wire)
                if int(wire["sequence"]) % 10 == 0:
                    retry = gateway.json("POST", "/events", expected=202, json=wire)
                    if not retry.get("duplicate"):
                        raise AssertionError("reconnect retry was not idempotent")
                    duplicate_retries += 1
                persisted.acknowledge(int(wire["sequence"]))
                rows.append(
                    {
                        "sequence": wire["sequence"],
                        "accepted": response.get("accepted"),
                        "remaining": len(persisted),
                    }
                )
            elapsed = time.perf_counter() - started
            checkpoint = gateway.json("GET", f"/devices/{device_id}/checkpoint")
            if checkpoint["last_sequence"] != buffered_events or len(persisted) != 0:
                raise AssertionError({"checkpoint": checkpoint, "remaining": len(persisted)})
            metrics = {
                "buffered_events": buffered_events,
                "peak_outbox_file_bytes": peak_outbox_file_bytes,
                "outbox_file_bytes_after_flush": outbox_path.stat().st_size,
                "out_of_order_rejected": out_of_order_rejected,
                "duplicate_retries": duplicate_retries,
                "reconnection_seconds": round(elapsed, 6),
                "reconnection_events_per_second": round(buffered_events / elapsed, 2),
                "final_sequence": checkpoint["last_sequence"],
                "remaining_buffered_events": len(persisted),
            }
            return {
                "details": "Persisted signed events while offline and delivered them safely in order after reconnection",
                "metrics": metrics,
                "rows": rows,
            }

    return BenchmarkSpec("Offline buffering and reconnection", "Resilience", "offline_reconnection", run)
