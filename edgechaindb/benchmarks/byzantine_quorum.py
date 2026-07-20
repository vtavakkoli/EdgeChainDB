from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from ..crypto import KeyPair
from ..device import DeviceClient
from ..ledger import EdgeChainLedger
from ..store import Database
from .common import BenchmarkSpec


def build_spec(*, authority_sizes: tuple[int, ...] = (1, 3, 4, 7)) -> BenchmarkSpec:
    def run() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        passed = 0
        total = 0
        with tempfile.TemporaryDirectory(prefix="edgechain-byzantine-") as raw_dir:
            root = Path(raw_dir)
            for authorities_count in authority_sizes:
                for quorum in range(1, authorities_count + 1):
                    for byzantine_count in range(0, authorities_count):
                        total += 1
                        db = Database(root / f"n{authorities_count}-q{quorum}-f{byzantine_count}.db")
                        ledger = EdgeChainLedger(db, quorum_threshold=quorum)
                        authorities: list[tuple[str, KeyPair]] = []
                        for index in range(authorities_count):
                            authority_id = f"authority-{index + 1}"
                            key = KeyPair.generate()
                            ledger.register_authority(authority_id, key.public_bytes)
                            authorities.append((authority_id, key))
                        device_key = KeyPair.generate()
                        ledger.register_device("device", device_key.public_bytes)
                        device = DeviceClient("device", device_key)
                        ledger.accept_event(device.create_event("quorum", {"value": total}))

                        proposer_id, proposer_key = authorities[0]
                        proposal = ledger.propose_block(
                            proposer_id, proposer_key.private_key, max_events=1
                        )
                        invalid_rejected = 0
                        byzantine_ids = {
                            authority_id
                            for authority_id, _ in authorities[-byzantine_count:]
                        } if byzantine_count else set()
                        # The proposer remains honest so every configuration can create a proposal.
                        byzantine_ids.discard(proposer_id)
                        for authority_id in sorted(byzantine_ids):
                            try:
                                ledger.add_external_signature(1, authority_id, b"\x00" * 64)
                            except ValueError:
                                invalid_rejected += 1
                        status = proposal["status"]
                        for authority_id, key in authorities[1:]:
                            if authority_id in byzantine_ids:
                                continue
                            status = ledger.sign_block(1, authority_id, key.private_key)
                        honest_signers = authorities_count - len(byzantine_ids)
                        expected_finalized = honest_signers >= quorum
                        actual_finalized = status == "finalized"
                        case_passed = (
                            actual_finalized == expected_finalized
                            and invalid_rejected == len(byzantine_ids)
                        )
                        passed += int(case_passed)
                        rows.append(
                            {
                                "authorities": authorities_count,
                                "quorum": quorum,
                                "configured_byzantine": byzantine_count,
                                "effective_byzantine_non_proposers": len(byzantine_ids),
                                "honest_signers": honest_signers,
                                "withholding_tolerance": authorities_count - quorum,
                                "expected_finalized": expected_finalized,
                                "actual_finalized": actual_finalized,
                                "invalid_signatures_rejected": invalid_rejected,
                                "case_passed": case_passed,
                            }
                        )
        metrics = {
            "authority_sizes": list(authority_sizes),
            "configurations": total,
            "configurations_passed": passed,
            "configuration_pass_rate": round(passed / total, 4),
            "invalid_signatures_are_counted": False,
            "liveness_rule": "finalizes when honest/available signatures >= quorum",
            "maximum_withholding_tolerance": max(authority_sizes) - 1,
        }
        if passed != total:
            raise AssertionError(metrics)
        return {
            "details": "Validated invalid-signature rejection and quorum liveness across authority and withholding configurations",
            "metrics": metrics,
            "notes": [
                "This is threshold-signature quorum testing, not a proof of asynchronous BFT consensus safety. "
                "Withholding tolerance for liveness is n-q; invalid signatures never count toward quorum."
            ],
            "rows": rows,
        }

    return BenchmarkSpec("Byzantine authority tolerance by quorum", "Consensus resilience", "byzantine_quorum", run)
