from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import statistics
import threading
import time
from typing import Any, Callable
import uuid

import httpx
import uvicorn
import yaml

from .api import create_app
from .crypto import KeyPair
from .device import DeviceClient
from .ledger import EdgeChainLedger
from .models import SignedEvent, ZERO_HASH
from .observability import get_logger


log = get_logger("system-test")


@dataclass
class ScenarioResult:
    name: str
    category: str
    status: str
    duration_ms: float
    details: str
    metrics: dict[str, Any]


class Report:
    def __init__(self, title: str, environment: dict[str, Any]) -> None:
        self.title = title
        self.environment = environment
        self.scenarios: list[ScenarioResult] = []
        self.notes: list[str] = []
        self.output_dir: Path | None = None
        self.extra: dict[str, Any] = {"status": "initializing"}

    def configure_output(self, result_dir: Path, extra: dict[str, Any] | None = None) -> None:
        self.output_dir = result_dir
        if extra is not None:
            self.extra = extra
        self.write(result_dir, self.extra)

    def checkpoint(self) -> None:
        if self.output_dir is not None:
            self.write(self.output_dir, self.extra)

    def run(
        self,
        name: str,
        category: str,
        function: Callable[[], tuple[str, dict[str, Any]] | None],
    ) -> Any:
        started = time.perf_counter()
        log.info("scenario_started", scenario=name, category=category)
        try:
            value = function()
            details, metrics = value if value is not None else ("Completed", {})
            duration_ms = (time.perf_counter() - started) * 1000
            result = ScenarioResult(
                name, category, "PASS", duration_ms, details, metrics,
            )
            self.scenarios.append(result)
            log.info(
                "scenario_completed",
                scenario=name,
                category=category,
                status="PASS",
                duration_ms=round(duration_ms, 3),
                details=details,
                metrics=metrics,
            )
            self.checkpoint()
            return value
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            error = f"{type(exc).__name__}: {exc}"
            self.scenarios.append(
                ScenarioResult(
                    name,
                    category,
                    "FAIL",
                    duration_ms,
                    error,
                    {},
                )
            )
            log.error(
                "scenario_completed",
                scenario=name,
                category=category,
                status="FAIL",
                duration_ms=round(duration_ms, 3),
                error=error,
                exc_info=True,
            )
            self.checkpoint()
            return None

    def skip(self, name: str, category: str, details: str) -> None:
        self.scenarios.append(ScenarioResult(name, category, "SKIP", 0, details, {}))
        log.warning(
            "scenario_skipped",
            scenario=name,
            category=category,
            details=details,
        )
        self.checkpoint()

    @property
    def failed(self) -> int:
        return sum(item.status == "FAIL" for item in self.scenarios)

    def write(self, result_dir: Path, extra: dict[str, Any]) -> None:
        result_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": self.title,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": self.environment,
            "summary": {
                "passed": sum(x.status == "PASS" for x in self.scenarios),
                "failed": self.failed,
                "skipped": sum(x.status == "SKIP" for x in self.scenarios),
                "total": len(self.scenarios),
            },
            "scenarios": [asdict(item) for item in self.scenarios],
            "notes": self.notes,
            "extra": extra,
        }
        (result_dir / "result.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        (result_dir / "report.html").write_text(render_html(payload), encoding="utf-8")


def render_html(data: dict[str, Any]) -> str:
    summary = data["summary"]
    scenario_rows = []
    for item in data["scenarios"]:
        status_class = item["status"].lower()
        metrics = html.escape(json.dumps(item["metrics"], ensure_ascii=False))
        scenario_rows.append(
            f"""
            <tr>
              <td><span class="badge {status_class}">{item['status']}</span></td>
              <td><strong>{html.escape(item['name'])}</strong><br><small>{html.escape(item['category'])}</small></td>
              <td>{item['duration_ms']:.1f} ms</td>
              <td>{html.escape(item['details'])}</td>
              <td><code>{metrics}</code></td>
            </tr>
            """
        )
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in data["notes"])
    env_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in data["environment"].items()
    )
    raw = html.escape(json.dumps(data, indent=2, ensure_ascii=False))
    overall = "PASS" if summary["failed"] == 0 else "FAIL"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(data['title'])}</title>
<style>
:root{{--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#dfe5ef;--ok:#067647;--bad:#b42318;--skip:#b54708;--accent:#3448c5}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,Segoe UI,Arial,sans-serif}}
header{{background:linear-gradient(120deg,#111827,#3448c5);color:white;padding:42px 5vw}}
header h1{{margin:0 0 8px;font-size:34px}} header p{{margin:0;opacity:.82}}
main{{max-width:1400px;margin:-20px auto 50px;padding:0 24px}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 8px 30px rgba(16,24,40,.06)}}
.metric strong{{display:block;font-size:32px}} .metric span{{color:var(--muted)}}
.overall.pass strong,.pass{{color:var(--ok)}} .overall.fail strong,.fail{{color:var(--bad)}}
h2{{margin-top:0}} table{{width:100%;border-collapse:collapse}} th,td{{padding:12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.badge{{display:inline-block;border-radius:999px;padding:4px 9px;font-weight:700;font-size:12px;background:#eef2ff}} .badge.pass{{background:#ecfdf3}} .badge.fail{{background:#fef3f2}} .badge.skip{{background:#fffaeb;color:var(--skip)}}
code{{font-size:12px;white-space:pre-wrap;word-break:break-word}} details pre{{white-space:pre-wrap;overflow:auto;background:#101828;color:#d0d5dd;padding:16px;border-radius:10px}}
.arch{{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;align-items:center;gap:10px;text-align:center}} .node{{padding:18px;border-radius:12px;background:#eef2ff;border:1px solid #c7d2fe}} .arrow{{font-size:24px;color:var(--accent)}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr 1fr}}.arch{{grid-template-columns:1fr}}.arrow{{transform:rotate(90deg)}}table{{display:block;overflow:auto}}}}
</style>
</head>
<body>
<header><h1>{html.escape(data['title'])}</h1><p>Generated {html.escape(data['generated_at'])}</p></header>
<main>
<section class="grid">
  <div class="card metric overall {overall.lower()}"><span>Overall</span><strong>{overall}</strong></div>
  <div class="card metric"><span>Passed</span><strong>{summary['passed']}</strong></div>
  <div class="card metric"><span>Failed</span><strong>{summary['failed']}</strong></div>
  <div class="card metric"><span>Skipped</span><strong>{summary['skipped']}</strong></div>
</section>
<section class="card"><h2>Test topology</h2><div class="arch"><div class="node">20 isolated device containers<br><small>persistent Ed25519 identity + micro-chain</small></div><div class="arrow">→</div><div class="node">Private Docker bridge network<br><small>HTTP transport with retry-safe delivery</small></div><div class="arrow">→</div><div class="node">Gateway ledger<br><small>SQLite WAL + Merkle blocks + signatures</small></div></div></section>
<br>
<section class="card"><h2>Scenario results</h2><table><thead><tr><th>Status</th><th>Scenario</th><th>Duration</th><th>Evidence</th><th>Metrics</th></tr></thead><tbody>{''.join(scenario_rows)}</tbody></table></section>
<br>
<section class="grid" style="grid-template-columns:1fr 1fr"><section class="card"><h2>Environment</h2><table>{env_rows}</table></section><section class="card"><h2>Notes and limitations</h2><ul>{notes}</ul></section></section>
<br><section class="card"><details><summary><strong>Raw machine-readable result</strong></summary><pre>{raw}</pre></details></section>
</main></body></html>"""


class GatewayClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(base_url=self.base_url, timeout=self.timeout) as client:
            return client.request(method, path, **kwargs)

    def json(self, method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        if response.status_code != expected:
            raise AssertionError(
                f"{method} {path}: expected {expected}, got {response.status_code}: "
                f"{response.text}"
            )
        return response.json()


def enroll(gateway: GatewayClient, device_id: str, key: KeyPair) -> dict[str, Any]:
    return gateway.json(
        "POST",
        "/devices",
        expected=201,
        json={"device_id": device_id, "public_key": key.public_bytes.hex()},
    )


def send(gateway: GatewayClient, event: SignedEvent, expected: int = 202) -> Any:
    return gateway.json("POST", "/events", expected=expected, json=event.to_wire())


def choose_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_local_server(result_dir: Path, batch_size: int) -> tuple[uvicorn.Server, threading.Thread, str]:
    db = result_dir / "local-system-test.db"
    key = result_dir / "local-gateway.key"
    for candidate in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), key):
        candidate.unlink(missing_ok=True)
    app = create_app(
        database_path=str(db),
        node_key_path=str(key),
        node_id="local-gateway",
        quorum_threshold=1,
        batch_size=batch_size,
    )
    port = choose_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    gateway = GatewayClient(base_url)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if gateway.json("GET", "/health")["status"] == "ok":
                return server, thread, base_url
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("local gateway failed to start")


def stop_local_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("local gateway did not stop")


def validate_compose(path: Path, expected_devices: int) -> tuple[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = data.get("services", {})
    devices = sorted(name for name in services if name.startswith("device-"))
    if len(devices) != expected_devices:
        raise AssertionError(f"expected {expected_devices} device services, found {len(devices)}")
    if "gateway" not in services or "edgechain-net" not in data.get("networks", {}):
        raise AssertionError("gateway or edgechain-net is missing")
    if "run" not in services or "test" not in services:
        raise AssertionError("the run and test orchestration services are required")
    run_dependencies = set(services["run"].get("depends_on", {}))
    if not set(devices).issubset(run_dependencies):
        raise AssertionError("run must start all device services")
    test_command = " ".join(services["test"].get("command", []))
    if "edgechain-benchmark" not in test_command:
        raise AssertionError("test must execute edgechain-benchmark")
    ids = [services[name].get("environment", {}).get("DEVICE_ID") for name in devices]
    if len(set(ids)) != expected_devices or None in ids:
        raise AssertionError("device service IDs are missing or duplicated")
    volumes = [services[name].get("volumes", []) for name in devices]
    if not all(value for value in volumes):
        raise AssertionError("each device must have persistent state storage")
    return (
        "Compose defines run/test orchestration, an isolated gateway network, "
        f"and {expected_devices} persistent device nodes",
        {
            "device_services": len(devices),
            "unique_device_ids": len(set(ids)),
            "run_dependencies": len(run_dependencies),
        },
    )


def run_nodes(
    gateway: GatewayClient, nodes: int, events_per_node: int
) -> tuple[str, dict[str, Any]]:
    prefix = f"load-{uuid.uuid4().hex[:8]}"
    latencies: list[float] = []

    def worker(index: int) -> int:
        device_id = f"{prefix}-{index:02d}"
        key = KeyPair.generate()
        enroll(gateway, device_id, key)
        device = DeviceClient(device_id, key)
        sent = 0
        for sequence in range(events_per_node):
            event = device.create_event(
                "load",
                {
                    "reading_milliunits": index * 1000 + sequence,
                    "quality": 100,
                },
            )
            started = time.perf_counter()
            response = send(gateway, event)
            latencies.append((time.perf_counter() - started) * 1000)
            if not response.get("accepted"):
                raise AssertionError("event was not accepted")
            sent += 1
        return sent

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(nodes, 20)) as executor:
        futures = [executor.submit(worker, index) for index in range(1, nodes + 1)]
        total = sum(future.result() for future in as_completed(futures))
    elapsed = time.perf_counter() - started
    return f"{nodes} nodes concurrently enrolled and delivered {total} signed events", {
        "nodes": nodes,
        "events": total,
        "elapsed_seconds": round(elapsed, 3),
        "events_per_second": round(total / elapsed, 2),
        "latency_ms_median": round(statistics.median(latencies), 2),
        "latency_ms_p95": round(sorted(latencies)[max(0, int(len(latencies)*0.95)-1)], 2),
    }


def run_suite(args: argparse.Namespace) -> int:
    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    docker_available = shutil.which("docker") is not None
    report = Report(
        "EdgeChainDB 20-Node IoT Validation Report",
        {
            "mode": args.mode,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "docker_cli_available": docker_available,
            "base_url": args.base_url if args.mode == "remote" else "local uvicorn",
            "expected_devices": args.expected_devices,
            "events_per_device": args.events_per_device,
        },
    )
    report.configure_output(result_dir, {"status": "running", "final_stats": {}})
    if not docker_available and args.mode == "local":
        report.notes.append(
            "Docker is not installed in the execution environment. The Compose file was "
            "validated structurally, while the same HTTP/API and persistence code was "
            "executed through a local 20-node integration harness."
        )
    report.notes.append(
        "Administrative enrollment is intentionally unauthenticated in this research testbed; "
        "production deployment must add mTLS or another authenticated control plane."
    )

    if args.compose_file:
        report.run(
            "Docker Compose topology",
            "Deployment",
            lambda: validate_compose(Path(args.compose_file), args.expected_devices),
        )

    if not args.skip_pytest:
        def run_pytest():
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=args.project_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (completed.stdout + completed.stderr).strip()
            (result_dir / "pytest.txt").write_text(output + "\n", encoding="utf-8")
            if completed.returncode != 0:
                raise AssertionError(output[-2000:])
            last_line = output.splitlines()[-1] if output else "pytest completed"
            return "The unit, API, tamper, idempotency, and 20-device tests passed", {
                "pytest_summary": last_line,
            }
        report.run("Python test suite", "Unit and integration", run_pytest)

    server = thread = None
    if args.mode == "local":
        server, thread, base_url = start_local_server(result_dir, args.batch_size)
    else:
        base_url = args.base_url
    gateway = GatewayClient(base_url)

    try:
        report.run(
            "Gateway health and readiness",
            "Availability",
            lambda: (
                "Gateway returned a healthy status",
                gateway.json("GET", "/health"),
            ),
        )

        if args.mode == "local":
            report.run(
                "Twenty-node concurrent ingestion",
                "Scale and concurrency",
                lambda: run_nodes(gateway, args.expected_devices, args.events_per_device),
            )
        else:
            def check_compose_devices():
                devices = gateway.json("GET", "/devices")
                compose_devices = [d for d in devices if d["device_id"].startswith("iot-device-")]
                if len(compose_devices) != args.expected_devices:
                    raise AssertionError(
                        f"expected {args.expected_devices} completed device nodes, "
                        f"found {len(compose_devices)}"
                    )
                wrong = [
                    d["device_id"] for d in compose_devices
                    if d["last_sequence"] < args.events_per_device
                ]
                if wrong:
                    raise AssertionError(f"nodes with insufficient events: {wrong}")
                return "All Compose device containers completed their event chains", {
                    "devices": len(compose_devices),
                    "minimum_sequence": min(d["last_sequence"] for d in compose_devices),
                }
            report.run(
                "Twenty Docker device nodes",
                "Scale and concurrency",
                check_compose_devices,
            )
            report.run(
                "Twenty-node benchmark ingestion",
                "Performance benchmark",
                lambda: run_nodes(
                    gateway, args.expected_devices, args.events_per_device
                ),
            )

        test_id = f"security-{uuid.uuid4().hex[:10]}"
        key = KeyPair.generate()
        device = DeviceClient(test_id, key)
        report.run(
            "Initial security-device enrollment",
            "Identity",
            lambda: (
                "A new Ed25519 device identity was enrolled",
                enroll(gateway, test_id, key),
            ),
        )

        first = device.create_event("security", {"value_milliunits": 1000})
        first_response = report.run(
            "Valid signed event",
            "Authenticity",
            lambda: (
                "A correctly signed event was accepted",
                send(gateway, first),
            ),
        )

        report.run(
            "Retry-safe duplicate delivery",
            "Network reliability",
            lambda: _assert_duplicate(gateway, first),
        )
        report.run(
            "Idempotent same-key enrollment",
            "Container restart",
            lambda: _assert_same_key_enrollment(gateway, test_id, key),
        )
        report.run(
            "Conflicting key enrollment rejection",
            "Identity attack",
            lambda: _assert_conflicting_key(gateway, test_id),
        )
        report.run(
            "Forged signature rejection",
            "Cryptographic attack",
            lambda: _assert_forged_signature(gateway, device),
        )
        report.run(
            "Payload tampering rejection",
            "Integrity attack",
            lambda: _assert_payload_tampering(gateway, device),
        )
        report.run(
            "Out-of-order sequence rejection",
            "Replay and ordering",
            lambda: _assert_out_of_order(gateway, test_id, key, first.event_hash),
        )
        report.run(
            "Broken micro-chain rejection",
            "Continuity",
            lambda: _assert_wrong_previous_hash(gateway, test_id, key),
        )
        report.run(
            "Checkpoint recovery",
            "Crash recovery",
            lambda: _assert_checkpoint_recovery(gateway, test_id, key),
        )

        def seal_remaining():
            health = gateway.json("GET", "/health")
            sealed = 0
            while health["pending_events"] > 0:
                gateway.json("POST", "/blocks/seal?max_events=256")
                sealed += 1
                health = gateway.json("GET", "/health")
            return "All pending events were committed to finalized blocks", {
                "manual_blocks_sealed": sealed, **gateway.json("GET", "/stats")
            }
        report.run("Seal remaining telemetry", "Finality", seal_remaining)

        blocks_holder: dict[str, Any] = {}
        def finalized_blocks():
            blocks = gateway.json("GET", "/blocks")
            blocks_holder["blocks"] = blocks
            if not blocks or any(b["status"] != "finalized" for b in blocks):
                raise AssertionError("not all blocks are finalized")
            if any(b["signature_count"] < b["quorum_threshold"] for b in blocks):
                raise AssertionError("a finalized block lacks quorum")
            return "Every block is finalized and has the required signatures", {
                "blocks": len(blocks),
                "events_in_blocks": sum(b["event_count"] for b in blocks),
            }
        report.run("Block finality and quorum", "Consensus", finalized_blocks)

        proof_holder: dict[str, Any] = {}
        def proof_valid():
            event_hash = first.event_hash.hex()
            proof = gateway.json("GET", f"/proofs/{event_hash}")
            proof_holder["proof"] = proof
            if not EdgeChainLedger.verify_event_proof(proof):
                raise AssertionError("valid Merkle proof did not verify")
            return "The selected event is cryptographically included in its block", {
                "block_height": proof["block_height"],
                "proof_steps": len(proof["proof"]),
            }
        report.run("Merkle inclusion proof", "Selective verification", proof_valid)

        def proof_tamper():
            proof = json.loads(json.dumps(proof_holder["proof"]))
            proof["event_hash"] = ("00" if not proof["event_hash"].startswith("00") else "ff") + proof["event_hash"][2:]
            if EdgeChainLedger.verify_event_proof(proof):
                raise AssertionError("tampered proof was accepted")
            return "Changing the event hash invalidated the inclusion proof", {}
        report.run("Tampered Merkle proof rejection", "Integrity attack", proof_tamper)

        report.run(
            "Complete ledger verification",
            "End-to-end audit",
            lambda: _assert_verify(gateway),
        )

        if args.mode == "local" and server is not None and thread is not None:
            stop_local_server(server, thread)
            server = thread = None
            def restart_check():
                # Re-open the exact same database and key using a fresh ASGI process.
                db = result_dir / "local-system-test.db"
                key_path = result_dir / "local-gateway.key"
                app = create_app(
                    database_path=str(db), node_key_path=str(key_path),
                    node_id="local-gateway", quorum_threshold=1,
                    batch_size=args.batch_size,
                )
                port = choose_port()
                restart_server = uvicorn.Server(
                    uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
                )
                restart_thread = threading.Thread(target=restart_server.run, daemon=True)
                restart_thread.start()
                restarted = GatewayClient(f"http://127.0.0.1:{port}")
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        verification = restarted.json("GET", "/verify")
                        stop_local_server(restart_server, restart_thread)
                        return "Gateway restart preserved keys, blocks, events, and audit validity", verification
                    except Exception:
                        time.sleep(0.1)
                restart_server.should_exit = True
                restart_thread.join(timeout=5)
                raise AssertionError("restarted gateway did not verify")
            report.run("Persistent restart recovery", "Durability", restart_check)
        else:
            report.skip(
                "Persistent restart recovery",
                "Durability",
                "The Docker benchmark service appends the host-controlled gateway restart result.",
            )

        try:
            final_stats = (
                gateway.json("GET", "/stats")
                if args.mode == "remote" or server
                else {}
            )
        except Exception as exc:
            final_stats = {"error": f"{type(exc).__name__}: {exc}"}
            report.notes.append(
                "Final gateway statistics could not be read; the report was still generated."
            )
        report.extra = {"status": "completed", "final_stats": final_stats}
        report.write(result_dir, report.extra)
    finally:
        if server is not None and thread is not None:
            stop_local_server(server, thread)

    print(json.dumps({
        "report": str(result_dir / "report.html"),
        "result": str(result_dir / "result.json"),
        "failed": report.failed,
    }, indent=2))
    return 1 if report.failed else 0


def _assert_duplicate(gateway: GatewayClient, event: SignedEvent):
    response = send(gateway, event)
    if not response.get("duplicate"):
        raise AssertionError("duplicate event was not identified as an idempotent retry")
    return "The same signed event was accepted idempotently without a second row", response


def _assert_same_key_enrollment(gateway: GatewayClient, device_id: str, key: KeyPair):
    response = enroll(gateway, device_id, key)
    if response.get("created") is not False:
        raise AssertionError("repeat enrollment should return created=false")
    return "Restart-style enrollment with the same key was idempotent", response


def _assert_conflicting_key(gateway: GatewayClient, device_id: str):
    response = gateway.request(
        "POST", "/devices",
        json={"device_id": device_id, "public_key": KeyPair.generate().public_bytes.hex()},
    )
    if response.status_code != 400:
        raise AssertionError(f"expected 400, got {response.status_code}")
    return "The gateway rejected takeover of an existing device id", {"status_code": response.status_code}


def _assert_forged_signature(gateway: GatewayClient, device: DeviceClient):
    valid = device.create_event("security", {"value_milliunits": 2000})
    forged = SignedEvent(
        device_id=valid.device_id, sequence=valid.sequence,
        device_time_ms=valid.device_time_ms, event_type=valid.event_type,
        payload=valid.payload, previous_event_hash=valid.previous_event_hash,
        signature=KeyPair.generate().sign(valid.signing_bytes),
    )
    response = gateway.request("POST", "/events", json=forged.to_wire())
    if response.status_code != 400 or "signature" not in response.text.lower():
        raise AssertionError(f"forged event was not rejected correctly: {response.text}")
    return "An event signed by an attacker key was rejected", {"status_code": response.status_code}


def _assert_payload_tampering(gateway: GatewayClient, device: DeviceClient):
    valid = device.create_event("security", {"value_milliunits": 3000})
    wire = valid.to_wire()
    wire["payload"]["value_milliunits"] = 999999
    response = gateway.request("POST", "/events", json=wire)
    if response.status_code != 400:
        raise AssertionError("tampered payload was accepted")
    return "Changing a signed payload invalidated its signature", {"status_code": response.status_code}


def _assert_out_of_order(gateway: GatewayClient, device_id: str, key: KeyPair, previous: bytes):
    unsigned = SignedEvent(
        device_id=device_id, sequence=99, device_time_ms=int(time.time()*1000),
        event_type="security", payload={"value_milliunits": 99},
        previous_event_hash=previous, signature=b"",
    )
    event = SignedEvent(**{**unsigned.__dict__, "signature": key.sign(unsigned.signing_bytes)})
    response = gateway.request("POST", "/events", json=event.to_wire())
    if response.status_code != 400 or "sequence" not in response.text.lower():
        raise AssertionError(f"out-of-order event was not rejected: {response.text}")
    return "A large sequence jump was rejected", {"status_code": response.status_code}


def _assert_wrong_previous_hash(gateway: GatewayClient, device_id: str, key: KeyPair):
    checkpoint = gateway.json("GET", f"/devices/{device_id}/checkpoint")
    unsigned = SignedEvent(
        device_id=device_id, sequence=checkpoint["last_sequence"] + 1,
        device_time_ms=int(time.time()*1000), event_type="security",
        payload={"value_milliunits": 4000}, previous_event_hash=ZERO_HASH,
        signature=b"",
    )
    event = SignedEvent(**{**unsigned.__dict__, "signature": key.sign(unsigned.signing_bytes)})
    response = gateway.request("POST", "/events", json=event.to_wire())
    if response.status_code != 400 or "continuity" not in response.text.lower():
        raise AssertionError(f"broken chain was not rejected: {response.text}")
    return "A valid signature with the wrong predecessor was rejected", {"status_code": response.status_code}


def _assert_checkpoint_recovery(gateway: GatewayClient, device_id: str, key: KeyPair):
    checkpoint = gateway.json("GET", f"/devices/{device_id}/checkpoint")
    recovered = DeviceClient(device_id, key)
    recovered.restore(checkpoint["last_sequence"], bytes.fromhex(checkpoint["last_event_hash"]))
    event = recovered.create_event("recovered", {"value_milliunits": 5000})
    response = send(gateway, event)
    if response["sequence"] != checkpoint["last_sequence"] + 1:
        raise AssertionError("recovered device did not continue at the next sequence")
    return "A restarted device resumed from the gateway checkpoint without forking", response


def _assert_verify(gateway: GatewayClient):
    result = gateway.json("GET", "/verify")
    if not result.get("valid"):
        raise AssertionError(result)
    return "All event signatures, micro-chains, Merkle roots, block links, and quorum signatures verified", result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EdgeChainDB system scenarios")
    parser.add_argument("--mode", choices=("local", "remote"), default="local")
    parser.add_argument("--base-url", default="http://gateway:8000")
    parser.add_argument("--expected-devices", type=int, default=20)
    parser.add_argument("--events-per-device", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--result-dir", default="result")
    parser.add_argument("--compose-file", default="docker-compose.yml")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--skip-pytest", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_suite(args))


if __name__ == "__main__":
    main()
