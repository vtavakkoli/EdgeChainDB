from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any, Iterable

try:
    import docker
    from docker.errors import DockerException, NotFound
except Exception:  # pragma: no cover - optional at import time
    docker = None
    DockerException = Exception
    NotFound = Exception


ALLOWED_ACTIONS = {"start", "stop", "restart", "pause", "unpause"}


@dataclass
class ClusterControlResult:
    service: str
    action: str
    ok: bool
    state: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "action": self.action,
            "ok": self.ok,
            "state": self.state,
            "error": self.error,
        }


class DockerClusterController:
    """Small Docker control-plane adapter for the local Compose research cluster.

    Access to the Docker socket is deliberately optional. The API/dashboard can
    still expose ledger state when the socket is not mounted. In production,
    replace this privileged development adapter with an authenticated external
    orchestrator.
    """

    def __init__(self, project_name: str | None = None) -> None:
        self.project_name = project_name or os.getenv(
            "EDGECHAIN_COMPOSE_PROJECT", "edgechaindb"
        )
        self.client = None
        self.error: str | None = None
        if docker is None:
            self.error = "Python Docker SDK is unavailable"
            return
        try:
            self.client = docker.from_env(timeout=5)
            self.client.ping()
        except Exception as exc:
            self.client = None
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self.client is not None

    def _containers(self, *, services: Iterable[str] | None = None) -> list[Any]:
        if self.client is None:
            return []
        filters = {
            "label": [f"com.docker.compose.project={self.project_name}"],
        }
        containers = self.client.containers.list(all=True, filters=filters)
        wanted = set(services or [])
        if wanted:
            containers = [
                container
                for container in containers
                if container.labels.get("com.docker.compose.service") in wanted
            ]
        return containers

    @staticmethod
    def _container_state(container: Any) -> dict[str, Any]:
        container.reload()
        attrs = container.attrs
        state = attrs.get("State", {})
        service = container.labels.get("com.docker.compose.service", container.name)
        health = state.get("Health", {}).get("Status")
        return {
            "service": service,
            "container_id": container.short_id,
            "container_name": container.name,
            "state": state.get("Status", container.status),
            "running": bool(state.get("Running")),
            "paused": bool(state.get("Paused")),
            "restarting": bool(state.get("Restarting")),
            "exit_code": state.get("ExitCode"),
            "health": health,
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "image": attrs.get("Config", {}).get("Image"),
        }

    def state(self) -> dict[str, Any]:
        if self.client is None:
            return {
                "available": False,
                "project": self.project_name,
                "error": self.error,
                "containers": [],
            }
        try:
            containers = sorted(
                (self._container_state(c) for c in self._containers()),
                key=lambda item: item["service"],
            )
            return {
                "available": True,
                "project": self.project_name,
                "error": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "containers": containers,
            }
        except Exception as exc:
            return {
                "available": False,
                "project": self.project_name,
                "error": f"{type(exc).__name__}: {exc}",
                "containers": [],
            }

    def device_services(self) -> list[str]:
        state = self.state()
        return sorted(
            item["service"]
            for item in state["containers"]
            if item["service"].startswith("device-")
        )

    def control(self, service: str, action: str) -> ClusterControlResult:
        if action not in ALLOWED_ACTIONS:
            return ClusterControlResult(
                service, action, False, error=f"unsupported action: {action}"
            )
        if not service.startswith("device-"):
            return ClusterControlResult(
                service, action, False, error="only device services may be controlled"
            )
        if self.client is None:
            return ClusterControlResult(service, action, False, error=self.error)
        try:
            containers = self._containers(services=[service])
            if not containers:
                return ClusterControlResult(
                    service, action, False, error="container not found"
                )
            container = containers[0]
            if action == "start":
                container.start()
            elif action == "stop":
                container.stop(timeout=5)
            elif action == "restart":
                container.restart(timeout=5)
            elif action == "pause":
                container.pause()
            elif action == "unpause":
                container.unpause()
            container.reload()
            return ClusterControlResult(
                service, action, True, state=container.attrs.get("State", {}).get("Status")
            )
        except Exception as exc:
            return ClusterControlResult(
                service, action, False, error=f"{type(exc).__name__}: {exc}"
            )

    def control_all(self, action: str) -> list[dict[str, Any]]:
        services = self.device_services()
        return [self.control(service, action).to_dict() for service in services]

    def container_for_service(self, service: str) -> Any:
        if self.client is None:
            raise RuntimeError(self.error or "Docker control is unavailable")
        containers = self._containers(services=[service])
        if not containers:
            raise LookupError(f"container not found for service {service}")
        return containers[0]
