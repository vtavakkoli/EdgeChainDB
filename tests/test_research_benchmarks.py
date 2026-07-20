from pathlib import Path

from edgechaindb.benchmarks.byzantine_quorum import build_spec as byzantine_spec
from edgechaindb.benchmarks.common import write_benchmark_index
from edgechaindb.benchmarks.event_size import build_spec as event_size_spec
from edgechaindb.benchmarks.finalization_latency import build_spec as finalization_spec
from edgechaindb.benchmarks.gateway_ingest import build_spec as ingest_spec
from edgechaindb.benchmarks.integrity_detection import build_spec as integrity_spec
from edgechaindb.benchmarks.offline_reconnect import build_spec as offline_spec
from edgechaindb.benchmarks.signing_energy import build_spec as energy_spec
from edgechaindb.benchmarks.storage_overhead import build_spec as storage_spec
from edgechaindb.system_test import GatewayClient, start_local_server, stop_local_server


def test_research_benchmarks_write_separate_json_and_csv_files(tmp_path):
    server, thread, base_url = start_local_server(tmp_path, batch_size=10)
    gateway = GatewayClient(base_url)
    specs = [
        energy_spec(iterations=100, assumed_cpu_watts=10),
        event_size_spec(samples=10),
        ingest_spec(base_url, nodes=2, events_per_node=3),
        finalization_spec(gateway, samples=2),
        storage_spec(events=20, block_size=5),
        integrity_spec(replay_trials=4, deletion_trials=4),
        offline_spec(gateway, buffered_events=5),
        byzantine_spec(authority_sizes=(1, 3)),
    ]
    try:
        for spec in specs:
            details, metrics = spec.execute(tmp_path)
            assert details
            assert metrics["artifact"].endswith(f"{spec.slug}.json")
            assert (tmp_path / "benchmarks" / f"{spec.slug}.json").exists()
            assert (tmp_path / "benchmarks" / f"{spec.slug}.csv").exists()
        write_benchmark_index(tmp_path)
        assert (tmp_path / "benchmarks" / "summary.json").exists()
        assert (tmp_path / "benchmarks" / "report.html").exists()
    finally:
        stop_local_server(server, thread)
