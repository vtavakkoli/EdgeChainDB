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

## Run the API

```bash
edgechain-api --database edgechain.db --host 127.0.0.1 --port 8000
```

Open the generated API documentation at `/docs`.

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

Version 0.2 adds a complete distributed testbed:

- one gateway container with persistent SQLite WAL storage;
- twenty independent device containers;
- a private `edgechain-iot-net` bridge network;
- a separate persistent key and chain checkpoint for every device;
- concurrent signed event delivery with retries and startup jitter;
- idempotent enrollment and retry-safe event ingestion;
- an automated attack, recovery, scale, Merkle-proof, and audit suite;
- an HTML report in `result/report.html`.

### Linux/macOS

```bash
./scripts/run_docker_tests.sh
```

### Windows PowerShell

```powershell
./scripts/run_docker_tests.ps1
```

The scripts build the image, start the gateway, run all 20 device containers,
execute the Python test suite and distributed scenarios, restart the gateway,
verify the persisted ledger, append the Docker restart result to the HTML report,
and save logs and reports under `result/`.

You can also start the topology manually:

```bash
docker compose build
docker compose up -d gateway
docker compose up device-01 device-02 device-03 device-04 device-05 \
  device-06 device-07 device-08 device-09 device-10 \
  device-11 device-12 device-13 device-14 device-15 \
  device-16 device-17 device-18 device-19 device-20
docker compose --profile test run --rm test-runner
```

### Run the equivalent local integration suite

This is useful on a machine without Docker. It starts the real FastAPI gateway,
creates 20 concurrent device clients, executes the same protocol and attack
scenarios, performs a process restart, and creates the HTML report.

```bash
edgechain-system-test --mode local --expected-devices 20 \
  --events-per-device 8 --result-dir result
```

### Scenarios covered

The generated report includes structural Compose validation, 20-node concurrent
ingestion, identity conflict prevention, forged signatures, signed-payload
tampering, replay-safe retries, out-of-order messages, broken chain links,
checkpoint recovery, automatic and manual block sealing, quorum finality,
valid and altered Merkle proofs, complete ledger audit, and persistent restart
recovery.
