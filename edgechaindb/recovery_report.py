from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import time
from typing import Any

import httpx

from .system_test import render_html


def append_recovery_result(
    *, base_url: str,
    result_dir: Path,
    timeout: float = 30.0,
) -> bool:
    result_path = result_dir / "result.json"
    if not result_path.exists():
        raise FileNotFoundError(f"missing report data: {result_path}")

    started = time.perf_counter()
    deadline = time.time() + timeout
    verification: dict[str, Any] | None = None
    error: str | None = None
    while time.time() < deadline:
        try:
            with httpx.Client(base_url=base_url, timeout=5) as client:
                health = client.get("/health")
                health.raise_for_status()
                response = client.get("/verify")
                response.raise_for_status()
                verification = response.json()
                if verification.get("valid"):
                    break
                error = json.dumps(verification)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)

    data = json.loads(result_path.read_text(encoding="utf-8"))
    data["scenarios"] = [
        item
        for item in data["scenarios"]
        if item["name"] != "Persistent restart recovery"
    ]
    passed = verification is not None and verification.get("valid") is True
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
            "metrics": verification or {},
        }
    )
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["environment"]["docker_restart_checked"] = True
    data["environment"]["recovery_checker_platform"] = platform.platform()
    data["summary"] = {
        "passed": sum(x["status"] == "PASS" for x in data["scenarios"]),
        "failed": sum(x["status"] == "FAIL" for x in data["scenarios"]),
        "skipped": sum(x["status"] == "SKIP" for x in data["scenarios"]),
        "total": len(data["scenarios"]),
    }
    data.setdefault("extra", {})["status"] = "completed"
    result_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (result_dir / "report.html").write_text(render_html(data), encoding="utf-8")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append a Docker restart result to the HTML report"
    )
    parser.add_argument("--base-url", default="http://gateway:8000")
    parser.add_argument("--result-dir", default="result")
    parser.add_argument("--timeout", type=float, default=30.0)
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
