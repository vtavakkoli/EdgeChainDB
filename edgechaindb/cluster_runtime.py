from __future__ import annotations

import json
import os
import signal
import time

import httpx


def main() -> None:
    base_url = os.getenv("GATEWAY_URL", "http://gateway:8000")
    interval = float(os.getenv("CLUSTER_STATUS_INTERVAL", "30"))
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        try:
            with httpx.Client(base_url=base_url, timeout=5) as client:
                health = client.get("/health")
                health.raise_for_status()
                print(json.dumps({"cluster": "running", **health.json()}), flush=True)
        except Exception as exc:
            print(json.dumps({"cluster": "degraded", "error": str(exc)}), flush=True)
        deadline = time.time() + interval
        while running and time.time() < deadline:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
