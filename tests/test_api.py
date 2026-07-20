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
