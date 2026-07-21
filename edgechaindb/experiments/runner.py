from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import socket
from typing import Any

from ..observability import get_logger
from .docker_runtime import DynamicDockerExperiment
from .model import ExperimentCase, load_plan
from .report import write_plan_artifacts, write_result_artifacts

log = get_logger("experiment-matrix-runner")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()



@contextmanager
def _campaign_lock(result_dir: Path):
    """Prevent two Compose aliases from writing the same campaign concurrently."""
    lock_path = result_dir / ".campaign.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another experiment campaign is already using {result_dir}; "
                "check `docker compose ps experiment experment`"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": _utc_now(),
        }, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()


def _load_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid results JSONL line {line_number}: {exc}") from exc
    return results


def _append_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_progress(path: Path, **value: Any) -> None:
    path.write_text(json.dumps({"updated_at": _utc_now(), **value}, indent=2), encoding="utf-8")


def _selected_cases(
    cases: list[ExperimentCase],
    *,
    shard_index: int,
    shard_count: int,
    max_runs: int | None,
) -> list[ExperimentCase]:
    selected = [case for index, case in enumerate(cases) if index % shard_count == shard_index]
    return selected[:max_runs] if max_runs is not None else selected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dynamically provision Docker gateways/devices and execute the EdgeChainDB experimental matrix"
    )
    parser.add_argument("--config", default="/app/experiments/full-matrix.yaml")
    parser.add_argument("--result-dir", default="/result/experiments")
    parser.add_argument("--repetitions", type=int, help="override the YAML repetition count")
    parser.add_argument("--dry-run", action="store_true", help="expand and report the matrix without starting Docker containers")
    parser.add_argument("--resume", action="store_true", help="skip run IDs already present in results.jsonl")
    parser.add_argument("--rerun-failed", action="store_true", help="with --resume, repeat only previously failed cases")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--report-every", type=int, default=10)
    args = parser.parse_args()

    if args.shard_count < 1:
        parser.error("--shard-count must be at least one")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be between zero and shard-count minus one")
    if args.report_every < 1:
        parser.error("--report-every must be at least one")

    plan = load_plan(args.config, repetitions_override=args.repetitions)
    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)

    with _campaign_lock(result_dir):
        _run_campaign(args, plan, result_dir)


def _run_campaign(args: argparse.Namespace, plan: Any, result_dir: Path) -> None:
    write_plan_artifacts(plan, result_dir)

    all_cases = list(plan.iter_cases())
    selected = _selected_cases(
        all_cases,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        max_runs=args.max_runs,
    )
    shard_manifest = {
        "generated_at": _utc_now(),
        "config": str(args.config),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "selected_runs": len(selected),
        "full_plan_runs": plan.runs,
        "run_ids": [case.run_id for case in selected],
    }
    (result_dir / f"shard-{args.shard_index:03d}-of-{args.shard_count:03d}.json").write_text(
        json.dumps(shard_manifest, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "plan": plan.name,
                "configurations": plan.configurations,
                "runs": plan.runs,
                "nominal_events": plan.nominal_events,
                "selected_runs": len(selected),
                "shard": f"{args.shard_index}/{args.shard_count}",
                "dry_run": args.dry_run,
                "plan_report": str(result_dir / "matrix-plan.html"),
            },
            indent=2,
        )
    )
    if args.dry_run:
        _write_progress(
            result_dir / "progress.json",
            status="planned",
            planned_runs=plan.runs,
            selected_runs=len(selected),
            nominal_events=plan.nominal_events,
        )
        return

    journal_path = result_dir / "journal.jsonl"
    previous = _load_results(journal_path)
    if not previous:
        previous = _load_results(result_dir / "results.jsonl")
    latest_by_run = {str(item.get("run_id")): item for item in previous if item.get("run_id")}
    # Create the comprehensive HTML/JSON/CSV report immediately, even before
    # Docker provisioning. Detached Compose users can open report.html at once.
    write_result_artifacts(plan, previous, result_dir, campaign={"status": "initializing_docker", "fatal_error": None})
    to_run: list[ExperimentCase] = []
    for case in selected:
        old = latest_by_run.get(case.run_id)
        if not args.resume or old is None:
            to_run.append(case)
        elif args.rerun_failed and old.get("status") != "PASS":
            to_run.append(case)

    executed: list[dict[str, Any]] = []
    failures = 0
    _write_progress(
        result_dir / "progress.json",
        status="initializing_docker",
        planned_runs=plan.runs,
        selected_runs=len(selected),
        already_recorded=len(previous),
        remaining_in_shard=len(to_run),
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )

    fatal_error: str | None = None
    try:
        runtime = DynamicDockerExperiment(plan.execution, result_dir)
        _write_progress(
            result_dir / "progress.json",
            status="running",
            planned_runs=plan.runs,
            selected_runs=len(selected),
            already_recorded=len(previous),
            remaining_in_shard=len(to_run),
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
        for position, case in enumerate(to_run, start=1):
            run_dir = result_dir / "runs" / case.run_id
            log.info(
                "matrix_case_started",
                position=position,
                selected_total=len(to_run),
                run_id=case.run_id,
            )
            result = runtime.run(case, run_dir)
            _append_result(journal_path, result)
            previous.append(result)
            executed.append(result)
            failures += int(result.get("status") != "PASS")
            _write_progress(
                result_dir / "progress.json",
                status="running",
                executed_this_process=position,
                selected_this_process=len(to_run),
                total_recorded=len(previous),
                failures_this_process=failures,
                current_run_id=case.run_id,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
            )
            if position % args.report_every == 0:
                write_result_artifacts(
                    plan, previous, result_dir,
                    campaign={"status": "running", "fatal_error": None, "current_run_id": case.run_id},
                )
    except KeyboardInterrupt:
        _write_progress(
            result_dir / "progress.json",
            status="interrupted",
            total_recorded=len(previous),
            failures_this_process=failures,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
        write_result_artifacts(plan, previous, result_dir, campaign={"status": "interrupted", "fatal_error": None})
        raise SystemExit(130)
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        log.error("matrix_campaign_failed", error=fatal_error, exc_info=True)

    summary = write_result_artifacts(
        plan, previous, result_dir,
        campaign={"status": "failed" if fatal_error else "completed_shard", "fatal_error": fatal_error},
    )
    _write_progress(
        result_dir / "progress.json",
        status="failed" if fatal_error else "completed_shard",
        total_recorded=len(previous),
        executed_this_process=len(executed),
        failures_this_process=failures,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        report=str(result_dir / "report.html"),
        coverage=summary["coverage"],
        fatal_error=fatal_error,
    )
    if fatal_error or failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
