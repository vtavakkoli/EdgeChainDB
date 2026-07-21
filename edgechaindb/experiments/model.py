from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Iterable

import yaml


@dataclass(frozen=True)
class AuthorityThreshold:
    authorities: int
    threshold: int

    @property
    def label(self) -> str:
        return f"{self.threshold}-of-{self.authorities}"

    def validate(self) -> None:
        if self.authorities < 1:
            raise ValueError("authorities must be at least one")
        if not 1 <= self.threshold <= self.authorities:
            raise ValueError(
                f"threshold {self.threshold} must be between 1 and {self.authorities}"
            )


@dataclass(frozen=True)
class ExperimentCase:
    devices: int
    events: int
    block_size: int
    authorities: int
    threshold: int
    packet_loss_percent: float
    outage_seconds: int
    repetition: int

    @property
    def quorum(self) -> str:
        return f"{self.threshold}-of-{self.authorities}"

    @property
    def case_key(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def run_id(self) -> str:
        return (
            f"d{self.devices}-e{self.events}-b{self.block_size}-"
            f"q{self.threshold}of{self.authorities}-p{self.packet_loss_percent:g}-"
            f"o{self.outage_seconds}-r{self.repetition}-{self.case_key[:8]}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "quorum": self.quorum, "case_key": self.case_key, "run_id": self.run_id}


@dataclass(frozen=True)
class ExecutionSettings:
    image: str = "edgechaindb:0.7.0"
    packet_loss_mode: str = "netem"
    gateway_start_timeout_seconds: int = 120
    run_timeout_seconds: int = 7200
    reconnect_grace_seconds: int = 300
    cleanup_successful_runs: bool = True
    retain_failed_containers: bool = False
    collect_success_logs: bool = False
    max_latency_samples_per_device: int = 10_000
    worker_generation_batch: int = 250

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "ExecutionSettings":
        value = value or {}
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value[key] for key in value if key in known})


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    repetitions: int
    devices: tuple[int, ...]
    events: tuple[int, ...]
    block_sizes: tuple[int, ...]
    authority_thresholds: tuple[AuthorityThreshold, ...]
    packet_loss_percent: tuple[float, ...]
    outage_seconds: tuple[int, ...]
    execution: ExecutionSettings

    def validate(self) -> None:
        if self.repetitions < 1:
            raise ValueError("repetitions must be at least one")
        for name, values in (
            ("devices", self.devices),
            ("events", self.events),
            ("block_sizes", self.block_sizes),
            ("packet_loss_percent", self.packet_loss_percent),
            ("outage_seconds", self.outage_seconds),
        ):
            if not values:
                raise ValueError(f"{name} cannot be empty")
        if any(value < 1 for value in self.devices):
            raise ValueError("device counts must be positive")
        if any(value < 1 for value in self.events):
            raise ValueError("event counts must be positive")
        if any(value < 1 or value > 10_000 for value in self.block_sizes):
            raise ValueError("block sizes must be between 1 and 10000")
        if any(value < 0 or value > 100 for value in self.packet_loss_percent):
            raise ValueError("packet loss must be between 0 and 100 percent")
        if any(value < 0 for value in self.outage_seconds):
            raise ValueError("outage durations cannot be negative")
        if not self.authority_thresholds:
            raise ValueError("authority_thresholds cannot be empty")
        for value in self.authority_thresholds:
            value.validate()
        if self.execution.packet_loss_mode not in {"netem", "application", "none"}:
            raise ValueError("packet_loss_mode must be netem, application, or none")

    @property
    def configurations(self) -> int:
        return (
            len(self.devices)
            * len(self.events)
            * len(self.block_sizes)
            * len(self.authority_thresholds)
            * len(self.packet_loss_percent)
            * len(self.outage_seconds)
        )

    @property
    def runs(self) -> int:
        return self.configurations * self.repetitions

    @property
    def nominal_events(self) -> int:
        multiplier = (
            len(self.devices)
            * len(self.block_sizes)
            * len(self.authority_thresholds)
            * len(self.packet_loss_percent)
            * len(self.outage_seconds)
            * self.repetitions
        )
        return sum(self.events) * multiplier

    @property
    def nominal_outage_seconds(self) -> int:
        multiplier = (
            len(self.devices)
            * len(self.events)
            * len(self.block_sizes)
            * len(self.authority_thresholds)
            * len(self.packet_loss_percent)
            * self.repetitions
        )
        return sum(self.outage_seconds) * multiplier

    def iter_cases(self) -> Iterable[ExperimentCase]:
        values = itertools.product(
            self.devices,
            self.events,
            self.block_sizes,
            self.authority_thresholds,
            self.packet_loss_percent,
            self.outage_seconds,
            range(1, self.repetitions + 1),
        )
        for devices, events, block_size, authority, loss, outage, repetition in values:
            yield ExperimentCase(
                devices=devices,
                events=events,
                block_size=block_size,
                authorities=authority.authorities,
                threshold=authority.threshold,
                packet_loss_percent=float(loss),
                outage_seconds=int(outage),
                repetition=repetition,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repetitions": self.repetitions,
            "matrix": {
                "devices": list(self.devices),
                "events": list(self.events),
                "block_size": list(self.block_sizes),
                "authority_thresholds": [
                    {"authorities": value.authorities, "threshold": value.threshold, "label": value.label}
                    for value in self.authority_thresholds
                ],
                "packet_loss_percent": list(self.packet_loss_percent),
                "outage_seconds": list(self.outage_seconds),
            },
            "execution": asdict(self.execution),
            "summary": {
                "configurations": self.configurations,
                "runs": self.runs,
                "nominal_events": self.nominal_events,
                "nominal_outage_seconds": self.nominal_outage_seconds,
            },
        }


def _tuple_int(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"matrix.{field} must be a list")
    return tuple(int(item) for item in value)


def load_plan(path: str | Path, *, repetitions_override: int | None = None) -> ExperimentPlan:
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("experiment configuration root must be a mapping")
    matrix = raw.get("matrix")
    if not isinstance(matrix, dict):
        raise ValueError("matrix must be a mapping")
    authority_values = matrix.get("authority_thresholds")
    if not isinstance(authority_values, list):
        raise ValueError("matrix.authority_thresholds must be a list")
    pairs = tuple(
        AuthorityThreshold(
            authorities=int(item["authorities"]), threshold=int(item["threshold"])
        )
        for item in authority_values
    )
    plan = ExperimentPlan(
        name=str(raw.get("name", source.stem)),
        repetitions=int(repetitions_override or raw.get("repetitions", 5)),
        devices=_tuple_int(matrix.get("devices"), "devices"),
        events=_tuple_int(matrix.get("events"), "events"),
        block_sizes=_tuple_int(matrix.get("block_size"), "block_size"),
        authority_thresholds=pairs,
        packet_loss_percent=tuple(float(item) for item in matrix.get("packet_loss_percent", [])),
        outage_seconds=_tuple_int(matrix.get("outage_seconds"), "outage_seconds"),
        execution=ExecutionSettings.from_mapping(raw.get("execution")),
    )
    plan.validate()
    return plan
