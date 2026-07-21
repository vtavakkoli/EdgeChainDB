from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import statistics
import subprocess
import threading
import time
from typing import Any

import httpx

from ..crypto import KeyPair, key_id
from ..device import DeviceClient
from ..observability import get_logger

log = get_logger("experiment-worker")


class ExperimentOutbox:
    """SQLite-backed outbox designed for 1M-event experiments.

    The normal device outbox intentionally optimizes for simplicity. The matrix
    worker uses a separate WAL queue so long outages and million-event cases do
    not rewrite a large JSON file for every append or acknowledgement.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox(
                    sequence INTEGER PRIMARY KEY,
                    wire_json TEXT NOT NULL,
                    wire_bytes INTEGER NOT NULL,
                    generated_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def append_many(self, rows: list[tuple[int, str, int, int]]) -> None:
        if not rows:
            return
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "INSERT INTO outbox(sequence, wire_json, wire_bytes, generated_at_ns) VALUES (?, ?, ?, ?)",
                    rows,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def peek(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT sequence, wire_json, wire_bytes, generated_at_ns FROM outbox ORDER BY sequence LIMIT 1"
            ).fetchone()

    def acknowledge(self, sequence: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM outbox WHERE sequence = ?", (sequence,))

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def size_bytes(self) -> int:
        base = Path(self.path)
        return sum(
            candidate.stat().st_size
            for candidate in (base, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
            if candidate.exists()
        )


class Reservoir:
    def __init__(self, limit: int, seed: int) -> None:
        self.limit = max(1, limit)
        self.values: list[float] = []
        self.seen = 0
        self.random = random.Random(seed)
        self.lock = threading.Lock()

    def add(self, value: float) -> None:
        with self.lock:
            self.seen += 1
            if len(self.values) < self.limit:
                self.values.append(value)
                return
            index = self.random.randint(1, self.seen)
            if index <= self.limit:
                self.values[index - 1] = value

    def summary(self) -> dict[str, float | int | None]:
        with self.lock:
            values = sorted(self.values)
            seen = self.seen
        if not values:
            return {"samples_seen": seen, "samples_retained": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "mean_ms": None}

        def percentile(fraction: float) -> float:
            index = max(0, min(len(values) - 1, math.ceil(len(values) * fraction) - 1))
            return round(values[index], 3)

        return {
            "samples_seen": seen,
            "samples_retained": len(values),
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "mean_ms": round(statistics.fmean(values), 3),
        }


@dataclass
class SharedState:
    generated: int = 0
    delivered: int = 0
    duplicate_deliveries: int = 0
    simulated_packet_drops: int = 0
    network_errors: int = 0
    signing_cpu_ns: int = 0
    wire_bytes: int = 0
    generator_done: bool = False
    error: str | None = None


def _configure_packet_loss(percent: float, mode: str) -> tuple[str, str | None]:
    if percent <= 0 or mode == "none":
        return "none", None
    if mode == "netem":
        command = ["tc", "qdisc", "replace", "dev", "eth0", "root", "netem", "loss", f"{percent}%"]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
            return "netem", completed.stderr.strip() or None
        except Exception as exc:
            return "application", f"netem unavailable; application fallback: {type(exc).__name__}: {exc}"
    return "application", None


def _generator(
    *,
    device: DeviceClient,
    outbox: ExperimentOutbox,
    events: int,
    batch_size: int,
    shared: SharedState,
    lock: threading.Lock,
    progress_step: int,
) -> None:
    try:
        batch: list[tuple[int, str, int, int]] = []
        for index in range(1, events + 1):
            cpu_clock = getattr(time, "thread_time_ns", time.process_time_ns)
            cpu_started = cpu_clock()
            event = device.create_event(
                "matrix",
                {
                    "reading_milliunits": index,
                    "quality": 100,
                    "source": device.device_id,
                },
            )
            signing_cpu_ns = max(1, cpu_clock() - cpu_started)
            wire = event.to_wire()
            encoded = json.dumps(wire, separators=(",", ":"), sort_keys=True)
            wire_bytes = len(encoded.encode("utf-8"))
            batch.append((event.sequence, encoded, wire_bytes, time.time_ns()))
            with lock:
                shared.generated += 1
                shared.signing_cpu_ns += signing_cpu_ns
                shared.wire_bytes += wire_bytes
            if len(batch) >= batch_size:
                outbox.append_many(batch)
                batch.clear()
            if index % progress_step == 0 or index == events:
                log.info("experiment_generation_progress", device_id=device.device_id, generated=index, target=events)
        outbox.append_many(batch)
    except Exception as exc:
        with lock:
            shared.error = f"generator: {type(exc).__name__}: {exc}"
        log.error("experiment_generator_failed", error=shared.error, exc_info=True)
    finally:
        with lock:
            shared.generator_done = True


def _sender(
    *,
    gateway_url: str,
    device_id: str,
    key: KeyPair,
    outbox: ExperimentOutbox,
    packet_loss_percent: float,
    packet_loss_mode: str,
    request_timeout: float,
    shared: SharedState,
    lock: threading.Lock,
    latency: Reservoir,
    random_seed: int,
    reconnect_deadline_seconds: int,
    progress_step: int,
) -> None:
    randomizer = random.Random(random_seed)
    enrolled = False
    last_progress = 0
    deadline_after_generation: float | None = None
    with httpx.Client(base_url=gateway_url, timeout=request_timeout) as client:
        while True:
            with lock:
                generator_done = shared.generator_done
                existing_error = shared.error
            if existing_error:
                return
            queued = outbox.count()
            if generator_done and queued == 0:
                return
            if generator_done and deadline_after_generation is None:
                deadline_after_generation = time.monotonic() + reconnect_deadline_seconds
            if deadline_after_generation is not None and time.monotonic() > deadline_after_generation:
                with lock:
                    shared.error = f"sender reconnect deadline exceeded with {queued} buffered events"
                return

            try:
                if not enrolled:
                    response = client.post(
                        "/devices",
                        json={"device_id": device_id, "public_key": key.public_bytes.hex()},
                    )
                    response.raise_for_status()
                    enrolled = True
                    log.info("experiment_device_enrolled", device_id=device_id, key_id=key_id(key.public_bytes))

                row = outbox.peek()
                if row is None:
                    time.sleep(0.01)
                    continue

                if packet_loss_mode == "application" and packet_loss_percent > 0 and randomizer.random() < packet_loss_percent / 100.0:
                    with lock:
                        shared.simulated_packet_drops += 1
                    time.sleep(0.001)
                    continue

                wire = json.loads(str(row["wire_json"]))
                started = time.perf_counter_ns()
                response = client.post("/events", json=wire)
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                response.raise_for_status()
                value = response.json()
                if not value.get("accepted"):
                    raise RuntimeError(f"gateway rejected event: {value}")
                outbox.acknowledge(int(row["sequence"]))
                latency.add(elapsed_ms)
                with lock:
                    shared.delivered += 1
                    shared.duplicate_deliveries += int(bool(value.get("duplicate")))
                    delivered = shared.delivered
                if delivered - last_progress >= progress_step:
                    last_progress = delivered
                    log.info("experiment_delivery_progress", device_id=device_id, delivered=delivered)
            except (httpx.HTTPError, OSError) as exc:
                enrolled = False
                with lock:
                    shared.network_errors += 1
                log.debug("experiment_delivery_waiting_for_network", device_id=device_id, error=f"{type(exc).__name__}: {exc}", buffered=queued)
                time.sleep(0.1)
            except Exception as exc:
                with lock:
                    shared.error = f"sender: {type(exc).__name__}: {exc}"
                log.error("experiment_sender_failed", error=shared.error, exc_info=True)
                return


def run_worker(
    *,
    device_id: str,
    gateway_url: str,
    events: int,
    state_dir: Path,
    packet_loss_percent: float,
    packet_loss_mode: str,
    request_timeout: float,
    reconnect_deadline_seconds: int,
    generation_batch: int,
    max_latency_samples: int,
    cpu_watts: float,
    random_seed: int,
) -> dict[str, Any]:
    if events < 0:
        raise ValueError("events must be non-negative")
    started_wall = time.time()
    started = time.perf_counter()
    state_dir.mkdir(parents=True, exist_ok=True)
    key = KeyPair.load_or_create(state_dir / "device.key")
    device = DeviceClient(device_id, key)
    outbox = ExperimentOutbox(state_dir / "experiment-outbox.db")
    effective_loss_mode, loss_note = _configure_packet_loss(packet_loss_percent, packet_loss_mode)
    shared = SharedState()
    lock = threading.Lock()
    latency = Reservoir(max_latency_samples, random_seed)
    progress_step = max(1, events // 10)

    log.info(
        "experiment_worker_started",
        device_id=device_id,
        events=events,
        gateway_url=gateway_url,
        packet_loss_percent=packet_loss_percent,
        requested_packet_loss_mode=packet_loss_mode,
        effective_packet_loss_mode=effective_loss_mode,
        packet_loss_note=loss_note,
    )

    generator = threading.Thread(
        target=_generator,
        kwargs={
            "device": device,
            "outbox": outbox,
            "events": events,
            "batch_size": generation_batch,
            "shared": shared,
            "lock": lock,
            "progress_step": progress_step,
        },
        name="matrix-generator",
        daemon=True,
    )
    sender = threading.Thread(
        target=_sender,
        kwargs={
            "gateway_url": gateway_url,
            "device_id": device_id,
            "key": key,
            "outbox": outbox,
            "packet_loss_percent": packet_loss_percent,
            "packet_loss_mode": effective_loss_mode,
            "request_timeout": request_timeout,
            "shared": shared,
            "lock": lock,
            "latency": latency,
            "random_seed": random_seed,
            "reconnect_deadline_seconds": reconnect_deadline_seconds,
            "progress_step": progress_step,
        },
        name="matrix-sender",
        daemon=True,
    )
    generator.start()
    sender.start()
    generator.join()
    sender.join()

    elapsed = time.perf_counter() - started
    with lock:
        snapshot = SharedState(**shared.__dict__)
    if snapshot.error:
        raise RuntimeError(snapshot.error)
    if snapshot.generated != events or snapshot.delivered != events:
        raise RuntimeError(
            f"worker incomplete: generated={snapshot.generated}, delivered={snapshot.delivered}, target={events}"
        )
    signing_seconds = snapshot.signing_cpu_ns / 1_000_000_000
    result = {
        "status": "PASS",
        "device_id": device_id,
        "started_at_epoch": started_wall,
        "completed_at_epoch": time.time(),
        "events_target": events,
        "events_generated": snapshot.generated,
        "events_delivered": snapshot.delivered,
        "duplicate_deliveries": snapshot.duplicate_deliveries,
        "network_errors": snapshot.network_errors,
        "simulated_packet_drops": snapshot.simulated_packet_drops,
        "packet_loss_percent": packet_loss_percent,
        "requested_packet_loss_mode": packet_loss_mode,
        "effective_packet_loss_mode": effective_loss_mode,
        "packet_loss_note": loss_note,
        "elapsed_seconds": round(elapsed, 6),
        "delivery_events_per_second": round(events / elapsed, 3) if elapsed else None,
        "wire_bytes_total": snapshot.wire_bytes,
        "wire_bytes_per_event": round(snapshot.wire_bytes / events, 3) if events else 0,
        "signing_cpu_seconds": round(signing_seconds, 9),
        "signing_cpu_ns_per_event": round(snapshot.signing_cpu_ns / events, 3) if events else 0,
        "signing_energy_estimate_joules": round(signing_seconds * cpu_watts, 9),
        "signing_energy_estimate_microjoules_per_event": round(signing_seconds * cpu_watts * 1_000_000 / events, 6) if events else 0,
        "energy_measurement_method": f"process CPU time multiplied by configured {cpu_watts:g} W; use RAPL benchmark for hardware energy",
        "latency": latency.summary(),
        "outbox_remaining": outbox.count(),
        "outbox_storage_bytes": outbox.size_bytes(),
    }
    (state_dir / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("experiment_worker_completed", **{key: value for key, value in result.items() if key not in {"latency"}})
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one scalable EdgeChainDB matrix device")
    parser.add_argument("--device-id", default=os.getenv("DEVICE_ID"), required=os.getenv("DEVICE_ID") is None)
    parser.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL", "http://gateway:8000"))
    parser.add_argument("--events", type=int, default=int(os.getenv("DEVICE_EVENTS", "1000")))
    parser.add_argument("--state-dir", default=os.getenv("DEVICE_STATE_DIR", "/data"))
    parser.add_argument("--packet-loss-percent", type=float, default=float(os.getenv("DEVICE_PACKET_LOSS_PERCENT", "0")))
    parser.add_argument("--packet-loss-mode", choices=("netem", "application", "none"), default=os.getenv("DEVICE_PACKET_LOSS_MODE", "netem"))
    parser.add_argument("--request-timeout", type=float, default=float(os.getenv("DEVICE_REQUEST_TIMEOUT", "5")))
    parser.add_argument("--reconnect-deadline-seconds", type=int, default=int(os.getenv("DEVICE_RECONNECT_DEADLINE_SECONDS", "900")))
    parser.add_argument("--generation-batch", type=int, default=int(os.getenv("DEVICE_GENERATION_BATCH", "250")))
    parser.add_argument("--max-latency-samples", type=int, default=int(os.getenv("DEVICE_MAX_LATENCY_SAMPLES", "10000")))
    parser.add_argument("--cpu-watts", type=float, default=float(os.getenv("DEVICE_CPU_WATTS", "15")))
    parser.add_argument("--random-seed", type=int, default=int(os.getenv("DEVICE_RANDOM_SEED", "1")))
    args = parser.parse_args()
    try:
        run_worker(
            device_id=args.device_id,
            gateway_url=args.gateway_url,
            events=args.events,
            state_dir=Path(args.state_dir),
            packet_loss_percent=args.packet_loss_percent,
            packet_loss_mode=args.packet_loss_mode,
            request_timeout=args.request_timeout,
            reconnect_deadline_seconds=args.reconnect_deadline_seconds,
            generation_batch=args.generation_batch,
            max_latency_samples=args.max_latency_samples,
            cpu_watts=args.cpu_watts,
            random_seed=args.random_seed,
        )
    except Exception as exc:
        failure = {"status": "FAIL", "device_id": args.device_id, "error": f"{type(exc).__name__}: {exc}"}
        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.state_dir) / "metrics.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
        log.error("experiment_worker_failed", **failure, exc_info=True)
        print(json.dumps(failure, sort_keys=True), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
