from fastapi.testclient import TestClient

from edgechaindb.api import create_app
from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient


def test_api_accepts_and_finalizes_event(tmp_path):
    app = create_app(
        database_path=str(tmp_path / "api.db"),
        node_key_path=str(tmp_path / "node.key"),
        node_id="node-a",
        quorum_threshold=1,
        batch_size=10,
    )
    client = TestClient(app)

    device_key = KeyPair.generate()
    response = client.post(
        "/devices",
        json={
            "device_id": "api-sensor",
            "public_key": device_key.public_bytes.hex(),
        },
    )
    assert response.status_code == 201

    device = DeviceClient("api-sensor", device_key)
    event = device.create_event("state", {"on": True})
    response = client.post("/events", json=event.to_wire())
    assert response.status_code == 202

    response = client.post("/blocks/seal")
    assert response.status_code == 200
    assert response.json()["status"] == "finalized"

    response = client.get("/verify")
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_dashboard_and_cluster_event_monitor(tmp_path):
    app = create_app(
        database_path=str(tmp_path / "monitor.db"),
        node_key_path=str(tmp_path / "monitor.key"),
        node_id="monitor-gateway",
        quorum_threshold=1,
        batch_size=100,
    )
    client = TestClient(app)

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "EdgeChainDB Cluster Monitor" in dashboard.text
    assert "Research metrics" in dashboard.text

    device_key = KeyPair.generate()
    assert client.post(
        "/devices",
        json={"device_id": "iot-device-01", "public_key": device_key.public_bytes.hex()},
    ).status_code == 201
    device = DeviceClient("iot-device-01", device_key)
    event = device.create_event("temperature", {"temperature_milli_celsius": 22000})
    assert client.post("/events", json=event.to_wire()).status_code == 202

    events = client.get("/cluster/events?limit=5")
    assert events.status_code == 200
    assert events.json()[0]["payload"]["temperature_milli_celsius"] == 22000

    state = client.get("/cluster/state")
    assert state.status_code == 200
    device_state = next(
        item for item in state.json()["devices"] if item["device_id"] == "iot-device-01"
    )
    assert device_state["last_sequence"] == 1


def test_cluster_state_exposes_live_sensor_observability(tmp_path):
    app = create_app(
        database_path=str(tmp_path / "observability.db"),
        node_key_path=str(tmp_path / "observability.key"),
        node_id="observability-gateway",
        quorum_threshold=1,
        batch_size=100,
    )
    client = TestClient(app)
    key = KeyPair.generate()
    assert client.post(
        "/devices",
        json={"device_id": "iot-device-02", "public_key": key.public_bytes.hex()},
    ).status_code == 201
    device = DeviceClient("iot-device-02", key)
    event = device.create_event(
        "environment",
        {
            "temperature_milli_celsius": 21340,
            "humidity_basis_points": 4875,
            "battery_millivolts": 3690,
            "quality": 99,
        },
    )
    assert client.post("/events", json=event.to_wire()).status_code == 202

    response = client.get("/cluster/state?include_metrics=false")
    assert response.status_code == 200
    body = response.json()
    assert body["monitor_port"] == 3030
    observed = next(
        item for item in body["devices"] if item["device_id"] == "iot-device-02"
    )
    assert observed["last_payload"]["temperature_milli_celsius"] == 21340
    assert observed["last_payload"]["battery_millivolts"] == 3690
    assert observed["events_last_minute"] >= 1
    assert observed["clock_lag_ms"] is not None


def test_monitor_health_and_database_metadata(tmp_path):
    app = create_app(
        database_path=str(tmp_path / "metadata.db"),
        node_key_path=str(tmp_path / "metadata.key"),
        node_id="metadata-gateway",
        quorum_threshold=1,
        batch_size=25,
    )
    app.state.api_port = 18000
    app.state.monitor_port = 13030
    client = TestClient(app)

    monitor = client.get("/monitor/health")
    assert monitor.status_code == 200
    assert monitor.json()["dashboard"] == "ready"
    assert monitor.json()["monitor_port"] == 13030

    info = client.get("/database/info?quick_check=true")
    assert info.status_code == 200
    body = info.json()
    assert body["engine"] == "SQLite"
    assert body["pragmas"]["journal_mode"].lower() == "wal"
    assert body["quick_check"] == "ok"
    assert "events" in body["tables"]
    assert "Merkle-rooted blocks" in body["features"]


def test_research_benchmark_artifacts_are_served_safely(tmp_path, monkeypatch):
    benchmark_dir = tmp_path / "results" / "benchmarks"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "summary.json").write_text(
        '{"generated_at":"2026-07-20T00:00:00Z","passed":8,"failed":0,"benchmarks":[]}',
        encoding="utf-8",
    )
    (benchmark_dir / "report.html").write_text(
        "<html><body>research report</body></html>", encoding="utf-8"
    )
    (benchmark_dir / "signing_energy.json").write_text(
        '{"status":"PASS"}', encoding="utf-8"
    )
    monkeypatch.setenv("EDGECHAIN_RESULT_DIR", str(tmp_path / "results"))
    app = create_app(
        database_path=str(tmp_path / "artifact.db"),
        node_key_path=str(tmp_path / "artifact.key"),
        node_id="artifact-gateway",
        quorum_threshold=1,
        batch_size=25,
    )
    client = TestClient(app)

    summary = client.get("/benchmark/research/summary")
    assert summary.status_code == 200
    assert summary.json()["passed"] == 8
    assert summary.json()["status"] == "completed"
    assert client.get("/benchmark/research/report").status_code == 200
    assert client.get(
        "/benchmark/research/artifacts/signing_energy.json"
    ).status_code == 200
    assert client.get(
        "/benchmark/research/artifacts/../result.json"
    ).status_code in {400, 404}
