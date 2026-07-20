from __future__ import annotations

import sqlite3

import pytest

from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient
from edgechaindb.ledger import EdgeChainLedger
from edgechaindb.store import Database


def make_ledger(tmp_path):
    database = Database(tmp_path / "ledger.db")
    ledger = EdgeChainLedger(database, quorum_threshold=2)
    authority_a = KeyPair.generate()
    authority_b = KeyPair.generate()
    ledger.register_authority("a", authority_a.public_bytes)
    ledger.register_authority("b", authority_b.public_bytes)
    device_key = KeyPair.generate()
    ledger.register_device("sensor-1", device_key.public_bytes)
    return database, ledger, authority_a, authority_b, DeviceClient(
        "sensor-1", device_key
    )


def test_end_to_end_and_merkle_proof(tmp_path):
    database, ledger, authority_a, authority_b, device = make_ledger(tmp_path)

    hashes = []
    for value in (21000, 21100, 21200):
        event = device.create_event(
            "temperature", {"temperature_milli_celsius": value}
        )
        ledger.accept_event(event)
        hashes.append(event.event_hash.hex())

    block = ledger.propose_block("a", authority_a.private_key)
    assert block["status"] == "proposed"
    assert ledger.sign_block(block["height"], "b", authority_b.private_key) == (
        "finalized"
    )

    report = ledger.verify_all()
    assert report["valid"], report["errors"]

    proof = ledger.event_proof(hashes[1])
    assert ledger.verify_event_proof(proof)


def test_replay_is_idempotent_and_sequence_jump_is_rejected(tmp_path):
    _, ledger, _, _, device = make_ledger(tmp_path)
    event = device.create_event("state", {"on": True})
    first = ledger.accept_event(event)
    retry = ledger.accept_event(event)
    assert first["duplicate"] is False
    assert retry["duplicate"] is True

    jumped = device.create_event("state", {"on": False})
    jumped = type(jumped)(
        device_id=jumped.device_id,
        sequence=jumped.sequence + 5,
        device_time_ms=jumped.device_time_ms,
        event_type=jumped.event_type,
        payload=jumped.payload,
        previous_event_hash=jumped.previous_event_hash,
        signature=b"",
        version=jumped.version,
    )
    # Re-sign the changed sequence so the failure is specifically ordering.
    jumped = type(jumped)(
        device_id=jumped.device_id,
        sequence=jumped.sequence,
        device_time_ms=jumped.device_time_ms,
        event_type=jumped.event_type,
        payload=jumped.payload,
        previous_event_hash=jumped.previous_event_hash,
        signature=device.key_pair.sign(jumped.signing_bytes),
        version=jumped.version,
    )
    with pytest.raises(ValueError, match="sequence"):
        ledger.accept_event(jumped)


def test_tampering_is_detected(tmp_path):
    database, ledger, authority_a, authority_b, device = make_ledger(tmp_path)
    event = device.create_event("state", {"on": True})
    ledger.accept_event(event)
    block = ledger.propose_block("a", authority_a.private_key)
    ledger.sign_block(block["height"], "b", authority_b.private_key)

    with database.connect() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE events SET payload_cbor = ? WHERE event_hash = ?",
            (b"tampered", event.event_hash),
        )
        connection.execute("COMMIT")

    report = ledger.verify_all()
    assert not report["valid"]
    assert any("payload" in error or "canonical" in error for error in report["errors"])


def test_floats_are_rejected(tmp_path):
    _, _, _, _, device = make_ledger(tmp_path)
    with pytest.raises(ValueError):
        device.create_event("temperature", {"temperature": 21.5})
