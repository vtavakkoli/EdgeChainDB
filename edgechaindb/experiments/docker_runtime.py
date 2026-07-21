from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import socket
import statistics
import tarfile
import threading
import time
from typing import Any

import docker
from docker.errors import DockerException, NotFound
import httpx

from ..observability import get_logger
from .model import ExecutionSettings, ExperimentCase

log = get_logger("experiment-docker-runtime")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 3)


def _read_archive_json(container: Any, path: str) -> dict[str, Any]:
    stream, _ = container.get_archive(path)
    raw = b"".join(stream)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        member = next((item for item in archive.getmembers() if item.isfile()), None)
        if member is None:
            raise RuntimeError(f"archive {path} contained no file")
        handle = archive.extractfile(member)
        if handle is None:
            raise RuntimeError(f"could not extract {path}")
        return json.loads(handle.read().decode("utf-8"))


def _container_logs(container: Any, *, tail: int = 5000) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True, timestamps=True, tail=tail)
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"log collection failed: {type(exc).__name__}: {exc}\n"


class GatewayResourceSampler:
    def __init__(self, container: Any, interval_seconds: float = 2.0) -> None:
        self.container = container
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="gateway-resource-sampler")
        self.memory_peak_bytes = 0
        self.cpu_percent_peak = 0.0
        self.samples = 0
        self.network_rx_bytes = 0
        self.network_tx_bytes = 0

    @staticmethod
    def _cpu_percent(stats: dict[str, Any]) -> float:
        cpu = stats.get("cpu_stats", {})
        previous = stats.get("precpu_stats", {})
        cpu_delta = float(cpu.get("cpu_usage", {}).get("total_usage", 0)) - float(previous.get("cpu_usage", {}).get("total_usage", 0))
        system_delta = float(cpu.get("system_cpu_usage", 0)) - float(previous.get("system_cpu_usage", 0))
        online = float(cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", [])) or 1)
        return max(0.0, cpu_delta / system_delta * online * 100.0) if system_delta > 0 and cpu_delta >= 0 else 0.0

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                stats = self.container.stats(stream=False)
                self.memory_peak_bytes = max(self.memory_peak_bytes, int(stats.get("memory_stats", {}).get("usage", 0)))
                self.cpu_percent_peak = max(self.cpu_percent_peak, self._cpu_percent(stats))
                networks = stats.get("networks", {}) or {}
                self.network_rx_bytes = max(self.network_rx_bytes, sum(int(item.get("rx_bytes", 0)) for item in networks.values()))
                self.network_tx_bytes = max(self.network_tx_bytes, sum(int(item.get("tx_bytes", 0)) for item in networks.values()))
                self.samples += 1
            except Exception:
                return

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        self.thread.join(timeout=5)
        return {
            "samples": self.samples,
            "memory_peak_bytes": self.memory_peak_bytes,
            "cpu_percent_peak": round(self.cpu_percent_peak, 3),
            "network_rx_bytes": self.network_rx_bytes,
            "network_tx_bytes": self.network_tx_bytes,
        }


class DynamicDockerExperiment:
    def __init__(self, settings: ExecutionSettings, result_root: Path) -> None:
        self.settings = settings
        self.result_root = result_root
        self.client = docker.from_env()
        self.client.ping()
        self.runner_container = self._find_runner_container()

    def _find_runner_container(self) -> Any | None:
        candidates = [os.getenv("HOSTNAME"), socket.gethostname()]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return self.client.containers.get(candidate)
            except NotFound:
                continue
        return None

    def _wait_http(self, base_url: str, path: str = "/health", timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout or self.settings.gateway_start_timeout_seconds)
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=base_url, timeout=5) as client:
                    response = client.get(path)
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)
        raise TimeoutError(f"gateway did not become ready at {base_url}{path}: {last_error}")

    @staticmethod
    def _events_per_device(total: int, devices: int) -> list[int]:
        base, remainder = divmod(total, devices)
        return [base + (1 if index < remainder else 0) for index in range(devices)]

    def run(self, case: ExperimentCase, run_dir: Path) -> dict[str, Any]:
        run_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"ecx-{case.case_key[:10]}"
        network_name = f"{prefix}-net"
        gateway_name = f"{prefix}-gateway"
        volume_name = f"{prefix}-gateway-data"
        network = None
        volume = None
        gateway = None
        workers: list[Any] = []
        attached_runner = False
        sampler: GatewayResourceSampler | None = None
        success = False
        started_at = _utc_now()
        wall_started = time.perf_counter()
        outage_started_at: float | None = None
        gateway_restarted_at: float | None = None
        base_url: str | None = None
        result: dict[str, Any]

        (run_dir / "config.json").write_text(json.dumps(case.to_dict(), indent=2), encoding="utf-8")
        # ``ExperimentCase.to_dict()`` already includes ``run_id``. Passing it
        # both explicitly and through ``**`` raises before the logger is called,
        # aborting the entire campaign on its first case. Keep one canonical
        # structured payload so every case field is logged exactly once.
        log.info("matrix_run_provisioning", **case.to_dict())

        try:
            network = self.client.networks.create(network_name, driver="bridge", internal=True, labels={"edgechaindb.experiment": case.run_id})
            volume = self.client.volumes.create(name=volume_name, labels={"edgechaindb.experiment": case.run_id})
            if self.runner_container is not None:
                network.connect(self.runner_container)
                attached_runner = True

            ports = None if self.runner_container is not None else {"8000/tcp": ("127.0.0.1", None)}
            gateway_command = [
                "python", "-m", "edgechaindb.gateway_server",
                "--database", "/data/edgechain.db",
                "--node-key", "/data/gateway.key",
                "--node-id", f"gateway-{case.case_key[:8]}",
                "--quorum", str(case.threshold),
                "--authorities", str(case.authorities),
                "--authority-key-dir", "/data/authorities",
                "--batch-size", str(case.block_size),
                "--host", "0.0.0.0",
                "--api-port", "8000",
                "--monitor-port", "3030",
            ]
            gateway = self.client.containers.run(
                self.settings.image,
                gateway_command,
                name=gateway_name,
                detach=True,
                network=network.name,
                hostname="gateway",
                volumes={volume.name: {"bind": "/data", "mode": "rw"}},
                ports=ports,
                read_only=True,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                labels={"edgechaindb.experiment": case.run_id, "edgechaindb.role": "gateway"},
                environment={"EDGECHAIN_LOG_LEVEL": "WARNING"},
            )
            if self.runner_container is not None:
                base_url = f"http://{gateway_name}:8000"
            else:
                gateway.reload()
                bindings = gateway.attrs["NetworkSettings"]["Ports"]["8000/tcp"]
                if not bindings:
                    raise RuntimeError("Docker did not publish a gateway port")
                base_url = f"http://127.0.0.1:{bindings[0]['HostPort']}"

            initial_health = self._wait_http(base_url)
            sampler = GatewayResourceSampler(gateway)
            sampler.start()

            if case.outage_seconds > 0:
                gateway.stop(timeout=10)
                outage_started_at = time.perf_counter()
                log.info("matrix_gateway_outage_started", run_id=case.run_id, outage_seconds=case.outage_seconds)

            distribution = self._events_per_device(case.events, case.devices)
            reconnect_deadline = case.outage_seconds + self.settings.reconnect_grace_seconds
            for index, event_count in enumerate(distribution, start=1):
                worker_name = f"{prefix}-device-{index:03d}"
                cap_add = ["NET_ADMIN"] if self.settings.packet_loss_mode == "netem" and case.packet_loss_percent > 0 else None
                worker = self.client.containers.run(
                    self.settings.image,
                    ["python", "-m", "edgechaindb.experiments.worker"],
                    name=worker_name,
                    detach=True,
                    network=network.name,
                    cap_drop=["ALL"],
                    cap_add=cap_add,
                    security_opt=["no-new-privileges:true"],
                    labels={"edgechaindb.experiment": case.run_id, "edgechaindb.role": "device", "edgechaindb.device_index": str(index)},
                    environment={
                        "DEVICE_ID": f"matrix-{case.case_key[:8]}-{index:03d}",
                        "GATEWAY_URL": f"http://{gateway_name}:8000",
                        "DEVICE_EVENTS": str(event_count),
                        "DEVICE_STATE_DIR": "/data",
                        "DEVICE_PACKET_LOSS_PERCENT": str(case.packet_loss_percent),
                        "DEVICE_PACKET_LOSS_MODE": self.settings.packet_loss_mode,
                        "DEVICE_REQUEST_TIMEOUT": "5",
                        "DEVICE_RECONNECT_DEADLINE_SECONDS": str(reconnect_deadline),
                        "DEVICE_GENERATION_BATCH": str(self.settings.worker_generation_batch),
                        "DEVICE_MAX_LATENCY_SAMPLES": str(self.settings.max_latency_samples_per_device),
                        "DEVICE_RANDOM_SEED": str(int(case.case_key[:8], 16) + index),
                        "EDGECHAIN_LOG_LEVEL": "WARNING",
                    },
                )
                workers.append(worker)

            workers_started_at = time.perf_counter()
            if case.outage_seconds > 0:
                remaining = case.outage_seconds - (time.perf_counter() - (outage_started_at or time.perf_counter()))
                if remaining > 0:
                    time.sleep(remaining)
                gateway.start()
                gateway_restarted_at = time.perf_counter()
                self._wait_http(base_url)
                log.info("matrix_gateway_outage_ended", run_id=case.run_id)

            deadline = time.monotonic() + self.settings.run_timeout_seconds
            active = set(worker.id for worker in workers)
            while active and time.monotonic() < deadline:
                for worker in workers:
                    if worker.id not in active:
                        continue
                    worker.reload()
                    if worker.status in {"exited", "dead"}:
                        active.remove(worker.id)
                if active:
                    time.sleep(1)
            if active:
                raise TimeoutError(f"{len(active)} device workers exceeded {self.settings.run_timeout_seconds}s")

            workers_completed_at = time.perf_counter()
            worker_metrics: list[dict[str, Any]] = []
            failed_workers: list[dict[str, Any]] = []
            for worker in workers:
                worker.reload()
                status_code = int(worker.attrs.get("State", {}).get("ExitCode", 1))
                try:
                    metrics = _read_archive_json(worker, "/data/metrics.json")
                except Exception as exc:
                    metrics = {"status": "FAIL", "error": f"metrics unavailable: {type(exc).__name__}: {exc}"}
                metrics["container"] = worker.name
                metrics["exit_code"] = status_code
                worker_metrics.append(metrics)
                if status_code != 0 or metrics.get("status") != "PASS":
                    failed_workers.append(metrics)
            if failed_workers:
                raise RuntimeError(f"{len(failed_workers)} workers failed: {failed_workers[:3]}")

            # Seal any partial final block.
            with httpx.Client(base_url=base_url, timeout=60) as client:
                while True:
                    stats = client.get("/stats").json()
                    pending = int(stats.get("pending_events", 0))
                    if pending <= 0:
                        break
                    response = client.post("/blocks/seal", params={"max_events": min(case.block_size, pending)})
                    response.raise_for_status()
                verify = client.get("/verify", timeout=max(60.0, min(600.0, case.events / 1000))).json()
                blocks = client.get("/blocks").json()
                database_info = client.get("/database/info", params={"quick_check": "true"}).json()
                final_health = client.get("/health").json()

            elapsed = workers_completed_at - workers_started_at
            active_delivery_elapsed = workers_completed_at - (gateway_restarted_at or workers_started_at)
            finalization_values = [float(item["finalization_latency_ms"]) for item in blocks if item.get("finalization_latency_ms") is not None]
            delivered = sum(int(item.get("events_delivered", 0)) for item in worker_metrics)
            wire_bytes = sum(int(item.get("wire_bytes_total", 0)) for item in worker_metrics)
            signing_energy = sum(float(item.get("signing_energy_estimate_joules", 0)) for item in worker_metrics)
            network_errors = sum(int(item.get("network_errors", 0)) for item in worker_metrics)
            packet_drops = sum(int(item.get("simulated_packet_drops", 0)) for item in worker_metrics)
            latency_p50_values = [float(item["latency"]["p50_ms"]) for item in worker_metrics if item.get("latency", {}).get("p50_ms") is not None]
            latency_p95_values = [float(item["latency"]["p95_ms"]) for item in worker_metrics if item.get("latency", {}).get("p95_ms") is not None]
            storage_bytes = int(database_info.get("database_bytes", 0)) + int(database_info.get("wal_bytes", 0))
            resource_metrics = sampler.stop() if sampler is not None else {}
            sampler = None
            success = bool(verify.get("valid")) and delivered == case.events and int(final_health.get("events", 0)) == case.events
            recovery_seconds = (workers_completed_at - gateway_restarted_at) if gateway_restarted_at is not None else 0.0
            result = {
                "status": "PASS" if success else "FAIL",
                "run_id": case.run_id,
                "started_at": started_at,
                "completed_at": _utc_now(),
                "case": case.to_dict(),
                "metrics": {
                    "events_delivered": delivered,
                    "devices_completed": len(worker_metrics),
                    "elapsed_seconds": round(elapsed, 6),
                    "gateway_ingest_events_per_second": round(delivered / active_delivery_elapsed, 3) if active_delivery_elapsed else None,
                    "end_to_end_events_per_second": round(delivered / elapsed, 3) if elapsed else None,
                    "active_delivery_seconds": round(active_delivery_elapsed, 6),
                    "recovery_after_gateway_start_seconds": round(recovery_seconds, 6),
                    "configured_outage_seconds": case.outage_seconds,
                    "effective_outage_seconds": round((gateway_restarted_at - outage_started_at), 6) if gateway_restarted_at is not None and outage_started_at is not None else 0,
                    "wire_bytes_total": wire_bytes,
                    "wire_bytes_per_event": round(wire_bytes / delivered, 3) if delivered else None,
                    "signing_energy_estimate_joules": round(signing_energy, 9),
                    "signing_energy_estimate_microjoules_per_event": round(signing_energy * 1_000_000 / delivered, 6) if delivered else None,
                    "storage_bytes": storage_bytes,
                    "storage_bytes_per_event": round(storage_bytes / delivered, 3) if delivered else None,
                    "blocks": len(blocks),
                    "finalization_latency_ms_p50": _percentile(finalization_values, 0.50),
                    "finalization_latency_ms_p95": _percentile(finalization_values, 0.95),
                    "finalization_latency_ms_p99": _percentile(finalization_values, 0.99),
                    "device_request_latency_ms_p50_median": round(statistics.median(latency_p50_values), 3) if latency_p50_values else None,
                    "device_request_latency_ms_p95_max": round(max(latency_p95_values), 3) if latency_p95_values else None,
                    "network_errors": network_errors,
                    "application_packet_drops": packet_drops,
                    "ledger_valid": bool(verify.get("valid")),
                    "quick_check": database_info.get("quick_check"),
                    "gateway_resources": resource_metrics,
                },
                "gateway": {
                    "initial_health": initial_health,
                    "final_health": final_health,
                    "database": database_info,
                    "verification": verify,
                },
                "workers": worker_metrics,
                "notes": [
                    "Packet loss uses Linux tc netem when available; each worker records an explicit application-level fallback if NET_ADMIN or tc is unavailable.",
                    "Signing energy in worker metrics is a CPU-time estimate; hardware-energy claims require the dedicated RAPL benchmark on supported hardware.",
                ],
            }
            if not success:
                result["error"] = "completeness or ledger verification failed"
                raise RuntimeError(result["error"])
        except Exception as exc:
            resource_metrics = sampler.stop() if sampler is not None else {}
            sampler = None
            result = {
                "status": "FAIL",
                "run_id": case.run_id,
                "started_at": started_at,
                "completed_at": _utc_now(),
                "case": case.to_dict(),
                "error": f"{type(exc).__name__}: {exc}",
                "metrics": {
                    "wall_seconds_before_failure": round(time.perf_counter() - wall_started, 6),
                    "gateway_resources": resource_metrics,
                },
            }
            log.error("matrix_run_failed", run_id=case.run_id, error=result["error"], exc_info=True)
        finally:
            if gateway is not None and (not success or self.settings.collect_success_logs):
                (run_dir / "gateway.log").write_text(_container_logs(gateway), encoding="utf-8")
            if workers and (not success or self.settings.collect_success_logs):
                logs_dir = run_dir / "devices"
                logs_dir.mkdir(exist_ok=True)
                for worker in workers:
                    (logs_dir / f"{worker.name}.log").write_text(_container_logs(worker, tail=2000), encoding="utf-8")
            (run_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

            keep = (not success and self.settings.retain_failed_containers) or (success and not self.settings.cleanup_successful_runs)
            if not keep:
                for worker in workers:
                    try:
                        worker.remove(force=True, v=True)
                    except Exception:
                        pass
                if gateway is not None:
                    try:
                        gateway.remove(force=True, v=True)
                    except Exception:
                        pass
                if attached_runner and network is not None and self.runner_container is not None:
                    try:
                        network.disconnect(self.runner_container, force=True)
                    except Exception:
                        pass
                if network is not None:
                    try:
                        network.remove()
                    except Exception:
                        pass
                if volume is not None:
                    try:
                        volume.remove(force=True)
                    except Exception:
                        pass
            log.info("matrix_run_finished", run_id=case.run_id, status=result.get("status"))
        return result
