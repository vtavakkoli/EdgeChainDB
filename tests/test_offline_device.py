import json
from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from edgechaindb.api import create_app
import edgechaindb.device_node as device_node


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_continuous_device_buffers_offline_then_reconnects(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    state_dir = tmp_path / "device"
    errors: list[BaseException] = []
    device_node._shutdown_requested = False

    def run() -> None:
        try:
            device_node.run_device(
                device_id="iot-device-99",
                gateway_url=base_url,
                state_dir=state_dir,
                events=1,
                interval_ms=20,
                startup_jitter_ms=0,
                request_timeout=0.1,
                retries=1,
                continuous=True,
                max_buffered_events=100,
            )
        except BaseException as exc:  # captured for assertion in the test thread
            errors.append(exc)

    device_thread = threading.Thread(target=run, daemon=True)
    device_thread.start()
    deadline = time.time() + 8
    while time.time() < deadline:
        path = state_dir / "outbox.json"
        if path.exists() and len(json.loads(path.read_text())) >= 3:
            break
        time.sleep(0.05)
    else:
        device_node._shutdown_requested = True
        device_thread.join(timeout=5)
        raise AssertionError("device did not buffer events while the gateway was offline")

    app = create_app(
        database_path=str(tmp_path / "gateway.db"),
        node_key_path=str(tmp_path / "gateway.key"),
        node_id="offline-test-gateway",
        quorum_threshold=1,
        batch_size=25,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        deadline = time.time() + 12
        checkpoint = None
        while time.time() < deadline:
            try:
                response = httpx.get(
                    f"{base_url}/devices/iot-device-99/checkpoint", timeout=1
                )
                if response.status_code == 200:
                    checkpoint = response.json()
                    outbox = json.loads((state_dir / "outbox.json").read_text())
                    if checkpoint["last_sequence"] >= 3 and not outbox:
                        break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"device did not flush after reconnection; checkpoint={checkpoint}"
            )
    finally:
        device_node._shutdown_requested = True
        device_thread.join(timeout=8)
        server.should_exit = True
        server_thread.join(timeout=8)
        device_node._shutdown_requested = False

    assert not errors
    assert not device_thread.is_alive()
