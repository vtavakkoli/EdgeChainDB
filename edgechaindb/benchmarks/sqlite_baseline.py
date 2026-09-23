from __future__ import annotations

from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
from typing import Any, Iterable

from ..canonical import dumps
from ..crypto import KeyPair
from ..device import DeviceClient
from ..ledger import EdgeChainLedger
from ..store import Database
from .common import BenchmarkSpec


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return ordered[index]


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def _checkpoint_sqlite(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path, timeout=30.0, isolation_level=None) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    return page_size * page_count, path.stat().st_size if path.exists() else 0


def _initialize_plain_sqlite(path: Path) -> None:
    with sqlite3.connect(path, timeout=30.0, isolation_level=None) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE events (
                device_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                device_time_ms INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_cbor BLOB NOT NULL,
                received_at_ms INTEGER NOT NULL,
                PRIMARY KEY(device_id, sequence)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_plain_device_time ON events(device_id, device_time_ms)"
        )


def _plain_insert(path: Path, event: Any, received_at_ms: int) -> None:
    # Match EdgeChainDB's persistence cadence: one connection + transaction per event.
    with sqlite3.connect(path, timeout=30.0, isolation_level=None) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO events(
                    device_id, sequence, device_time_ms, event_type,
                    payload_cbor, received_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.device_id,
                    event.sequence,
                    event.device_time_ms,
                    event.event_type,
                    dumps(event.payload),
                    received_at_ms,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _generate_events(device_count: int, total_events: int) -> list[tuple[str, KeyPair, Any]]:
    devices: list[tuple[str, KeyPair, DeviceClient]] = []
    for index in range(device_count):
        device_id = f"baseline-device-{index + 1:03d}"
        key = KeyPair.generate()
        devices.append((device_id, key, DeviceClient(device_id, key)))

    generated: list[tuple[str, KeyPair, Any]] = []
    base_time = 1_800_000_000_000
    for index in range(total_events):
        device_id, key, client = devices[index % device_count]
        event = client.create_event(
            "baseline",
            {
                "sample": index,
                "temperature_milli_celsius": 20_000 + (index % 500),
                "quality": 100,
            },
            device_time_ms=base_time + index,
        )
        generated.append((device_id, key, event))
    return generated


def _run_plain(path: Path, events: list[tuple[str, KeyPair, Any]]) -> dict[str, Any]:
    _initialize_plain_sqlite(path)
    latencies: list[float] = []
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for index, (_, _, event) in enumerate(events):
        started = time.perf_counter()
        _plain_insert(path, event, event.device_time_ms + index)
        latencies.append((time.perf_counter() - started) * 1000.0)
    cpu_seconds = time.process_time() - cpu_started
    wall_seconds = time.perf_counter() - wall_started

    with sqlite3.connect(path) as connection:
        row_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])
    if row_count != len(events) or quick_check != "ok":
        raise AssertionError({"row_count": row_count, "quick_check": quick_check})
    allocated_bytes, file_bytes = _checkpoint_sqlite(path)
    return {
        "system": "sqlite_plain",
        "events": len(events),
        "wall_seconds": wall_seconds,
        "throughput_events_per_second": len(events) / wall_seconds,
        "cpu_microseconds_per_event": cpu_seconds * 1_000_000.0 / len(events),
        "p95_ingest_ms": _p95(latencies),
        "allocated_bytes": allocated_bytes,
        "file_bytes": file_bytes,
        "bytes_per_event": allocated_bytes / len(events),
        "quick_check": quick_check,
    }


def _run_edgechain(
    path: Path,
    events: list[tuple[str, KeyPair, Any]],
    *,
    block_size: int,
) -> dict[str, Any]:
    database = Database(path)
    ledger = EdgeChainLedger(database, quorum_threshold=1)
    authority = KeyPair.generate()
    ledger.register_authority("baseline-authority", authority.public_bytes)

    registered: set[str] = set()
    for device_id, key, _ in events:
        if device_id not in registered:
            ledger.register_device(device_id, key.public_bytes)
            registered.add(device_id)

    latencies: list[float] = []
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for _, _, event in events:
        started = time.perf_counter()
        accepted = ledger.accept_event(event)
        latencies.append((time.perf_counter() - started) * 1000.0)
        if not accepted.get("accepted"):
            raise AssertionError(f"EdgeChainDB rejected baseline event: {accepted}")
        if database.pending_count() >= block_size:
            ledger.propose_block(
                "baseline-authority", authority.private_key, max_events=block_size
            )
    if database.pending_count():
        ledger.propose_block(
            "baseline-authority", authority.private_key, max_events=block_size
        )
    cpu_seconds = time.process_time() - cpu_started
    wall_seconds = time.perf_counter() - wall_started

    verification = ledger.verify_all()
    info = database.database_info(run_quick_check=True)
    if not verification["valid"] or verification["events"] != len(events):
        raise AssertionError(verification)
    if info["quick_check"] != "ok":
        raise AssertionError(info)
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    info = database.database_info(run_quick_check=True)
    return {
        "system": "edgechaindb",
        "events": len(events),
        "wall_seconds": wall_seconds,
        "throughput_events_per_second": len(events) / wall_seconds,
        "cpu_microseconds_per_event": cpu_seconds * 1_000_000.0 / len(events),
        "p95_ingest_ms": _p95(latencies),
        "allocated_bytes": int(info["allocated_bytes"]),
        "file_bytes": int(info["database_bytes"]),
        "bytes_per_event": int(info["allocated_bytes"]) / len(events),
        "quick_check": info["quick_check"],
        "ledger_verified": True,
        "finalized_blocks": len(database.all_blocks()),
    }


def build_spec(
    *,
    device_counts: tuple[int, ...] = (1, 20, 100),
    events_per_run: tuple[int, ...] = (1_000, 10_000),
    repetitions: int = 5,
    block_size: int = 64,
) -> BenchmarkSpec:
    if not device_counts or any(value < 1 for value in device_counts):
        raise ValueError("device_counts must contain positive integers")
    if not events_per_run or any(value < 1 for value in events_per_run):
        raise ValueError("events_per_run must contain positive integers")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if block_size < 1:
        raise ValueError("block_size must be positive")

    def run() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        comparison_rows: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="edgechain-sqlite-baseline-") as raw_dir:
            root = Path(raw_dir)
            for device_count in device_counts:
                for event_count in events_per_run:
                    for repetition in range(1, repetitions + 1):
                        events = _generate_events(device_count, event_count)
                        plain = _run_plain(
                            root / f"plain-d{device_count}-n{event_count}-r{repetition}.db",
                            events,
                        )
                        edge = _run_edgechain(
                            root / f"edge-d{device_count}-n{event_count}-r{repetition}.db",
                            events,
                            block_size=block_size,
                        )
                        for value in (plain, edge):
                            rows.append(
                                {
                                    "kind": "measurement",
                                    "devices": device_count,
                                    "events": event_count,
                                    "repetition": repetition,
                                    **{
                                        key: round(val, 6) if isinstance(val, float) else val
                                        for key, val in value.items()
                                    },
                                }
                            )
                        comparison = {
                            "kind": "comparison",
                            "devices": device_count,
                            "events": event_count,
                            "repetition": repetition,
                            "edge_to_plain_throughput_ratio": (
                                edge["throughput_events_per_second"]
                                / plain["throughput_events_per_second"]
                            ),
                            "throughput_overhead_percent": 100.0
                            * (
                                1.0
                                - edge["throughput_events_per_second"]
                                / plain["throughput_events_per_second"]
                            ),
                            "cpu_overhead_ratio": (
                                edge["cpu_microseconds_per_event"]
                                / plain["cpu_microseconds_per_event"]
                            ),
                            "storage_overhead_ratio": (
                                edge["bytes_per_event"] / plain["bytes_per_event"]
                            ),
                        }
                        comparison_rows.append(comparison)
                        rows.append(
                            {
                                key: round(value, 6) if isinstance(value, float) else value
                                for key, value in comparison.items()
                            }
                        )

        metrics = {
            "workload_cases": len(device_counts) * len(events_per_run),
            "repetitions": repetitions,
            "measurement_runs": len(comparison_rows) * 2,
            "block_size": block_size,
            "mean_edge_to_plain_throughput_ratio": round(
                _mean(row["edge_to_plain_throughput_ratio"] for row in comparison_rows), 4
            ),
            "mean_throughput_overhead_percent": round(
                _mean(row["throughput_overhead_percent"] for row in comparison_rows), 2
            ),
            "mean_cpu_overhead_ratio": round(
                _mean(row["cpu_overhead_ratio"] for row in comparison_rows), 4
            ),
            "mean_storage_overhead_ratio": round(
                _mean(row["storage_overhead_ratio"] for row in comparison_rows), 4
            ),
        }
        return {
            "details": (
                "Compared EdgeChainDB with an unsigned SQLite WAL baseline using "
                "matched event streams and per-event transaction cadence"
            ),
            "metrics": metrics,
            "notes": [
                "Events are pre-generated before timing so the comparison isolates gateway-side persistence, verification, and block-finalization overhead; device-side signing generation is not timed.",
                "The SQLite baseline intentionally omits signatures, hash chains, Merkle blocks, authority approval, and finalization.",
            ],
            "rows": rows,
        }

    return BenchmarkSpec(
        "Unsigned SQLite baseline overhead",
        "Performance baseline",
        "sqlite_baseline",
        run,
    )
