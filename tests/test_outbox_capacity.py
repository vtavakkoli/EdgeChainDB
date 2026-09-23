import pytest

from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient
from edgechaindb.outbox import DurableOutbox, OutboxFullError


def test_durable_outbox_capacity_is_fail_closed_and_recovers_after_ack(tmp_path):
    key = KeyPair.generate()
    device = DeviceClient("capacity-test-device", key)
    path = tmp_path / "outbox.json"
    outbox = DurableOutbox(path, max_items=2)

    first = device.create_event("test", {"value": 1}, device_time_ms=1_800_000_000_001)
    second = device.create_event("test", {"value": 2}, device_time_ms=1_800_000_000_002)
    third = device.create_event("test", {"value": 3}, device_time_ms=1_800_000_000_003)

    outbox.append(first.to_wire())
    outbox.append(second.to_wire())
    assert outbox.at_capacity
    assert outbox.remaining_capacity == 0

    with pytest.raises(OutboxFullError):
        outbox.append(third.to_wire())

    reloaded = DurableOutbox(path, max_items=2)
    assert [item["sequence"] for item in reloaded.items()] == [1, 2]

    assert reloaded.acknowledge(1) == 1
    assert not reloaded.at_capacity
    assert reloaded.remaining_capacity == 1
    reloaded.append(third.to_wire())
    assert [item["sequence"] for item in reloaded.items()] == [2, 3]


def test_durable_outbox_rejects_invalid_capacity(tmp_path):
    with pytest.raises(ValueError):
        DurableOutbox(tmp_path / "outbox.json", max_items=0)
