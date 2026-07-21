from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import threading
import time
from typing import Any, Iterable

from .observability import get_logger

try:
    import docker
except Exception:  # pragma: no cover - optional at import time
    docker = None


ALLOWED_ACTIONS = {"start", "stop", "restart", "pause", "unpause"}
ALLOWED_LOG_SERVICES = {"gateway", "run", "test"}
log = get_logger("cluster-control")


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
    """Docker control and observability adapter for the local Compose cluster.

    The Docker socket is optional. When it is absent the API still exposes
    ledger-derived telemetry, but container controls, logs, network details, and
    resource metrics are reported as unavailable.
    """

    def __init__(
        self,
        project_name: str | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.project_name = project_name or os.getenv(
            "EDGECHAIN_COMPOSE_PROJECT", "edgechaindb"
        )
        if enabled is None:
            raw_enabled = os.getenv("EDGECHAIN_CLUSTER_CONTROL_ENABLED", "1").strip().lower()
            enabled = raw_enabled not in {"0", "false", "no", "off", "disabled"}
        self.enabled = bool(enabled)
        self.client = None
        self.error: str | None = None
        self._metrics_cache: dict[str, Any] = {
            "created_monotonic": 0.0,
            "value": {},
        }
        self._metrics_lock = threading.Lock()
        if not self.enabled:
            self.error = "Docker cluster control disabled by configuration"
            log.info(
                "docker_control_disabled",
                project=self.project_name,
                reason=self.error,
            )
            return
        if docker is None:
            self.error = "Python Docker SDK is unavailable"
            log.warning("docker_sdk_unavailable", error=self.error)
            return
        try:
            self.client = docker.from_env(timeout=8)
            self.client.ping()
            log.info("docker_control_connected", project=self.project_name)
        except Exception as exc:
            self.client = None
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("docker_control_connection_failed", error=self.error)

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
        config = attrs.get("Config", {})
        host_config = attrs.get("HostConfig", {})
        service = container.labels.get("com.docker.compose.service", container.name)
        health = state.get("Health", {}).get("Status")
        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        network_items = [
            {
                "name": name,
                "ip_address": value.get("IPAddress"),
                "gateway": value.get("Gateway"),
                "mac_address": value.get("MacAddress"),
                "aliases": value.get("Aliases") or [],
            }
            for name, value in networks.items()
        ]
        return {
            "service": service,
            "container_id": container.short_id,
            "container_name": container.name,
            "state": state.get("Status", container.status),
            "running": bool(state.get("Running")),
            "paused": bool(state.get("Paused")),
            "restarting": bool(state.get("Restarting")),
            "oom_killed": bool(state.get("OOMKilled")),
            "exit_code": state.get("ExitCode"),
            "health": health,
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "restart_count": int(attrs.get("RestartCount", 0)),
            "image": config.get("Image"),
            "hostname": config.get("Hostname"),
            "read_only_rootfs": bool(host_config.get("ReadonlyRootfs")),
            "networks": network_items,
        }

    @staticmethod
    def _container_metrics(container: Any) -> dict[str, Any]:
        try:
            stats = container.stats(stream=False, one_shot=True)
            cpu = stats.get("cpu_stats", {})
            pre_cpu = stats.get("precpu_stats", {})
            cpu_total = cpu.get("cpu_usage", {}).get("total_usage", 0)
            pre_total = pre_cpu.get("cpu_usage", {}).get("total_usage", 0)
            system_total = cpu.get("system_cpu_usage", 0)
            pre_system = pre_cpu.get("system_cpu_usage", 0)
            online_cpus = cpu.get("online_cpus") or len(
                cpu.get("cpu_usage", {}).get("percpu_usage") or []
            ) or 1
            cpu_delta = max(cpu_total - pre_total, 0)
            system_delta = max(system_total - pre_system, 0)
            cpu_percent = (
                (cpu_delta / system_delta) * online_cpus * 100
                if system_delta > 0 and cpu_delta >= 0
                else 0.0
            )

            memory = stats.get("memory_stats", {})
            memory_usage = int(memory.get("usage", 0))
            cache = int(memory.get("stats", {}).get("inactive_file", 0))
            memory_working_set = max(memory_usage - cache, 0)
            memory_limit = int(memory.get("limit", 0))
            memory_percent = (
                memory_working_set / memory_limit * 100 if memory_limit else 0.0
            )

            rx_bytes = tx_bytes = rx_packets = tx_packets = 0
            for net in (stats.get("networks") or {}).values():
                rx_bytes += int(net.get("rx_bytes", 0))
                tx_bytes += int(net.get("tx_bytes", 0))
                rx_packets += int(net.get("rx_packets", 0))
                tx_packets += int(net.get("tx_packets", 0))

            return {
                "cpu_percent": round(cpu_percent, 3),
                "memory_bytes": memory_working_set,
                "memory_limit_bytes": memory_limit,
                "memory_percent": round(memory_percent, 3),
                "network_rx_bytes": rx_bytes,
                "network_tx_bytes": tx_bytes,
                "network_rx_packets": rx_packets,
                "network_tx_packets": tx_packets,
                "pids": int(stats.get("pids_stats", {}).get("current", 0)),
                "read_at": stats.get("read"),
                "metrics_error": None,
            }
        except Exception as exc:
            return {
                "cpu_percent": 0.0,
                "memory_bytes": 0,
                "memory_limit_bytes": 0,
                "memory_percent": 0.0,
                "network_rx_bytes": 0,
                "network_tx_bytes": 0,
                "network_rx_packets": 0,
                "network_tx_packets": 0,
                "pids": 0,
                "read_at": None,
                "metrics_error": f"{type(exc).__name__}: {exc}",
            }

    def _metrics(self, containers: list[Any], cache_seconds: float = 3.0) -> dict[str, Any]:
        with self._metrics_lock:
            now = time.monotonic()
            cached = self._metrics_cache
            if now - float(cached["created_monotonic"]) < cache_seconds:
                return dict(cached["value"])

            result: dict[str, Any] = {}
            running = [c for c in containers if c.status in {"running", "paused"}]
            if running:
                with ThreadPoolExecutor(max_workers=min(8, len(running))) as pool:
                    jobs = {pool.submit(self._container_metrics, c): c for c in running}
                    for future in as_completed(jobs):
                        container = jobs[future]
                        service = container.labels.get(
                            "com.docker.compose.service", container.name
                        )
                        try:
                            result[service] = future.result()
                        except Exception as exc:  # defensive around worker failures
                            result[service] = {
                                "metrics_error": f"{type(exc).__name__}: {exc}"
                            }
            self._metrics_cache = {
                "created_monotonic": now,
                "value": result,
            }
            return dict(result)

    def state(self, *, include_metrics: bool = False) -> dict[str, Any]:
        if self.client is None:
            return {
                "available": False,
                "enabled": self.enabled,
                "project": self.project_name,
                "error": self.error,
                "containers": [],
            }
        try:
            containers = self._containers()
            states = sorted(
                (self._container_state(c) for c in containers),
                key=lambda item: item["service"],
            )
            if include_metrics:
                metrics = self._metrics(containers)
                for item in states:
                    item["metrics"] = metrics.get(item["service"], {})
            return {
                "available": True,
                "enabled": self.enabled,
                "project": self.project_name,
                "error": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "containers": states,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.error("docker_state_failed", error=error)
            return {
                "available": False,
                "enabled": self.enabled,
                "project": self.project_name,
                "error": error,
                "containers": [],
            }

    def network_state(self) -> dict[str, Any]:
        if self.client is None:
            return {
                "available": False,
                "project": self.project_name,
                "error": self.error,
                "networks": [],
            }
        try:
            networks = self.client.networks.list(
                filters={
                    "label": [f"com.docker.compose.project={self.project_name}"]
                }
            )
            values: list[dict[str, Any]] = []
            for network in networks:
                network.reload()
                attrs = network.attrs
                attached = []
                for container_id, item in (attrs.get("Containers") or {}).items():
                    try:
                        container = self.client.containers.get(container_id)
                        service = container.labels.get(
                            "com.docker.compose.service", container.name
                        )
                    except Exception:
                        service = item.get("Name") or container_id[:12]
                    attached.append(
                        {
                            "service": service,
                            "name": item.get("Name"),
                            "ipv4_address": item.get("IPv4Address"),
                            "ipv6_address": item.get("IPv6Address"),
                            "mac_address": item.get("MacAddress"),
                        }
                    )
                values.append(
                    {
                        "id": network.short_id,
                        "name": attrs.get("Name", network.name),
                        "driver": attrs.get("Driver"),
                        "scope": attrs.get("Scope"),
                        "internal": bool(attrs.get("Internal")),
                        "attachable": bool(attrs.get("Attachable")),
                        "subnets": [
                            {
                                "subnet": cfg.get("Subnet"),
                                "gateway": cfg.get("Gateway"),
                            }
                            for cfg in attrs.get("IPAM", {}).get("Config", [])
                        ],
                        "connected_containers": len(attached),
                        "containers": sorted(
                            attached, key=lambda item: item["service"]
                        ),
                    }
                )
            return {
                "available": True,
                "project": self.project_name,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "networks": values,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.error("docker_network_state_failed", error=error)
            return {
                "available": False,
                "enabled": self.enabled,
                "project": self.project_name,
                "error": error,
                "networks": [],
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
            log.info("container_action_requested", service=service, action=action)
            if action == "start":
                container.start()
            elif action == "stop":
                container.stop(timeout=10)
            elif action == "restart":
                container.restart(timeout=10)
            elif action == "pause":
                container.pause()
            elif action == "unpause":
                container.unpause()
            container.reload()
            state = container.attrs.get("State", {}).get("Status")
            self._metrics_cache["created_monotonic"] = 0.0
            log.info(
                "container_action_completed",
                service=service,
                action=action,
                state=state,
            )
            return ClusterControlResult(service, action, True, state=state)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.error(
                "container_action_failed",
                service=service,
                action=action,
                error=error,
            )
            return ClusterControlResult(service, action, False, error=error)

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

    def logs(self, service: str, *, tail: int = 250) -> dict[str, Any]:
        if not (
            service.startswith("device-") or service in ALLOWED_LOG_SERVICES
        ):
            raise ValueError("unsupported service")
        if self.client is None:
            raise RuntimeError(self.error or "Docker control is unavailable")
        container = self.container_for_service(service)
        tail = min(max(int(tail), 1), 2000)
        raw = container.logs(tail=tail, timestamps=True, stdout=True, stderr=True)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return {
            "service": service,
            "container_id": container.short_id,
            "container_name": container.name,
            "tail": tail,
            "line_count": len(lines),
            "lines": lines,
            "text": text,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
