# EdgeChainDB

EdgeChainDB is a Python prototype for a **tamper-evident IoT database**. It avoids proof-of-work and instead combines:

1. **Device micro-chains** — every device signs each event and links it to its previous event.
2. **Gateway macro-chain** — verified events are grouped into Merkle blocks.
3. **Authority quorum** — a block becomes final only after the configured number of authorities sign it.
4. **Queryable SQLite storage** — telemetry remains easy to query while cryptographic verification detects changes.
5. **Selective proofs** — one event can be proven to belong to a block without disclosing the whole block.
6. **Policy commitment** — each block commits to the active validation and batching policy.

This combination is intended for factories, smart buildings, energy systems, fleets, and municipal IoT. It is a research-quality prototype, not a claim that the architecture is patent-new and not yet a production security product.

## Why this design fits IoT

A public proof-of-work blockchain is normally a poor fit for small devices: it adds latency, energy consumption, and operational complexity. EdgeChainDB keeps signing on the device lightweight and moves batching, storage, quorum finality, and auditing to gateways or infrastructure nodes.

The device chain detects:

- replayed messages;
- duplicated sequence numbers;
- reordered messages;
- missing continuity;
- forged telemetry.

The gateway chain detects:

- deleted or altered database rows;
- changed block ordering;
- altered event membership;
- insufficient authority approval.

## Architecture

```text
IoT device
  └─ signed event #1 → signed event #2 → signed event #3
                         device micro-chain
                                  │
                                  ▼
                       validating edge gateway
                                  │
                    verified pending event pool
                                  │
                                  ▼
     Merkle block N-1 ← Merkle block N ← Merkle block N+1
                                  │
                         2-of-3 authority quorum
                                  │
                                  ▼
                      finalized queryable ledger
```

## Security choices

- Ed25519 signatures for devices and authorities.
- SHA-256 domain-separated Merkle tree.
- Deterministic CBOR for all signed and hashed structures.
- Integer sensor units rather than floating-point values, for example
  `temperature_milli_celsius: 23650`.
- Atomic SQLite transactions and WAL mode.
- Immutable authority snapshots inside each block.
- Exact per-device sequence and previous-event-hash validation.

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Run the demonstration

```bash
edgechain-demo --database demo.db --events 12
```

The demo creates three authorities, requires a 2-of-3 quorum, enrolls two devices, generates signed events, finalizes blocks, verifies the complete database, and verifies a Merkle inclusion proof.

## Run the API and monitor

```bash
python -m edgechaindb.gateway_server \
  --database edgechain.db \
  --node-key edgechain-node.key \
  --host 127.0.0.1 \
  --api-port 8000 \
  --monitor-port 3030
```

Open the API documentation at `http://127.0.0.1:8000/docs` and the live monitor at
`http://127.0.0.1:3030`. The installed `edgechain-*` console commands remain
convenience aliases, but Docker Compose deliberately uses `python -m ...` so a
missing generated console executable cannot prevent container startup.

The API node creates a persistent Ed25519 authority key in
`edgechain-node.key`. Its default development quorum is one. Multi-node
deployments should enroll separate authority public keys and use a threshold
greater than one.

## Minimal Python usage

```python
from edgechaindb.crypto import KeyPair
from edgechaindb.device import DeviceClient
from edgechaindb.ledger import EdgeChainLedger
from edgechaindb.store import Database

db = Database("factory.db")
ledger = EdgeChainLedger(db, quorum_threshold=1)

authority = KeyPair.generate()
ledger.register_authority("factory-gateway-a", authority.public_bytes)

device_key = KeyPair.generate()
ledger.register_device("temperature-01", device_key.public_bytes)

device = DeviceClient("temperature-01", device_key)
event = device.create_event(
    event_type="temperature",
    payload={"temperature_milli_celsius": 23650},
)
ledger.accept_event(event)

block = ledger.propose_block("factory-gateway-a", authority.private_key)
print(block["status"])
print(ledger.verify_all())
```

## REST event format

```json
{
  "device_id": "temperature-01",
  "sequence": 1,
  "device_time_ms": 1784548800000,
  "event_type": "temperature",
  "payload": {
    "temperature_milli_celsius": 23650
  },
  "previous_event_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "signature": "hex-encoded-ed25519-signature"
}
```

## MQTT integration

MQTT should be used as the transport, not as the source of truth. A production
adapter can subscribe to a topic such as:

```text
edgechain/v1/events/{device_id}
```

The MQTT payload should be the deterministic CBOR representation of the event.
The gateway must verify the signature and continuity before acknowledging or
persisting the event. MQTT QoS does not replace replay protection; duplicate
delivery is expected and is handled by the event hash and sequence constraints.

## Suggested research contribution

A defensible paper contribution could be evaluated as:

**Continuity-Aware Quorum Ledger for Intermittently Connected IoT**

The testable hypothesis is that the dual micro/macro-chain detects dropped,
replayed, and gateway-tampered telemetry with lower device energy and lower
finalization latency than proof-of-work or per-event distributed consensus.

Measure:

- signing energy per event;
- bytes per event;
- gateway ingest throughput;
- block finalization latency;
- storage overhead;
- replay and deletion detection rate;
- behavior under offline buffering and reconnection;
- Byzantine authority tolerance for different quorum sizes.

Do not describe the system as scientifically novel until a structured
literature and patent search has been completed.

## Production work still required

- Mutual TLS and authenticated administrative enrollment.
- Hardware-backed device keys or secure elements.
- Key rotation and revocation as ledger-governed events.
- Real networked quorum protocol with authenticated peer transport.
- Crash recovery for stale proposed blocks.
- Rate limiting, quotas, observability, and backup procedures.
- Privacy retention policies and encryption of sensitive payloads.
- External checkpoint anchoring.
- Independent security review and fuzz testing.


## Docker Compose: 20 isolated IoT nodes

Version 0.6 provides a complete distributed, observable, and research-benchmark testbed:

- one gateway container with persistent SQLite WAL storage;
- twenty continuously running, independently controlled device containers;
- a private `edgechain-iot-net` bridge network;
- a persistent key and chain checkpoint for every device;
- a live operations dashboard on port `3030` for state, telemetry values, container logs, resource usage, and node controls;
- automated attack, recovery, scale, Merkle-proof, and audit scenarios;
- fail-safe JSON and HTML reports under `result/`.

The exact `run` and `test` commands are documented below. The helper scripts
`./scripts/run_docker_tests.sh` and `./scripts/run_docker_tests.ps1` execute the
same benchmark workflow and wait for its completion.

### Run the equivalent local integration suite

This is useful on a machine without Docker. It starts the real FastAPI gateway,
creates 20 concurrent device clients, executes the same protocol and attack
scenarios, performs a process restart, and creates the HTML report.

```bash
edgechain-system-test --mode local --expected-devices 20 \
  --events-per-device 8 --result-dir result
```

### Scenarios covered

The generated system report includes structural Compose validation, 20-node
concurrent ingestion, identity conflict prevention, forged signatures, signed-
payload tampering, replay-safe retries, out-of-order messages, broken chain
links, checkpoint recovery, automatic and manual block sealing, quorum finality,
valid and altered Merkle proofs, complete ledger audit, and persistent restart
recovery.

Version 0.6 also executes eight research benchmarks:

1. Ed25519 signing energy per event, using Linux RAPL when available and an
   explicitly labelled CPU-time/power estimate otherwise.
2. Canonical payload, signature, wire JSON, and logical storage bytes per event.
3. Concurrent gateway ingest throughput with p50, p95, and p99 latency.
4. Event-to-quorum-finality latency.
5. Incremental SQLite, index, block, and signature storage overhead.
6. Replay and destructive deletion detection rates.
7. Durable offline outbox buffering, ordered reconnection, and idempotent retry.
8. Invalid-signature rejection and liveness across multiple authority/quorum
   configurations. This is threshold-quorum testing, not a proof of general
   asynchronous Byzantine consensus.

## Docker workflows and live cluster dashboard

The Compose topology now exposes two explicit workflows.

### 1. Start the complete running cluster

```bash
docker compose up -d run
```

The equivalent legacy spelling is:

```bash
docker-compose up -d run
```

This starts:

- the persistent gateway;
- 20 continuously running IoT device containers;
- the `run` coordinator that keeps the topology active.

Open the dedicated network operations dashboard at:

```text
http://127.0.0.1:3030
```

Port `3030` is now a real second listener inside the gateway container. Port
`8000` remains the device/API listener. Both listeners share the same in-process
ledger, locks, database object, and Docker controller; port `3030` is no longer a
host-side alias to port `8000`.

The dashboard shows:

- gateway, run, test, and all 20 device container states;
- live temperature, humidity, battery, quality, sequence, finality, and clock lag;
- per-container CPU, memory, RX/TX bytes, IP address, restart count, and health;
- the private Docker network, subnet, gateway, and connected containers;
- recent signed telemetry and the current benchmark phase;
- selectable logs for `gateway`, `run`, `test`, and every device;
- start, stop, restart, pause, and resume controls for one or all devices.

The API documentation remains available at `http://127.0.0.1:8000/docs`, and
the same dashboard is also reachable at `http://127.0.0.1:8000/dashboard`.

The gateway intentionally runs as root only for access to the local Docker
socket used by the development dashboard. All capabilities are dropped except
`DAC_OVERRIDE`, which is required to write the persistent `/data` volume that
is initialized for UID 1000. Do not expose this development control plane to a
public network.

#### Upgrade or repair the dashboard listener

An older container can keep the previous port mapping even after the Compose
file changes. Rebuild and force-recreate the gateway and run coordinator:

```bash
docker compose up -d --build --force-recreate gateway run
```

Then run the built-in diagnosis:

```powershell
.\scripts\diagnose_dashboard.ps1
```

or on Linux/macOS:

```bash
./scripts/diagnose_dashboard.sh
```

The diagnostic verifies the published port, API health, monitor health,
dashboard HTML marker, SQLite WAL mode, `PRAGMA quick_check`, and recent gateway
logs. Add `-Repair` on PowerShell or `--repair` on shell to rebuild first.

The existing `gateway-data` volume is preserved. Do not run `down -v` unless
you intentionally want to erase the ledger and all device checkpoints.

### 2. Run the complete Docker benchmark

```bash
docker compose up -d test
```

The `test` service waits until all 20 devices have generated telemetry, stops
them to create a stable benchmark window, runs unit/integration/security/scale
scenarios, restarts the gateway, verifies persistent recovery, writes the
report, and resumes the devices.

Follow its structured JSON progress with:

```bash
docker compose logs -f test
```

All services use Docker's rotating `local` logging driver with five 10 MB log
files. The dashboard can display those logs without shell access. After a
benchmark, individual service logs are also copied into `result/logs/`.

Generated files:

```text
result/report.html                         # complete system validation
result/result.json                         # machine-readable system result
result/pytest.txt
result/docker-compose.log
result/benchmark-status.json
result/logs/gateway.log
result/logs/device-01.log
result/logs/test.log
result/benchmarks/report.html              # research benchmark dashboard
result/benchmarks/summary.json
result/benchmarks/signing_energy.{json,csv}
result/benchmarks/bytes_per_event.{json,csv}
result/benchmarks/gateway_ingest_throughput.{json,csv}
result/benchmarks/block_finalization_latency.{json,csv}
result/benchmarks/storage_overhead.{json,csv}
result/benchmarks/integrity_detection.{json,csv}
result/benchmarks/offline_reconnection.{json,csv}
result/benchmarks/byzantine_quorum.{json,csv}
```

The monitor serves the research summary at
`http://127.0.0.1:3030/benchmark/research/summary` and the HTML report at
`http://127.0.0.1:3030/benchmark/research/report`.

The latest completed report is also linked from the dashboard and served at
`http://127.0.0.1:3030/benchmark/report`.


### Container entry-point fix

Older packages started services through generated commands such as
`edgechain-gateway`. If a cached image was built before that console script was
installed, OCI startup failed with `executable file not found in $PATH`. Version
0.6 removes that dependency from Compose:

```yaml
command: ["python", "-m", "edgechaindb.gateway_server", ...]
```

The gateway, devices, run coordinator, and test runner all use importable Python
modules. The Compose image is explicitly tagged `edgechaindb:0.8.0`, and the
Docker build executes module smoke checks. Therefore a normal first run builds
the new image instead of silently reusing the older broken command.

For a clean upgrade that preserves all volumes:

```bash
docker compose down --remove-orphans
docker compose up -d --build --force-recreate run
```

Do not add `-v` unless the ledger and every device identity should be erased.

### Durable offline operation

Each device now writes a signed event to `/data/outbox.json` before attempting
network delivery. While the gateway is unavailable, the device continues to
create a bounded, hash-linked local stream. After reconnection it enrolls or
checks its checkpoint, removes events already accepted by the gateway, and
replays the remaining FIFO in sequence. A crash after remote acceptance but
before local acknowledgement is safe because the gateway returns the same event
as an idempotent duplicate. `DEVICE_MAX_BUFFERED_EVENTS` bounds disk growth.

### Database architecture

EdgeChainDB is not a peer-to-peer cryptocurrency blockchain. It is an
edge-gateway telemetry database built on SQLite WAL with cryptographic ledger
semantics:

- every device has an Ed25519 identity and signs each event;
- every device stream is a strict sequence-linked micro-chain;
- accepted events are stored transactionally and delivered idempotently;
- pending events are grouped into Merkle-rooted blocks;
- block headers link to the previous finalized block and capture the authority
  set, quorum threshold, and policy hash;
- authorities sign blocks until quorum finality is reached;
- the verifier rechecks signatures, sequence continuity, Merkle roots, block
  links, authority snapshots, and quorum signatures;
- SQLite WAL, busy timeouts, foreign keys, and persistent Docker volumes provide
  local crash recovery and durable storage.

The dashboard exposes database size, WAL size, journal mode, schema version,
row counts, and integrity features. A deeper check is available at:

```text
http://127.0.0.1:3030/database/info?quick_check=true
```

This design is strong for a research prototype, industrial edge gateway, or
single-site auditable telemetry store. It is not yet a horizontally replicated
multi-gateway database: production use still needs authenticated enrollment,
TLS/mTLS, secret management, authorization, backup/restore, schema migrations,
rate limits, multi-authority deployment, and replicated failover.

### Fixed restart-verification timeout

Full ledger verification validates every signature, device micro-chain, Merkle
root, block link, and quorum signature. On a ledger with roughly 9,600 events,
that verification took about 13 seconds. The previous recovery checker used a
fixed five-second HTTP read timeout and therefore reported a false `ReadTimeout`
after a successful gateway restart. Version 0.8 waits for gateway health first and assigns a ledger-size-aware verification timeout of 60 to 1,800 seconds.

### Why the previous PowerShell script failed

The device containers are one-shot in the old test workflow. A successful
container could finish before this command ran:

```powershell
docker compose ps -q device-02
```

By default, `docker compose ps -q` returns only running containers. Therefore,
`device-02` existed and exited successfully, but the script interpreted the
empty lookup as “No container found.” The corrected scripts query the one-shot
`test` container with `--all`, and the benchmark no longer performs a fragile
per-device container lookup.

### Development security note

The local dashboard controls containers through the mounted Docker socket. The
published port is therefore restricted to `127.0.0.1`. Treat this as a local
research/development control plane only. Do not expose it to a network or the
Internet, and replace Docker-socket access with an authenticated orchestrator
before production use.

## Version 0.7: dynamic full-factorial Docker experiments

Version 0.7 adds a Docker-socket experiment controller that provisions only the containers required by each experimental case. It supports the complete matrix in `experiments/full-matrix.yaml`, the preferred ten-repetition plan, and a small smoke plan.

Generate the complete plan without starting workload containers:

```bash
docker compose --profile experiment run --rm experiment \
  python -m edgechaindb.experiments.runner \
  --config /app/experiments/full-matrix.yaml \
  --result-dir /result/experiments --dry-run
```

Run a smoke campaign:

```bash
CONFIG=smoke.yaml MAX_RUNS=4 ./scripts/run_experiments.sh
```

Run or resume the full five-repetition matrix:

```bash
./scripts/run_experiments.sh
```

A full campaign contains 24,000 runs and 6.666 billion nominal events. Use deterministic shards for practical execution:

```bash
SHARD_COUNT=8 SHARD_INDEX=0 ./scripts/run_experiments.sh
```

Merge independently generated shard reports:

```bash
./scripts/merge_experiments.sh
```

The dashboard exposes the resulting report at `http://127.0.0.1:3030/experiments/report` and the plan at `/experiments/plan`.

See `docs/EXPERIMENTAL_PROTOCOL.md`, `docs/SCALING_AND_SHARDING.md`, and `docs/REPORTING_SCHEMA.md` for the complete methodology and artifact definitions.

## One-command complete experimental campaign

Run the complete five-repetition matrix as a detached, resumable Docker service:

```bash
docker compose up --build -d experiment
```

The commonly typed service alias `experment` is also accepted. Do not start both aliases together; a campaign lock prevents concurrent writes. Follow progress with `docker compose logs -f experiment`. Comprehensive artifacts are continuously written to `result/experiments/`, including `report.html`, `summary.json`, `results.csv`, factor-level JSON/CSV files, raw run evidence, and `progress.json`. Re-running the command resumes passed runs and retries failed runs.
