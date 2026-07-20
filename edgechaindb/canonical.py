from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import cbor2


class CanonicalEncodingError(ValueError):
    """Raised when a value cannot be encoded deterministically."""


def _validate(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str, bytes)):
        return
    if isinstance(value, float):
        raise CanonicalEncodingError(
            f"{path}: floating-point values are forbidden in signed payloads; "
            "use scaled integers such as milli-units"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalEncodingError(f"{path}: map keys must be strings")
            _validate(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        for index, item in enumerate(value):
            _validate(item, f"{path}[{index}]")
        return
    raise CanonicalEncodingError(
        f"{path}: unsupported deterministic value type {type(value).__name__}"
    )


def dumps(value: Any) -> bytes:
    """Encode a supported value using deterministic/canonical CBOR."""
    _validate(value)
    return cbor2.dumps(value, canonical=True)


def loads(data: bytes) -> Any:
    """Decode CBOR and reject trailing data or non-canonical encodings."""
    value = cbor2.loads(data)
    canonical = dumps(value)
    if canonical != data:
        raise CanonicalEncodingError("CBOR input is not in canonical form")
    return value
