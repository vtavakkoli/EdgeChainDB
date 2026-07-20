from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterator

from .models import BlockHeader, SignedEvent, ZERO_HASH


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    key_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    last_sequence INTEGER NOT NULL DEFAULT 0,
    last_event_hash BLOB NOT NULL,
    enrolled_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS authorities (
    authority_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    key_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    enrolled_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_hash BLOB PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    sequence INTEGER NOT NULL,
    device_time_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_cbor BLOB NOT NULL,
    previous_event_hash BLOB NOT NULL,
    signature BLOB NOT NULL,
    unsigned_cbor BLOB NOT NULL,
    received_at_ms INTEGER NOT NULL,
    block_height INTEGER,
    finalized INTEGER NOT NULL DEFAULT 0 CHECK (finalized IN (0, 1)),
    UNIQUE(device_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_pending
ON events(block_height, received_at_ms);

CREATE INDEX IF NOT EXISTS idx_events_device_time
ON events(device_id, device_time_ms);

CREATE TABLE IF NOT EXISTS blocks (
    height INTEGER PRIMARY KEY,
    block_hash BLOB NOT NULL UNIQUE,
    previous_hash BLOB NOT NULL,
    created_at_ms INTEGER NOT NULL,
    merkle_root BLOB NOT NULL,
    event_count INTEGER NOT NULL,
    proposer_id TEXT NOT NULL,
    authority_set_hash BLOB NOT NULL,
    quorum_threshold INTEGER NOT NULL,
    policy_hash BLOB NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'finalized'))
);

CREATE TABLE IF NOT EXISTS block_events (
    block_height INTEGER NOT NULL REFERENCES blocks(height),
    position INTEGER NOT NULL,
    event_hash BLOB NOT NULL UNIQUE REFERENCES events(event_hash),
    PRIMARY KEY(block_height, position)
);

CREATE TABLE IF NOT EXISTS block_authorities (
    block_height INTEGER NOT NULL REFERENCES blocks(height),
    authority_id TEXT NOT NULL,
    public_key BLOB NOT NULL,
    PRIMARY KEY(block_height, authority_id)
);

CREATE TABLE IF NOT EXISTS block_signatures (
    block_height INTEGER NOT NULL REFERENCES blocks(height),
    authority_id TEXT NOT NULL,
    signature BLOB NOT NULL,
    signed_at_ms INTEGER NOT NULL,
    PRIMARY KEY(block_height, authority_id)
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)

    def execute_read(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, parameters).fetchall())

    def register_device(
        self,
        device_id: str,
        public_key: bytes,
        key_id: str,
        enrolled_at_ms: int,
    ) -> bool:
        """Register a device and return True when it was newly created.

        Repeating enrollment with the same key is intentionally idempotent so a
        container can safely restart. Reusing a device id with another key is a
        hard failure.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT public_key, status FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if existing is not None:
                    if existing["public_key"] != public_key:
                        raise ValueError("device id is already bound to another key")
                    if existing["status"] != "active":
                        raise ValueError("device is not active")
                    connection.execute("COMMIT")
                    return False

                connection.execute(
                    """
                    INSERT INTO devices(
                        device_id, public_key, key_id, status,
                        last_sequence, last_event_hash, enrolled_at_ms
                    ) VALUES (?, ?, ?, 'active', 0, ?, ?)
                    """,
                    (device_id, public_key, key_id, ZERO_HASH, enrolled_at_ms),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def register_authority(
        self,
        authority_id: str,
        public_key: bytes,
        key_id: str,
        enrolled_at_ms: int,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT public_key, active FROM authorities WHERE authority_id = ?",
                    (authority_id,),
                ).fetchone()
                if existing is not None:
                    if existing["public_key"] != public_key:
                        raise ValueError("authority id is already bound to another key")
                    if not bool(existing["active"]):
                        raise ValueError("authority is not active")
                    connection.execute("COMMIT")
                    return False

                connection.execute(
                    """
                    INSERT INTO authorities(
                        authority_id, public_key, key_id, active, enrolled_at_ms
                    ) VALUES (?, ?, ?, 1, ?)
                    """,
                    (authority_id, public_key, key_id, enrolled_at_ms),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get_device(self, device_id: str) -> sqlite3.Row | None:
        rows = self.execute_read(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        )
        return rows[0] if rows else None

    def get_event(self, event_hash: bytes) -> sqlite3.Row | None:
        rows = self.execute_read(
            "SELECT * FROM events WHERE event_hash = ?", (event_hash,)
        )
        return rows[0] if rows else None

    def list_devices(self) -> list[sqlite3.Row]:
        return self.execute_read(
            """
            SELECT device_id, key_id, status, last_sequence,
                   last_event_hash, enrolled_at_ms
            FROM devices
            ORDER BY device_id
            """
        )

    def recent_events(self, limit: int = 100) -> list[sqlite3.Row]:
        limit = min(max(int(limit), 1), 1000)
        return self.execute_read(
            """
            SELECT event_hash, device_id, sequence, device_time_ms, event_type,
                   payload_cbor, previous_event_hash, received_at_ms,
                   block_height, finalized
            FROM events
            ORDER BY received_at_ms DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        )

    def device_activity(self, *, window_ms: int = 60_000) -> dict[str, dict]:
        """Return the latest telemetry and short-window activity for every device.

        The query deliberately uses the most recent database row rather than only
        the device checkpoint so the monitoring UI can display the actual sensor
        values, finality state, clock lag, and recent event rate.
        """

        with self.connect() as connection:
            now_ms = int(
                connection.execute(
                    "SELECT CAST(strftime('%s','now') AS INTEGER) * 1000"
                ).fetchone()[0]
            )
            rows = list(
                connection.execute(
                    """
                    SELECT
                        d.device_id,
                        d.status,
                        d.last_sequence,
                        e.event_type,
                        e.payload_cbor,
                        e.device_time_ms,
                        e.received_at_ms,
                        e.block_height,
                        e.finalized,
                        COALESCE(r.events_in_window, 0) AS events_in_window
                    FROM devices AS d
                    LEFT JOIN events AS e
                      ON e.rowid = (
                          SELECT latest.rowid
                          FROM events AS latest
                          WHERE latest.device_id = d.device_id
                          ORDER BY latest.received_at_ms DESC, latest.rowid DESC
                          LIMIT 1
                      )
                    LEFT JOIN (
                        SELECT device_id, COUNT(*) AS events_in_window
                        FROM events
                        WHERE received_at_ms >= ?
                        GROUP BY device_id
                    ) AS r ON r.device_id = d.device_id
                    ORDER BY d.device_id
                    """,
                    (now_ms - max(int(window_ms), 1),),
                ).fetchall()
            )
        result: dict[str, dict] = {}
        for row in rows:
            result[row["device_id"]] = {
                "status": row["status"],
                "last_sequence": int(row["last_sequence"]),
                "event_type": row["event_type"],
                "payload_cbor": row["payload_cbor"],
                "device_time_ms": (
                    int(row["device_time_ms"])
                    if row["device_time_ms"] is not None
                    else None
                ),
                "last_event_at_ms": (
                    int(row["received_at_ms"])
                    if row["received_at_ms"] is not None
                    else None
                ),
                "block_height": (
                    int(row["block_height"])
                    if row["block_height"] is not None
                    else None
                ),
                "finalized": (
                    bool(row["finalized"])
                    if row["finalized"] is not None
                    else None
                ),
                "events_in_window": int(row["events_in_window"]),
                "window_ms": max(int(window_ms), 1),
            }
        return result

    def statistics(self) -> dict[str, int]:
        with self.connect() as connection:
            devices = connection.execute(
                "SELECT COUNT(*) AS count FROM devices"
            ).fetchone()["count"]
            events = connection.execute(
                "SELECT COUNT(*) AS count FROM events"
            ).fetchone()["count"]
            finalized_events = connection.execute(
                "SELECT COUNT(*) AS count FROM events WHERE finalized = 1"
            ).fetchone()["count"]
            pending_events = connection.execute(
                "SELECT COUNT(*) AS count FROM events WHERE block_height IS NULL"
            ).fetchone()["count"]
            blocks = connection.execute(
                "SELECT COUNT(*) AS count FROM blocks"
            ).fetchone()["count"]
            finalized_blocks = connection.execute(
                "SELECT COUNT(*) AS count FROM blocks WHERE status = 'finalized'"
            ).fetchone()["count"]
        return {
            "devices": int(devices),
            "events": int(events),
            "finalized_events": int(finalized_events),
            "pending_events": int(pending_events),
            "blocks": int(blocks),
            "finalized_blocks": int(finalized_blocks),
        }

    def active_authorities(self) -> list[sqlite3.Row]:
        return self.execute_read(
            """
            SELECT authority_id, public_key, key_id
            FROM authorities
            WHERE active = 1
            ORDER BY authority_id
            """
        )

    def insert_verified_event(
        self,
        event: SignedEvent,
        payload_cbor: bytes,
        received_at_ms: int,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT unsigned_cbor, signature
                    FROM events
                    WHERE event_hash = ?
                    """,
                    (event.event_hash,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["unsigned_cbor"] != event.signing_bytes
                        or existing["signature"] != event.signature
                    ):
                        raise RuntimeError("event hash collision or inconsistent replay")
                    connection.execute("COMMIT")
                    return False

                device = connection.execute(
                    "SELECT * FROM devices WHERE device_id = ?",
                    (event.device_id,),
                ).fetchone()
                if device is None:
                    raise ValueError("device is not enrolled")
                if device["status"] != "active":
                    raise ValueError("device is not active")

                expected_sequence = int(device["last_sequence"]) + 1
                if event.sequence != expected_sequence:
                    raise ValueError(
                        f"invalid sequence: expected {expected_sequence}, "
                        f"received {event.sequence}"
                    )
                if event.previous_event_hash != device["last_event_hash"]:
                    raise ValueError("device-chain continuity check failed")

                connection.execute(
                    """
                    INSERT INTO events(
                        event_hash, device_id, sequence, device_time_ms,
                        event_type, payload_cbor, previous_event_hash,
                        signature, unsigned_cbor, received_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_hash,
                        event.device_id,
                        event.sequence,
                        event.device_time_ms,
                        event.event_type,
                        payload_cbor,
                        event.previous_event_hash,
                        event.signature,
                        event.signing_bytes,
                        received_at_ms,
                    ),
                )
                connection.execute(
                    """
                    UPDATE devices
                    SET last_sequence = ?, last_event_hash = ?
                    WHERE device_id = ?
                    """,
                    (event.sequence, event.event_hash, event.device_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def pending_count(self) -> int:
        rows = self.execute_read(
            "SELECT COUNT(*) AS count FROM events WHERE block_height IS NULL"
        )
        return int(rows[0]["count"])

    def proposed_block(self) -> sqlite3.Row | None:
        rows = self.execute_read(
            "SELECT * FROM blocks WHERE status = 'proposed' ORDER BY height LIMIT 1"
        )
        return rows[0] if rows else None

    def last_finalized_block(self) -> sqlite3.Row | None:
        rows = self.execute_read(
            "SELECT * FROM blocks WHERE status = 'finalized' ORDER BY height DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def create_proposal(
        self,
        header: BlockHeader,
        event_hashes: list[bytes],
        authorities: list[tuple[str, bytes]],
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                open_block = connection.execute(
                    "SELECT height FROM blocks WHERE status = 'proposed' LIMIT 1"
                ).fetchone()
                if open_block is not None:
                    raise ValueError(
                        f"block {open_block['height']} is still awaiting quorum"
                    )

                placeholders = ",".join("?" for _ in event_hashes)
                pending = connection.execute(
                    f"""
                    SELECT event_hash
                    FROM events
                    WHERE block_height IS NULL
                      AND event_hash IN ({placeholders})
                    """,
                    tuple(event_hashes),
                ).fetchall()
                actual = {row["event_hash"] for row in pending}
                if actual != set(event_hashes):
                    raise RuntimeError(
                        "one or more selected events were claimed by another block"
                    )

                connection.execute(
                    """
                    INSERT INTO blocks(
                        height, block_hash, previous_hash, created_at_ms,
                        merkle_root, event_count, proposer_id,
                        authority_set_hash, quorum_threshold, policy_hash,
                        version, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
                    """,
                    (
                        header.height,
                        header.block_hash,
                        header.previous_hash,
                        header.created_at_ms,
                        header.merkle_root,
                        header.event_count,
                        header.proposer_id,
                        header.authority_set_hash,
                        header.quorum_threshold,
                        header.policy_hash,
                        header.version,
                    ),
                )
                for position, event_hash in enumerate(event_hashes):
                    connection.execute(
                        """
                        INSERT INTO block_events(block_height, position, event_hash)
                        VALUES (?, ?, ?)
                        """,
                        (header.height, position, event_hash),
                    )
                    connection.execute(
                        "UPDATE events SET block_height = ? WHERE event_hash = ?",
                        (header.height, event_hash),
                    )
                for authority_id, public_key in authorities:
                    connection.execute(
                        """
                        INSERT INTO block_authorities(
                            block_height, authority_id, public_key
                        ) VALUES (?, ?, ?)
                        """,
                        (header.height, authority_id, public_key),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def block(self, height: int) -> sqlite3.Row | None:
        rows = self.execute_read("SELECT * FROM blocks WHERE height = ?", (height,))
        return rows[0] if rows else None

    def block_event_hashes(self, height: int) -> list[bytes]:
        rows = self.execute_read(
            """
            SELECT event_hash
            FROM block_events
            WHERE block_height = ?
            ORDER BY position
            """,
            (height,),
        )
        return [row["event_hash"] for row in rows]

    def block_authorities(self, height: int) -> list[sqlite3.Row]:
        return self.execute_read(
            """
            SELECT authority_id, public_key
            FROM block_authorities
            WHERE block_height = ?
            ORDER BY authority_id
            """,
            (height,),
        )

    def block_signatures(self, height: int) -> list[sqlite3.Row]:
        return self.execute_read(
            """
            SELECT authority_id, signature, signed_at_ms
            FROM block_signatures
            WHERE block_height = ?
            ORDER BY authority_id
            """,
            (height,),
        )

    def insert_block_signature(
        self,
        height: int,
        authority_id: str,
        signature: bytes,
        signed_at_ms: int,
    ) -> str:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                block = connection.execute(
                    "SELECT * FROM blocks WHERE height = ?", (height,)
                ).fetchone()
                if block is None:
                    raise ValueError("block does not exist")
                if block["status"] == "finalized":
                    existing = connection.execute(
                        """
                        SELECT 1 FROM block_signatures
                        WHERE block_height = ? AND authority_id = ?
                        """,
                        (height, authority_id),
                    ).fetchone()
                    if existing:
                        connection.execute("COMMIT")
                        return "finalized"

                member = connection.execute(
                    """
                    SELECT 1 FROM block_authorities
                    WHERE block_height = ? AND authority_id = ?
                    """,
                    (height, authority_id),
                ).fetchone()
                if member is None:
                    raise ValueError("authority is not in this block's snapshot")

                connection.execute(
                    """
                    INSERT OR IGNORE INTO block_signatures(
                        block_height, authority_id, signature, signed_at_ms
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (height, authority_id, signature, signed_at_ms),
                )
                signature_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM block_signatures
                    WHERE block_height = ?
                    """,
                    (height,),
                ).fetchone()["count"]

                status = block["status"]
                if signature_count >= block["quorum_threshold"]:
                    previous_ok = (
                        block["height"] == 1 and block["previous_hash"] == ZERO_HASH
                    )
                    if block["height"] > 1:
                        previous = connection.execute(
                            """
                            SELECT block_hash, status
                            FROM blocks
                            WHERE height = ?
                            """,
                            (block["height"] - 1,),
                        ).fetchone()
                        previous_ok = (
                            previous is not None
                            and previous["status"] == "finalized"
                            and previous["block_hash"] == block["previous_hash"]
                        )
                    if not previous_ok:
                        raise RuntimeError("previous block is not finalized")

                    connection.execute(
                        "UPDATE blocks SET status = 'finalized' WHERE height = ?",
                        (height,),
                    )
                    connection.execute(
                        "UPDATE events SET finalized = 1 WHERE block_height = ?",
                        (height,),
                    )
                    status = "finalized"

                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def all_blocks(self) -> list[sqlite3.Row]:
        return self.execute_read("SELECT * FROM blocks ORDER BY height")

    def all_events(self) -> list[sqlite3.Row]:
        return self.execute_read(
            "SELECT * FROM events ORDER BY device_id, sequence"
        )

    def events_for_device(
        self, device_id: str, limit: int = 100
    ) -> list[sqlite3.Row]:
        return self.execute_read(
            """
            SELECT * FROM events
            WHERE device_id = ?
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (device_id, limit),
        )
