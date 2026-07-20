from pathlib import Path

from edgechaindb.system_test import validate_compose


def test_compose_exposes_run_and_test_workflows():
    details, metrics = validate_compose(Path("docker-compose.yml"), 20)
    assert "run/test orchestration" in details
    assert metrics["device_services"] == 20
    assert metrics["unique_device_ids"] == 20
