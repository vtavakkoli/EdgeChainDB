from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class OutboxCorruptionError(RuntimeError):
    pass


class DurableOutbox:
    """Small durable FIFO for already-signed device events.

    Events are persisted before network delivery. A duplicate response is safe:
    the gateway classifies the same signed event as an idempotent retry and the
    device then acknowledges it locally.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OutboxCorruptionError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(value, list):
            raise OutboxCorruptionError("outbox root must be a JSON list")
        previous: int | None = None
        for item in value:
            if not isinstance(item, dict):
                raise OutboxCorruptionError("outbox entries must be JSON objects")
            try:
                sequence = int(item["sequence"])
                bytes.fromhex(str(item["event_hash"]))
                bytes.fromhex(str(item["previous_event_hash"]))
                bytes.fromhex(str(item["signature"]))
            except Exception as exc:
                raise OutboxCorruptionError(f"invalid outbox entry: {exc}") from exc
            if previous is not None and sequence != previous + 1:
                raise OutboxCorruptionError("outbox sequences are not contiguous")
            previous = sequence
            self._items.append(dict(item))

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._items, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items]

    def append(self, event: dict[str, Any]) -> None:
        value = dict(event)
        sequence = int(value["sequence"])
        if self._items and sequence != int(self._items[-1]["sequence"]) + 1:
            raise OutboxCorruptionError("new event does not continue the outbox sequence")
        self._items.append(value)
        self._persist()

    def acknowledge(self, sequence: int) -> int:
        before = len(self._items)
        self._items = [item for item in self._items if int(item["sequence"]) > sequence]
        removed = before - len(self._items)
        self._persist()
        return removed

    def reconcile(self, gateway_sequence: int) -> int:
        removed = self.acknowledge(gateway_sequence)
        if self._items and int(self._items[0]["sequence"]) != gateway_sequence + 1:
            raise OutboxCorruptionError(
                "outbox does not begin at the next gateway sequence"
            )
        return removed

    def latest(self) -> dict[str, Any] | None:
        return dict(self._items[-1]) if self._items else None
