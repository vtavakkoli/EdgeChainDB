from __future__ import annotations

import sqlite3
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .canonical import dumps, loads
from .crypto import key_id, sha256, verify_signature
from .merkle import ProofStep, proof as merkle_proof, root as merkle_root
from .merkle import verify as verify_merkle
from .models import BlockHeader, SignedEvent, ZERO_HASH
from .store import Database


DEFAULT_POLICY = {
    "event_schema_version": 1,
    "float_policy": "forbid-use-scaled-integers",
    "hash": "sha-256",
    "signature": "ed25519",
    "merkle": "domain-separated-binary-tree-v1",
    "sequence_policy": "strict-plus-one",
}


class EdgeChainLedger:
    def __init__(
        self,
        database: Database,
        *,
        quorum_threshold: int = 1,
        policy: dict[str, Any] | None = None,
    ) -> None:
        if quorum_threshold < 1:
            raise ValueError("quorum_threshold must be at least one")
        self.database = database
        self.quorum_threshold = quorum_threshold
        self.policy = policy or DEFAULT_POLICY
        self.policy_hash = sha256(dumps(self.policy))

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def register_device(self, device_id: str, public_key: bytes) -> dict[str, str]:
        if not device_id or len(device_id) > 128:
            raise ValueError("device_id must contain 1 to 128 characters")
        if len(public_key) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        identifier = key_id(public_key)
        created = self.database.register_device(
            device_id, public_key, identifier, self._now_ms()
        )
        device = self.database.get_device(device_id)
        assert device is not None
        return {
            "device_id": device_id,
            "key_id": identifier,
            "created": created,
            "last_sequence": int(device["last_sequence"]),
            "last_event_hash": device["last_event_hash"].hex(),
        }

    def register_authority(
        self, authority_id: str, public_key: bytes
    ) -> dict[str, str]:
        if not authority_id or len(authority_id) > 128:
            raise ValueError("authority_id must contain 1 to 128 characters")
        if len(public_key) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        identifier = key_id(public_key)
        created = self.database.register_authority(
            authority_id, public_key, identifier, self._now_ms()
        )
        return {
            "authority_id": authority_id,
            "key_id": identifier,
            "created": created,
        }

    def accept_event(self, event: SignedEvent) -> dict[str, Any]:
        if event.version != 1:
            raise ValueError("unsupported event version")
        if event.sequence < 1:
            raise ValueError("event sequence must be positive")
        if not event.event_type or len(event.event_type) > 128:
            raise ValueError("event_type must contain 1 to 128 characters")

        device = self.database.get_device(event.device_id)
        if device is None:
            raise ValueError("device is not enrolled")
        if device["status"] != "active":
            raise ValueError("device is not active")
        if not verify_signature(
            device["public_key"], event.signature, event.signing_bytes
        ):
            raise ValueError("invalid device signature")

        payload_cbor = dumps(event.payload)
        inserted = self.database.insert_verified_event(
            event, payload_cbor, self._now_ms()
        )
        return {
            "accepted": True,
            "duplicate": not inserted,
            "event_hash": event.event_hash.hex(),
            "device_id": event.device_id,
            "sequence": event.sequence,
        }

    def _authority_snapshot(self) -> list[tuple[str, bytes]]:
        rows = self.database.active_authorities()
        authorities = [(row["authority_id"], row["public_key"]) for row in rows]
        if len(authorities) < self.quorum_threshold:
            raise ValueError(
                f"need at least {self.quorum_threshold} active authorities; "
                f"found {len(authorities)}"
            )
        return authorities

    @staticmethod
    def _authority_set_hash(authorities: list[tuple[str, bytes]]) -> bytes:
        value = [
            {"authority_id": authority_id, "public_key": public_key}
            for authority_id, public_key in authorities
        ]
        return sha256(dumps(value))

    def propose_block(
        self,
        proposer_id: str,
        proposer_private_key: Ed25519PrivateKey,
        *,
        max_events: int = 256,
    ) -> dict[str, Any]:
        if max_events < 1 or max_events > 10000:
            raise ValueError("max_events must be between 1 and 10000")
        if self.database.proposed_block() is not None:
            raise ValueError("an earlier block is still awaiting quorum")

        authorities = self._authority_snapshot()
        authority_map = dict(authorities)
        if proposer_id not in authority_map:
            raise ValueError("proposer is not an active authority")

        derived_public = proposer_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if derived_public != authority_map[proposer_id]:
            raise ValueError("proposer private key does not match enrolled key")

        pending_rows = self.database.execute_read(
            """
            SELECT event_hash
            FROM events
            WHERE block_height IS NULL
            ORDER BY received_at_ms, event_hash
            LIMIT ?
            """,
            (max_events,),
        )
        event_hashes = [row["event_hash"] for row in pending_rows]
        if not event_hashes:
            raise ValueError("there are no pending events")

        previous = self.database.last_finalized_block()
        height = 1 if previous is None else int(previous["height"]) + 1
        previous_hash = ZERO_HASH if previous is None else previous["block_hash"]

        header = BlockHeader(
            height=height,
            previous_hash=previous_hash,
            created_at_ms=self._now_ms(),
            merkle_root=merkle_root(event_hashes),
            event_count=len(event_hashes),
            proposer_id=proposer_id,
            authority_set_hash=self._authority_set_hash(authorities),
            quorum_threshold=self.quorum_threshold,
            policy_hash=self.policy_hash,
        )
        self.database.create_proposal(header, event_hashes, authorities)
        status = self.sign_block(height, proposer_id, proposer_private_key)
        return {
            "height": height,
            "block_hash": header.block_hash.hex(),
            "event_count": len(event_hashes),
            "merkle_root": header.merkle_root.hex(),
            "status": status,
            "signatures": len(self.database.block_signatures(height)),
            "required_signatures": self.quorum_threshold,
        }

    def sign_block(
        self,
        height: int,
        authority_id: str,
        private_key: Ed25519PrivateKey,
    ) -> str:
        block = self.database.block(height)
        if block is None:
            raise ValueError("block does not exist")

        members = {
            row["authority_id"]: row["public_key"]
            for row in self.database.block_authorities(height)
        }
        expected_public = members.get(authority_id)
        if expected_public is None:
            raise ValueError("authority is not part of this block")
        actual_public = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if actual_public != expected_public:
            raise ValueError("authority private key does not match snapshot")

        signature = private_key.sign(block["block_hash"])
        if not verify_signature(expected_public, signature, block["block_hash"]):
            raise RuntimeError("generated authority signature failed verification")
        return self.database.insert_block_signature(
            height, authority_id, signature, self._now_ms()
        )

    def add_external_signature(
        self,
        height: int,
        authority_id: str,
        signature: bytes,
    ) -> str:
        block = self.database.block(height)
        if block is None:
            raise ValueError("block does not exist")
        members = {
            row["authority_id"]: row["public_key"]
            for row in self.database.block_authorities(height)
        }
        public_key = members.get(authority_id)
        if public_key is None:
            raise ValueError("authority is not part of this block")
        if not verify_signature(public_key, signature, block["block_hash"]):
            raise ValueError("invalid authority signature")
        return self.database.insert_block_signature(
            height, authority_id, signature, self._now_ms()
        )

    @staticmethod
    def _header_from_row(row: sqlite3.Row) -> BlockHeader:
        return BlockHeader(
            height=row["height"],
            previous_hash=row["previous_hash"],
            created_at_ms=row["created_at_ms"],
            merkle_root=row["merkle_root"],
            event_count=row["event_count"],
            proposer_id=row["proposer_id"],
            authority_set_hash=row["authority_set_hash"],
            quorum_threshold=row["quorum_threshold"],
            policy_hash=row["policy_hash"],
            version=row["version"],
        )

    def event_proof(self, event_hash_hex: str) -> dict[str, Any]:
        try:
            event_hash = bytes.fromhex(event_hash_hex)
        except ValueError as exc:
            raise ValueError("event hash must be hexadecimal") from exc
        rows = self.database.execute_read(
            """
            SELECT block_height, position
            FROM block_events
            WHERE event_hash = ?
            """,
            (event_hash,),
        )
        if not rows:
            raise ValueError("event is not assigned to a block")
        height = int(rows[0]["block_height"])
        position = int(rows[0]["position"])
        block = self.database.block(height)
        assert block is not None
        hashes = self.database.block_event_hashes(height)
        steps = merkle_proof(hashes, position)
        return {
            "event_hash": event_hash.hex(),
            "block_height": height,
            "block_hash": block["block_hash"].hex(),
            "merkle_root": block["merkle_root"].hex(),
            "position": position,
            "proof": [step.to_wire() for step in steps],
        }

    @staticmethod
    def verify_event_proof(proof_value: dict[str, Any]) -> bool:
        try:
            event_hash = bytes.fromhex(proof_value["event_hash"])
            expected_root = bytes.fromhex(proof_value["merkle_root"])
            steps = [
                ProofStep(
                    side=item["side"],
                    sibling=bytes.fromhex(item["sibling"]),
                )
                for item in proof_value["proof"]
            ]
        except (KeyError, TypeError, ValueError):
            return False
        return verify_merkle(event_hash, steps, expected_root)

    def verify_all(self) -> dict[str, Any]:
        errors: list[str] = []
        previous_hash = ZERO_HASH
        expected_height = 1

        for block in self.database.all_blocks():
            height = int(block["height"])
            if height != expected_height:
                errors.append(
                    f"block height gap: expected {expected_height}, found {height}"
                )
            if block["previous_hash"] != previous_hash:
                errors.append(f"block {height}: previous hash mismatch")

            header = self._header_from_row(block)
            if header.block_hash != block["block_hash"]:
                errors.append(f"block {height}: block hash mismatch")

            event_hashes = self.database.block_event_hashes(height)
            if len(event_hashes) != block["event_count"]:
                errors.append(f"block {height}: event count mismatch")
            if merkle_root(event_hashes) != block["merkle_root"]:
                errors.append(f"block {height}: Merkle root mismatch")

            for event_hash in event_hashes:
                references = self.database.execute_read(
                    """
                    SELECT block_height, finalized
                    FROM events
                    WHERE event_hash = ?
                    """,
                    (event_hash,),
                )
                if not references:
                    errors.append(
                        f"block {height}: references missing event {event_hash.hex()}"
                    )
                    continue
                event_row = references[0]
                if event_row["block_height"] != height:
                    errors.append(
                        f"block {height}: event {event_hash.hex()} has inconsistent "
                        "block_height"
                    )
                if (
                    block["status"] == "finalized"
                    and not bool(event_row["finalized"])
                ):
                    errors.append(
                        f"block {height}: event {event_hash.hex()} is not finalized"
                    )

            authorities = [
                (row["authority_id"], row["public_key"])
                for row in self.database.block_authorities(height)
            ]
            if self._authority_set_hash(authorities) != block["authority_set_hash"]:
                errors.append(f"block {height}: authority snapshot mismatch")

            member_map = dict(authorities)
            valid_signatures = 0
            for signature_row in self.database.block_signatures(height):
                public_key = member_map.get(signature_row["authority_id"])
                if public_key and verify_signature(
                    public_key,
                    signature_row["signature"],
                    block["block_hash"],
                ):
                    valid_signatures += 1
                else:
                    errors.append(
                        f"block {height}: invalid signature from "
                        f"{signature_row['authority_id']}"
                    )

            if block["status"] == "finalized":
                if valid_signatures < block["quorum_threshold"]:
                    errors.append(f"block {height}: finalized without quorum")
                previous_hash = block["block_hash"]
                expected_height += 1
            else:
                # A proposal is allowed only as the final row.
                if height != self.database.all_blocks()[-1]["height"]:
                    errors.append(f"block {height}: non-final block is not last")

        chain_state: dict[str, tuple[int, bytes]] = {}
        for event in self.database.all_events():
            device_id = event["device_id"]
            device = self.database.get_device(device_id)
            if device is None:
                errors.append(f"event for unknown device {device_id}")
                continue

            expected_sequence, expected_previous = chain_state.get(
                device_id, (1, ZERO_HASH)
            )
            if event["sequence"] != expected_sequence:
                errors.append(
                    f"device {device_id}: expected sequence {expected_sequence}, "
                    f"found {event['sequence']}"
                )
            if event["previous_event_hash"] != expected_previous:
                errors.append(
                    f"device {device_id} sequence {event['sequence']}: "
                    "previous event hash mismatch"
                )

            try:
                unsigned = loads(event["unsigned_cbor"])
                if unsigned["device_id"] != device_id:
                    errors.append(f"event {event['event_hash'].hex()}: device mismatch")
                if unsigned["sequence"] != event["sequence"]:
                    errors.append(f"event {event['event_hash'].hex()}: sequence mismatch")
                if dumps(unsigned["payload"]) != event["payload_cbor"]:
                    errors.append(f"event {event['event_hash'].hex()}: payload mismatch")
            except Exception as exc:
                errors.append(
                    f"event {event['event_hash'].hex()}: invalid canonical data: {exc}"
                )

            if not verify_signature(
                device["public_key"],
                event["signature"],
                event["unsigned_cbor"],
            ):
                errors.append(f"event {event['event_hash'].hex()}: invalid signature")

            computed_hash = sha256(
                b"edgechaindb:event:v1\x00"
                + event["unsigned_cbor"]
                + event["signature"]
            )
            if computed_hash != event["event_hash"]:
                errors.append(f"event {event['event_hash'].hex()}: hash mismatch")

            chain_state[device_id] = (
                int(event["sequence"]) + 1,
                event["event_hash"],
            )

        for device_id, (next_sequence, latest_hash) in chain_state.items():
            device = self.database.get_device(device_id)
            if device is None:
                continue
            if int(device["last_sequence"]) != next_sequence - 1:
                errors.append(f"device {device_id}: stored sequence checkpoint mismatch")
            if device["last_event_hash"] != latest_hash:
                errors.append(f"device {device_id}: stored hash checkpoint mismatch")

        assigned_rows = self.database.execute_read(
            """
            SELECT event_hash, block_height
            FROM events
            WHERE block_height IS NOT NULL
            """
        )
        for assigned in assigned_rows:
            mapping = self.database.execute_read(
                """
                SELECT block_height
                FROM block_events
                WHERE event_hash = ?
                """,
                (assigned["event_hash"],),
            )
            if not mapping or mapping[0]["block_height"] != assigned["block_height"]:
                errors.append(
                    f"event {assigned['event_hash'].hex()}: block mapping mismatch"
                )

        return {
            "valid": not errors,
            "errors": errors,
            "blocks": len(self.database.all_blocks()),
            "events": len(self.database.all_events()),
            "pending_events": self.database.pending_count(),
        }
