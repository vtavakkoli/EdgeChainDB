from edgechaindb.system_test import _verification_timeout_seconds


class _Gateway:
    def __init__(self, events: int, blocks: int):
        self.events = events
        self.blocks = blocks

    def json(self, method: str, path: str):
        assert method == "GET"
        assert path == "/stats"
        return {"events": self.events, "blocks": self.blocks}


def test_large_persistent_ledger_gets_adaptive_verification_timeout():
    timeout = _verification_timeout_seconds(_Gateway(375_136, 15_050))
    assert timeout > 600
    assert timeout <= 1800


def test_small_ledger_keeps_safe_minimum_timeout():
    assert _verification_timeout_seconds(_Gateway(622, 36)) == 60.0
