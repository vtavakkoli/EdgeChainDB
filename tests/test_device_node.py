from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from edgechaindb.api import create_app
from edgechaindb.device_node import run_device


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_container_device_process_persists_and_resumes(tmp_path):
    app = create_app(
        database_path=str(tmp_path / "gateway.db"),
        node_key_path=str(tmp_path / "gateway.key"),
        node_id="test-gateway",
        quorum_threshold=1,
        batch_size=100,
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    else:
        raise AssertionError("test gateway did not start")

    try:
        state_dir = tmp_path / "device-state"
        first = run_device(
            device_id="iot-device-01",
            gateway_url=base_url,
            state_dir=state_dir,
            events=3,
            interval_ms=0,
            startup_jitter_ms=0,
            request_timeout=5,
            retries=5,
            continuous=False,
        )
        second = run_device(
            device_id="iot-device-01",
            gateway_url=base_url,
            state_dir=state_dir,
            events=2,
            interval_ms=0,
            startup_jitter_ms=0,
            request_timeout=5,
            retries=5,
            continuous=False,
        )
        assert first["last_sequence"] == 3
        assert second["last_sequence"] == 5
        checkpoint = httpx.get(
            f"{base_url}/devices/iot-device-01/checkpoint", timeout=5
        ).json()
        assert checkpoint["last_sequence"] == 5
        assert (state_dir / "device.key").exists()
        assert (state_dir / "state.json").exists()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
