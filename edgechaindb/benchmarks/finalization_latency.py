from __future__ import annotations

import statistics
import time
from typing import Any
import uuid

from ..crypto import KeyPair
from ..device import DeviceClient
from .common import BenchmarkSpec


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))
    return ordered[index]


def build_spec(gateway: Any, *, samples: int = 10) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        while gateway.json("GET", "/health")["pending_events"]:
            gateway.json("POST", "/blocks/seal?max_events=10000")
        device_id = f"finality-{uuid.uuid4().hex[:10]}"
        key = KeyPair.generate()
        gateway.json(
            "POST",
            "/devices",
            expected=201,
            json={"device_id": device_id, "public_key": key.public_bytes.hex()},
        )
        device = DeviceClient(device_id, key)
        rows: list[dict[str, Any]] = []
        for sample in range(1, samples + 1):
            event = device.create_event("finality", {"sample": sample, "quality": 100})
            event_started = time.perf_counter()
            accepted = gateway.json("POST", "/events", expected=202, json=event.to_wire())
            accepted_at = time.perf_counter()
            block = gateway.json("POST", "/blocks/seal?max_events=1")
            finalized_at = time.perf_counter()
            if block.get("status") != "finalized":
                raise AssertionError(f"block did not finalize: {block}")
            rows.append(
                {
                    "sample": sample,
                    "sequence": accepted["sequence"],
                    "block_height": block["height"],
                    "ingest_latency_ms": round((accepted_at - event_started) * 1000, 3),
                    "seal_latency_ms": round((finalized_at - accepted_at) * 1000, 3),
                    "event_to_finality_ms": round((finalized_at - event_started) * 1000, 3),
                    "required_signatures": block["required_signatures"],
                    "signatures": block["signatures"],
                }
            )
        values = [float(row["event_to_finality_ms"]) for row in rows]
        metrics = {
            "samples": samples,
            "event_to_finality_ms_mean": round(statistics.mean(values), 3),
            "event_to_finality_ms_p50": round(statistics.median(values), 3),
            "event_to_finality_ms_p95": round(_percentile(values, 0.95), 3),
            "event_to_finality_ms_max": round(max(values), 3),
        }
        return {
            "details": "Measured signed-event acceptance through quorum-finalized block completion",
            "metrics": metrics,
            "rows": rows,
        }

    return BenchmarkSpec("Block finalization latency", "Consensus performance", "block_finalization_latency", run)
