from pathlib import Path

from edgechaindb.experiments.model import load_plan
from edgechaindb.experiments.report import write_result_artifacts


def test_full_matrix_config_is_packaged_and_loadable():
    plan = load_plan(Path("experiments/full-matrix.yaml"))
    assert plan.runs == 24000
    assert plan.execution.image == "edgechaindb:0.8.2"


def test_empty_campaign_creates_comprehensive_report_immediately(tmp_path):
    plan = load_plan(Path("experiments/smoke.yaml"))
    summary = write_result_artifacts(plan, [], tmp_path)
    assert summary["coverage"]["completed_runs"] == 0
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "EdgeChainDB Experimental Matrix Report" in report
    assert "0/" in report
    for name in ("summary.json", "results.csv", "by_devices.csv", "by_outage_duration.csv"):
        assert (tmp_path / name).exists()


def test_dockerfile_copies_experiment_configs_into_image():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY experiments ./experiments" in dockerfile
    assert "--config /app/experiments/smoke.yaml" in dockerfile
