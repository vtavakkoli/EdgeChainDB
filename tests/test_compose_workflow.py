from pathlib import Path

from edgechaindb.system_test import validate_compose


def test_compose_exposes_run_and_test_workflows():
    details, metrics = validate_compose(Path("docker-compose.yml"), 20)
    assert "run/test orchestration" in details
    assert metrics["device_services"] == 20
    assert metrics["unique_device_ids"] == 20


def test_gateway_can_write_existing_named_volume_after_capability_drop():
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    gateway = compose["services"]["gateway"]
    assert gateway["user"] == "0:0"
    assert "ALL" in gateway["cap_drop"]
    assert "DAC_OVERRIDE" in gateway["cap_add"]
    assert "gateway-data:/data" in gateway["volumes"]


def test_gateway_publishes_monitor_port_and_rotates_all_logs():
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = compose["services"]
    assert "127.0.0.1:3030:3030" in services["gateway"]["ports"]
    assert services["gateway"]["command"][:3] == ["python", "-m", "edgechaindb.gateway_server"]
    assert "--monitor-port" in services["gateway"]["command"]
    for name in ["gateway", "run", "test", "device-01", "device-20"]:
        logging = services[name]["logging"]
        assert logging["driver"] == "local"
        assert logging["options"]["max-size"] == "10m"
        assert logging["options"]["max-file"] == "5"


def test_compose_uses_module_entrypoints_and_versioned_image():
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = compose["services"]
    assert services["gateway"]["image"] == "edgechaindb:0.8.1"
    assert services["run"]["command"] == ["python", "-m", "edgechaindb.cluster_runtime"]
    assert services["test"]["command"][:3] == ["python", "-m", "edgechaindb.benchmark"]
    assert services["device-01"]["command"][:3] == ["python", "-m", "edgechaindb.device_node"]


def test_compose_has_dynamic_experiment_runner():
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    experiment = compose["services"]["experiment"]
    assert "experiment" in experiment["profiles"]
    assert experiment["image"] == "edgechaindb:0.8.1"
    assert "/var/run/docker.sock:/var/run/docker.sock" in experiment["volumes"]
    assert experiment["command"][:3] == ["python", "-m", "edgechaindb.experiments.runner"]
    assert "--dry-run" not in experiment["command"]
    assert "/app/experiments/full-matrix.yaml" in experiment["command"]
    assert "/result/experiments" in experiment["command"]
    assert "--resume" in experiment["command"]
    assert "--rerun-failed" in experiment["command"]
    assert "./result/experiments:/result/experiments" in experiment["volumes"]
    alias = compose["services"]["experment"]
    assert alias["command"] == experiment["command"]
