from __future__ import annotations

import re
import os
import math
from collections import deque
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .crypto import p2pkh_address_from_private_key
from .model import Puzzle
from .partition import KeyChunk
from .registry import get_puzzle


EngineStatus = Literal["complete", "found", "error"]


@dataclass(frozen=True, slots=True)
class EngineTuning:
    device: int | None = None
    blocks: int | None = None
    threads: int | None = None
    points: int | None = None

    def __post_init__(self) -> None:
        for name in ("device", "blocks", "threads", "points"):
            value = getattr(self, name)
            if value is not None and value < (0 if name == "device" else 1):
                raise ValueError(f"{name} has an invalid value")
        if self.threads is not None and self.threads % 32:
            raise ValueError("BitCrack threads must be a multiple of 32")


@dataclass(frozen=True, slots=True)
class EngineOutcome:
    status: EngineStatus
    checked: int
    elapsed_seconds: float
    rate_keys_per_second: float
    found_key: int | None = None
    returncode: int | None = None
    message: str = ""
    reported_rate_keys_per_second: float | None = None
    first_rate_seconds: float | None = None


_RATE_PATTERN = re.compile(
    r"(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<prefix>[kKmMgGtT]?)"
    r"(?:Key|keys)/s"
)
_LABELED_KEY_PATTERN = re.compile(
    r"(?i)private\s*key\s*[:=]\s*(?:0x)?([0-9a-f]{1,64})\b"
)
_FULL_KEY_PATTERN = re.compile(r"(?i)(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")


def parse_reported_rate(output: str) -> float | None:
    matches = list(_RATE_PATTERN.finditer(output))
    if not matches:
        return None
    match = matches[-1]
    value = float(match.group("value").replace(",", ""))
    multiplier = {
        "": 1.0,
        "k": 1e3,
        "m": 1e6,
        "g": 1e9,
        "t": 1e12,
    }[match.group("prefix").lower()]
    rate = value * multiplier
    return rate if math.isfinite(rate) and rate >= 0 else None


def candidate_keys_from_output(output: str) -> tuple[int, ...]:
    values: set[int] = set()
    for pattern in (_LABELED_KEY_PATTERN, _FULL_KEY_PATTERN):
        for match in pattern.finditer(output):
            value = int(match.group(1), 16)
            if value:
                values.add(value)
    return tuple(sorted(values))


def verified_candidate(puzzle: Puzzle, chunk: KeyChunk, output: str) -> int | None:
    for candidate in candidate_keys_from_output(output):
        if not chunk.start <= candidate <= chunk.end:
            continue
        if p2pkh_address_from_private_key(candidate) == puzzle.address:
            return candidate
    return None


class BitCrackEngine:
    """Strict adapter for a locally installed cuBitCrack/clBitCrack binary.

    PuzzleForge owns range allocation and independently verifies every reported
    candidate. The adapter never accepts an address or range from the CLI.
    """

    def __init__(
        self,
        binary: Path,
        tuning: EngineTuning | None = None,
        timeout_seconds: float | None = None,
        abort_event: threading.Event | None = None,
        poll_seconds: float = 0.25,
        progress: Callable[[dict], None] | None = None,
    ) -> None:
        self.binary = binary.expanduser().resolve()
        self.tuning = tuning or EngineTuning()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.timeout_seconds = timeout_seconds
        if poll_seconds <= 0:
            raise ValueError("engine poll interval must be positive")
        self.abort_event = abort_event
        self.poll_seconds = poll_seconds
        self.progress = progress

    def _assert_binary(self) -> None:
        if not self.binary.is_file():
            raise FileNotFoundError(f"BitCrack binary not found: {self.binary}")

    @property
    def name(self) -> str:
        return f"bitcrack:{self.binary.name}"

    @staticmethod
    def _assert_registered(puzzle: Puzzle) -> None:
        if get_puzzle(puzzle.number) != puzzle:
            raise ValueError("GPU scans are limited to the reviewed puzzle registry")

    def build_command(self, puzzle: Puzzle, chunk: KeyChunk, output_file: Path) -> list[str]:
        self._assert_registered(puzzle)
        if chunk.start < puzzle.start or chunk.end > puzzle.end or chunk.end < chunk.start:
            raise ValueError("GPU chunk is outside the reviewed puzzle interval")

        command = [str(self.binary)]
        if self.tuning.device is not None:
            command.extend(("--device", str(self.tuning.device)))
        if self.tuning.blocks is not None:
            command.extend(("--blocks", str(self.tuning.blocks)))
        if self.tuning.threads is not None:
            command.extend(("--threads", str(self.tuning.threads)))
        if self.tuning.points is not None:
            command.extend(("--points", str(self.tuning.points)))
        command.extend(
            (
                "--compression",
                "compressed",
                "--keyspace",
                f"{chunk.start:x}:{chunk.end:x}",
                "--out",
                str(output_file),
                puzzle.address,
            )
        )
        return command

    def probe(self) -> str:
        self._assert_binary()
        completed = subprocess.run(
            [str(self.binary), "--list-devices"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise RuntimeError(
                f"BitCrack device probe failed with code {completed.returncode}: "
                f"{_tail(output)}"
            )
        return output.strip()

    def scan(self, puzzle: Puzzle, chunk: KeyChunk) -> EngineOutcome:
        self._assert_binary()
        self._assert_registered(puzzle)
        with tempfile.TemporaryDirectory(prefix="puzzleforge-bitcrack-") as directory:
            output_file = Path(directory) / "matches.txt"
            command = self.build_command(puzzle, chunk, output_file)
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            captured = _LiveOutput(puzzle, chunk, started)
            readers = [threading.Thread(target=captured.read, args=(stream,), daemon=True)
                       for stream in (process.stdout, process.stderr)]
            for reader in readers:
                reader.start()
            deadline = (
                None
                if self.timeout_seconds is None
                else started + self.timeout_seconds
            )
            stop_reason = None
            try:
                if self.progress:
                    self.progress({"phase": "initializing"})
                while True:
                    now = time.monotonic()
                    wait_seconds = self.poll_seconds
                    if deadline is not None:
                        wait_seconds = min(wait_seconds, max(0.001, deadline - now))
                    try:
                        process.wait(timeout=wait_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        if self.progress:
                            self.progress(captured.progress())
                        if captured.found is not None:
                            _stop_process(process)
                            break
                        if self.abort_event is not None and self.abort_event.is_set():
                            _stop_process(process)
                            stop_reason = "BitCrack stopped by the local safety guard"
                            break
                        if deadline is not None and time.monotonic() >= deadline:
                            _stop_process(process)
                            stop_reason = "BitCrack timed out"
                            break
            except BaseException:
                _stop_process(process)
                raise
            finally:
                for reader in readers:
                    reader.join(timeout=5)

            elapsed = max(time.monotonic() - started, 1e-9)
            output = captured.output()
            if output_file.exists():
                output += "\n" + output_file.read_text(encoding="utf-8", errors="replace")

            found = captured.found or verified_candidate(puzzle, chunk, output)
            reported_rate = captured.last_rate
            if found is not None:
                return EngineOutcome(
                    status="found",
                    checked=0,
                    elapsed_seconds=elapsed,
                    rate_keys_per_second=reported_rate or 0.0,
                    found_key=found,
                    returncode=process.returncode,
                    message="Candidate independently verified by PuzzleForge.",
                    reported_rate_keys_per_second=reported_rate,
                    first_rate_seconds=captured.first_rate_seconds,
                )

            if process.returncode != 0 or not captured.completed or stop_reason or captured.read_error:
                return EngineOutcome(
                    status="error",
                    checked=0,
                    elapsed_seconds=elapsed,
                    rate_keys_per_second=reported_rate or 0.0,
                    returncode=process.returncode,
                    message=(
                        (stop_reason or captured.read_error or f"BitCrack exited with code {process.returncode}") +
                        f"{' without an end-of-keyspace confirmation' if not captured.completed else ''}; "
                        "range was not credited. "
                        f"{_tail(output)}"
                    ),
                )

            measured_rate = chunk.size / elapsed
            return EngineOutcome(
                status="complete",
                checked=chunk.size,
                elapsed_seconds=elapsed,
                rate_keys_per_second=measured_rate,
                returncode=process.returncode,
                message="Entire leased range completed without a match.",
                reported_rate_keys_per_second=reported_rate,
                first_rate_seconds=captured.first_rate_seconds,
            )


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class _LiveOutput:
    """Drain both pipes on Windows/POSIX, keeping bounded output and verified matches."""
    def __init__(self, puzzle, chunk, started):
        self.puzzle, self.chunk, self.started = puzzle, chunk, started
        self.lines = deque(maxlen=32)
        self.last_rate = self.rate_at = self.first_rate_seconds = self.found = None
        self.completed = False
        self.read_error = None
        self.lock = threading.Lock()

    def read(self, stream):
        pending = b""
        try:
            while data := os.read(stream.fileno(), 4096):
                pending += data
                parts = re.split(rb"[\r\n]", pending)
                pending = parts.pop()
                for part in parts:
                    self.accept(part)
                if len(pending) > 16384:
                    self.accept(pending[:-128])
                    pending = pending[-128:]
            if pending:
                self.accept(pending)
        except (OSError, ValueError):
            self.read_error = "BitCrack output could not be fully read"
        finally:
            stream.close()

    def accept(self, raw):
        text = raw.decode("utf-8", errors="replace")
        rate = parse_reported_rate(text)
        found = verified_candidate(self.puzzle, self.chunk, text)
        with self.lock:
            self.lines.append(text[-2000:])
            if "reached end of keyspace" in text.lower():
                self.completed = True
            if rate is not None:
                self.last_rate, self.rate_at = rate, time.time()
                if self.first_rate_seconds is None:
                    self.first_rate_seconds = time.monotonic() - self.started
            if found is not None:
                self.found = found

    def output(self):
        with self.lock:
            return "\n".join(self.lines)

    def progress(self):
        with self.lock:
            return {"phase": "scanning" if self.last_rate is not None else "initializing",
                    "reported_rate_keys_per_second": self.last_rate,
                    "rate_at_epoch": self.rate_at, "first_rate_seconds": self.first_rate_seconds}


def _tail(output: str, lines: int = 12, width: int = 2_000) -> str:
    output = _LABELED_KEY_PATTERN.sub("Private key: [redacted]", output)
    output = _FULL_KEY_PATTERN.sub("[redacted hex value]", output)
    compact = "\n".join(output.strip().splitlines()[-lines:])
    return compact[-width:]
