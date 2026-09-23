# Changelog

All notable changes to EdgeChainDB are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning for published releases.

## [Unreleased]

### Fixed

- Included the repository Dockerfile in the runtime/test image so the containerized pytest suite can execute repository-structure tests from `/app` without `FileNotFoundError`.
- Changed experiment-worker timeout handling from a fixed post-generation drain deadline to a no-delivery-progress timeout. High-contention workers can now continue draining while events are actively delivered instead of being reported as failed solely because total drain time exceeded the configured window.
- Added a regression test covering the timeout boundary and invalid timeout configuration.

### Added

- Generic validation runner with adversarial tamper injection, matched unsigned SQLite baseline measurements, and offline durability/checkpoint-loss boundary experiments. The historical reviewer-validation command remains as a compatibility alias.
- Optional fail-closed durable outbox capacity enforcement with explicit overflow errors and regression coverage.
- CI smoke execution for the generic validation profile on the Python 3.12 reference runtime.
- Docker Compose `validation` profile for reproducible smoke and full validation runs.
- MIT license.
- GitHub Actions validation across Python 3.11, 3.12, and 3.13.
- Docker Compose configuration validation.
- Dependabot configuration for Python and GitHub Actions dependencies.
- Security policy, contribution guide, pull-request template, and citation metadata.

## [0.8.3] - 2026-07-21

### Added

- Dynamic Docker experiment matrix with balanced screening, outage injection, packet-loss simulation, resumable execution, and HTML/JSON/CSV reporting.
- Scalable SQLite-backed experiment outboxes and resource sampling.
- Tamper-evident device micro-chains, Merkle-rooted blocks, authority quorum finality, selective proofs, and SQLite WAL persistence.
