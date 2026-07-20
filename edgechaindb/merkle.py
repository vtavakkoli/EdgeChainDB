from __future__ import annotations

from dataclasses import dataclass

from .crypto import sha256


EMPTY_ROOT = sha256(b"\x02edgechaindb-empty-merkle-tree")


def _leaf(value: bytes) -> bytes:
    return sha256(b"\x00" + value)


def _node(left: bytes, right: bytes) -> bytes:
    return sha256(b"\x01" + left + right)


def root(values: list[bytes]) -> bytes:
    if not values:
        return EMPTY_ROOT
    level = [_leaf(value) for value in values]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


@dataclass(frozen=True)
class ProofStep:
    side: str
    sibling: bytes

    def to_wire(self) -> dict[str, str]:
        return {"side": self.side, "sibling": self.sibling.hex()}


def proof(values: list[bytes], index: int) -> list[ProofStep]:
    if index < 0 or index >= len(values):
        raise IndexError("Merkle proof index out of range")

    level = [_leaf(value) for value in values]
    position = index
    result: list[ProofStep] = []

    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])

        if position % 2 == 0:
            result.append(ProofStep("right", level[position + 1]))
        else:
            result.append(ProofStep("left", level[position - 1]))

        position //= 2
        level = [_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]

    return result


def verify(value: bytes, steps: list[ProofStep], expected_root: bytes) -> bool:
    current = _leaf(value)
    for step in steps:
        if step.side == "left":
            current = _node(step.sibling, current)
        elif step.side == "right":
            current = _node(current, step.sibling)
        else:
            return False
    return current == expected_root
