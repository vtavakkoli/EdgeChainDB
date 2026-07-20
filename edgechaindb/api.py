from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .crypto import KeyPair
from .ledger import EdgeChainLedger
from .models import SignedEvent
from .store import Database


class DeviceEnrollment(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    public_key: str


class EventInput(BaseModel):
    device_id: str
    sequence: int
    device_time_ms: int
    event_type: str
    payload: dict[str, Any]
    previous_event_hash: str
    signature: str
    version: int = 1


class ExternalSignature(BaseModel):
    authority_id: str
    signature: str


def create_app(
    *,
    database_path: str | None = None,
    node_key_path: str | None = None,
    node_id: str | None = None,
    quorum_threshold: int | None = None,
    batch_size: int | None = None,
) -> FastAPI:
    database_path = database_path or os.getenv("EDGECHAIN_DB", "edgechain.db")
    node_key_path = node_key_path or os.getenv(
        "EDGECHAIN_NODE_KEY", "edgechain-node.key"
    )
    node_id = node_id or os.getenv("EDGECHAIN_NODE_ID", "edge-gateway-1")
    quorum_threshold = quorum_threshold or int(os.getenv("EDGECHAIN_QUORUM", "1"))
    batch_size = batch_size or int(os.getenv("EDGECHAIN_BATCH_SIZE", "64"))

    database = Database(database_path)
    ledger = EdgeChainLedger(database, quorum_threshold=quorum_threshold)
    node_key = KeyPair.load_or_create(node_key_path)
    ledger.register_authority(node_id, node_key.public_bytes)

    app = FastAPI(
        title="EdgeChainDB",
        version="0.2.0",
        description=(
            "Edge-first, signed and quorum-finalized IoT telemetry ledger. "
            "Administrative endpoints require authentication before production use."
        ),
    )
    app.state.database = database
    app.state.ledger = ledger
    app.state.node_key = node_key
    app.state.node_id = node_id
    app.state.batch_size = batch_size
    app.state.seal_lock = threading.Lock()

    @app.get("/health")
    def health() -> dict[str, Any]:
        proposal = database.proposed_block()
        open_proposal = None
        if proposal is not None:
            open_proposal = {
                "height": proposal["height"],
                "block_hash": proposal["block_hash"].hex(),
                "event_count": proposal["event_count"],
                "status": proposal["status"],
            }
        return {
            "status": "ok",
            "node_id": app.state.node_id,
            **database.statistics(),
            "open_proposal": open_proposal,
        }

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        return {"node_id": app.state.node_id, **database.statistics()}

    @app.get("/devices")
    def list_devices() -> list[dict[str, Any]]:
        return [
            {
                "device_id": row["device_id"],
                "key_id": row["key_id"],
                "status": row["status"],
                "last_sequence": row["last_sequence"],
                "last_event_hash": row["last_event_hash"].hex(),
                "enrolled_at_ms": row["enrolled_at_ms"],
            }
            for row in database.list_devices()
        ]

    @app.get("/devices/{device_id}/checkpoint")
    def device_checkpoint(device_id: str) -> dict[str, Any]:
        row = database.get_device(device_id)
        if row is None:
            raise HTTPException(status_code=404, detail="device is not enrolled")
        return {
            "device_id": device_id,
            "status": row["status"],
            "last_sequence": row["last_sequence"],
            "last_event_hash": row["last_event_hash"].hex(),
        }

    @app.post("/devices", status_code=201)
    def enroll_device(value: DeviceEnrollment) -> dict[str, Any]:
        try:
            public_key = bytes.fromhex(value.public_key)
            return ledger.register_device(value.device_id, public_key)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/events", status_code=202)
    def submit_event(value: EventInput) -> dict[str, Any]:
        try:
            event = SignedEvent.from_wire(value.model_dump())
            accepted = ledger.accept_event(event)
            if database.pending_count() >= app.state.batch_size:
                with app.state.seal_lock:
                    if (
                        database.pending_count() >= app.state.batch_size
                        and database.proposed_block() is None
                    ):
                        accepted["block"] = ledger.propose_block(
                            app.state.node_id,
                            app.state.node_key.private_key,
                            max_events=app.state.batch_size,
                        )
            return accepted
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/blocks/seal")
    def seal_block(max_events: int = 256) -> dict[str, Any]:
        try:
            with app.state.seal_lock:
                return ledger.propose_block(
                    app.state.node_id,
                    app.state.node_key.private_key,
                    max_events=max_events,
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/blocks/{height}/signatures")
    def add_signature(height: int, value: ExternalSignature) -> dict[str, str]:
        try:
            signature = bytes.fromhex(value.signature)
            status = ledger.add_external_signature(
                height, value.authority_id, signature
            )
            return {"status": status}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/blocks")
    def list_blocks() -> list[dict[str, Any]]:
        result = []
        for row in database.all_blocks():
            result.append(
                {
                    "height": row["height"],
                    "block_hash": row["block_hash"].hex(),
                    "previous_hash": row["previous_hash"].hex(),
                    "created_at_ms": row["created_at_ms"],
                    "merkle_root": row["merkle_root"].hex(),
                    "event_count": row["event_count"],
                    "proposer_id": row["proposer_id"],
                    "status": row["status"],
                    "signature_count": len(database.block_signatures(row["height"])),
                    "quorum_threshold": row["quorum_threshold"],
                }
            )
        return result

    @app.get("/devices/{device_id}/events")
    def device_events(device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 1000)
        return [
            {
                "event_hash": row["event_hash"].hex(),
                "device_id": row["device_id"],
                "sequence": row["sequence"],
                "device_time_ms": row["device_time_ms"],
                "event_type": row["event_type"],
                "previous_event_hash": row["previous_event_hash"].hex(),
                "block_height": row["block_height"],
                "finalized": bool(row["finalized"]),
            }
            for row in database.events_for_device(device_id, limit)
        ]

    @app.get("/proofs/{event_hash}")
    def event_proof(event_hash: str) -> dict[str, Any]:
        try:
            return ledger.event_proof(event_hash)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/verify")
    def verify() -> dict[str, Any]:
        result = ledger.verify_all()
        if not result["valid"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    return app

