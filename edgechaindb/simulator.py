from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import time

from .crypto import KeyPair
from .device import DeviceClient
from .ledger import EdgeChainLedger
from .store import Database


def run_demo(database_path: str, event_count: int) -> dict:
    path = Path(database_path)
    path.unlink(missing_ok=True)
    Path(str(path) + "-wal").unlink(missing_ok=True)
    Path(str(path) + "-shm").unlink(missing_ok=True)

    database = Database(path)
    ledger = EdgeChainLedger(database, quorum_threshold=2)

    authorities = {
        name: KeyPair.generate()
        for name in ("gateway-vienna", "gateway-graz", "auditor")
    }
    for name, key in authorities.items():
        ledger.register_authority(name, key.public_bytes)

    devices = {}
    for name in ("boiler-temperature-01", "pump-vibration-07"):
        key = KeyPair.generate()
        ledger.register_device(name, key.public_bytes)
        devices[name] = DeviceClient(name, key)

    block_summaries = []
    event_hashes = []

    for index in range(event_count):
        name = list(devices)[index % len(devices)]
        client = devices[name]
        if "temperature" in name:
            event_type = "temperature"
            payload = {
                "temperature_milli_celsius": 58000
                + random.randint(-1500, 1500),
                "quality": 100,
            }
        else:
            event_type = "vibration"
            payload = {
                "rms_micrometers_per_second": 4100
                + random.randint(-300, 300),
                "quality": 98,
            }

        event = client.create_event(
            event_type,
            payload,
            device_time_ms=int(time.time() * 1000) + index,
        )
        ledger.accept_event(event)
        event_hashes.append(event.event_hash.hex())

        if database.pending_count() >= 4:
            block = ledger.propose_block(
                "gateway-vienna",
                authorities["gateway-vienna"].private_key,
                max_events=4,
            )
            status = ledger.sign_block(
                block["height"],
                "gateway-graz",
                authorities["gateway-graz"].private_key,
            )
            block["status"] = status
            block["signatures"] = len(
                database.block_signatures(block["height"])
            )
            block_summaries.append(block)

    if database.pending_count():
        block = ledger.propose_block(
            "gateway-vienna",
            authorities["gateway-vienna"].private_key,
            max_events=256,
        )
        status = ledger.sign_block(
            block["height"],
            "auditor",
            authorities["auditor"].private_key,
        )
        block["status"] = status
        block["signatures"] = len(
            database.block_signatures(block["height"])
        )
        block_summaries.append(block)

    proof = ledger.event_proof(event_hashes[0])
    verification = ledger.verify_all()
    return {
        "database": str(path),
        "authorities": 3,
        "quorum": "2-of-3",
        "devices": len(devices),
        "blocks": block_summaries,
        "verification": verification,
        "sample_merkle_proof_valid": ledger.verify_event_proof(proof),
        "sample_merkle_proof": proof,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the EdgeChainDB IoT demo")
    parser.add_argument("--database", default="demo.db")
    parser.add_argument("--events", type=int, default=12)
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    print(json.dumps(run_demo(args.database, args.events), indent=2))


if __name__ == "__main__":
    main()
