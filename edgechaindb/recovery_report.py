from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import time
from typing import Any

import httpx

from .observability import get_logger
from .system_test import render_html


log = get_logger("recovery-check")


def _wait_for_gateway_health(base_url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with httpx.Client(
                base_url=base_url,
                timeout=httpx.Timeout(connect=3, read=5, write=5, pool=5),
            ) as client:
                response = client.get("/health")
                response.raise_for_status()
                value = response.json()
                if value.get("status") == "ok":
                    log.info(
                        "gateway_recovery_health_ready",
                        attempt=attempt,
                        events=value.get("events"),
                        blocks=value.get("blocks"),
                        uptime_seconds=value.get("uptime_seconds"),
                    )
                    return value
                last_error = f"unexpected health payload: {value}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "gateway_recovery_health_retry",
                attempt=attempt,
                error=last_error,
            )
        time.sleep(0.5)
    raise TimeoutError(f"gateway did not become healthy after restart: {last_error}")


def _verify_recovered_ledger(
    base_url: str,
    health: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    # Full verification is intentionally expensive: it validates every event
    # signature, micro-chain, Merkle root, block link, and quorum signature.
    # The old fixed 5-second read timeout incorrectly failed on ledgers that
    # required ~13 seconds. Scale the budget with ledger size and keep a safe
    # minimum for slower Docker Desktop / WSL2 hosts.
    event_count = int(health.get("events", 0))
    estimated_seconds = 20 + event_count / 250
    read_timeout = min(max(timeout, estimated_seconds, 60), 300)
    log.info(
        "gateway_recovery_verification_started",
        events=event_count,
        blocks=health.get("blocks"),
        read_timeout_seconds=round(read_timeout, 3),
    )
    started = time.perf_counter()
    with httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(
            connect=5,
            read=read_timeout,
            write=10,
            pool=10,
        ),
    ) as client:
        response = client.get("/verify")
        response.raise_for_status()
        value = response.json()
    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "gateway_recovery_verification_completed",
        valid=value.get("valid"),
        events=value.get("events"),
        blocks=value.get("blocks"),
        duration_ms=round(duration_ms, 3),
    )
    if not value.get("valid"):
        raise AssertionError(json.dumps(value))
    value.setdefault("duration_ms", round(duration_ms, 3))
    return value


def append_recovery_result(
    *,
    base_url: str,
    result_dir: Path,
    timeout: float = 120.0,
) -> bool:
    result_path = result_dir / "result.json"
    if not result_path.exists():
        raise FileNotFoundError(f"missing report data: {result_path}")

    started = time.perf_counter()
    verification: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    error: str | None = None
    log.info(
        "gateway_recovery_check_started",
        base_url=base_url,
        timeout_seconds=timeout,
        result_path=str(result_path),
    )
    try:
        health_budget = min(max(timeout * 0.35, 20), 60)
        health = _wait_for_gateway_health(base_url, health_budget)
        verification = _verify_recovered_ledger(base_url, health, timeout)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.error(
            "gateway_recovery_check_failed",
            error=error,
            exc_info=True,
        )

    data = json.loads(result_path.read_text(encoding="utf-8"))
    data["scenarios"] = [
        item
        for item in data["scenarios"]
        if item["name"] != "Persistent restart recovery"
    ]
    passed = verification is not None and verification.get("valid") is True
    metrics: dict[str, Any] = verification or {}
    if health:
        metrics = {"post_restart_health": health, **metrics}
    data["scenarios"].append(
        {
            "name": "Persistent restart recovery",
            "category": "Durability",
            "status": "PASS" if passed else "FAIL",
            "duration_ms": (time.perf_counter() - started) * 1000,
            "details": (
                "The Docker gateway restarted and the persisted ledger remained valid"
                if passed
                else f"The Docker restart verification failed: {error}"
            ),
            "metrics": metrics,
        }
    )
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["environment"]["docker_restart_checked"] = True
    data["environment"]["recovery_checker_platform"] = platform.platform()
    data["environment"]["recovery_timeout_seconds"] = timeout
    data["summary"] = {
        "passed": sum(x["status"] == "PASS" for x in data["scenarios"]),
        "failed": sum(x["status"] == "FAIL" for x in data["scenarios"]),
        "skipped": sum(x["status"] == "SKIP" for x in data["scenarios"]),
        "total": len(data["scenarios"]),
    }
    data.setdefault("extra", {})["status"] = "completed" if passed else "failed"
    result_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (result_dir / "report.html").write_text(render_html(data), encoding="utf-8")
    log.info(
        "gateway_recovery_report_updated",
        passed=passed,
        report=str(result_dir / "report.html"),
    )
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append a Docker restart result to the HTML report"
    )
    parser.add_argument("--base-url", default="http://gateway:8000")
    parser.add_argument("--result-dir", default="result")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    try:
        passed = append_recovery_result(
            base_url=args.base_url,
            result_dir=Path(args.result_dir),
            timeout=args.timeout,
        )
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "valid": passed,
                "report": str(Path(args.result_dir) / "report.html"),
            },
            indent=2,
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
