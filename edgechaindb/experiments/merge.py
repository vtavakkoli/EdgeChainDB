from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any

from .model import load_plan
from .report import write_plan_artifacts, write_result_artifacts


def _read_directory(path: Path) -> list[dict[str, Any]]:
    journal = path / "journal.jsonl"
    jsonl = path / "results.jsonl"
    json_file = path / "results.json"
    for candidate in (journal, jsonl):
        if candidate.exists():
            return [json.loads(line) for line in candidate.read_text(encoding="utf-8").splitlines() if line.strip()]
    if json_file.exists():
        value = json.loads(json_file.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(f"{json_file} must contain a list")
        return value
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge independently executed EdgeChainDB matrix shards")
    parser.add_argument("--config", default="/app/experiments/one-day.yaml")
    parser.add_argument("--input", action="append", required=True, help="directory or glob; may be repeated")
    parser.add_argument("--output", default="/result/experiments/combined")
    parser.add_argument("--repetitions", type=int)
    args = parser.parse_args()

    paths: list[Path] = []
    for pattern in args.input:
        matches = glob.glob(pattern)
        paths.extend(Path(item) for item in matches)
    unique_paths = sorted({path.resolve() for path in paths if path.is_dir()})
    if not unique_paths:
        parser.error("no input shard directories matched")

    latest: dict[str, dict[str, Any]] = {}
    source_counts: dict[str, int] = {}
    for path in unique_paths:
        values = _read_directory(path)
        source_counts[str(path)] = len(values)
        for item in values:
            run_id = str(item.get("run_id", ""))
            if not run_id:
                continue
            previous = latest.get(run_id)
            if previous is None or str(item.get("completed_at", "")) >= str(previous.get("completed_at", "")):
                latest[run_id] = item

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    plan = load_plan(args.config, repetitions_override=args.repetitions)
    write_plan_artifacts(plan, output)
    results = sorted(latest.values(), key=lambda item: str(item.get("run_id")))
    summary = write_result_artifacts(plan, results, output)
    manifest = {
        "inputs": [str(path) for path in unique_paths],
        "source_result_counts": source_counts,
        "unique_runs": len(results),
        "duplicates_removed": sum(source_counts.values()) - len(results),
        "coverage": summary["coverage"],
    }
    (output / "merge-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
