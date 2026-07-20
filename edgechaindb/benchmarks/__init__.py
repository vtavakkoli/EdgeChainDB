from __future__ import annotations

from pathlib import Path
from typing import Any

from .byzantine_quorum import build_spec as byzantine_spec
from .common import BenchmarkSpec, write_benchmark_index
from .event_size import build_spec as event_size_spec
from .finalization_latency import build_spec as finalization_spec
from .gateway_ingest import build_spec as ingest_spec
from .integrity_detection import build_spec as integrity_spec
from .offline_reconnect import build_spec as offline_spec
from .signing_energy import build_spec as energy_spec
from .storage_overhead import build_spec as storage_spec


def build_specs(gateway: Any, base_url: str) -> list[BenchmarkSpec]:
    return [
        energy_spec(),
        event_size_spec(),
        ingest_spec(base_url),
        finalization_spec(gateway),
        storage_spec(),
        integrity_spec(),
        offline_spec(gateway),
        byzantine_spec(),
    ]


def finalize(result_dir: Path) -> None:
    write_benchmark_index(result_dir)


__all__ = ["BenchmarkSpec", "build_specs", "finalize"]
