from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .canonical import loads
from .cluster_control import ALLOWED_ACTIONS, DockerClusterController
from .crypto import KeyPair
from .dashboard import render_dashboard
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


def _iso_millis(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


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
        version="0.3.0",
        description=(
            "Edge-first, signed and quorum-finalized IoT telemetry ledger with a "
            "development cluster monitor. Administrative endpoints require strong "
            "authentication before production use."
        ),
    )
    app.state.database = database
    app.state.ledger = ledger
    app.state.node_key = node_key
    app.state.node_id = node_id
    app.state.batch_size = batch_size
    app.state.seal_lock = threading.Lock()
    app.state.cluster_controller = DockerClusterController()
    app.state.result_dir = Path(os.getenv("EDGECHAIN_RESULT_DIR", "result"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        return HTMLResponse(render_dashboard())

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(render_dashboard())

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
            "cluster_control_available": app.state.cluster_controller.available,
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

    @app.get("/cluster/events")
    def cluster_events(limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "event_hash": row["event_hash"].hex(),
                "device_id": row["device_id"],
                "sequence": row["sequence"],
                "device_time_ms": row["device_time_ms"],
                "event_type": row["event_type"],
                "payload": loads(row["payload_cbor"]),
                "received_at_ms": row["received_at_ms"],
                "received_at": _iso_millis(row["received_at_ms"]),
                "block_height": row["block_height"],
                "finalized": bool(row["finalized"]),
            }
            for row in database.recent_events(limit)
        ]

    @app.get("/cluster/state")
    def cluster_state() -> dict[str, Any]:
        controller_state = app.state.cluster_controller.state()
        activity = database.device_activity()
        runtime_by_service = {
            item["service"]: item
            for item in controller_state.get("containers", [])
            if item["service"].startswith("device-")
        }
        services = sorted(
            set(runtime_by_service)
            | {
                "device-" + device_id.rsplit("-", 1)[-1]
                for device_id in activity
                if device_id.startswith("iot-device-")
            }
        )
        devices = []
        for service in services:
            suffix = service.rsplit("-", 1)[-1]
            device_id = f"iot-device-{suffix}"
            runtime = runtime_by_service.get(service, {})
            ledger_activity = activity.get(device_id, {})
            devices.append(
                {
                    "service": service,
                    "device_id": device_id,
                    "state": runtime.get("state", "unknown"),
                    "running": bool(runtime.get("running", False)),
                    "paused": bool(runtime.get("paused", False)),
                    "health": runtime.get("health"),
                    "exit_code": runtime.get("exit_code"),
                    "container_id": runtime.get("container_id"),
                    "last_sequence": ledger_activity.get("last_sequence", 0),
                    "last_event_at_ms": ledger_activity.get("last_event_at_ms"),
                    "last_event_at": _iso_millis(
                        ledger_activity.get("last_event_at_ms")
                    ),
                }
            )
        return {
            "node_id": app.state.node_id,
            "ledger": database.statistics(),
            "controller": {
                key: value
                for key, value in controller_state.items()
                if key != "containers"
            },
            "summary": {
                "total_devices": len(devices),
                "running_devices": sum(item["running"] for item in devices),
                "paused_devices": sum(item["paused"] for item in devices),
                "stopped_devices": sum(
                    item["state"] in {"exited", "dead"} for item in devices
                ),
            },
            "devices": devices,
        }

    @app.post("/cluster/devices/{action}")
    def control_all_devices(action: str) -> dict[str, Any]:
        if action not in ALLOWED_ACTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported action: {action}")
        results = app.state.cluster_controller.control_all(action)
        if not results and not app.state.cluster_controller.available:
            raise HTTPException(
                status_code=503,
                detail=app.state.cluster_controller.error
                or "Docker control is unavailable",
            )
        return {
            "action": action,
            "ok": all(item["ok"] for item in results),
            "results": results,
        }

    @app.post("/cluster/devices/{service}/{action}")
    def control_device(service: str, action: str) -> dict[str, Any]:
        result = app.state.cluster_controller.control(service, action).to_dict()
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result)
        return result

    @app.get("/benchmark/report", include_in_schema=False)
    def benchmark_report() -> FileResponse:
        path = app.state.result_dir / "report.html"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="No benchmark report exists yet. Run: docker compose up -d test",
            )
        return FileResponse(path, media_type="text/html")

    @app.get("/benchmark/status")
    def benchmark_status() -> dict[str, Any]:
        path = app.state.result_dir / "benchmark-status.json"
        if not path.exists():
            return {"status": "not_started"}
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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
