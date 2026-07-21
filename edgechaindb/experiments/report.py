from __future__ import annotations

import csv
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable

from .model import ExperimentPlan


METRICS = (
    "gateway_ingest_events_per_second",
    "end_to_end_events_per_second",
    "finalization_latency_ms_p50",
    "finalization_latency_ms_p95",
    "device_request_latency_ms_p50_median",
    "device_request_latency_ms_p95_max",
    "recovery_after_gateway_start_seconds",
    "wire_bytes_per_event",
    "signing_energy_estimate_microjoules_per_event",
    "storage_bytes_per_event",
    "network_errors",
    "application_packet_drops",
)

DIMENSIONS = {
    "devices": "devices",
    "events": "events",
    "block_size": "block_size",
    "authority_threshold": "quorum",
    "packet_loss": "packet_loss_percent",
    "outage_duration": "outage_seconds",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            writer.writerow({key: _cell(row.get(key)) for key in fields})


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    case = result.get("case", {})
    metrics = result.get("metrics", {})
    gateway_resources = metrics.get("gateway_resources", {}) or {}
    row: dict[str, Any] = {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "started_at": result.get("started_at"),
        "completed_at": result.get("completed_at"),
        "error": result.get("error"),
        "devices": case.get("devices"),
        "events": case.get("events"),
        "block_size": case.get("block_size"),
        "authorities": case.get("authorities"),
        "threshold": case.get("threshold"),
        "quorum": case.get("quorum"),
        "packet_loss_percent": case.get("packet_loss_percent"),
        "outage_seconds": case.get("outage_seconds"),
        "repetition": case.get("repetition"),
    }
    for key in METRICS:
        row[key] = metrics.get(key)
    row.update(
        {
            "events_delivered": metrics.get("events_delivered"),
            "elapsed_seconds": metrics.get("elapsed_seconds"),
            "blocks": metrics.get("blocks"),
            "ledger_valid": metrics.get("ledger_valid"),
            "quick_check": metrics.get("quick_check"),
            "gateway_memory_peak_bytes": gateway_resources.get("memory_peak_bytes"),
            "gateway_cpu_percent_peak": gateway_resources.get("cpu_percent_peak"),
            "gateway_network_rx_bytes": gateway_resources.get("network_rx_bytes"),
            "gateway_network_tx_bytes": gateway_resources.get("network_tx_bytes"),
        }
    )
    return row


def write_plan_artifacts(plan: ExperimentPlan, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc_now(),
        **plan.to_dict(),
        "warnings": [
            "The full five-repetition matrix is a large campaign. Use shards and --resume.",
            "One-million-event runs and five-minute outages should be scheduled on dedicated Docker hosts with adequate disk capacity.",
            "The matrix pairs authority counts with the requested thresholds; it does not cross every threshold with every authority count.",
        ],
    }
    (output_dir / "matrix-plan.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    rows = [case.to_dict() for case in plan.iter_cases()]
    _write_csv(output_dir / "matrix-plan.csv", rows)
    document = _render_plan_html(payload)
    (output_dir / "matrix-plan.html").write_text(document, encoding="utf-8")
    return payload


def _render_plan_html(payload: dict[str, Any]) -> str:
    matrix = payload["matrix"]
    summary = payload["summary"]
    warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in payload.get("warnings", []))
    rows = [
        ("Devices", matrix["devices"]),
        ("Events", matrix["events"]),
        ("Block size", matrix["block_size"]),
        ("Authority thresholds", [item["label"] for item in matrix["authority_thresholds"]]),
        ("Packet loss", [f"{item}%" for item in matrix["packet_loss_percent"]]),
        ("Outage duration", [f"{item} s" for item in matrix["outage_seconds"]]),
        ("Repetitions", payload["repetitions"]),
    ]
    table_rows = "".join(
        f"<tr><th>{html.escape(str(name))}</th><td>{html.escape(json.dumps(value))}</td></tr>"
        for name, value in rows
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EdgeChainDB experimental matrix plan</title><style>{_css()}</style></head><body>
<header><h1>EdgeChainDB Experimental Matrix Plan</h1><p>{html.escape(payload['name'])}</p></header><main>
<section class="cards"><div class="card metric"><span>Configurations</span><strong>{summary['configurations']:,}</strong></div>
<div class="card metric"><span>Runs</span><strong>{summary['runs']:,}</strong></div>
<div class="card metric"><span>Nominal events</span><strong>{summary['nominal_events']:,}</strong></div>
<div class="card metric"><span>Outage time</span><strong>{summary['nominal_outage_seconds']/3600:,.1f} h</strong></div></section>
<section class="card"><h2>Required factors</h2><table>{table_rows}</table></section>
<section class="card warning"><h2>Execution guidance</h2><ul>{warning_items}</ul>
<pre>docker compose up --build -d experiment
docker compose logs -f experiment

Report: result/experiments/report.html
Progress: result/experiments/progress.json</pre></section>
</main></body></html>"""


def _numeric(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _metric_summary(values: list[float], prefix: str) -> dict[str, Any]:
    if not values:
        return {
            f"{prefix}_n": 0,
            f"{prefix}_mean": None,
            f"{prefix}_median": None,
            f"{prefix}_stdev": None,
            f"{prefix}_ci95_low": None,
            f"{prefix}_ci95_high": None,
            f"{prefix}_min": None,
            f"{prefix}_max": None,
        }
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    margin = 1.96 * stdev / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {
        f"{prefix}_n": len(values),
        f"{prefix}_mean": round(mean, 6),
        f"{prefix}_median": round(statistics.median(values), 6),
        f"{prefix}_stdev": round(stdev, 6),
        f"{prefix}_ci95_low": round(mean - margin, 6),
        f"{prefix}_ci95_high": round(mean + margin, 6),
        f"{prefix}_min": round(min(values), 6),
        f"{prefix}_max": round(max(values), 6),
    }


def aggregate_by(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get(field))
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        passed = [item for item in items if item.get("status") == "PASS"]
        value: dict[str, Any] = {
            "value": key,
            "runs": len(items),
            "passed": len(passed),
            "failed": len(items) - len(passed),
            "success_rate": round(len(passed) / len(items), 6) if items else None,
        }
        for metric in METRICS:
            value.update(_metric_summary(_numeric(item.get(metric) for item in passed), metric))
        output.append(value)

    def sort_key(item: dict[str, Any]) -> tuple[int, Any]:
        raw = item["value"]
        try:
            return (0, float(raw))
        except ValueError:
            return (1, raw)

    return sorted(output, key=sort_key)


def _svg_bar(rows: list[dict[str, Any]], label: str, metric: str, title: str) -> str:
    values = [(str(row["value"]), row.get(f"{metric}_mean")) for row in rows]
    values = [(name, float(value)) for name, value in values if value is not None]
    if not values:
        return "<p class=muted>No completed data.</p>"
    width, height = 760, 280
    left, right, top, bottom = 70, 20, 35, 65
    plot_w = width - left - right
    plot_h = height - top - bottom
    maximum = max(value for _, value in values) or 1.0
    slot = plot_w / len(values)
    bars: list[str] = []
    for index, (name, value) in enumerate(values):
        bar_h = plot_h * value / maximum
        x = left + index * slot + slot * 0.18
        y = top + plot_h - bar_h
        bar_w = slot * 0.64
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4"><title>{html.escape(name)}: {value:.3f}</title></rect>')
        bars.append(f'<text x="{x+bar_w/2:.1f}" y="{height-42}" text-anchor="middle">{html.escape(name)}</text>')
    return f'<figure><figcaption>{html.escape(title)}</figcaption><svg viewBox="0 0 {width} {height}" role="img"><line x1="{left}" y1="{top+plot_h}" x2="{width-right}" y2="{top+plot_h}"/><text x="12" y="20">{html.escape(label)}</text>{"".join(bars)}</svg></figure>'


def write_result_artifacts(
    plan: ExperimentPlan,
    results: list[dict[str, Any]],
    output_dir: Path,
    *,
    campaign: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for item in results:
        run_id = str(item.get("run_id", ""))
        if not run_id:
            anonymous.append(item)
            continue
        previous = latest.get(run_id)
        if previous is None or str(item.get("completed_at", "")) >= str(previous.get("completed_at", "")):
            latest[run_id] = item
    canonical_results = sorted(latest.values(), key=lambda item: str(item.get("run_id"))) + anonymous
    rows = [flatten_result(item) for item in canonical_results]
    _write_csv(output_dir / "results.csv", rows)
    (output_dir / "results.json").write_text(json.dumps(canonical_results, indent=2), encoding="utf-8")
    jsonl = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in canonical_results)
    (output_dir / "results.jsonl").write_text(jsonl, encoding="utf-8")

    group_payload: dict[str, list[dict[str, Any]]] = {}
    for output_name, field in DIMENSIONS.items():
        grouped = aggregate_by(rows, field)
        group_payload[output_name] = grouped
        _write_csv(output_dir / f"by_{output_name}.csv", grouped)
        (output_dir / f"by_{output_name}.json").write_text(json.dumps(grouped, indent=2), encoding="utf-8")

    passed_rows = [row for row in rows if row.get("status") == "PASS"]
    summary: dict[str, Any] = {
        "generated_at": utc_now(),
        "plan": plan.to_dict(),
        "coverage": {
            "planned_runs": plan.runs,
            "completed_runs": len(canonical_results),
            "passed_runs": len(passed_rows),
            "failed_runs": len(canonical_results) - len(passed_rows),
            "remaining_runs": max(0, plan.runs - len(canonical_results)),
            "completion_rate": round(len(canonical_results) / plan.runs, 6) if plan.runs else 1.0,
            "success_rate": round(len(passed_rows) / len(canonical_results), 6) if canonical_results else None,
        },
        "overall_metrics": {},
        "groups": group_payload,
        "campaign": campaign or {"status": "reporting", "fatal_error": None},
        "artifacts": {
            "html_report": "report.html",
            "progress": "progress.json",
            "raw_results_jsonl": "journal.jsonl",
            "canonical_results_json": "results.json",
            "canonical_results_csv": "results.csv",
            "plan_html": "matrix-plan.html",
            "plan_json": "matrix-plan.json",
            "plan_csv": "matrix-plan.csv",
        },
    }
    for metric in METRICS:
        summary["overall_metrics"].update(_metric_summary(_numeric(row.get(metric) for row in passed_rows), metric))
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "report.html").write_text(_render_results_html(summary, rows), encoding="utf-8")
    return summary


def _render_results_html(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    coverage = summary["coverage"]
    overall = summary["overall_metrics"]
    groups = summary["groups"]
    campaign = summary.get("campaign", {}) or {}
    plan = summary["plan"]
    matrix = plan["matrix"]
    failed = [row for row in rows if row.get("status") != "PASS"]
    failure_rows = "".join(
        f"<tr><td>{html.escape(str(row.get('run_id')))}</td><td>{html.escape(str(row.get('error')))}</td></tr>"
        for row in failed[:100]
    ) or '<tr><td colspan="2">No failures recorded.</td></tr>'

    summary_rows = []
    for label, metric in (
        ("Gateway throughput (events/s)", "gateway_ingest_events_per_second"),
        ("Finalization p95 (ms)", "finalization_latency_ms_p95"),
        ("Recovery after restart (s)", "recovery_after_gateway_start_seconds"),
        ("Wire bytes/event", "wire_bytes_per_event"),
        ("Storage bytes/event", "storage_bytes_per_event"),
        ("Signing energy estimate (µJ/event)", "signing_energy_estimate_microjoules_per_event"),
    ):
        summary_rows.append(
            f"<tr><th>{html.escape(label)}</th><td>{_fmt(overall.get(metric+'_mean'))}</td>"
            f"<td>{_fmt(overall.get(metric+'_median'))}</td><td>{_fmt(overall.get(metric+'_ci95_low'))} – {_fmt(overall.get(metric+'_ci95_high'))}</td></tr>"
        )

    device_chart = _svg_bar(groups.get("devices", []), "events/s", "gateway_ingest_events_per_second", "Mean throughput by device count")
    block_chart = _svg_bar(groups.get("block_size", []), "ms", "finalization_latency_ms_p95", "Mean p95 finalization latency by block size")
    loss_chart = _svg_bar(groups.get("packet_loss", []), "s", "recovery_after_gateway_start_seconds", "Mean recovery time by packet loss")

    campaign_error = campaign.get("fatal_error")
    campaign_class = "warning" if campaign_error else ""
    campaign_error_html = (
        f"<p><strong>Fatal campaign error:</strong> {html.escape(str(campaign_error))}</p>"
        if campaign_error else ""
    )
    matrix_rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(json.dumps(value))}</td></tr>"
        for label, value in (
            ("Devices", matrix["devices"]),
            ("Events", matrix["events"]),
            ("Block sizes", matrix["block_size"]),
            ("Authority thresholds", [item["label"] for item in matrix["authority_thresholds"]]),
            ("Packet loss (%)", matrix["packet_loss_percent"]),
            ("Outage duration (s)", matrix["outage_seconds"]),
            ("Repetitions", plan["repetitions"]),
        )
    )

    group_sections: list[str] = []
    for name, grouped in groups.items():
        rows_html = "".join(
            "<tr>"
            f"<td>{html.escape(str(item['value']))}</td><td>{item['runs']}</td><td>{item['passed']}</td>"
            f"<td>{_fmt(item['success_rate'], percent=True)}</td>"
            f"<td>{_fmt(item.get('gateway_ingest_events_per_second_mean'))}</td>"
            f"<td>{_fmt(item.get('finalization_latency_ms_p95_mean'))}</td>"
            f"<td>{_fmt(item.get('recovery_after_gateway_start_seconds_mean'))}</td>"
            f"<td>{_fmt(item.get('storage_bytes_per_event_mean'))}</td></tr>"
            for item in grouped
        )
        group_sections.append(
            f"<section class=card><h2>Grouped by {html.escape(name.replace('_',' '))}</h2><div class=scroll><table><thead><tr><th>Value</th><th>Runs</th><th>Passed</th><th>Success</th><th>Throughput</th><th>Finalization p95</th><th>Recovery</th><th>Storage/event</th></tr></thead><tbody>{rows_html}</tbody></table></div></section>"
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EdgeChainDB matrix report</title><style>{_css()}</style></head><body>
<header><h1>EdgeChainDB Experimental Matrix Report</h1><p>Generated {html.escape(summary['generated_at'])}</p></header><main>
<section class=cards><div class="card metric"><span>Completed</span><strong>{coverage['completed_runs']:,}/{coverage['planned_runs']:,}</strong></div>
<div class="card metric"><span>Passed</span><strong>{coverage['passed_runs']:,}</strong></div><div class="card metric"><span>Failed</span><strong>{coverage['failed_runs']:,}</strong></div>
<div class="card metric"><span>Completion</span><strong>{coverage['completion_rate']*100:.2f}%</strong></div></section>
<section class="card {campaign_class}"><h2>Campaign status</h2><p><strong>{html.escape(str(campaign.get('status', 'reporting')).replace('_', ' ').title())}</strong></p>{campaign_error_html}<p>Reports are canonicalized by run ID, so retries do not inflate repetition counts or confidence intervals.</p></section>
<section class=card><h2>Experimental matrix</h2><table>{matrix_rows}</table></section>
<section class=card><h2>Overall performance</h2><table><thead><tr><th>Metric</th><th>Mean</th><th>Median</th><th>95% CI of mean</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></section>
<section class="chart-grid card"><div>{device_chart}</div><div>{block_chart}</div><div>{loss_chart}</div></section>
{''.join(group_sections)}
<section class=card><h2>Failures and anomalies</h2><div class=scroll><table><thead><tr><th>Run</th><th>Error</th></tr></thead><tbody>{failure_rows}</tbody></table></div></section>
<section class="card warning"><h2>Interpretation boundaries</h2><ul><li>netem is the preferred packet-loss mechanism. Every worker records when it falls back to deterministic application-level dropping.</li><li>Signing energy is an estimate unless the dedicated RAPL benchmark reports hardware energy.</li><li>Confidence intervals describe repetition variability on the tested host; they do not establish portability to other hardware.</li><li>Quorum tests measure threshold-signature behavior, not a complete asynchronous BFT consensus protocol.</li></ul></section>
</main></body></html>"""


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
        return f"{number*100:.2f}%" if percent else f"{number:,.3f}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _css() -> str:
    return """
:root{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#dfe5ef;--accent:#3448c5;--warn:#9a6700}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,Segoe UI,Arial,sans-serif}
header{background:linear-gradient(120deg,#111827,#3448c5);color:#fff;padding:38px 5vw}header h1{margin:0 0 7px;font-size:32px}header p{margin:0;opacity:.82}
main{max-width:1500px;margin:-18px auto 50px;padding:0 24px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:18px;box-shadow:0 8px 28px rgba(16,24,40,.06);margin-bottom:18px}.metric span{color:var(--muted)}.metric strong{display:block;font-size:28px}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.scroll{overflow:auto}
.warning{border-left:5px solid #e5a000}.warning h2{color:var(--warn)}pre{white-space:pre-wrap;background:#101828;color:#d0d5dd;padding:14px;border-radius:10px;overflow:auto}
.chart-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}figure{margin:0}figcaption{font-weight:700;margin-bottom:8px}svg{width:100%;height:auto;background:#fbfcfe;border:1px solid var(--line);border-radius:10px}svg rect{fill:var(--accent)}svg line{stroke:#98a2b3}svg text{font-size:11px;fill:#475467}.muted{color:var(--muted)}
@media(max-width:900px){.cards,.chart-grid{grid-template-columns:1fr 1fr}}@media(max-width:620px){.cards,.chart-grid{grid-template-columns:1fr}}
"""
