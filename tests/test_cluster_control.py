from edgechaindb.cluster_control import DockerClusterController


def test_cluster_control_can_be_disabled_without_touching_docker(monkeypatch):
    monkeypatch.setenv("EDGECHAIN_CLUSTER_CONTROL_ENABLED", "0")
    controller = DockerClusterController()
    assert controller.enabled is False
    assert controller.available is False
    state = controller.state()
    assert state["enabled"] is False
    assert "disabled" in state["error"].lower()
