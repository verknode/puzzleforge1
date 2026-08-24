from __future__ import annotations

import csv
import io
import math
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any, Callable


NVIDIA_FIELDS = (
    "index",
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "power.draw",
    "power.limit",
    "memory.used",
    "memory.total",
    "clocks.sm",
)


def nvidia_snapshot(
    device: int | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    command = [
        "nvidia-smi",
        f"--query-gpu={','.join(NVIDIA_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    if device is not None:
        command.append(f"--id={device}")
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return _unavailable(f"nvidia-smi unavailable: {exc}")

    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout or "").split())
        return _unavailable(
            f"nvidia-smi exited with code {completed.returncode}: {detail[:500]}"
        )
    try:
        return parse_nvidia_csv(completed.stdout)
    except ValueError as exc:
        return _unavailable(str(exc))


def parse_nvidia_csv(output: str) -> dict[str, object]:
    rows = list(csv.reader(io.StringIO(output.strip())))
    if not rows or len(rows[0]) != len(NVIDIA_FIELDS):
        raise ValueError("nvidia-smi returned an unexpected telemetry row")
    values = [value.strip() for value in rows[0]]
    index = _number(values[0])
    if index is None:
        raise ValueError("nvidia-smi returned an invalid device index")
    memory_used = _number(values[6])
    memory_total = _number(values[7])
    memory_percent = (
        None
        if memory_used is None or not memory_total
        else memory_used / memory_total * 100
    )
    return {
        "available": True,
        "source": "nvidia-smi",
        "sampled_at": datetime.now(UTC).isoformat(),
        "device": int(index),
        "name": values[1],
        "temperature_c": _number(values[2]),
        "utilization_percent": _number(values[3]),
        "power_w": _number(values[4]),
        "power_limit_w": _number(values[5]),
        "memory_used_mib": memory_used,
        "memory_total_mib": memory_total,
        "memory_percent": memory_percent,
        "sm_clock_mhz": _number(values[8]),
        "error": None,
    }


class TelemetryCache:
    def __init__(self, device: int | None, *, ttl_seconds: float = 5.0) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("telemetry cache duration must be positive")
        self.device = device
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._sampled_monotonic = 0.0
        self._snapshot: dict[str, object] | None = None

    def sample(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            if (
                self._snapshot is None
                or now - self._sampled_monotonic >= self.ttl_seconds
            ):
                self._snapshot = nvidia_snapshot(self.device)
                self._sampled_monotonic = now
            return dict(self._snapshot)


def _number(value: str) -> float | None:
    compact = value.strip()
    if not compact or compact.lower() in {"n/a", "[not supported]", "not supported"}:
        return None
    try:
        parsed = float(compact)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _unavailable(error: str) -> dict[str, object]:
    return {
        "available": False,
        "source": "nvidia-smi",
        "sampled_at": datetime.now(UTC).isoformat(),
        "error": error,
    }
