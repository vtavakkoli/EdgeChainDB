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


def test_reduced_event_full_matrix_has_required_24000_runs():
    plan = load_plan("experiments/full-matrix.yaml")
    assert plan.configurations == 4800
    assert plan.runs == 24000
    assert plan.nominal_events == 66_660_000
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
    assert plan.nominal_events == 133_320_000


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



def test_one_day_balanced_screening_plan_covers_every_level_exactly():
    from collections import Counter

    plan = load_plan("experiments/one-day.yaml")
    assert plan.design.type == "balanced_screening"
    assert plan.configurations == 60
    assert plan.runs == 180
    assert plan.nominal_events == 499_950
    assert plan.nominal_outage_seconds == 20_100
    rows = plan.selected_configurations
    assert len(rows) == len(set(rows)) == 60
    assert Counter(row[0] for row in rows) == {1: 12, 5: 12, 20: 12, 50: 12, 100: 12}
    assert Counter(row[1] for row in rows) == {10: 15, 100: 15, 1000: 15, 10000: 15}
    assert Counter(row[2] for row in rows) == {1: 12, 16: 12, 64: 12, 256: 12, 1024: 12}
    assert Counter(row[3].label for row in rows) == {
        "1-of-1": 15, "2-of-3": 15, "3-of-4": 15, "5-of-7": 15
    }
    assert Counter(row[4] for row in rows) == {0.0: 15, 1.0: 15, 5.0: 15, 10.0: 15}
    assert Counter(row[5] for row in rows) == {5: 20, 30: 20, 300: 20}


def test_result_reporting_ignores_rows_from_another_plan(tmp_path: Path):
    plan = load_plan("experiments/one-day.yaml")
    current = next(plan.iter_cases())
    stale = {
        "run_id": "old-unrelated-run",
        "status": "PASS",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "case": {},
        "metrics": {},
    }
    current_result = {
        "run_id": current.run_id,
        "status": "PASS",
        "completed_at": "2026-01-01T00:00:02+00:00",
        "case": current.to_dict(),
        "metrics": {},
    }
    summary = write_result_artifacts(plan, [stale, current_result], tmp_path)
    assert summary["coverage"]["completed_runs"] == 1
    ignored = json.loads((tmp_path / "ignored-results-from-other-plans.json").read_text())
    assert ignored[0]["run_id"] == "old-unrelated-run"

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


def test_dynamic_runtime_logs_case_without_duplicate_run_id(tmp_path, monkeypatch):
    """Regression: campaign must reach Docker provisioning for the first case."""
    from edgechaindb.experiments import docker_runtime
    from edgechaindb.experiments.docker_runtime import DynamicDockerExperiment
    from edgechaindb.experiments.model import ExecutionSettings, ExperimentCase

    case = ExperimentCase(
        devices=1,
        events=1000,
        block_size=1,
        authorities=1,
        threshold=1,
        packet_loss_percent=0,
        outage_seconds=5,
        repetition=1,
    )
    captured = []

    def capture(event, **fields):
        captured.append((event, fields))

    class EmptyCollection:
        def list(self, **kwargs):
            return []

    class FailingNetworks(EmptyCollection):
        def create(self, *args, **kwargs):
            raise RuntimeError("docker provisioning reached")

    class FakeClient:
        containers = EmptyCollection()
        networks = FailingNetworks()
        volumes = EmptyCollection()

    runtime = object.__new__(DynamicDockerExperiment)
    runtime.settings = ExecutionSettings()
    runtime.result_root = tmp_path
    runtime.client = FakeClient()
    runtime.runner_container = None
    monkeypatch.setattr(docker_runtime.log, "info", capture)

    result = runtime.run(case, tmp_path / case.run_id)

    provisioning = next(item for item in captured if item[0] == "matrix_run_provisioning")
    assert provisioning[1]["run_id"] == case.run_id
    assert list(provisioning[1]).count("run_id") == 1
    assert result["status"] == "FAIL"
    assert result["error"] == "RuntimeError: docker provisioning reached"


def test_dynamic_gateway_disables_nested_docker_control():
    from edgechaindb.experiments.docker_runtime import DynamicDockerExperiment
    from edgechaindb.experiments.model import ExperimentCase

    case = ExperimentCase(
        devices=1, events=1000, block_size=1, authorities=1, threshold=1,
        packet_loss_percent=0, outage_seconds=5, repetition=1,
    )
    environment = DynamicDockerExperiment._gateway_environment(case)
    assert environment["EDGECHAIN_CLUSTER_CONTROL_ENABLED"] == "0"
    assert environment["EDGECHAIN_EXPERIMENT_RUN_ID"] == case.run_id


def test_resource_sampler_survives_expected_gateway_outage():
    import time
    from edgechaindb.experiments.docker_runtime import GatewayResourceSampler

    class FakeContainer:
        def __init__(self):
            self.calls = 0

        def stats(self, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("container temporarily stopped")
            return {
                "cpu_stats": {"cpu_usage": {"total_usage": 20}, "system_cpu_usage": 100, "online_cpus": 1},
                "precpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 50},
                "memory_stats": {"usage": 1024, "limit": 4096, "stats": {"inactive_file": 0}},
                "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}},
            }

    sampler = GatewayResourceSampler(FakeContainer(), interval_seconds=0.01)
    sampler.start()
    time.sleep(0.08)
    result = sampler.stop()
    assert result["sample_errors"] >= 1
    assert result["samples"] >= 1
    assert result["memory_peak_bytes"] == 1024


def test_dynamic_runtime_cleans_stale_resources_before_resume(monkeypatch):
    from edgechaindb.experiments.docker_runtime import DynamicDockerExperiment
    from edgechaindb.experiments.model import ExperimentCase

    removed = []

    class Resource:
        def __init__(self, kind):
            self.kind = kind
        def remove(self, **kwargs):
            removed.append(self.kind)
        def disconnect(self, *args, **kwargs):
            removed.append("disconnect")

    class Collection:
        def __init__(self, resources):
            self.resources = resources
        def list(self, **kwargs):
            assert kwargs["filters"]["label"].startswith("edgechaindb.experiment=")
            return self.resources

    class Client:
        containers = Collection([Resource("container")])
        networks = Collection([Resource("network")])
        volumes = Collection([Resource("volume")])

    case = ExperimentCase(
        devices=1, events=1000, block_size=1, authorities=1, threshold=1,
        packet_loss_percent=0, outage_seconds=5, repetition=1,
    )
    runtime = object.__new__(DynamicDockerExperiment)
    runtime.client = Client()
    runtime.runner_container = object()
    result = runtime._cleanup_stale_case_resources(case)
    assert result == {"containers": 1, "networks": 1, "volumes": 1}
    assert removed == ["container", "disconnect", "network", "volume"]
