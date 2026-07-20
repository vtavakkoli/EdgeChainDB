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
    )
    if jitter_ms:
        time.sleep(jitter_ms / 1000.0)

    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / "device.key"
    key_existed = key_path.exists()
    key = KeyPair.load_or_create(key_path)
    state_path = state_dir / "state.json"
    local_state = {"sequence": 0, "previous_event_hash": "00" * 32}
    if state_path.exists():
        local_state = json.loads(state_path.read_text(encoding="utf-8"))
    logger.info(
        "device_identity_loaded",
        device_id=device_id,
        key_id=key_id(key.public_bytes),
        existing_key=key_existed,
        local_sequence=int(local_state.get("sequence", 0)),
    )

    sent = 0
    client_state = DeviceClient(device_id, key)
    try:
        with httpx.Client(base_url=gateway_url, timeout=request_timeout) as client:
            wait_for_gateway(client, retries, 0.25, logger=logger)
            enrollment = client.post(
                "/devices",
                json={"device_id": device_id, "public_key": key.public_bytes.hex()},
            )
            enrollment.raise_for_status()
            enrollment_data = enrollment.json()
            logger.info(
                "device_enrollment_completed",
                device_id=device_id,
                created=enrollment_data.get("created"),
                gateway_sequence=enrollment_data.get("last_sequence"),
            )

            checkpoint_response = client.get(f"/devices/{device_id}/checkpoint")
            checkpoint_response.raise_for_status()
            checkpoint = checkpoint_response.json()
            server_sequence = int(checkpoint["last_sequence"])
            server_hash = str(checkpoint["last_event_hash"])
            local_sequence = int(local_state.get("sequence", 0))

            if local_sequence > server_sequence:
                logger.error(
                    "device_state_ahead_of_gateway",
                    local_sequence=local_sequence,
                    gateway_sequence=server_sequence,
                )
                raise DeviceStateError(
                    "local device state is ahead of the gateway; refusing to fork the chain"
                )

            client_state.restore(server_sequence, bytes.fromhex(server_hash))
            _atomic_json(
                state_path,
                {"sequence": server_sequence, "previous_event_hash": server_hash},
            )
            logger.info(
                "checkpoint_synchronized",
                local_sequence_before=local_sequence,
                gateway_sequence=server_sequence,
                repaired=local_sequence != server_sequence,
                previous_event_hash=server_hash,
            )

            while ((continuous and not _shutdown_requested) or sent < events):
                if _shutdown_requested:
                    break
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
                logger.info(
                    "sensor_sample_created",
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=payload,
                    previous_event_hash=event.previous_event_hash.hex(),
                )
                result = submit_with_retry(
                    client,
                    event.to_wire(),
                    retries,
                    0.2,
                    logger=logger,
                )
                if not result.get("accepted"):
                    raise RuntimeError(f"gateway did not accept event: {result}")
                _atomic_json(
                    state_path,
                    {
                        "sequence": event.sequence,
                        "previous_event_hash": event.event_hash.hex(),
                    },
                )
                logger.info(
                    "device_state_persisted",
                    sequence=event.sequence,
                    event_hash=event.event_hash.hex(),
                    state_path=str(state_path),
                )
                sent += 1
                if interval_ms and not _shutdown_requested:
                    time.sleep(interval_ms / 1000.0)
    except Exception as exc:
        logger.error(
            "device_failed",
            error=f"{type(exc).__name__}: {exc}",
            events_sent=sent,
            last_sequence=client_state.sequence,
            exc_info=True,
        )
        raise
    finally:
        logger.info(
            "device_stopped",
            shutdown_requested=_shutdown_requested,
            events_sent=sent,
            last_sequence=client_state.sequence,
            duration_seconds=round(time.perf_counter() - started, 3),
        )

    return {
        "device_id": device_id,
        "events_sent": sent,
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
    )
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
