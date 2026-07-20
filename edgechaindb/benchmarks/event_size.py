from __future__ import annotations

import json
import statistics
from typing import Any

from ..canonical import dumps
from ..crypto import KeyPair
from ..device import DeviceClient
from .common import BenchmarkSpec


def build_spec(*, samples: int = 200) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        device = DeviceClient("size-probe", KeyPair.generate())
        rows: list[dict[str, Any]] = []
        for index in range(samples):
            event = device.create_event(
                "environment",
                {
                    "temperature_milli_celsius": 20000 + index,
                    "humidity_basis_points": 4500 + index % 50,
                    "battery_millivolts": 3700 - index % 30,
                    "quality": 100,
                },
            )
            wire_bytes = len(
                json.dumps(
                    event.to_wire(), separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
            )
            payload_bytes = len(dumps(event.payload))
            logical_storage = (
                32
                + len(event.device_id.encode())
                + 8
                + 8
                + len(event.event_type.encode())
                + payload_bytes
                + 32
                + 64
                + len(event.signing_bytes)
                + 8
                + 8
            )
            rows.append(
                {
                    "sample": index + 1,
                    "payload_cbor_bytes": payload_bytes,
                    "signing_bytes": len(event.signing_bytes),
                    "signature_bytes": len(event.signature),
                    "event_hash_bytes": len(event.event_hash),
                    "wire_json_bytes": wire_bytes,
                    "logical_storage_bytes": logical_storage,
                }
            )

        def avg(field: str) -> float:
            return round(statistics.mean(float(row[field]) for row in rows), 2)

        metrics = {
            "samples": samples,
            "average_payload_cbor_bytes": avg("payload_cbor_bytes"),
            "average_signing_bytes": avg("signing_bytes"),
            "signature_bytes": 64,
            "event_hash_bytes": 32,
            "average_wire_json_bytes": avg("wire_json_bytes"),
            "average_logical_storage_bytes": avg("logical_storage_bytes"),
            "wire_bytes_min": min(row["wire_json_bytes"] for row in rows),
            "wire_bytes_max": max(row["wire_json_bytes"] for row in rows),
        }
        return {
            "details": "Calculated canonical payload, signature, wire, and logical storage bytes per event",
            "metrics": metrics,
            "rows": rows,
        }

    return BenchmarkSpec("Bytes per event", "Serialization efficiency", "bytes_per_event", run)
