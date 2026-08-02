from __future__ import annotations

import pytest

from edgechaindb.experiments.worker import _progress_timeout_exceeded


def test_timeout_is_based_on_inactivity_not_total_drain_time() -> None:
    """Long-running drains must stay alive while deliveries continue."""

    timeout_seconds = 600

    # The overall drain may already have run for much longer than 600 seconds,
    # but a delivery 599 seconds ago is still considered active progress.
    assert not _progress_timeout_exceeded(
        now=10_000.0,
        last_progress_at=9_401.0,
        timeout_seconds=timeout_seconds,
    )

    assert _progress_timeout_exceeded(
        now=10_001.0,
        last_progress_at=9_401.0,
        timeout_seconds=timeout_seconds,
    )


def test_timeout_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="at least one second"):
        _progress_timeout_exceeded(
            now=1.0,
            last_progress_at=0.0,
            timeout_seconds=0,
        )
