from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
from typing import Any

import httpx

from .cluster_control import DockerClusterController
from .recovery_report import append_recovery_result
from .system_test import run_suite


def _write_status(path: Path, status: str, **extra: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _clean_result_dir(result_dir: Path) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    for child in result_dir.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _wait_for_devices(
    base_url: str,
    expected_devices: int,
    events_per_device: int,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            with httpx.Client(base_url=base_url, timeout=5) as client:
                response = client.get("/devices")
                response.raise_for_status()
                devices = [
                    item
                    for item in response.json()
                    if item["device_id"].startswith("iot-device-")
                ]
                last = devices
                ready = [
                    item
                    for item in devices
                    if int(item["last_sequence"]) >= events_per_device
                ]
                if len(ready) >= expected_devices:
                    return {
                        "devices": len(devices),
                        "minimum_sequence": min(
                            int(item["last_sequence"]) for item in ready
                        ),
                    }
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise TimeoutError(
        f"timed out waiting for {expected_devices} devices with at least "
        f"{events_per_device} events; last state: {last}"
    )


def _collect_logs(controller: DockerClusterController, result_dir: Path) -> None:
    if not controller.available:
        return
    sections: list[str] = []
    state = controller.state()
    for item in state.get("containers", []):
        service = item["service"]
        try:
            container = controller.container_for_service(service)
            raw = container.logs(tail=2000, timestamps=True)
            text = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            text = f"log collection failed: {type(exc).__name__}: {exc}"
        sections.append(f"===== {service} =====\n{text}\n")
    (result_dir / "docker-compose.log").write_text(
        "\n".join(sections), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the complete Docker benchmark")
    parser.add_argument("--base-url", default="http://gateway:8000")
    parser.add_argument("--expected-devices", type=int, default=20)
    parser.add_argument("--events-per-device", type=int, default=8)
    parser.add_argument("--result-dir", default="/app/result")
    parser.add_argument("--compose-file", default="/app/docker-compose.yml")
    parser.add_argument("--project-root", default="/app")
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    _clean_result_dir(result_dir)
    status_path = result_dir / "benchmark-status.json"
    controller = DockerClusterController()
    stopped_services: list[str] = []
    exit_code = 1

    try:
        _write_status(status_path, "waiting_for_cluster")
        if not controller.available:
            raise RuntimeError(
                "Docker socket control is unavailable in the test container: "
                + str(controller.error)
            )

        readiness = _wait_for_devices(
            args.base_url,
            args.expected_devices,
            args.events_per_device,
            args.startup_timeout,
        )
        _write_status(status_path, "quiescing_devices", readiness=readiness)

        stop_results = controller.control_all("stop")
        failed_stops = [item for item in stop_results if not item["ok"]]
        if failed_stops:
            raise RuntimeError(f"failed to stop devices: {failed_stops}")
        stopped_services = [item["service"] for item in stop_results]

        _write_status(
            status_path,
            "running_benchmark",
            stopped_devices=len(stopped_services),
        )
        suite_args = SimpleNamespace(
            mode="remote",
            base_url=args.base_url,
            expected_devices=args.expected_devices,
            events_per_device=args.events_per_device,
            batch_size=25,
            result_dir=str(result_dir),
            compose_file=args.compose_file,
            project_root=args.project_root,
            skip_pytest=False,
        )
        exit_code = run_suite(suite_args)

        _write_status(status_path, "restarting_gateway", suite_exit_code=exit_code)
        gateway = controller.container_for_service("gateway")
        gateway.restart(timeout=10)
        recovery_ok = append_recovery_result(
            base_url=args.base_url,
            result_dir=result_dir,
            timeout=45,
        )
        if not recovery_ok:
            exit_code = 1

        _write_status(
            status_path,
            "completed" if exit_code == 0 else "failed",
            suite_exit_code=exit_code,
            recovery_ok=recovery_ok,
            report="report.html",
        )
    except Exception as exc:
        _write_status(
            status_path,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            report_exists=(result_dir / "report.html").exists(),
        )
        if not (result_dir / "report.html").exists():
            # A minimal report is still useful when the cluster fails before the
            # full suite can begin.
            payload = {
                "title": "EdgeChainDB Docker Benchmark Failure",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "environment": {"mode": "remote", "base_url": args.base_url},
                "summary": {"passed": 0, "failed": 1, "skipped": 0, "total": 1},
                "scenarios": [
                    {
                        "name": "Benchmark bootstrap",
                        "category": "Deployment",
                        "status": "FAIL",
                        "duration_ms": 0,
                        "details": f"{type(exc).__name__}: {exc}",
                        "metrics": {},
                    }
                ],
                "notes": [],
                "extra": {"status": "failed"},
            }
            from .system_test import render_html

            (result_dir / "result.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            (result_dir / "report.html").write_text(
                render_html(payload), encoding="utf-8"
            )
        exit_code = 1
    finally:
        if stopped_services:
            for service in stopped_services:
                controller.control(service, "start")
        _collect_logs(controller, result_dir)

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
