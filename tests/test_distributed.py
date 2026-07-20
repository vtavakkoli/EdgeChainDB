from concurrent.futures import ThreadPoolExecutor

import pytest

from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient
from edgechaindb.ledger import EdgeChainLedger
from edgechaindb.store import Database


def test_twenty_devices_concurrently_build_valid_chains(tmp_path):
    database = Database(tmp_path / "twenty.db")
    ledger = EdgeChainLedger(database, quorum_threshold=1)
    authority = KeyPair.generate()
    ledger.register_authority("gateway", authority.public_bytes)

    def worker(index: int) -> int:
        key = KeyPair.generate()
        device_id = f"device-{index:02d}"
        ledger.register_device(device_id, key.public_bytes)
        device = DeviceClient(device_id, key)
        for reading in range(10):
            ledger.accept_event(
                device.create_event(
                    "load",
                    {"reading_milliunits": index * 1000 + reading},
                )
            )
        return 10

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(worker, range(1, 21))) == 200

    while database.pending_count():
        ledger.propose_block("gateway", authority.private_key, max_events=32)

    result = ledger.verify_all()
    assert result["valid"], result["errors"]
    assert result["events"] == 200
    assert result["blocks"] == 7


def test_retry_idempotency_and_enrollment_conflict(tmp_path):
    database = Database(tmp_path / "retry.db")
    ledger = EdgeChainLedger(database)
    key = KeyPair.generate()
    first_enrollment = ledger.register_device("device", key.public_bytes)
    second_enrollment = ledger.register_device("device", key.public_bytes)
    assert first_enrollment["created"] is True
    assert second_enrollment["created"] is False

    event = DeviceClient("device", key).create_event("state", {"on": True})
    assert ledger.accept_event(event)["duplicate"] is False
    assert ledger.accept_event(event)["duplicate"] is True
    assert len(database.all_events()) == 1

    with pytest.raises(ValueError, match="another key"):
        ledger.register_device("device", KeyPair.generate().public_bytes)
