from pathlib import Path
import json
import socket
import threading
import time

from fastapi import FastAPI
import uvicorn

from edgechaindb.recovery_report import append_recovery_result


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_restart_recovery_allows_slow_full_verification(tmp_path: Path):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "events": 10_000, "blocks": 400}

    @app.get("/verify")
    def verify():
        time.sleep(0.2)
        return {"valid": True, "events": 10_000, "blocks": 400, "errors": []}

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)

    payload = {
        "title": "Recovery test",
        "generated_at": "2026-07-20T00:00:00+00:00",
        "environment": {"mode": "remote"},
        "summary": {"passed": 0, "failed": 0, "skipped": 1, "total": 1},
        "scenarios": [
            {
                "name": "Persistent restart recovery",
                "category": "Durability",
                "status": "SKIP",
                "duration_ms": 0,
                "details": "pending",
                "metrics": {},
            }
        ],
        "notes": [],
        "extra": {},
    }
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    try:
        assert append_recovery_result(
            base_url=f"http://127.0.0.1:{port}",
            result_dir=tmp_path,
            timeout=1,
        )
        result = json.loads((tmp_path / "result.json").read_text())
        scenario = result["scenarios"][-1]
        assert scenario["status"] == "PASS"
        assert scenario["metrics"]["events"] == 10_000
        assert (tmp_path / "report.html").exists()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
