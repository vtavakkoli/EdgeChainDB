from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def _free_port() -> int:
    with socket.socket() as value:
        value.bind(("127.0.0.1", 0))
        return int(value.getsockname()[1])


def _read_json(url: str, timeout: float = 1.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def test_dual_api_and_monitor_listeners(tmp_path: Path):
    api_port = _free_port()
    monitor_port = _free_port()
    while monitor_port == api_port:
        monitor_port = _free_port()

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "edgechaindb.gateway_server",
            "--database",
            str(tmp_path / "dual.db"),
            "--node-key",
            str(tmp_path / "dual.key"),
            "--host",
            "127.0.0.1",
            "--api-port",
            str(api_port),
            "--monitor-port",
            str(monitor_port),
            "--log-level",
            "WARNING",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise AssertionError(f"gateway exited early: {output}")
            try:
                api_health = _read_json(f"http://127.0.0.1:{api_port}/health")
                monitor_health = _read_json(
                    f"http://127.0.0.1:{monitor_port}/monitor/health"
                )
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        else:
            raise AssertionError(f"listeners did not become ready: {last_error}")

        assert api_health["status"] == "ok"
        assert monitor_health["dashboard"] == "ready"
        assert monitor_health["api_port"] == api_port
        assert monitor_health["monitor_port"] == monitor_port

        with urllib.request.urlopen(
            f"http://127.0.0.1:{monitor_port}/", timeout=2
        ) as response:
            html = response.read()
        assert b"EdgeChainDB Cluster Monitor" in html
        assert len(html) > 20_000

        database = _read_json(
            f"http://127.0.0.1:{monitor_port}/database/info?quick_check=true",
            timeout=5,
        )
        assert database["quick_check"] == "ok"
        assert database["pragmas"]["journal_mode"].lower() == "wal"
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
