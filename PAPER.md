# EdgeChainDB paper

The research paper describing and evaluating EdgeChainDB has been published in the proceedings of **IEEE CloudCom 2026**.

## Reference

K. Mohsenzadegan, V. Tavakkoli, and K. Kyamakya, “EdgeChainDB: A Continuity-Aware Quorum-Signed Edge Ledger for Resilient IoT Telemetry,” *2026 IEEE 17th International Conference on Cloud Computing Technology and Science (CloudCom)*, Paris, France, 2026, pp. 516–523, doi: [10.1109/CloudCom71355.2026.00074](https://doi.org/10.1109/CloudCom71355.2026.00074).

The paper presents the continuity-aware EdgeChainDB architecture and evaluates device-origin signatures, hash-linked device micro-chains, Merkle-rooted gateway blocks, authority-threshold finality, SQLite WAL persistence, durable offline outboxes, selective membership proofs, and full-ledger verification.

In the reported evaluation, all **180 canonical runs** completed successfully, with **499,950 events delivered and finalized** and every full-ledger verification and SQLite integrity check passing.

## BibTeX

```bibtex
@inproceedings{mohsenzadegan2026edgechaindb,
  author    = {Kabeh Mohsenzadegan and Vahid Tavakkoli and Kyandoghere Kyamakya},
  title     = {EdgeChainDB: A Continuity-Aware Quorum-Signed Edge Ledger for Resilient IoT Telemetry},
  booktitle = {2026 IEEE 17th International Conference on Cloud Computing Technology and Science (CloudCom)},
  year      = {2026},
  pages     = {516--523},
  address   = {Paris, France},
  publisher = {IEEE},
  doi       = {10.1109/CloudCom71355.2026.00074},
  url       = {https://doi.org/10.1109/CloudCom71355.2026.00074}
}
```

**Keywords (as supplied with the IEEE citation):** Internet of Things; Timing; Information rates; Printing; Throughput; Radio access networks; Regional area networks; Databases; Poles and zeros; Sequences; edge computing; tamper-evident database; permissioned ledger; Merkle tree; Ed25519; SQLite; fault injection.

## Repository

Source code and reproducible experiment tooling:

https://github.com/vtavakkoli/EdgeChainDB
