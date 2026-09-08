from __future__ import annotations

import json
import math
import os
import platform
import statistics
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from .engine import BitCrackEngine, EngineOutcome, EngineTuning
from .partition import ChunkPlan, KeyChunk
from .registry import get_puzzle


class ScanEngine(Protocol):
    def scan(self, puzzle, chunk: KeyChunk) -> EngineOutcome: ...


@dataclass(frozen=True, slots=True)
class TuningResult:
    tuning: EngineTuning
    successful_runs: int
    failed_runs: int
    median_keys_per_second: float
    minimum_keys_per_second: float
    maximum_keys_per_second: float
    relative_spread: float
    elapsed_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["tuning"] = asdict(self.tuning)
        return payload


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    schema: int
    created_at: str
    puzzle: int
    address: str
    chunk_start_hex: str
    chunk_end_hex: str
    chunk_keys: int
    repeats: int
    binary_name: str
    device_probe: str
    system: str
    results: tuple[TuningResult, ...]
    warmup_runs: int = 0
    maximum_relative_spread: float = 0.20

    @property
    def best(self) -> TuningResult | None:
        valid = [result for result in self.results
                 if result.successful_runs == self.repeats and not result.failed_runs
                 and math.isfinite(result.median_keys_per_second)
                 and result.median_keys_per_second > 0
                 and result.relative_spread <= self.maximum_relative_spread]
        if not valid:
            return None
        return max(
            valid,
            key=lambda result: (
                result.median_keys_per_second,
                -result.relative_spread,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        best = self.best
        return {
            "schema": self.schema,
            "created_at": self.created_at,
            "puzzle": self.puzzle,
            "address": self.address,
            "chunk_start_hex": self.chunk_start_hex,
            "chunk_end_hex": self.chunk_end_hex,
            "chunk_keys": self.chunk_keys,
            "repeats": self.repeats,
            "warmup_runs": self.warmup_runs,
            "maximum_relative_spread": self.maximum_relative_spread,
            "measurement": "completed keys / total scan wall time (including initialization and cooling)",
            "binary_name": self.binary_name,
            "device_probe": self.device_probe,
            "system": self.system,
            "best": None if best is None else best.to_dict(),
            "recommended_flags": (
                None if best is None else tuning_flags(best.tuning)
            ),
            "results": [result.to_dict() for result in self.results],
        }


def tuning_profiles(name: str, device: int | None = None) -> tuple[EngineTuning, ...]:
    presets = {
        "quick": (
            (16, 128, 256),
            (32, 128, 512),
            (32, 256, 512),
            (64, 256, 1024),
        ),
        "balanced": tuple(
            (blocks, threads, points)
            for blocks in (16, 32, 64)
            for threads in (128, 256)
            for points in (256, 512)
        ),
        "full": tuple(
            (blocks, threads, points)
            for blocks in (16, 32, 64)
            for threads in (128, 256, 512)
            for points in (256, 512, 1024)
        ),
    }
    try:
        values = presets[name]
    except KeyError as exc:
        raise ValueError("benchmark profile must be quick, balanced, or full") from exc
    return tuple(
        EngineTuning(
            device=device,
            blocks=blocks,
            threads=threads,
            points=points,
        )
        for blocks, threads, points in values
    )


def run_benchmark(
    *,
    puzzle_number: int,
    chunk_size: int,
    seed: str,
    sequence: int,
    repeats: int,
    profiles: tuple[EngineTuning, ...],
    engine_factory: Callable[[EngineTuning], ScanEngine],
    binary_name: str,
    device_probe: str,
    warmup_runs: int = 0,
    progress: Callable[[str], None] | None = None,
    validate_tuning: bool = False,
) -> BenchmarkReport:
    puzzle = get_puzzle(puzzle_number)
    if puzzle.status != "unsolved":
        raise ValueError("throughput benchmarks must use a reviewed open puzzle")
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if not profiles:
        raise ValueError("at least one tuning profile is required")
    if not 0 <= warmup_runs <= 10:
        raise ValueError("warmup_runs must be in [0, 10]")
    plan = ChunkPlan(puzzle=puzzle, chunk_size=chunk_size, seed=seed)
    chunk = plan.chunk_for_sequence(sequence)
    results: list[TuningResult] = []

    samples = {tuning: [] for tuning in profiles}
    failures = {tuning: [] for tuning in profiles}
    durations = {tuning: 0.0 for tuning in profiles}
    # Interleave profiles so later settings do not get all of the hot-GPU runs.
    for trial in range(warmup_runs + repeats):
        order = profiles if trial % 2 == 0 else tuple(reversed(profiles))
        for tuning in order:
            if progress:
                progress(f"{'Warmup' if trial < warmup_runs else 'Measured'} run {trial + 1}/{warmup_runs + repeats}: {tuning_flags(tuning)}")
            try:
                engine = engine_factory(tuning)
                if validate_tuning and trial == 0:
                    validate_known_puzzle(engine)
                outcome = engine.scan(puzzle, chunk)
            except (OSError, RuntimeError, ValueError) as exc:
                failures[tuning].append(str(exc))
                continue
            if outcome.status == "found":
                raise BenchmarkMatch(outcome.found_key)
            if outcome.status != "complete" or outcome.checked != chunk.size:
                failures[tuning].append(outcome.message or "engine did not complete the full range")
                continue
            if not math.isfinite(outcome.elapsed_seconds) or outcome.elapsed_seconds <= 0:
                failures[tuning].append("invalid elapsed time")
                continue
            durations[tuning] += outcome.elapsed_seconds
            if trial >= warmup_runs:
                samples[tuning].append(outcome.checked / outcome.elapsed_seconds)

    for tuning in profiles:
        rates, errors, total_elapsed = samples[tuning], failures[tuning], durations[tuning]
        if rates:
            median = statistics.median(rates)
            minimum = min(rates)
            maximum = max(rates)
            spread = 0.0 if median == 0 else (maximum - minimum) / median
        else:
            median = minimum = maximum = spread = 0.0
        results.append(
            TuningResult(
                tuning=tuning,
                successful_runs=len(rates),
                failed_runs=len(errors),
                median_keys_per_second=median,
                minimum_keys_per_second=minimum,
                maximum_keys_per_second=maximum,
                relative_spread=spread,
                elapsed_seconds=total_elapsed,
                error="; ".join(errors)[:2_000] or None,
            )
        )

    results.sort(
        key=lambda result: (
            result.successful_runs > 0,
            result.median_keys_per_second,
            -result.relative_spread,
        ),
        reverse=True,
    )
    return BenchmarkReport(
        schema=2,
        created_at=datetime.now(UTC).isoformat(),
        puzzle=puzzle.number,
        address=puzzle.address,
        chunk_start_hex=f"{chunk.start:x}",
        chunk_end_hex=f"{chunk.end:x}",
        chunk_keys=chunk.size,
        repeats=repeats,
        binary_name=binary_name,
        device_probe=device_probe,
        system=f"{platform.system()} {platform.release()} / {platform.machine()}",
        results=tuple(results),
        warmup_runs=warmup_runs,
    )


class BenchmarkMatch(RuntimeError):
    def __init__(self, private_key):
        self.private_key = private_key
        super().__init__("Benchmark found a candidate; stop tuning and independently verify it.")


def validate_known_puzzle(engine: BitCrackEngine) -> None:
    puzzle = get_puzzle(8)
    outcome = engine.scan(
        puzzle,
        KeyChunk(
            ordinal=0,
            chunk_id=0,
            start=puzzle.start,
            end=puzzle.end,
        ),
    )
    if outcome.status != "found" or outcome.found_key != 0xE0:
        raise RuntimeError("GPU validation against solved puzzle #8 failed")


def tuning_flags(tuning: EngineTuning) -> str:
    values: list[str] = []
    if tuning.device is not None:
        values.extend(("--device", str(tuning.device)))
    if tuning.blocks is not None:
        values.extend(("--blocks", str(tuning.blocks)))
    if tuning.threads is not None:
        values.extend(("--threads", str(tuning.threads)))
    if tuning.points is not None:
        values.extend(("--points", str(tuning.points)))
    return " ".join(values)


def save_report(path: Path, report: BenchmarkReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
