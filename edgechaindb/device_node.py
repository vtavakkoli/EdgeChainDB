from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import signal
import time
from typing import Any

import httpx

from .crypto import KeyPair, key_id
from .device import DeviceClient
from .observability import get_logger
from .outbox import DurableOutbox


class DeviceStateError(RuntimeError):
    pass


_shutdown_requested = False


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def wait_for_gateway(
    client: httpx.Client,
    retries: int,
    delay: float,
    *,
    logger=None,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.get("/health")
            response.raise_for_status()
            if response.json().get("status") == "ok":
                if logger:
                    logger.info("gateway_ready", attempt=attempt)
                return
        except Exception as exc:
            last_error = exc
            if logger:
                logger.warning(
                    "gateway_wait_retry",
                    attempt=attempt,
                    retries=retries,
                    error=f"{type(exc).__name__}: {exc}",
                )
        time.sleep(min(delay * attempt, 5.0))
    raise RuntimeError(f"gateway did not become ready: {last_error}")


def submit_with_retry(
    client: httpx.Client,
    event_body: dict[str, Any],
    retries: int,
    delay: float,
    *,
    logger=None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            response = client.post("/events", json=event_body)
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            if response.status_code == 202:
                result = response.json()
                if logger:
                    logger.info(
                        "event_delivery_accepted",
                        sequence=event_body.get("sequence"),
                        event_type=event_body.get("event_type"),
                        attempt=attempt,
                        duration_ms=duration_ms,
                        duplicate=result.get("duplicate", False),
                        event_hash=result.get("event_hash"),
                    )
                return result
            if 400 <= response.status_code < 500:
                raise DeviceStateError(
                    f"event rejected ({response.status_code}): {response.text}"
                )
            response.raise_for_status()
        except DeviceStateError:
            if logger:
                logger.error(
                    "event_delivery_rejected",
                    sequence=event_body.get("sequence"),
                    event_type=event_body.get("event_type"),
                    status_code=response.status_code,
                    response=response.text,
                )
            raise
        except Exception as exc:
            last_error = exc
            if logger:
                logger.warning(
                    "event_delivery_retry",
                    sequence=event_body.get("sequence"),
                    attempt=attempt,
                    retries=retries,
                    error=f"{type(exc).__name__}: {exc}",
                )
            time.sleep(min(delay * attempt, 5.0))
    raise RuntimeError(f"event submission failed after retries: {last_error}")


def _sync_and_flush(
    *,
    client: httpx.Client,
    device_id: str,
    key: KeyPair,
    client_state: DeviceClient,
    local_state: dict[str, Any],
    state_path: Path,
    outbox: DurableOutbox,
    retries: int,
    logger: Any,
) -> int:
    """Enroll, reconcile the durable outbox, and deliver pending events."""

    wait_for_gateway(client, retries, 0.2, logger=logger)
    enrollment = client.post(
        "/devices",
        json={"device_id": device_id, "public_key": key.public_bytes.hex()},
    )
    enrollment.raise_for_status()
    checkpoint_response = client.get(f"/devices/{device_id}/checkpoint")
    checkpoint_response.raise_for_status()
    checkpoint = checkpoint_response.json()
    server_sequence = int(checkpoint["last_sequence"])
    server_hash = str(checkpoint["last_event_hash"])
    local_sequence = int(local_state.get("sequence", 0))

    if server_sequence > local_sequence:
        raise DeviceStateError(
            "gateway checkpoint is ahead of local durable state; refusing an unsafe rollback"
        )
    outbox.reconcile(server_sequence)
    if local_sequence > server_sequence and not outbox.items():
        raise DeviceStateError(
            "local state is ahead of the gateway but the durable outbox is empty"
        )
    if local_sequence == server_sequence:
        local_state.update(
            {"sequence": server_sequence, "previous_event_hash": server_hash}
        )
        _atomic_json(state_path, local_state)
        client_state.restore(server_sequence, bytes.fromhex(server_hash))

    delivered = 0
    for wire in outbox.items():
        result = submit_with_retry(client, wire, retries, 0.2, logger=logger)
        if not result.get("accepted"):
            raise RuntimeError(f"gateway did not accept buffered event: {result}")
        outbox.acknowledge(int(wire["sequence"]))
        delivered += 1
        logger.info(
            "offline_event_replayed",
            sequence=wire["sequence"],
            duplicate=result.get("duplicate", False),
            remaining_buffered_events=len(outbox),
        )
    logger.info(
        "device_gateway_synchronized",
        created=enrollment.json().get("created"),
        gateway_sequence=server_sequence,
        local_sequence=local_sequence,
        buffered_events_delivered=delivered,
    )
    return delivered


def run_device(
    *,
    device_id: str,
    gateway_url: str,
    state_dir: Path,
    events: int,
    interval_ms: int,
    startup_jitter_ms: int,
    request_timeout: float,
    retries: int,
    continuous: bool,
    max_buffered_events: int = 10_000,
) -> dict[str, Any]:
    logger = get_logger(device_id)
    started = time.perf_counter()
    jitter_ms = random.uniform(0, startup_jitter_ms) if startup_jitter_ms else 0
    logger.info(
        "device_starting",
        device_id=device_id,
        gateway_url=gateway_url,
        state_dir=str(state_dir),
        continuous=continuous,
        configured_events=events,
        interval_ms=interval_ms,
        startup_jitter_ms=round(jitter_ms, 3),
        request_timeout=request_timeout,
        retries=retries,
        max_buffered_events=max_buffered_events,
    )
    if jitter_ms:
        time.sleep(jitter_ms / 1000.0)

    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / "device.key"
    key_existed = key_path.exists()
    key = KeyPair.load_or_create(key_path)
    state_path = state_dir / "state.json"
    outbox = DurableOutbox(state_dir / "outbox.json")
    local_state: dict[str, Any] = {
        "sequence": 0,
        "previous_event_hash": "00" * 32,
    }
    if state_path.exists():
        local_state = json.loads(state_path.read_text(encoding="utf-8"))

    latest_buffered = outbox.latest()
    if latest_buffered and int(latest_buffered["sequence"]) > int(local_state.get("sequence", 0)):
        # Recover the narrow crash window between durable outbox append and state-file update.
        local_state = {
            "sequence": int(latest_buffered["sequence"]),
            "previous_event_hash": str(latest_buffered["event_hash"]),
        }
        _atomic_json(state_path, local_state)

    logger.info(
        "device_identity_loaded",
        device_id=device_id,
        key_id=key_id(key.public_bytes),
        existing_key=key_existed,
        local_sequence=int(local_state.get("sequence", 0)),
        buffered_events=len(outbox),
    )

    generated = 0
    delivered = 0
    client_state = DeviceClient(device_id, key)
    client_state.restore(
        int(local_state.get("sequence", 0)),
        bytes.fromhex(str(local_state.get("previous_event_hash", "00" * 32))),
    )
    try:
        with httpx.Client(base_url=gateway_url, timeout=request_timeout) as client:
            try:
                delivered += _sync_and_flush(
                    client=client,
                    device_id=device_id,
                    key=key,
                    client_state=client_state,
                    local_state=local_state,
                    state_path=state_path,
                    outbox=outbox,
                    retries=retries if not continuous else min(retries, 3),
                    logger=logger,
                )
            except Exception as exc:
                if not continuous:
                    raise
                logger.warning(
                    "device_entered_offline_mode",
                    error=f"{type(exc).__name__}: {exc}",
                    buffered_events=len(outbox),
                )

            while ((continuous and not _shutdown_requested) or generated < events):
                if _shutdown_requested:
                    break

                # Reconnect and flush before generating more data when possible.
                if len(outbox):
                    try:
                        delivered += _sync_and_flush(
                            client=client,
                            device_id=device_id,
                            key=key,
                            client_state=client_state,
                            local_state=local_state,
                            state_path=state_path,
                            outbox=outbox,
                            retries=1 if continuous else retries,
                            logger=logger,
                        )
                    except (RuntimeError, httpx.HTTPError, DeviceStateError) as exc:
                        logger.warning(
                            "offline_flush_deferred",
                            error=f"{type(exc).__name__}: {exc}",
                            buffered_events=len(outbox),
                        )
                        if not continuous:
                            raise

                if len(outbox) >= max_buffered_events:
                    logger.error(
                        "offline_buffer_full",
                        buffered_events=len(outbox),
                        max_buffered_events=max_buffered_events,
                    )
                    if not continuous:
                        raise RuntimeError("offline buffer is full")
                    time.sleep(max(interval_ms / 1000.0, 0.25))
                    continue

                sensor_index = (
                    int(device_id.rsplit("-", 1)[-1]) if "-" in device_id else 0
                )
                payload = {
                    "temperature_milli_celsius": 20000
                    + sensor_index * 17
                    + random.randint(-250, 250),
                    "humidity_basis_points": 4500 + random.randint(-300, 300),
                    "battery_millivolts": 3700 + random.randint(-40, 40),
                    "quality": 100,
                }
                event = client_state.create_event("environment", payload)
                wire = event.to_wire()
                outbox.append(wire)
                local_state = {
                    "sequence": event.sequence,
                    "previous_event_hash": event.event_hash.hex(),
                }
                _atomic_json(state_path, local_state)
                generated += 1
                logger.info(
                    "sensor_sample_buffered",
                    sequence=event.sequence,
                    payload=payload,
                    buffered_events=len(outbox),
                    state_path=str(state_path),
                )

                try:
                    delivered += _sync_and_flush(
                        client=client,
                        device_id=device_id,
                        key=key,
                        client_state=client_state,
                        local_state=local_state,
                        state_path=state_path,
                        outbox=outbox,
                        retries=1 if continuous else retries,
                        logger=logger,
                    )
                except (RuntimeError, httpx.HTTPError, DeviceStateError) as exc:
                    logger.warning(
                        "event_retained_for_reconnection",
                        sequence=event.sequence,
                        error=f"{type(exc).__name__}: {exc}",
                        buffered_events=len(outbox),
                    )
                    if not continuous:
                        raise

                if interval_ms and not _shutdown_requested:
                    time.sleep(interval_ms / 1000.0)
    except Exception as exc:
        logger.error(
            "device_failed",
            error=f"{type(exc).__name__}: {exc}",
            events_generated=generated,
            events_delivered=delivered,
            buffered_events=len(outbox),
            last_sequence=client_state.sequence,
            exc_info=True,
        )
        raise
    finally:
        logger.info(
            "device_stopped",
            shutdown_requested=_shutdown_requested,
            events_generated=generated,
            events_delivered=delivered,
            buffered_events=len(outbox),
            last_sequence=client_state.sequence,
            duration_seconds=round(time.perf_counter() - started, 3),
        )

    return {
        "device_id": device_id,
        "events_sent": generated,
        "events_generated": generated,
        "events_delivered": delivered,
        "buffered_events": len(outbox),
        "last_sequence": client_state.sequence,
        "last_event_hash": client_state.previous_event_hash.hex(),
    }


def main() -> None:
    global _shutdown_requested

    parser = argparse.ArgumentParser(description="Run one EdgeChainDB IoT device")
    parser.add_argument("--device-id", default=os.getenv("DEVICE_ID"))
    parser.add_argument(
        "--gateway-url", default=os.getenv("GATEWAY_URL", "http://gateway:8000")
    )
    parser.add_argument("--state-dir", default=os.getenv("DEVICE_STATE_DIR", "/data"))
    parser.add_argument("--events", type=int, default=int(os.getenv("DEVICE_EVENTS", "8")))
    parser.add_argument(
        "--interval-ms", type=int, default=int(os.getenv("DEVICE_INTERVAL_MS", "25"))
    )
    parser.add_argument(
        "--startup-jitter-ms",
        type=int,
        default=int(os.getenv("DEVICE_STARTUP_JITTER_MS", "1000")),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.getenv("DEVICE_REQUEST_TIMEOUT", "10")),
    )
    parser.add_argument(
        "--retries", type=int, default=int(os.getenv("DEVICE_MAX_RETRIES", "30"))
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        default=os.getenv("DEVICE_CONTINUOUS", "0") == "1",
    )
    parser.add_argument(
        "--max-buffered-events",
        type=int,
        default=int(os.getenv("DEVICE_MAX_BUFFERED_EVENTS", "10000")),
    )
    args = parser.parse_args()
    if not args.device_id:
        parser.error("--device-id or DEVICE_ID is required")

    def request_shutdown(signum: int, _: Any) -> None:
        global _shutdown_requested
        _shutdown_requested = True
        get_logger(args.device_id).warning(
            "shutdown_signal_received", signal=signum
        )

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    result = run_device(
        device_id=args.device_id,
        gateway_url=args.gateway_url,
        state_dir=Path(args.state_dir),
        events=args.events,
        interval_ms=args.interval_ms,
        startup_jitter_ms=args.startup_jitter_ms,
        request_timeout=args.request_timeout,
        retries=args.retries,
        continuous=args.continuous,
        max_buffered_events=args.max_buffered_events,
    )
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
