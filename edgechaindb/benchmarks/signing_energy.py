from __future__ import annotations

import glob
import os
from pathlib import Path
import time
from typing import Any

from ..crypto import KeyPair
from ..models import SignedEvent, ZERO_HASH
from .common import BenchmarkSpec


def _rapl_snapshot() -> tuple[int, list[str]] | None:
    paths = sorted(set(glob.glob("/sys/class/powercap/**/energy_uj", recursive=True)))
    values: list[int] = []
    readable: list[str] = []
    for raw in paths:
        try:
            values.append(int(Path(raw).read_text().strip()))
            readable.append(raw)
        except (OSError, ValueError):
            continue
    return (sum(values), readable) if values else None


def build_spec(*, iterations: int = 5000, assumed_cpu_watts: float | None = None) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        watts = float(
            assumed_cpu_watts
            if assumed_cpu_watts is not None
            else os.getenv("EDGECHAIN_SIGNING_CPU_WATTS", "15")
        )
        key = KeyPair.generate()
        unsigned = SignedEvent(
            device_id="energy-probe",
            sequence=1,
            device_time_ms=1_700_000_000_000,
            event_type="energy",
            payload={"temperature_milli_celsius": 21500, "quality": 100},
            previous_event_hash=ZERO_HASH,
            signature=b"",
        )
        message = unsigned.signing_bytes
        for _ in range(200):
            key.sign(message)

        rapl_before = _rapl_snapshot()
        cpu_before = time.process_time_ns()
        wall_before = time.perf_counter_ns()
        checksum = 0
        for _ in range(iterations):
            checksum ^= key.sign(message)[0]
        wall_seconds = (time.perf_counter_ns() - wall_before) / 1_000_000_000
        cpu_seconds = (time.process_time_ns() - cpu_before) / 1_000_000_000
        rapl_after = _rapl_snapshot()

        estimated_joules = cpu_seconds * watts
        method = "CPU-time estimate"
        energy_joules = estimated_joules
        notes = [
            "The CPU-time estimate multiplies process CPU seconds by the configured "
            f"{watts:g} W power assumption. Set EDGECHAIN_SIGNING_CPU_WATTS for your hardware."
        ]
        rapl_domains: list[str] = []
        if rapl_before and rapl_after and rapl_after[0] >= rapl_before[0]:
            measured = (rapl_after[0] - rapl_before[0]) / 1_000_000
            if measured > 0:
                energy_joules = measured
                method = "Linux RAPL measurement"
                rapl_domains = rapl_after[1]
                notes = [
                    "RAPL measures package/domain energy and may include activity from other processes."
                ]

        metrics = {
            "iterations": iterations,
            "signing_bytes": len(message),
            "wall_seconds": round(wall_seconds, 6),
            "cpu_seconds": round(cpu_seconds, 6),
            "signatures_per_second": round(iterations / wall_seconds, 2),
            "energy_method": method,
            "energy_joules_total": round(energy_joules, 9),
            "energy_joules_per_event": round(energy_joules / iterations, 12),
            "energy_microjoules_per_event": round(energy_joules * 1_000_000 / iterations, 3),
            "assumed_cpu_watts": watts,
            "rapl_domains": rapl_domains,
            "checksum": checksum,
        }
        return {
            "details": "Measured Ed25519 signing cost and energy per signed event",
            "metrics": metrics,
            "notes": notes,
            "rows": [metrics],
        }

    return BenchmarkSpec("Signing energy per event", "Energy efficiency", "signing_energy", run)
