# Contributing to EdgeChainDB

Thank you for helping improve EdgeChainDB. Contributions should preserve the project's integrity guarantees, deterministic data model, and reproducible experiment workflow.

## Development setup

EdgeChainDB requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the validation suite before opening a pull request:

```bash
python -m compileall -q edgechaindb tests
python -m pytest

docker compose config --quiet
```

## Contribution workflow

1. Create a focused branch from the current `main` branch.
2. Keep changes small enough to review and test independently.
3. Add or update tests for every behavior change and bug fix.
4. Update documentation when commands, configuration, metrics, or security assumptions change.
5. Use clear commit messages that describe the observable change.
6. Open a pull request using the repository template and describe the root cause, solution, and validation performed.

## Engineering expectations

- Preserve deterministic CBOR, cryptographic domain separation, sequence continuity, idempotency, and transactional database behavior.
- Never weaken signature, quorum, Merkle-proof, or full-ledger verification checks to improve benchmark results.
- Treat experiment timeouts as liveness controls. They must not classify an actively progressing worker as failed.
- Keep generated benchmark artifacts out of source-control changes unless they are intentionally included as reproducible evidence.
- Avoid claims of Byzantine-fault tolerance, production readiness, or measured hardware energy unless the implementation and evidence directly support them.

## Tests

Unit tests should be deterministic and fast. Integration or Docker tests should document their resource requirements and provide actionable failure output. A regression test is required when correcting a reported defect.

## Security

Do not report vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md) for private reporting.

## License

By contributing, you agree that your contributions will be licensed under the repository's MIT License.
