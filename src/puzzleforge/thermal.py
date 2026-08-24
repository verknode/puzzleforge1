from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .engine import EngineOutcome
from .telemetry import nvidia_snapshot


@dataclass(frozen=True, slots=True)
class ThermalPolicy:
    maximum_c: float = 82.0
    resume_c: float = 72.0
    poll_seconds: float = 3.0
    max_retries: int = 3
    telemetry_failures: int = 3

    def __post_init__(self) -> None:
        if not math.isfinite(self.maximum_c) or not 40 <= self.maximum_c <= 100:
            raise ValueError("maximum GPU temperature must be in [40, 100] C")
        if not math.isfinite(self.resume_c) or not 20 <= self.resume_c < self.maximum_c:
            raise ValueError("resume GPU temperature must be below the maximum")
        if not math.isfinite(self.poll_seconds) or not 0.01 <= self.poll_seconds <= 60:
            raise ValueError("thermal poll interval must be in [0.01, 60] seconds")
        if isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 100:
            raise ValueError("thermal retries must be in [0, 100]")
        if (
            isinstance(self.telemetry_failures, bool)
            or not 1 <= self.telemetry_failures <= 100
        ):
            raise ValueError("telemetry failure limit must be in [1, 100]")


class ThermalGuardedEngine:
    """Abort and retry an uncredited chunk when the GPU reaches a hard limit."""

    def __init__(
        self,
        engine,
        abort_event: threading.Event,
        *,
        device: int | None,
        policy: ThermalPolicy | None = None,
        snapshot: Callable[[int | None], dict[str, object]] = nvidia_snapshot,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.engine = engine
        self.abort_event = abort_event
        self.device = device
        self.policy = policy or ThermalPolicy()
        self.snapshot = snapshot
        self.sleep = sleep
        self._cooling_required = False

    def scan(self, puzzle, chunk) -> EngineOutcome:
        retries = 0
        thermal_elapsed = 0.0
        cooling = self._cooling_required
        while True:
            readiness_error = self._wait_until_ready(cooling=cooling)
            if readiness_error is not None:
                return _error(readiness_error, elapsed=thermal_elapsed)
            self._cooling_required = False

            self.abort_event.clear()
            monitor_stopped = threading.Event()
            trip_reason: list[str] = []
            monitor = threading.Thread(
                target=self._monitor,
                args=(monitor_stopped, trip_reason),
                name="puzzleforge-thermal-guard",
                daemon=True,
            )
            monitor.start()
            try:
                outcome = self.engine.scan(puzzle, chunk)
            finally:
                monitor_stopped.set()
                monitor.join(timeout=self.policy.poll_seconds + 6)

            if not trip_reason:
                return outcome

            if outcome.status in {"complete", "found"}:
                self._cooling_required = True
                return outcome

            thermal_elapsed += outcome.elapsed_seconds
            retries += 1
            if retries > self.policy.max_retries:
                return _error(
                    f"thermal guard stopped the GPU {retries} times; "
                    f"last reason: {trip_reason[-1]}",
                    elapsed=thermal_elapsed,
                )
            cooling = True

    def _wait_until_ready(self, *, cooling: bool) -> str | None:
        threshold = self.policy.resume_c if cooling else self.policy.maximum_c
        while True:
            snapshot = self.snapshot(self.device)
            if not snapshot.get("available"):
                return f"thermal guard has no telemetry: {snapshot.get('error', 'unknown error')}"
            temperature = _temperature(snapshot)
            if temperature is None:
                return "thermal guard received no GPU temperature"
            if temperature < threshold or (cooling and temperature <= threshold):
                return None
            cooling = True
            threshold = self.policy.resume_c
            self.sleep(self.policy.poll_seconds)

    def _monitor(
        self,
        stopped: threading.Event,
        trip_reason: list[str],
    ) -> None:
        failures = 0
        while not stopped.wait(self.policy.poll_seconds):
            snapshot = self.snapshot(self.device)
            if not snapshot.get("available"):
                failures += 1
                if failures >= self.policy.telemetry_failures:
                    trip_reason.append(
                        "GPU telemetry failed repeatedly: "
                        f"{snapshot.get('error', 'unknown error')}"
                    )
                    self.abort_event.set()
                    return
                continue

            temperature = _temperature(snapshot)
            if temperature is None:
                failures += 1
                if failures >= self.policy.telemetry_failures:
                    trip_reason.append("GPU temperature disappeared from telemetry")
                    self.abort_event.set()
                    return
                continue

            failures = 0
            if temperature >= self.policy.maximum_c:
                trip_reason.append(
                    f"GPU reached {temperature:.1f} C "
                    f"(limit {self.policy.maximum_c:.1f} C)"
                )
                self.abort_event.set()
                return


def _temperature(snapshot: dict[str, object]) -> float | None:
    value = snapshot.get("temperature_c")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _error(message: str, *, elapsed: float = 0.0) -> EngineOutcome:
    return EngineOutcome(
        status="error",
        checked=0,
        elapsed_seconds=elapsed,
        rate_keys_per_second=0.0,
        message=message,
    )
