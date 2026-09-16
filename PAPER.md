# EdgeChainDB paper

The research paper describing and evaluating EdgeChainDB has been accepted at **CloudCom 2026**.

## Reference

**Kabeh Mohsenzadegan, Vahid Tavakkoli, and Kyandoghere Kyamakya.**  
**“EdgeChainDB: A Continuity-Aware Quorum-Signed Edge Ledger for Resilient IoT Telemetry.”**  
Accepted at the **2026 IEEE International Conference on Cloud Computing Technology and Science (CloudCom 2026)**.

The paper presents the continuity-aware EdgeChainDB architecture and evaluates device-origin signatures, hash-linked device micro-chains, Merkle-rooted gateway blocks, authority-threshold finality, SQLite WAL persistence, durable offline outboxes, selective membership proofs, and full-ledger verification.

In the reported evaluation, all **180 canonical runs** completed successfully, with **499,950 events delivered and finalized** and every full-ledger verification and SQLite integrity check passing.

## BibTeX

```bibtex
@inproceedings{mohsenzadegan2026edgechaindb,
  author    = {Kabeh Mohsenzadegan and Vahid Tavakkoli and Kyandoghere Kyamakya},
  title     = {EdgeChainDB: A Continuity-Aware Quorum-Signed Edge Ledger for Resilient IoT Telemetry},
  booktitle = {2026 IEEE International Conference on Cloud Computing Technology and Science (CloudCom)},
  year      = {2026},
  note      = {Accepted for publication},
  url       = {https://github.com/vtavakkoli/EdgeChainDB}
}
```

The DOI, page numbers, and final IEEE Xplore bibliographic metadata should be added here after publication.

## Repository

Source code and reproducible experiment tooling:

https://github.com/vtavakkoli/EdgeChainDB
