from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from .benchmarks.common import write_benchmark_index
from .benchmarks.integrity_detection import build_spec as integrity_spec
from .benchmarks.resilience_boundaries import build_spec as resilience_spec
from .benchmarks.sqlite_baseline import build_spec as sqlite_baseline_spec
from .system_test import GatewayClient, start_local_server, stop_local_server


def _prepare_result_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    benchmark_dir = path / "benchmarks"
    if benchmark_dir.exists():
        shutil.rmtree(benchmark_dir)
    for candidate in (
        path / "local-system-test.db",
        Path(str(path / "local-system-test.db") + "-wal"),
        Path(str(path / "local-system-test.db") + "-shm"),
        path / "local-gateway.key",
    ):
        candidate.unlink(missing_ok=True)


def _effective_profile(profile: str) -> str:
    # "paper" is retained as a backwards-compatible alias for older commands.
    return "full" if profile == "paper" else profile


def _specs(profile: str, gateway: GatewayClient):
    profile = _effective_profile(profile)
    if profile == "full":
        return [
            integrity_spec(
                replay_trials=20,
                deletion_trials=20,
                tamper_trials_per_class=3,
                control_trials=20,
                seed=2026,
            ),
            sqlite_baseline_spec(
                device_counts=(1, 20, 100),
                events_per_run=(1_000, 10_000),
                repetitions=5,
                block_size=64,
            ),
            resilience_spec(
                gateway,
                capacity=1_000,
                overflow_attempts=500,
                checkpoint_events=20,
            ),
        ]
    return [
        integrity_spec(
            replay_trials=4,
            deletion_trials=4,
            tamper_trials_per_class=1,
            control_trials=2,
            seed=2026,
        ),
        sqlite_baseline_spec(
            device_counts=(1, 3),
            events_per_run=(20, 50),
            repetitions=1,
            block_size=10,
        ),
        resilience_spec(
            gateway,
            capacity=5,
            overflow_attempts=3,
            checkpoint_events=3,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the EdgeChainDB integrity, baseline, and resilience validation suite"
    )
    parser.add_argument(
        "--profile",
        choices=("smoke", "full", "paper"),
        default="smoke",
        help=(
            "smoke is CI-friendly; full runs the complete validation matrix; "
            "paper is a backwards-compatible alias for full"
        ),
    )
    parser.add_argument(
        "--result-dir",
        default="result/validation",
        help="directory for JSON, CSV, and HTML validation artifacts",
    )
    args = parser.parse_args()

    effective_profile = _effective_profile(args.profile)
    result_dir = Path(args.result_dir)
    _prepare_result_dir(result_dir)
    server, thread, base_url = start_local_server(result_dir, batch_size=64)
    gateway = GatewayClient(base_url)
    failed: list[dict[str, str]] = []
    try:
        for spec in _specs(effective_profile, gateway):
            try:
                spec.execute(result_dir)
            except Exception as exc:
                failed.append(
                    {
                        "benchmark": spec.slug,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        write_benchmark_index(result_dir)
        manifest = {
            "profile": effective_profile,
            "requested_profile": args.profile,
            "result_dir": str(result_dir),
            "failed": failed,
            "status": "FAIL" if failed else "PASS",
            "artifacts": {
                "summary": str(result_dir / "benchmarks" / "summary.json"),
                "report": str(result_dir / "benchmarks" / "report.html"),
            },
        }
        serialized = json.dumps(manifest, indent=2)
        (result_dir / "validation.json").write_text(serialized, encoding="utf-8")
        # Preserve the historical artifact name so existing automation does not break.
        (result_dir / "reviewer-validation.json").write_text(
            serialized, encoding="utf-8"
        )
    finally:
        stop_local_server(server, thread)

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
