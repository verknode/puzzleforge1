from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from .registry import get_puzzle


QUADRILLION = 10**15
_MODEL_NOISE = re.compile(r"\b(NVIDIA|GEFORCE|TESLA|GRAPHICS|GPU)\b")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize_gpu_model(value: str) -> str:
    cleaned = _MODEL_NOISE.sub(" ", value.upper())
    return _NON_ALNUM.sub("", cleaned)


@dataclass(frozen=True, slots=True)
class CloudOffer:
    provider: str
    offer_id: str
    gpu_model: str
    gpu_count: int
    hourly_usd: float
    reliability: float | None
    verified: bool
    interruptible: bool
    available: bool = True
    region: str = ""
    cuda_version: str = ""

    def __post_init__(self) -> None:
        if not self.provider or not self.offer_id or not self.gpu_model:
            raise ValueError("cloud offer identity fields must not be empty")
        if isinstance(self.gpu_count, bool) or self.gpu_count < 1:
            raise ValueError("gpu_count must be positive")
        if not math.isfinite(self.hourly_usd) or self.hourly_usd <= 0:
            raise ValueError("hourly_usd must be finite and positive")
        if self.reliability is not None and not 0 <= self.reliability <= 1:
            raise ValueError("reliability must be in [0, 1]")

    @property
    def normalized_model(self) -> str:
        return normalize_gpu_model(self.gpu_model)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CloudOffer":
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class BenchmarkRate:
    gpu_model: str
    keys_per_second_per_gpu: float
    validated: bool = True
    relative_spread: float = 0.0

    def __post_init__(self) -> None:
        if not self.gpu_model:
            raise ValueError("benchmark GPU model must not be empty")
        if (
            not math.isfinite(self.keys_per_second_per_gpu)
            or self.keys_per_second_per_gpu <= 0
        ):
            raise ValueError("benchmark rate must be finite and positive")
        if not math.isfinite(self.relative_spread) or self.relative_spread < 0:
            raise ValueError("benchmark spread must be finite and non-negative")

    @property
    def normalized_model(self) -> str:
        return normalize_gpu_model(self.gpu_model)


@dataclass(frozen=True, slots=True)
class CloudPolicy:
    max_instances: int
    max_total_hourly_usd: float
    max_daily_usd: float
    max_offer_hourly_usd: float
    max_cost_per_quadrillion_usd: float
    min_reliability: float = 0.98
    max_benchmark_spread: float = 0.20
    allow_interruptible: bool = True
    require_verified: bool = True
    allow_unknown_reliability: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_instances, bool) or self.max_instances < 1:
            raise ValueError("max_instances must be positive")
        for name in (
            "max_total_hourly_usd",
            "max_daily_usd",
            "max_offer_hourly_usd",
            "max_cost_per_quadrillion_usd",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.min_reliability <= 1:
            raise ValueError("min_reliability must be in [0, 1]")
        if self.max_benchmark_spread < 0:
            raise ValueError("max_benchmark_spread must not be negative")


@dataclass(frozen=True, slots=True)
class OfferDecision:
    offer: CloudOffer
    selected: bool
    reason: str
    measured_keys_per_second: float | None
    cost_per_quadrillion_usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer": self.offer.to_dict(),
            "selected": self.selected,
            "reason": self.reason,
            "measured_keys_per_second": self.measured_keys_per_second,
            "cost_per_quadrillion_usd": self.cost_per_quadrillion_usd,
        }


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    puzzle: int
    selected_instances: int
    selected_gpus: int
    added_hourly_usd: float
    projected_daily_usd: float
    added_keys_per_second: float
    exact_daily_probability: str
    decisions: tuple[OfferDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "dry_run": True,
            "puzzle": self.puzzle,
            "selected_instances": self.selected_instances,
            "selected_gpus": self.selected_gpus,
            "added_hourly_usd": self.added_hourly_usd,
            "projected_daily_usd": self.projected_daily_usd,
            "added_keys_per_second": self.added_keys_per_second,
            "exact_daily_probability": self.exact_daily_probability,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def plan_capacity(
    *,
    puzzle_number: int,
    offers: list[CloudOffer],
    benchmarks: list[BenchmarkRate],
    policy: CloudPolicy,
    running_instances: int = 0,
    running_hourly_usd: float = 0.0,
    spent_today_usd: float = 0.0,
    hours_remaining_today: float = 24.0,
) -> CapacityPlan:
    puzzle = get_puzzle(puzzle_number)
    if running_instances < 0 or running_hourly_usd < 0 or spent_today_usd < 0:
        raise ValueError("running capacity and spend must not be negative")
    if not 0 <= hours_remaining_today <= 24:
        raise ValueError("hours_remaining_today must be in [0, 24]")
    rates: dict[str, BenchmarkRate] = {}
    for benchmark in benchmarks:
        if benchmark.normalized_model in rates:
            raise ValueError(f"duplicate benchmark model: {benchmark.gpu_model}")
        rates[benchmark.normalized_model] = benchmark

    evaluated: list[tuple[CloudOffer, BenchmarkRate | None, float | None, str | None]] = []
    seen_offers: set[tuple[str, str]] = set()
    for offer in offers:
        identity = (offer.provider, offer.offer_id)
        if identity in seen_offers:
            raise ValueError(f"duplicate cloud offer: {offer.provider}/{offer.offer_id}")
        seen_offers.add(identity)
        benchmark = rates.get(offer.normalized_model)
        reason = _static_rejection(offer, benchmark, policy)
        cost = None
        if benchmark is not None:
            total_rate = benchmark.keys_per_second_per_gpu * offer.gpu_count
            cost = offer.hourly_usd / (total_rate * 3600 / QUADRILLION)
            if reason is None and cost > policy.max_cost_per_quadrillion_usd:
                reason = "measured cost per quadrillion exceeds policy"
        evaluated.append((offer, benchmark, cost, reason))

    eligible = sorted(
        (item for item in evaluated if item[3] is None),
        key=lambda item: (
            item[2],
            -(item[0].reliability or 0.0),
            item[0].hourly_usd,
        ),
    )
    selected: set[tuple[str, str]] = set()
    added_hourly = 0.0
    added_rate = 0.0
    selected_gpus = 0
    slots = max(0, policy.max_instances - running_instances)
    budget_reasons: dict[tuple[str, str], str] = {}
    for offer, benchmark, _, _ in eligible:
        identity = (offer.provider, offer.offer_id)
        if len(selected) >= slots:
            budget_reasons[identity] = "instance limit reached"
            continue
        candidate_hourly = running_hourly_usd + added_hourly + offer.hourly_usd
        if candidate_hourly > policy.max_total_hourly_usd:
            budget_reasons[identity] = "hourly budget would be exceeded"
            continue
        projected_daily = spent_today_usd + candidate_hourly * hours_remaining_today
        if projected_daily > policy.max_daily_usd:
            budget_reasons[identity] = "daily budget would be exceeded"
            continue
        selected.add(identity)
        added_hourly += offer.hourly_usd
        selected_gpus += offer.gpu_count
        added_rate += benchmark.keys_per_second_per_gpu * offer.gpu_count

    decisions: list[OfferDecision] = []
    for offer, benchmark, cost, static_reason in evaluated:
        identity = (offer.provider, offer.offer_id)
        is_selected = identity in selected
        reason = (
            "selected by measured cost efficiency"
            if is_selected
            else static_reason or budget_reasons.get(identity, "not selected")
        )
        measured = (
            None
            if benchmark is None
            else benchmark.keys_per_second_per_gpu * offer.gpu_count
        )
        decisions.append(
            OfferDecision(
                offer=offer,
                selected=is_selected,
                reason=reason,
                measured_keys_per_second=measured,
                cost_per_quadrillion_usd=cost,
            )
        )

    decisions.sort(
        key=lambda decision: (
            not decision.selected,
            decision.cost_per_quadrillion_usd or math.inf,
        )
    )
    with localcontext() as context:
        context.prec = 60
        daily_unique = min(
            Decimal(puzzle.size), Decimal(str(added_rate)) * Decimal(86_400)
        )
        probability = daily_unique / Decimal(puzzle.size)
    return CapacityPlan(
        puzzle=puzzle.number,
        selected_instances=len(selected),
        selected_gpus=selected_gpus,
        added_hourly_usd=added_hourly,
        projected_daily_usd=(
            spent_today_usd
            + (running_hourly_usd + added_hourly) * hours_remaining_today
        ),
        added_keys_per_second=added_rate,
        exact_daily_probability=str(probability),
        decisions=tuple(decisions),
    )


def _static_rejection(
    offer: CloudOffer,
    benchmark: BenchmarkRate | None,
    policy: CloudPolicy,
) -> str | None:
    if not offer.available:
        return "offer is unavailable"
    if policy.require_verified and not offer.verified:
        return "offer is not provider-verified"
    if offer.interruptible and not policy.allow_interruptible:
        return "interruptible capacity is disabled"
    if offer.hourly_usd > policy.max_offer_hourly_usd:
        return "offer hourly price exceeds policy"
    if offer.reliability is None and not policy.allow_unknown_reliability:
        return "offer has no reliability measurement"
    if offer.reliability is not None and offer.reliability < policy.min_reliability:
        return "offer reliability is below policy"
    if benchmark is None:
        return "no measured benchmark for this GPU model"
    if not benchmark.validated:
        return "GPU model has not passed validation"
    if benchmark.relative_spread > policy.max_benchmark_spread:
        return "benchmark spread exceeds policy"
    return None


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
