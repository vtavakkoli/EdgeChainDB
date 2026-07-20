from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics
import threading
import time
from typing import Any
import uuid

import httpx

from ..crypto import KeyPair
from ..device import DeviceClient
from .common import BenchmarkSpec


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))
    return ordered[index]


def build_spec(base_url: str, *, nodes: int = 20, events_per_node: int = 20) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        prefix = f"throughput-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(nodes)
        latency_lock = threading.Lock()
        all_latencies: list[float] = []
        node_rows: list[dict[str, Any]] = []

        def worker(index: int) -> dict[str, Any]:
            key = KeyPair.generate()
            device_id = f"{prefix}-{index:02d}"
            device = DeviceClient(device_id, key)
            latencies: list[float] = []
            with httpx.Client(base_url=base_url, timeout=30.0) as client:
                enrollment = client.post(
                    "/devices",
                    json={"device_id": device_id, "public_key": key.public_bytes.hex()},
                )
                enrollment.raise_for_status()
                barrier.wait(timeout=30)
                started = time.perf_counter()
                for sequence in range(events_per_node):
                    event = device.create_event(
                        "throughput",
                        {"reading_milliunits": index * 1000 + sequence, "quality": 100},
                    )
                    request_started = time.perf_counter()
                    response = client.post("/events", json=event.to_wire())
                    response.raise_for_status()
                    if not response.json().get("accepted"):
                        raise AssertionError("gateway did not accept throughput event")
                    latencies.append((time.perf_counter() - request_started) * 1000)
                elapsed = time.perf_counter() - started
            with latency_lock:
                all_latencies.extend(latencies)
            return {
                "node": index,
                "events": events_per_node,
                "elapsed_seconds": round(elapsed, 6),
                "events_per_second": round(events_per_node / elapsed, 2),
                "latency_ms_p50": round(statistics.median(latencies), 3),
                "latency_ms_p95": round(_percentile(latencies, 0.95), 3),
            }

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=nodes) as executor:
            futures = [executor.submit(worker, index) for index in range(1, nodes + 1)]
            for future in as_completed(futures):
                node_rows.append(future.result())
        elapsed = time.perf_counter() - started
        total = nodes * events_per_node
        metrics = {
            "nodes": nodes,
            "events_per_node": events_per_node,
            "total_events": total,
            "elapsed_seconds": round(elapsed, 6),
            "gateway_ingest_events_per_second": round(total / elapsed, 2),
            "latency_ms_p50": round(statistics.median(all_latencies), 3),
            "latency_ms_p95": round(_percentile(all_latencies, 0.95), 3),
            "latency_ms_p99": round(_percentile(all_latencies, 0.99), 3),
        }
        return {
            "details": f"Gateway ingested {total} signed events from {nodes} concurrent clients",
            "metrics": metrics,
            "rows": sorted(node_rows, key=lambda row: int(row["node"])),
        }

    return BenchmarkSpec("Gateway ingest throughput", "Performance", "gateway_ingest_throughput", run)
