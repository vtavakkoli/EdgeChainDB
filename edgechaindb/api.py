from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .canonical import loads
from .cluster_control import ALLOWED_ACTIONS, DockerClusterController
from .crypto import KeyPair
from .dashboard import render_dashboard
from .ledger import EdgeChainLedger
from .models import SignedEvent
from .observability import get_logger
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


gateway_log = get_logger("gateway")


def create_app(
    *,
    database_path: str | None = None,
    node_key_path: str | None = None,
    node_id: str | None = None,
    quorum_threshold: int | None = None,
    batch_size: int | None = None,
    authority_count: int | None = None,
    authority_key_dir: str | None = None,
) -> FastAPI:
    database_path = database_path or os.getenv("EDGECHAIN_DB", "edgechain.db")
    node_key_path = node_key_path or os.getenv(
        "EDGECHAIN_NODE_KEY", "edgechain-node.key"
    )
    node_id = node_id or os.getenv("EDGECHAIN_NODE_ID", "edge-gateway-1")
    quorum_threshold = quorum_threshold or int(os.getenv("EDGECHAIN_QUORUM", "1"))
    batch_size = batch_size or int(os.getenv("EDGECHAIN_BATCH_SIZE", "64"))
    authority_count = authority_count or int(os.getenv("EDGECHAIN_AUTHORITIES", "1"))
    authority_key_dir = authority_key_dir or os.getenv(
        "EDGECHAIN_AUTHORITY_KEY_DIR",
        str(Path(node_key_path).parent / "authorities"),
    )
    if authority_count < 1:
        raise ValueError("authority_count must be at least one")
    if quorum_threshold > authority_count:
        raise ValueError(
            f"quorum threshold {quorum_threshold} exceeds authority count {authority_count}"
        )

    database = Database(database_path)
    ledger = EdgeChainLedger(database, quorum_threshold=quorum_threshold)
    node_key = KeyPair.load_or_create(node_key_path)
    authority_keys: list[tuple[str, KeyPair]] = [(node_id, node_key)]
    ledger.register_authority(node_id, node_key.public_bytes)
    key_root = Path(authority_key_dir)
    key_root.mkdir(parents=True, exist_ok=True)
    for index in range(2, authority_count + 1):
        authority_id = f"{node_id}-authority-{index:02d}"
        authority_key = KeyPair.load_or_create(key_root / f"authority-{index:02d}.key")
        ledger.register_authority(authority_id, authority_key.public_bytes)
        authority_keys.append((authority_id, authority_key))

    app = FastAPI(
        title="EdgeChainDB",
        version="0.8.0",
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
    app.state.authority_keys = authority_keys
    app.state.authority_count = authority_count
    app.state.quorum_threshold = quorum_threshold
    app.state.seal_lock = threading.Lock()
    app.state.cluster_controller = DockerClusterController()
    app.state.result_dir = Path(os.getenv("EDGECHAIN_RESULT_DIR", "result"))
    app.state.started_monotonic = time.monotonic()
    app.state.started_at = datetime.now(timezone.utc).isoformat()

    gateway_log.info(
        "gateway_initialized",
        database_path=str(database_path),
        node_key_path=str(node_key_path),
        node_id=node_id,
        quorum_threshold=quorum_threshold,
        batch_size=batch_size,
        authority_count=authority_count,
        monitor_url="http://localhost:3030",
    )

    def propose_and_finalize(max_events: int) -> dict[str, Any]:
        result = ledger.propose_block(
            app.state.node_id,
            app.state.node_key.private_key,
            max_events=max_events,
        )
        status = str(result["status"])
        if status != "finalized":
            for authority_id, authority_key in app.state.authority_keys[1:]:
                status = ledger.sign_block(
                    int(result["height"]), authority_id, authority_key.private_key
                )
                if status == "finalized":
                    break
        block = database.block(int(result["height"]))
        result["status"] = status
        result["signatures"] = len(database.block_signatures(int(result["height"])))
        result["required_signatures"] = app.state.quorum_threshold
        if block is not None:
            finalized_at = block["finalized_at_ms"]
            result["finalized_at_ms"] = finalized_at
            result["finalization_latency_ms"] = (
                int(finalized_at) - int(block["created_at_ms"])
                if finalized_at is not None
                else None
            )
        return result

    @app.middleware("http")
    async def request_log_middleware(request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            gateway_log.error(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            fields = {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            }
            if request.url.path == "/health" and status_code < 400:
                gateway_log.debug("http_request", **fields)
            elif status_code >= 400:
                gateway_log.warning("http_request", **fields)
            else:
                gateway_log.info("http_request", **fields)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        return HTMLResponse(render_dashboard())

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(render_dashboard())

    @app.get("/monitor/health")
    def monitor_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "dashboard": "ready",
            "node_id": app.state.node_id,
            "api_port": getattr(app.state, "api_port", 8000),
            "monitor_port": getattr(app.state, "monitor_port", 3030),
            "dashboard_marker": "EdgeChainDB Cluster Monitor",
        }

    @app.get("/database/info")
    def database_info(quick_check: bool = False) -> dict[str, Any]:
        return {
            **database.database_info(run_quick_check=quick_check),
            "statistics": database.statistics(),
        }

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
            "started_at": app.state.started_at,
            "uptime_seconds": round(time.monotonic() - app.state.started_monotonic, 3),
            **database.statistics(),
            "open_proposal": open_proposal,
            "cluster_control_available": app.state.cluster_controller.available,
            "authority_count": app.state.authority_count,
            "quorum_threshold": app.state.quorum_threshold,
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
            result = ledger.register_device(value.device_id, public_key)
            gateway_log.info(
                "device_enrolled",
                device_id=value.device_id,
                created_new=result.get("created"),
                last_sequence=result.get("last_sequence"),
                key_id=result.get("key_id"),
            )
            return result
        except Exception as exc:
            gateway_log.warning(
                "device_enrollment_rejected",
                device_id=value.device_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/events", status_code=202)
    def submit_event(value: EventInput) -> dict[str, Any]:
        try:
            event = SignedEvent.from_wire(value.model_dump())
            accepted = ledger.accept_event(event)
            gateway_log.info(
                "telemetry_accepted",
                device_id=event.device_id,
                sequence=event.sequence,
                event_type=event.event_type,
                duplicate=accepted.get("duplicate", False),
                event_hash=accepted.get("event_hash"),
                payload=event.payload,
            )
            if database.pending_count() >= app.state.batch_size:
                with app.state.seal_lock:
                    if (
                        database.pending_count() >= app.state.batch_size
                        and database.proposed_block() is None
                    ):
                        accepted["block"] = propose_and_finalize(app.state.batch_size)
                        gateway_log.info(
                            "block_auto_sealed",
                            **accepted["block"],
                        )
            return accepted
        except Exception as exc:
            gateway_log.warning(
                "telemetry_rejected",
                device_id=value.device_id,
                sequence=value.sequence,
                event_type=value.event_type,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/blocks/seal")
    def seal_block(max_events: int = 256) -> dict[str, Any]:
        try:
            with app.state.seal_lock:
                result = propose_and_finalize(max_events)
            gateway_log.info("block_manually_sealed", **result)
            return result
        except Exception as exc:
            gateway_log.warning(
                "block_seal_rejected", error=f"{type(exc).__name__}: {exc}"
            )
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
                    "finalized_at_ms": row["finalized_at_ms"],
                    "finalization_latency_ms": (int(row["finalized_at_ms"]) - int(row["created_at_ms"])) if row["finalized_at_ms"] is not None else None,
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
    def cluster_state(include_metrics: bool = True) -> dict[str, Any]:
        controller_state = app.state.cluster_controller.state(
            include_metrics=include_metrics
        )
        activity = database.device_activity(window_ms=60_000)
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
        now_ms = int(time.time() * 1000)
        devices = []
        for service in services:
            suffix = service.rsplit("-", 1)[-1]
            device_id = f"iot-device-{suffix}"
            runtime = runtime_by_service.get(service, {})
            ledger_activity = activity.get(device_id, {})
            payload_bytes = ledger_activity.get("payload_cbor")
            payload = loads(payload_bytes) if payload_bytes is not None else {}
            last_event_at_ms = ledger_activity.get("last_event_at_ms")
            age_ms = (
                max(now_ms - int(last_event_at_ms), 0)
                if last_event_at_ms is not None
                else None
            )
            running = bool(runtime.get("running", False))
            if age_ms is None:
                telemetry_status = "never"
            elif running and age_ms <= 10_000:
                telemetry_status = "live"
            elif age_ms <= 60_000:
                telemetry_status = "delayed"
            else:
                telemetry_status = "stale"
            device_time_ms = ledger_activity.get("device_time_ms")
            clock_lag_ms = (
                int(last_event_at_ms) - int(device_time_ms)
                if last_event_at_ms is not None and device_time_ms is not None
                else None
            )
            events_in_window = int(ledger_activity.get("events_in_window", 0))
            devices.append(
                {
                    "service": service,
                    "device_id": device_id,
                    "state": runtime.get("state", "unknown"),
                    "running": running,
                    "paused": bool(runtime.get("paused", False)),
                    "restarting": bool(runtime.get("restarting", False)),
                    "oom_killed": bool(runtime.get("oom_killed", False)),
                    "health": runtime.get("health"),
                    "exit_code": runtime.get("exit_code"),
                    "restart_count": runtime.get("restart_count", 0),
                    "container_id": runtime.get("container_id"),
                    "container_name": runtime.get("container_name"),
                    "networks": runtime.get("networks", []),
                    "metrics": runtime.get("metrics", {}),
                    "last_sequence": ledger_activity.get("last_sequence", 0),
                    "event_type": ledger_activity.get("event_type"),
                    "last_payload": payload,
                    "last_event_at_ms": last_event_at_ms,
                    "last_event_at": _iso_millis(last_event_at_ms),
                    "last_event_age_ms": age_ms,
                    "device_time_ms": device_time_ms,
                    "clock_lag_ms": clock_lag_ms,
                    "block_height": ledger_activity.get("block_height"),
                    "finalized": ledger_activity.get("finalized"),
                    "events_last_minute": events_in_window,
                    "events_per_second": round(events_in_window / 60, 3),
                    "telemetry_status": telemetry_status,
                }
            )
        all_containers = controller_state.get("containers", [])
        metrics = [item.get("metrics", {}) for item in all_containers]
        total_rx = sum(int(item.get("network_rx_bytes", 0)) for item in metrics)
        total_tx = sum(int(item.get("network_tx_bytes", 0)) for item in metrics)
        total_recent_events = sum(item["events_last_minute"] for item in devices)
        return {
            "node_id": app.state.node_id,
            "monitor_port": 3030,
            "gateway_started_at": app.state.started_at,
            "gateway_uptime_seconds": round(
                time.monotonic() - app.state.started_monotonic, 3
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ledger": database.statistics(),
            "controller": {
                key: value
                for key, value in controller_state.items()
                if key != "containers"
            },
            "summary": {
                "total_devices": len(devices),
                "running_devices": sum(item["running"] for item in devices),
                "live_devices": sum(
                    item["telemetry_status"] == "live" for item in devices
                ),
                "delayed_devices": sum(
                    item["telemetry_status"] == "delayed" for item in devices
                ),
                "stale_devices": sum(
                    item["telemetry_status"] in {"stale", "never"}
                    for item in devices
                ),
                "paused_devices": sum(item["paused"] for item in devices),
                "stopped_devices": sum(
                    item["state"] in {"exited", "dead"} for item in devices
                ),
                "events_last_minute": total_recent_events,
                "events_per_second": round(total_recent_events / 60, 3),
                "network_rx_bytes": total_rx,
                "network_tx_bytes": total_tx,
            },
            "services": [
                item
                for item in all_containers
                if not item["service"].startswith("device-")
            ],
            "devices": devices,
        }

    @app.get("/cluster/network")
    def cluster_network() -> dict[str, Any]:
        return app.state.cluster_controller.network_state()

    @app.get("/cluster/logs/{service}")
    def cluster_logs(service: str, tail: int = 250) -> dict[str, Any]:
        try:
            return app.state.cluster_controller.logs(service, tail=tail)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/cluster/devices/{action}")
    def control_all_devices(action: str) -> dict[str, Any]:
        if action not in ALLOWED_ACTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported action: {action}")
        gateway_log.info("cluster_action_requested", action=action, scope="all")
        results = app.state.cluster_controller.control_all(action)
        if not results and not app.state.cluster_controller.available:
            raise HTTPException(
                status_code=503,
                detail=app.state.cluster_controller.error
                or "Docker control is unavailable",
            )
        response = {
            "action": action,
            "ok": all(item["ok"] for item in results),
            "results": results,
        }
        gateway_log.info(
            "cluster_action_completed",
            action=action,
            scope="all",
            ok=response["ok"],
            affected=len(results),
        )
        return response

    @app.post("/cluster/devices/{service}/{action}")
    def control_device(service: str, action: str) -> dict[str, Any]:
        gateway_log.info(
            "cluster_action_requested", action=action, scope="single", service=service
        )
        result = app.state.cluster_controller.control(service, action).to_dict()
        if not result["ok"]:
            gateway_log.warning(
                "cluster_action_failed",
                action=action,
                service=service,
                error=result.get("error"),
            )
            raise HTTPException(status_code=400, detail=result)
        gateway_log.info(
            "cluster_action_completed",
            action=action,
            service=service,
            state=result.get("state"),
        )
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

    @app.get("/benchmark/research/summary")
    def research_benchmark_summary() -> dict[str, Any]:
        path = app.state.result_dir / "benchmarks" / "summary.json"
        if not path.exists():
            return {"status": "not_started", "passed": 0, "failed": 0, "benchmarks": []}
        try:
            import json

            return {"status": "completed", **json.loads(path.read_text(encoding="utf-8"))}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/benchmark/research/report", include_in_schema=False)
    def research_benchmark_report() -> FileResponse:
        path = app.state.result_dir / "benchmarks" / "report.html"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="No research benchmark report exists yet. Run: docker compose up -d test",
            )
        return FileResponse(path, media_type="text/html")

    @app.get("/benchmark/research/artifacts/{filename}", include_in_schema=False)
    def research_benchmark_artifact(filename: str) -> FileResponse:
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.endswith((".json", ".csv")):
            raise HTTPException(status_code=400, detail="unsupported artifact name")
        path = app.state.result_dir / "benchmarks" / safe_name
        if not path.exists():
            raise HTTPException(status_code=404, detail="benchmark artifact does not exist")
        media_type = "application/json" if path.suffix == ".json" else "text/csv"
        return FileResponse(path, media_type=media_type, filename=safe_name)

    def _experiment_artifact(name: str) -> Path:
        candidates = [
            app.state.result_dir / "experiments" / "combined" / name,
            app.state.result_dir / "experiments" / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    @app.get("/experiments/progress")
    def experiment_progress() -> dict[str, Any]:
        path = _experiment_artifact("progress.json")
        if not path.exists():
            return {"status": "not_started"}
        try:
            import json
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/experiments/summary")
    def experiment_summary() -> dict[str, Any]:
        path = _experiment_artifact("summary.json")
        if not path.exists():
            return {"status": "not_started", "coverage": {}}
        try:
            import json
            return {"status": "available", **json.loads(path.read_text(encoding="utf-8"))}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/experiments/report", include_in_schema=False)
    def experiment_report() -> FileResponse:
        path = _experiment_artifact("report.html")
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="No matrix report exists yet. Run scripts/run_experiments or merge_experiments.",
            )
        return FileResponse(path, media_type="text/html")

    @app.get("/experiments/plan", include_in_schema=False)
    def experiment_plan_report() -> FileResponse:
        path = _experiment_artifact("matrix-plan.html")
        if not path.exists():
            raise HTTPException(status_code=404, detail="No matrix plan exists yet.")
        return FileResponse(path, media_type="text/html")

    @app.get("/proofs/{event_hash}")
    def event_proof(event_hash: str) -> dict[str, Any]:
        try:
            return ledger.event_proof(event_hash)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/verify")
    def verify() -> dict[str, Any]:
        started = time.perf_counter()
        gateway_log.info("ledger_verification_started")
        result = ledger.verify_all()
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        gateway_log.info(
            "ledger_verification_completed",
            valid=result.get("valid"),
            blocks=result.get("blocks"),
            events=result.get("events"),
            errors=len(result.get("errors", [])),
            duration_ms=duration_ms,
        )
        result["duration_ms"] = duration_ms
        if not result["valid"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    return app
