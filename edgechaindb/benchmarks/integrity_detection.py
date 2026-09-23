from __future__ import annotations

from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable

from ..crypto import KeyPair
from ..device import DeviceClient
from ..ledger import EdgeChainLedger
from ..store import Database
from .common import BenchmarkSpec


Mutation = Callable[[sqlite3.Connection, int], str]


def _checkpoint(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _flip_first_byte(value: bytes) -> bytes:
    if not value:
        return b"\x01"
    return bytes([value[0] ^ 0x01]) + value[1:]


def _row_at(connection: sqlite3.Connection, query: str, trial: int) -> sqlite3.Row:
    rows = connection.execute(query).fetchall()
    if not rows:
        raise AssertionError(f"mutation query returned no rows: {query}")
    return rows[trial % len(rows)]


def _mutate_event_payload(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash, payload_cbor FROM events ORDER BY device_id, sequence",
        trial,
    )
    connection.execute(
        "UPDATE events SET payload_cbor = ? WHERE event_hash = ?",
        (_flip_first_byte(row["payload_cbor"]), row["event_hash"]),
    )
    return "modified stored event payload bytes"


def _mutate_event_signature(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash, signature FROM events ORDER BY device_id, sequence",
        trial,
    )
    connection.execute(
        "UPDATE events SET signature = ? WHERE event_hash = ?",
        (_flip_first_byte(row["signature"]), row["event_hash"]),
    )
    return "corrupted Ed25519 event signature"


def _mutate_previous_event_hash(connection: sqlite3.Connection, trial: int) -> str:
    rows = connection.execute(
        "SELECT event_hash, previous_event_hash FROM events WHERE sequence > 1 ORDER BY sequence"
    ).fetchall()
    row = rows[trial % len(rows)]
    connection.execute(
        "UPDATE events SET previous_event_hash = ? WHERE event_hash = ?",
        (_flip_first_byte(row["previous_event_hash"]), row["event_hash"]),
    )
    return "corrupted device-chain previous-event hash"


def _mutate_sequence(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash, sequence FROM events ORDER BY sequence",
        trial,
    )
    connection.execute(
        "UPDATE events SET sequence = ? WHERE event_hash = ?",
        (10_000 + int(row["sequence"]), row["event_hash"]),
    )
    return "changed stored device sequence number"


def _delete_event(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash FROM events WHERE sequence > 1 ORDER BY sequence",
        trial,
    )
    connection.execute("DELETE FROM events WHERE event_hash = ?", (row["event_hash"],))
    return "deleted an interior finalized event row"


def _reorder_block_membership(connection: sqlite3.Connection, trial: int) -> str:
    block = _row_at(
        connection,
        "SELECT block_height FROM block_events GROUP BY block_height HAVING COUNT(*) >= 2 ORDER BY block_height",
        trial,
    )
    height = int(block["block_height"])
    positions = connection.execute(
        "SELECT position FROM block_events WHERE block_height = ? ORDER BY position LIMIT 2",
        (height,),
    ).fetchall()
    first, second = int(positions[0]["position"]), int(positions[1]["position"])
    temporary = -1 - trial
    connection.execute(
        "UPDATE block_events SET position = ? WHERE block_height = ? AND position = ?",
        (temporary, height, first),
    )
    connection.execute(
        "UPDATE block_events SET position = ? WHERE block_height = ? AND position = ?",
        (first, height, second),
    )
    connection.execute(
        "UPDATE block_events SET position = ? WHERE block_height = ? AND position = ?",
        (second, height, temporary),
    )
    return f"swapped two event positions in block {height}"


def _mutate_merkle_root(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(connection, "SELECT height, merkle_root FROM blocks ORDER BY height", trial)
    connection.execute(
        "UPDATE blocks SET merkle_root = ? WHERE height = ?",
        (_flip_first_byte(row["merkle_root"]), row["height"]),
    )
    return f"corrupted Merkle root in block {row['height']}"


def _mutate_previous_block_hash(connection: sqlite3.Connection, trial: int) -> str:
    rows = connection.execute(
        "SELECT height, previous_hash FROM blocks WHERE height > 1 ORDER BY height"
    ).fetchall()
    row = rows[trial % len(rows)]
    connection.execute(
        "UPDATE blocks SET previous_hash = ? WHERE height = ?",
        (_flip_first_byte(row["previous_hash"]), row["height"]),
    )
    return f"corrupted previous-block hash in block {row['height']}"


def _mutate_policy_hash(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(connection, "SELECT height, policy_hash FROM blocks ORDER BY height", trial)
    connection.execute(
        "UPDATE blocks SET policy_hash = ? WHERE height = ?",
        (_flip_first_byte(row["policy_hash"]), row["height"]),
    )
    return f"corrupted policy commitment in block {row['height']}"


def _mutate_authority_snapshot(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT block_height, authority_id, public_key FROM block_authorities ORDER BY block_height, authority_id",
        trial,
    )
    connection.execute(
        "UPDATE block_authorities SET public_key = ? WHERE block_height = ? AND authority_id = ?",
        (
            _flip_first_byte(row["public_key"]),
            row["block_height"],
            row["authority_id"],
        ),
    )
    return f"corrupted authority snapshot key for {row['authority_id']}"


def _mutate_block_signature(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT block_height, authority_id, signature FROM block_signatures ORDER BY block_height, authority_id",
        trial,
    )
    connection.execute(
        "UPDATE block_signatures SET signature = ? WHERE block_height = ? AND authority_id = ?",
        (
            _flip_first_byte(row["signature"]),
            row["block_height"],
            row["authority_id"],
        ),
    )
    return f"corrupted quorum signature from {row['authority_id']}"


def _delete_quorum_signature(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT block_height, authority_id FROM block_signatures ORDER BY block_height, authority_id",
        trial,
    )
    connection.execute(
        "DELETE FROM block_signatures WHERE block_height = ? AND authority_id = ?",
        (row["block_height"], row["authority_id"]),
    )
    return f"removed quorum signature from {row['authority_id']}"


def _mutate_finalized_flag(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash FROM events WHERE finalized = 1 ORDER BY sequence",
        trial,
    )
    connection.execute(
        "UPDATE events SET finalized = 0 WHERE event_hash = ?", (row["event_hash"],)
    )
    return "cleared finalized flag on an event in a finalized block"


def _mutate_block_mapping(connection: sqlite3.Connection, trial: int) -> str:
    row = _row_at(
        connection,
        "SELECT event_hash FROM events WHERE block_height IS NOT NULL ORDER BY sequence",
        trial,
    )
    connection.execute(
        "UPDATE events SET block_height = NULL WHERE event_hash = ?", (row["event_hash"],)
    )
    return "detached event row from its block while retaining block membership"


TAMPER_CASES: tuple[tuple[str, Mutation], ...] = (
    ("event_payload_modified", _mutate_event_payload),
    ("event_signature_corrupted", _mutate_event_signature),
    ("previous_event_hash_corrupted", _mutate_previous_event_hash),
    ("sequence_modified", _mutate_sequence),
    ("event_deleted", _delete_event),
    ("block_membership_reordered", _reorder_block_membership),
    ("merkle_root_corrupted", _mutate_merkle_root),
    ("previous_block_hash_corrupted", _mutate_previous_block_hash),
    ("policy_hash_corrupted", _mutate_policy_hash),
    ("authority_snapshot_corrupted", _mutate_authority_snapshot),
    ("block_signature_corrupted", _mutate_block_signature),
    ("quorum_signature_deleted", _delete_quorum_signature),
    ("finalized_flag_cleared", _mutate_finalized_flag),
    ("block_mapping_detached", _mutate_block_mapping),
)


def _build_baseline(path: Path, event_count: int) -> tuple[EdgeChainLedger, list[Any]]:
    database = Database(path)
    ledger = EdgeChainLedger(database, quorum_threshold=2)
    authorities: list[tuple[str, KeyPair]] = []
    for index in range(3):
        authority_id = f"integrity-authority-{index + 1}"
        authority = KeyPair.generate()
        ledger.register_authority(authority_id, authority.public_bytes)
        authorities.append((authority_id, authority))

    device_key = KeyPair.generate()
    ledger.register_device("integrity-device", device_key.public_bytes)
    device = DeviceClient("integrity-device", device_key)
    events = []
    for index in range(event_count):
        event = device.create_event(
            "integrity",
            {"sample": index, "quality": 100},
            device_time_ms=1_800_000_000_000 + index,
        )
        ledger.accept_event(event)
        events.append(event)

    block_size = max(2, event_count // 2)
    while database.pending_count():
        proposal = ledger.propose_block(
            authorities[0][0], authorities[0][1].private_key, max_events=block_size
        )
        status = ledger.sign_block(
            int(proposal["height"]), authorities[1][0], authorities[1][1].private_key
        )
        if status != "finalized":
            raise AssertionError(f"baseline block did not finalize: {proposal}")

    _checkpoint(database)
    verification = ledger.verify_all()
    if not verification["valid"]:
        raise AssertionError(f"baseline ledger is invalid: {verification}")
    return ledger, events


def build_spec(
    *,
    replay_trials: int = 20,
    deletion_trials: int = 20,
    tamper_trials_per_class: int = 1,
    control_trials: int = 5,
    seed: int = 2026,
) -> BenchmarkSpec:
    if replay_trials < 1 or deletion_trials < 1:
        raise ValueError("replay_trials and deletion_trials must be positive")
    if tamper_trials_per_class < 1 or control_trials < 1:
        raise ValueError("tamper and control trial counts must be positive")

    def run() -> dict[str, Any]:
        rng = random.Random(seed)
        event_count = max(replay_trials, deletion_trials, 12)
        with tempfile.TemporaryDirectory(prefix="edgechain-integrity-") as raw_dir:
            directory = Path(raw_dir)
            baseline = directory / "baseline.db"
            ledger, events = _build_baseline(baseline, event_count)

            rows: list[dict[str, Any]] = []
            replay_detected = 0
            for trial in range(replay_trials):
                event = events[trial % len(events)]
                result = ledger.accept_event(event)
                detected = bool(result.get("duplicate"))
                replay_detected += int(detected)
                rows.append(
                    {
                        "kind": "replay",
                        "attack": "replay",
                        "trial": trial + 1,
                        "detected": detected,
                    }
                )

            # Preserve the original deletion-rate metric with randomized event/signature deletion.
            deletion_detected = 0
            for trial in range(deletion_trials):
                case_path = directory / f"deletion-{trial:03d}.db"
                shutil.copy2(baseline, case_path)
                with sqlite3.connect(case_path) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys=OFF")
                    if trial % 2 == 0:
                        rows_for_delete = connection.execute(
                            "SELECT event_hash FROM events WHERE sequence > 1 ORDER BY sequence"
                        ).fetchall()
                        victim = rows_for_delete[rng.randrange(len(rows_for_delete))]["event_hash"]
                        connection.execute("DELETE FROM events WHERE event_hash = ?", (victim,))
                        deletion_type = "event_row"
                    else:
                        signature_rows = connection.execute(
                            "SELECT block_height, authority_id FROM block_signatures ORDER BY block_height, authority_id"
                        ).fetchall()
                        victim = signature_rows[rng.randrange(len(signature_rows))]
                        connection.execute(
                            "DELETE FROM block_signatures WHERE block_height = ? AND authority_id = ?",
                            (victim["block_height"], victim["authority_id"]),
                        )
                        deletion_type = "quorum_signature"
                    connection.commit()
                verification = EdgeChainLedger(Database(case_path), quorum_threshold=2).verify_all()
                detected = not bool(verification["valid"])
                deletion_detected += int(detected)
                rows.append(
                    {
                        "kind": "deletion",
                        "attack": deletion_type,
                        "trial": trial + 1,
                        "detected": detected,
                        "error_count": len(verification["errors"]),
                    }
                )

            false_positives = 0
            control_verify_ms: list[float] = []
            for trial in range(control_trials):
                case_path = directory / f"control-{trial:03d}.db"
                shutil.copy2(baseline, case_path)
                started = time.perf_counter()
                verification = EdgeChainLedger(Database(case_path), quorum_threshold=2).verify_all()
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                control_verify_ms.append(elapsed_ms)
                false_positive = not bool(verification["valid"])
                false_positives += int(false_positive)
                rows.append(
                    {
                        "kind": "control",
                        "attack": "none",
                        "trial": trial + 1,
                        "detected": false_positive,
                        "verification_ms": round(elapsed_ms, 4),
                        "error_count": len(verification["errors"]),
                    }
                )

            tamper_detected = 0
            tamper_total = 0
            verification_times: list[float] = []
            per_attack: dict[str, dict[str, int]] = {}
            for attack_name, mutate in TAMPER_CASES:
                attack_detected = 0
                for trial in range(tamper_trials_per_class):
                    tamper_total += 1
                    case_path = directory / f"tamper-{attack_name}-{trial:03d}.db"
                    shutil.copy2(baseline, case_path)
                    with sqlite3.connect(case_path) as connection:
                        connection.row_factory = sqlite3.Row
                        connection.execute("PRAGMA foreign_keys=OFF")
                        description = mutate(connection, trial)
                        connection.commit()
                    started = time.perf_counter()
                    verification = EdgeChainLedger(
                        Database(case_path), quorum_threshold=2
                    ).verify_all()
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    verification_times.append(elapsed_ms)
                    detected = not bool(verification["valid"])
                    attack_detected += int(detected)
                    tamper_detected += int(detected)
                    rows.append(
                        {
                            "kind": "tamper",
                            "attack": attack_name,
                            "trial": trial + 1,
                            "mutation": description,
                            "detected": detected,
                            "verification_ms": round(elapsed_ms, 4),
                            "error_count": len(verification["errors"]),
                            "first_error": verification["errors"][0]
                            if verification["errors"]
                            else "",
                        }
                    )
                per_attack[attack_name] = {
                    "trials": tamper_trials_per_class,
                    "detected": attack_detected,
                }

            metrics = {
                "replay_trials": replay_trials,
                "replay_detected": replay_detected,
                "replay_detection_rate": round(replay_detected / replay_trials, 4),
                "deletion_trials": deletion_trials,
                "deletion_detected": deletion_detected,
                "deletion_detection_rate": round(deletion_detected / deletion_trials, 4),
                "tamper_attack_classes": len(TAMPER_CASES),
                "tamper_trials": tamper_total,
                "tamper_detected": tamper_detected,
                "tamper_detection_rate": round(tamper_detected / tamper_total, 4),
                "control_trials": control_trials,
                "false_positives": false_positives,
                "false_positive_rate": round(false_positives / control_trials, 4),
                "mean_tamper_verification_ms": round(
                    sum(verification_times) / len(verification_times), 4
                ),
                "mean_control_verification_ms": round(
                    sum(control_verify_ms) / len(control_verify_ms), 4
                ),
                "per_attack": per_attack,
                "seed": seed,
            }
            if (
                replay_detected != replay_trials
                or deletion_detected != deletion_trials
                or tamper_detected != tamper_total
                or false_positives != 0
            ):
                raise AssertionError(metrics)
            return {
                "details": (
                    "Injected replay plus stored-ledger tampering across event, chain, "
                    "Merkle, policy, authority, quorum, finalization, and mapping state"
                ),
                "metrics": metrics,
                "notes": [
                    "Each destructive tamper case operates on a fresh copy of the same valid finalized baseline ledger.",
                    "Untouched control copies are verified to measure false-positive behavior.",
                ],
                "rows": rows,
            }

    return BenchmarkSpec(
        "Adversarial tamper and replay detection",
        "Tamper evidence",
        "integrity_detection",
        run,
    )
