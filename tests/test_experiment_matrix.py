from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from edgechaindb.api import create_app
from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient
from edgechaindb.experiments.model import load_plan
from edgechaindb.experiments.report import write_plan_artifacts, write_result_artifacts
from edgechaindb.experiments.worker import run_worker
from edgechaindb.system_test import start_local_server, stop_local_server


def test_full_matrix_has_required_24000_runs_and_event_volume():
    plan = load_plan("experiments/full-matrix.yaml")
    assert plan.configurations == 4800
    assert plan.runs == 24000
    assert plan.nominal_events == 6_666_000_000
    assert [value.label for value in plan.authority_thresholds] == [
        "1-of-1",
        "2-of-3",
        "3-of-4",
        "5-of-7",
    ]
    cases = list(plan.iter_cases())
    assert len(cases) == 24000
    assert len({case.run_id for case in cases}) == 24000


def test_ten_repetition_plan_has_48000_runs():
    plan = load_plan("experiments/preferred-10-repetitions.yaml")
    assert plan.runs == 48000
    assert plan.nominal_events == 13_332_000_000


def test_plan_and_result_reports_are_separate_and_complete(tmp_path: Path):
    plan = load_plan("experiments/smoke.yaml")
    write_plan_artifacts(plan, tmp_path)
    assert (tmp_path / "matrix-plan.json").exists()
    assert (tmp_path / "matrix-plan.csv").exists()
    assert "Configurations" in (tmp_path / "matrix-plan.html").read_text()

    first = next(plan.iter_cases())
    result = {
        "status": "PASS",
        "run_id": first.run_id,
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "case": first.to_dict(),
        "metrics": {
            "gateway_ingest_events_per_second": 123.4,
            "finalization_latency_ms_p50": 2.0,
            "finalization_latency_ms_p95": 4.0,
            "recovery_after_gateway_start_seconds": 1.2,
            "wire_bytes_per_event": 520.0,
            "signing_energy_estimate_microjoules_per_event": 50.0,
            "storage_bytes_per_event": 800.0,
            "network_errors": 0,
            "application_packet_drops": 0,
            "events_delivered": first.events,
            "elapsed_seconds": 1.0,
            "blocks": 7,
            "ledger_valid": True,
            "quick_check": "ok",
            "gateway_resources": {"memory_peak_bytes": 1000, "cpu_percent_peak": 10},
        },
    }
    summary = write_result_artifacts(plan, [result], tmp_path)
    assert summary["coverage"]["completed_runs"] == 1
    for name in (
        "results.csv",
        "results.json",
        "results.jsonl",
        "summary.json",
        "report.html",
        "by_devices.csv",
        "by_events.csv",
        "by_block_size.csv",
        "by_authority_threshold.csv",
        "by_packet_loss.csv",
        "by_outage_duration.csv",
    ):
        assert (tmp_path / name).exists(), name


def test_gateway_auto_finalizes_multi_authority_quorum(tmp_path: Path):
    app = create_app(
        database_path=str(tmp_path / "multi.db"),
        node_key_path=str(tmp_path / "gateway.key"),
        authority_key_dir=str(tmp_path / "authorities"),
        node_id="gateway-a",
        quorum_threshold=2,
        authority_count=3,
        batch_size=1,
    )
    device_key = KeyPair.generate()
    device = DeviceClient("device-a", device_key)
    with TestClient(app) as client:
        enrolled = client.post(
            "/devices",
            json={"device_id": "device-a", "public_key": device_key.public_bytes.hex()},
        )
        assert enrolled.status_code == 201
        response = client.post("/events", json=device.create_event("test", {"value": 1}).to_wire())
        assert response.status_code == 202
        block = response.json()["block"]
        assert block["status"] == "finalized"
        assert block["required_signatures"] == 2
        assert block["signatures"] == 2
        assert block["finalization_latency_ms"] is not None
        health = client.get("/health").json()
        assert health["authority_count"] == 3
        assert health["quorum_threshold"] == 2


def test_experiment_worker_delivers_scalable_outbox(tmp_path: Path):
    server, thread, base_url = start_local_server(tmp_path / "server", batch_size=8)
    try:
        result = run_worker(
            device_id="matrix-worker-001",
            gateway_url=base_url,
            events=25,
            state_dir=tmp_path / "worker",
            packet_loss_percent=0,
            packet_loss_mode="application",
            request_timeout=3,
            reconnect_deadline_seconds=30,
            generation_batch=5,
            max_latency_samples=100,
            cpu_watts=10,
            random_seed=7,
        )
        assert result["status"] == "PASS"
        assert result["events_delivered"] == 25
        assert result["outbox_remaining"] == 0
        assert result["wire_bytes_per_event"] > 0
        assert result["signing_cpu_ns_per_event"] > 0
        assert json.loads((tmp_path / "worker" / "metrics.json").read_text())["events_delivered"] == 25
    finally:
        stop_local_server(server, thread)
