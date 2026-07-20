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
    assert "127.0.0.1:3030:8000" in services["gateway"]["ports"]
    for name in ["gateway", "run", "test", "device-01", "device-20"]:
        logging = services[name]["logging"]
        assert logging["driver"] == "local"
        assert logging["options"]["max-size"] == "10m"
        assert logging["options"]["max-file"] == "5"
