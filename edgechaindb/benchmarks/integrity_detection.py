from __future__ import annotations

from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
from typing import Any

from ..crypto import KeyPair
from ..device import DeviceClient
from ..ledger import EdgeChainLedger
from ..store import Database
from .common import BenchmarkSpec


def _checkpoint(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def build_spec(*, replay_trials: int = 20, deletion_trials: int = 20) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="edgechain-integrity-") as raw_dir:
            directory = Path(raw_dir)
            baseline = directory / "baseline.db"
            database = Database(baseline)
            ledger = EdgeChainLedger(database, quorum_threshold=1)
            authority = KeyPair.generate()
            ledger.register_authority("integrity-authority", authority.public_bytes)
            device_key = KeyPair.generate()
            ledger.register_device("integrity-device", device_key.public_bytes)
            device = DeviceClient("integrity-device", device_key)
            events = []
            for index in range(max(replay_trials, deletion_trials, 10)):
                event = device.create_event("integrity", {"sample": index, "quality": 100})
                ledger.accept_event(event)
                events.append(event)
            ledger.propose_block(
                "integrity-authority", authority.private_key, max_events=len(events)
            )
            _checkpoint(database)
            if not ledger.verify_all()["valid"]:
                raise AssertionError("baseline ledger is invalid")

            rows: list[dict[str, Any]] = []
            replay_detected = 0
            for trial in range(replay_trials):
                event = events[trial % len(events)]
                result = ledger.accept_event(event)
                detected = bool(result.get("duplicate"))
                replay_detected += int(detected)
                rows.append({"attack": "replay", "trial": trial + 1, "detected": detected})

            deletion_detected = 0
            for trial in range(deletion_trials):
                case_path = directory / f"deletion-{trial:03d}.db"
                shutil.copy2(baseline, case_path)
                with sqlite3.connect(case_path) as connection:
                    connection.execute("PRAGMA foreign_keys=OFF")
                    if trial % 2 == 0:
                        victim = events[random.randrange(len(events))].event_hash
                        connection.execute("DELETE FROM events WHERE event_hash = ?", (victim,))
                        deletion_type = "event_row"
                    else:
                        connection.execute(
                            "DELETE FROM block_signatures WHERE block_height = 1"
                        )
                        deletion_type = "quorum_signature"
                    connection.commit()
                verification = EdgeChainLedger(Database(case_path), quorum_threshold=1).verify_all()
                detected = not bool(verification["valid"])
                deletion_detected += int(detected)
                rows.append(
                    {
                        "attack": "deletion",
                        "deletion_type": deletion_type,
                        "trial": trial + 1,
                        "detected": detected,
                        "error_count": len(verification["errors"]),
                    }
                )

            metrics = {
                "replay_trials": replay_trials,
                "replay_detected": replay_detected,
                "replay_detection_rate": round(replay_detected / replay_trials, 4),
                "deletion_trials": deletion_trials,
                "deletion_detected": deletion_detected,
                "deletion_detection_rate": round(deletion_detected / deletion_trials, 4),
            }
            if replay_detected != replay_trials or deletion_detected != deletion_trials:
                raise AssertionError(metrics)
            return {
                "details": "Detected idempotent replay attempts and destructive ledger-row deletions",
                "metrics": metrics,
                "rows": rows,
            }

    return BenchmarkSpec("Replay and deletion detection rate", "Tamper evidence", "integrity_detection", run)
