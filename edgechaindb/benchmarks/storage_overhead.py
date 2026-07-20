from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from ..canonical import dumps
from ..crypto import KeyPair
from ..device import DeviceClient
from ..ledger import EdgeChainLedger
from ..store import Database
from .common import BenchmarkSpec


def _checkpoint(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def build_spec(*, events: int = 1000, block_size: int = 50) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="edgechain-storage-") as raw_dir:
            directory = Path(raw_dir)
            database = Database(directory / "storage.db")
            ledger = EdgeChainLedger(database, quorum_threshold=1)
            authority = KeyPair.generate()
            ledger.register_authority("storage-authority", authority.public_bytes)
            device_key = KeyPair.generate()
            ledger.register_device("storage-device", device_key.public_bytes)
            device = DeviceClient("storage-device", device_key)
            _checkpoint(database)
            before = database.database_info()
            raw_payload_bytes = 0
            wire_equivalent_bytes = 0
            for index in range(events):
                payload = {
                    "temperature_milli_celsius": 20000 + index % 1000,
                    "humidity_basis_points": 4500 + index % 300,
                    "battery_millivolts": 3700 - index % 100,
                    "quality": 100,
                }
                event = device.create_event("storage", payload)
                raw_payload_bytes += len(dumps(payload))
                wire_equivalent_bytes += len(event.signing_bytes) + len(event.signature) + 32
                ledger.accept_event(event)
                if (index + 1) % block_size == 0:
                    ledger.propose_block(
                        "storage-authority", authority.private_key, max_events=block_size
                    )
            if database.pending_count():
                ledger.propose_block(
                    "storage-authority", authority.private_key, max_events=block_size
                )
            _checkpoint(database)
            after = database.database_info(run_quick_check=True)
            delta_used = max(0, int(after["used_bytes"]) - int(before["used_bytes"]))
            delta_file = max(0, int(after["database_bytes"]) - int(before["database_bytes"]))
            stats = database.statistics()
            metrics = {
                "events": events,
                "blocks": stats["blocks"],
                "block_size": block_size,
                "raw_payload_bytes": raw_payload_bytes,
                "signed_event_logical_bytes": wire_equivalent_bytes,
                "database_used_bytes_delta": delta_used,
                "database_file_bytes_delta": delta_file,
                "storage_bytes_per_event": round(delta_used / events, 2),
                "storage_to_payload_ratio": round(delta_used / raw_payload_bytes, 3),
                "storage_to_signed_event_ratio": round(delta_used / wire_equivalent_bytes, 3),
                "quick_check": after["quick_check"],
            }
            if after["quick_check"] != "ok":
                raise AssertionError(after["quick_check"])
            return {
                "details": "Measured incremental SQLite, index, Merkle-block, and signature storage overhead",
                "metrics": metrics,
                "rows": [metrics],
            }

    return BenchmarkSpec("Storage overhead", "Storage efficiency", "storage_overhead", run)
