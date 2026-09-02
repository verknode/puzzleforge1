from __future__ import annotations

import json
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

    @property
    def best(self) -> TuningResult | None:
        valid = [result for result in self.results if result.successful_runs]
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
    # ``None`` benchmarks the engine's own device defaults, with no grid flags
    # passed at all. A hand-tuned binary often beats every fixed grid here, and
    # without this entry the search could never find that out.
    presets = {
        "quick": (
            None,
            (16, 128, 256),
            (32, 128, 512),
            (32, 256, 512),
            (64, 256, 1024),
            (128, 256, 1024),
        ),
        "balanced": (None,) + tuple(
            (blocks, threads, points)
            for blocks in (16, 32, 64, 128)
            for threads in (128, 256)
            for points in (256, 512, 1024)
        ),
        "full": (None,) + tuple(
            (blocks, threads, points)
            for blocks in (16, 32, 64, 128)
            for threads in (128, 256, 512)
            for points in (256, 512, 1024, 2048)
        ),
    }
    try:
        values = presets[name]
    except KeyError as exc:
        raise ValueError("benchmark profile must be quick, balanced, or full") from exc
    return tuple(
        EngineTuning(device=device)
        if entry is None
        else EngineTuning(
            device=device,
            blocks=entry[0],
            threads=entry[1],
            points=entry[2],
        )
        for entry in values
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
) -> BenchmarkReport:
    puzzle = get_puzzle(puzzle_number)
    if puzzle.status != "unsolved":
        raise ValueError("throughput benchmarks must use a reviewed open puzzle")
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if not profiles:
        raise ValueError("at least one tuning profile is required")
    plan = ChunkPlan(puzzle=puzzle, chunk_size=chunk_size, seed=seed)
    chunk = plan.chunk_for_sequence(sequence)
    results: list[TuningResult] = []

    for tuning in profiles:
        rates: list[float] = []
        total_elapsed = 0.0
        errors: list[str] = []
        for _ in range(repeats):
            outcome = engine_factory(tuning).scan(puzzle, chunk)
            total_elapsed += outcome.elapsed_seconds
            if outcome.status == "found":
                raise RuntimeError(
                    "benchmark engine reported a verified match; stop benchmarking immediately"
                )
            if outcome.status != "complete" or outcome.checked != chunk.size:
                errors.append(outcome.message or "engine did not complete the full range")
                continue
            rates.append(outcome.rate_keys_per_second)

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
        schema=1,
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
    )


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
