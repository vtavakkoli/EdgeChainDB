from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import html
import json
from pathlib import Path
from typing import Any, Callable

from ..observability import get_logger

log = get_logger("research-benchmarks")


def _json_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("status\nno_rows\n", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_value(row.get(key)) for key in fields})


@dataclass(frozen=True)
class BenchmarkSpec:
    name: str
    category: str
    slug: str
    runner: Callable[[], dict[str, Any]]

    def execute(self, result_dir: Path) -> tuple[str, dict[str, Any]]:
        benchmark_dir = result_dir / "benchmarks"
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        json_path = benchmark_dir / f"{self.slug}.json"
        csv_path = benchmark_dir / f"{self.slug}.csv"
        started = datetime.now(timezone.utc).isoformat()
        try:
            value = self.runner()
            details = str(value.get("details", "Benchmark completed"))
            metrics = dict(value.get("metrics", {}))
            payload = {
                "name": self.name,
                "category": self.category,
                "status": "PASS",
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "details": details,
                "metrics": metrics,
                "notes": list(value.get("notes", [])),
                "rows": list(value.get("rows", [])),
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            _write_csv(csv_path, payload["rows"] or [metrics])
            log.info("research_benchmark_artifact_written", benchmark=self.slug)
            return details, {**metrics, "artifact": str(json_path)}
        except Exception as exc:
            payload = {
                "name": self.name,
                "category": self.category,
                "status": "FAIL",
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "details": f"{type(exc).__name__}: {exc}",
                "metrics": {},
                "notes": [],
                "rows": [],
            }
            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            _write_csv(csv_path, [{"status": "FAIL", "error": payload["details"]}])
            raise


def write_benchmark_index(result_dir: Path) -> None:
    benchmark_dir = result_dir / "benchmarks"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(benchmark_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            items.append({
                "name": path.stem,
                "category": "Artifact",
                "status": "FAIL",
                "details": f"Could not parse artifact: {exc}",
                "metrics": {},
            })
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": sum(item.get("status") == "PASS" for item in items),
        "failed": sum(item.get("status") == "FAIL" for item in items),
        "benchmarks": items,
    }
    (benchmark_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    rows = []
    for item in items:
        metrics = html.escape(json.dumps(item.get("metrics", {}), ensure_ascii=False))
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(str(item.get('name', '')))}</strong><br>"
            f"<small>{html.escape(str(item.get('category', '')))}</small></td>"
            f"<td>{html.escape(str(item.get('status', '')))}</td>"
            f"<td>{html.escape(str(item.get('details', '')))}</td>"
            f"<td><code>{metrics}</code></td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EdgeChainDB Research Benchmarks</title>
<style>body{{font:15px/1.5 system-ui;margin:0;background:#f4f7fb;color:#172033}}header{{padding:36px 5vw;background:#111827;color:#fff}}main{{max-width:1500px;margin:24px auto;padding:0 24px}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:12px;border:1px solid #dfe5ef;text-align:left;vertical-align:top}}code{{white-space:pre-wrap;word-break:break-word;font-size:12px}}.cards{{display:flex;gap:16px;margin-bottom:20px}}.card{{background:#fff;padding:16px;border-radius:12px;border:1px solid #dfe5ef}}</style></head>
<body><header><h1>EdgeChainDB Research Benchmarks</h1><p>Generated {html.escape(summary['generated_at'])}</p></header>
<main><div class="cards"><div class="card"><strong>{summary['passed']}</strong><br>Passed</div><div class="card"><strong>{summary['failed']}</strong><br>Failed</div></div>
<table><thead><tr><th>Benchmark</th><th>Status</th><th>Finding</th><th>Metrics</th></tr></thead><tbody>{''.join(rows)}</tbody></table></main></body></html>"""
    (benchmark_dir / "report.html").write_text(document, encoding="utf-8")
