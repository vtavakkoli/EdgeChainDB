from __future__ import annotations

import os
import signal
import time

import httpx

from .observability import get_logger


log = get_logger("cluster-runtime")


def main() -> None:
    base_url = os.getenv("GATEWAY_URL", "http://gateway:8000")
    interval = float(os.getenv("CLUSTER_STATUS_INTERVAL", "30"))
    running = True
    started = time.perf_counter()

    def stop(signum: int, *_: object) -> None:
        nonlocal running
        running = False
        log.warning("shutdown_signal_received", signal=signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "cluster_runtime_started",
        gateway_url=base_url,
        status_interval_seconds=interval,
    )
    while running:
        try:
            with httpx.Client(base_url=base_url, timeout=10) as client:
                health = client.get("/health")
                health.raise_for_status()
                value = health.json()
                log.info(
                    "cluster_heartbeat",
                    status="running",
                    devices=value.get("devices"),
                    events=value.get("events"),
                    pending_events=value.get("pending_events"),
                    blocks=value.get("blocks"),
                    gateway_uptime_seconds=value.get("uptime_seconds"),
                )
        except Exception as exc:
            log.error(
                "cluster_heartbeat_failed",
                status="degraded",
                error=f"{type(exc).__name__}: {exc}",
            )
        deadline = time.time() + interval
        while running and time.time() < deadline:
            time.sleep(0.5)
    log.info(
        "cluster_runtime_stopped",
        duration_seconds=round(time.perf_counter() - started, 3),
    )


if __name__ == "__main__":
    main()
