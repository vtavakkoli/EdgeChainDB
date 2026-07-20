from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import time
from typing import Any

import httpx

from .crypto import KeyPair
from .device import DeviceClient


class DeviceStateError(RuntimeError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def wait_for_gateway(client: httpx.Client, retries: int, delay: float) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.get("/health")
            response.raise_for_status()
            if response.json().get("status") == "ok":
                return
        except Exception as exc:
            last_error = exc
        time.sleep(min(delay * attempt, 5.0))
    raise RuntimeError(f"gateway did not become ready: {last_error}")


def submit_with_retry(
    client: httpx.Client,
    event_body: dict[str, Any],
    retries: int,
    delay: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.post("/events", json=event_body)
            if response.status_code == 202:
                return response.json()
            # Validation failures are deterministic and should not be retried.
            if 400 <= response.status_code < 500:
                raise DeviceStateError(
                    f"event rejected ({response.status_code}): {response.text}"
                )
            response.raise_for_status()
        except DeviceStateError:
            raise
        except Exception as exc:
            last_error = exc
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
    if startup_jitter_ms:
        time.sleep(random.uniform(0, startup_jitter_ms) / 1000.0)

    state_dir.mkdir(parents=True, exist_ok=True)
    key = KeyPair.load_or_create(state_dir / "device.key")
    state_path = state_dir / "state.json"
    local_state = {"sequence": 0, "previous_event_hash": "00" * 32}
    if state_path.exists():
        local_state = json.loads(state_path.read_text(encoding="utf-8"))

    with httpx.Client(base_url=gateway_url, timeout=request_timeout) as client:
        wait_for_gateway(client, retries, 0.25)
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

        if local_sequence > server_sequence:
            raise DeviceStateError(
                "local device state is ahead of the gateway; refusing to fork the chain"
            )
        # The gateway is authoritative for already acknowledged events. This also
        # repairs the common crash-after-acceptance-before-local-save case.
        client_state = DeviceClient(device_id, key)
        client_state.restore(server_sequence, bytes.fromhex(server_hash))
        _atomic_json(
            state_path,
            {"sequence": server_sequence, "previous_event_hash": server_hash},
        )

        sent = 0
        while continuous or sent < events:
            sensor_index = int(device_id.rsplit("-", 1)[-1]) if "-" in device_id else 0
            payload = {
                "temperature_milli_celsius": 20000
                + sensor_index * 17
                + random.randint(-250, 250),
                "humidity_basis_points": 4500 + random.randint(-300, 300),
                "battery_millivolts": 3700 + random.randint(-40, 40),
                "quality": 100,
            }
            event = client_state.create_event("environment", payload)
            result = submit_with_retry(client, event.to_wire(), retries, 0.2)
            if not result.get("accepted"):
                raise RuntimeError(f"gateway did not accept event: {result}")
            _atomic_json(
                state_path,
                {
                    "sequence": event.sequence,
                    "previous_event_hash": event.event_hash.hex(),
                },
            )
            sent += 1
            if interval_ms:
                time.sleep(interval_ms / 1000.0)

    return {
        "device_id": device_id,
        "events_sent": sent,
        "last_sequence": client_state.sequence,
        "last_event_hash": client_state.previous_event_hash.hex(),
    }


def main() -> None:
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
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
