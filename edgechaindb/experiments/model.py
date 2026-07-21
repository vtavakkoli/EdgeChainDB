from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cached_property
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
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
    image: str = "edgechaindb:0.8.3"
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
class DesignSettings:
    """How matrix levels are converted into executable configurations."""

    type: str = "full_factorial"
    configurations_per_repetition: int | None = None
    seed: int = 20260721
    target_runtime_hours: float | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "DesignSettings":
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
    design: DesignSettings = DesignSettings()

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
        if self.design.type not in {"full_factorial", "balanced_screening"}:
            raise ValueError("design.type must be full_factorial or balanced_screening")
        if self.design.target_runtime_hours is not None and self.design.target_runtime_hours <= 0:
            raise ValueError("design.target_runtime_hours must be positive")
        if self.design.type == "balanced_screening":
            samples = self.design.configurations_per_repetition
            if samples is None or samples < 1:
                raise ValueError(
                    "balanced_screening requires design.configurations_per_repetition"
                )
            full_count = self._full_factorial_count
            if samples > full_count:
                raise ValueError(
                    "design.configurations_per_repetition cannot exceed the full factorial size"
                )
            for name, values in self._axes:
                if samples % len(values) != 0:
                    raise ValueError(
                        f"balanced_screening size {samples} must be divisible by "
                        f"the {len(values)} levels of {name}"
                    )

    @property
    def _axes(self) -> tuple[tuple[str, tuple[Any, ...]], ...]:
        return (
            ("devices", tuple(self.devices)),
            ("events", tuple(self.events)),
            ("block_size", tuple(self.block_sizes)),
            ("authority_threshold", tuple(self.authority_thresholds)),
            ("packet_loss_percent", tuple(self.packet_loss_percent)),
            ("outage_seconds", tuple(self.outage_seconds)),
        )

    @property
    def _full_factorial_count(self) -> int:
        result = 1
        for _, values in self._axes:
            result *= len(values)
        return result

    @cached_property
    def selected_configurations(self) -> tuple[tuple[Any, ...], ...]:
        """Return factor tuples without the repetition dimension.

        The balanced screening design keeps exact marginal coverage for every
        factor while selecting a deterministic, low-correlation subset of the
        full factorial. It searches several independently shuffled balanced
        column designs and retains the one with the lowest pairwise imbalance.
        """
        axes = [values for _, values in self._axes]
        if self.design.type == "full_factorial":
            return tuple(itertools.product(*axes))

        sample_count = int(self.design.configurations_per_repetition or 0)
        best_rows: tuple[tuple[Any, ...], ...] | None = None
        best_score: float | None = None
        attempts = 512

        for attempt in range(attempts):
            rng = random.Random(self.design.seed + attempt * 104729)
            columns: list[list[Any]] = []
            for values in axes:
                repeats = sample_count // len(values)
                column = list(values) * repeats
                rng.shuffle(column)
                columns.append(column)
            rows = tuple(tuple(column[index] for column in columns) for index in range(sample_count))
            if len(set(rows)) != sample_count:
                continue

            score = 0.0
            for left in range(len(axes)):
                for right in range(left + 1, len(axes)):
                    pair_counts: dict[tuple[Any, Any], int] = {
                        (a, b): 0 for a in axes[left] for b in axes[right]
                    }
                    for row in rows:
                        pair_counts[(row[left], row[right])] += 1
                    ideal = sample_count / (len(axes[left]) * len(axes[right]))
                    score += sum((count - ideal) ** 2 for count in pair_counts.values())
            if best_score is None or score < best_score:
                best_rows = rows
                best_score = score
                if math.isclose(score, 0.0):
                    break

        if best_rows is None:
            raise RuntimeError("could not generate a unique balanced screening design")

        # Execute shorter outages and smaller event counts first so users get
        # useful results and an ETA quickly while the campaign remains resumable.
        return tuple(
            sorted(
                best_rows,
                key=lambda row: (
                    int(row[5]),  # outage
                    int(row[1]),  # events
                    int(row[0]),  # devices
                    int(row[2]),  # block size
                    float(row[4]),
                    row[3].authorities,
                    row[3].threshold,
                ),
            )
        )

    @property
    def configurations(self) -> int:
        return len(self.selected_configurations)

    @property
    def runs(self) -> int:
        return self.configurations * self.repetitions

    @property
    def nominal_events(self) -> int:
        return sum(int(row[1]) for row in self.selected_configurations) * self.repetitions

    @property
    def nominal_outage_seconds(self) -> int:
        return sum(int(row[5]) for row in self.selected_configurations) * self.repetitions

    def iter_cases(self) -> Iterable[ExperimentCase]:
        for devices, events, block_size, authority, loss, outage in self.selected_configurations:
            for repetition in range(1, self.repetitions + 1):
                yield ExperimentCase(
                    devices=int(devices),
                    events=int(events),
                    block_size=int(block_size),
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
            "design": asdict(self.design),
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
                "design_type": self.design.type,
                "full_factorial_configurations": self._full_factorial_count,
                "configurations": self.configurations,
                "runs": self.runs,
                "nominal_events": self.nominal_events,
                "nominal_outage_seconds": self.nominal_outage_seconds,
                "target_runtime_hours": self.design.target_runtime_hours,
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
        design=DesignSettings.from_mapping(raw.get("design")),
    )
    plan.validate()
    return plan
