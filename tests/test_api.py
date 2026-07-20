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
