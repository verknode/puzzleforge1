from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    return value * multiplier


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
    ) -> None:
        self.binary = binary.expanduser().resolve()
        self.tuning = tuning or EngineTuning()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.timeout_seconds = timeout_seconds

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
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed = time.monotonic() - started
                partial = _coerce_output(exc.stdout) + _coerce_output(exc.stderr)
                return EngineOutcome(
                    status="error",
                    checked=0,
                    elapsed_seconds=elapsed,
                    rate_keys_per_second=0.0,
                    message=f"BitCrack timed out; range was not credited. {_tail(partial)}",
                )

            elapsed = max(time.monotonic() - started, 1e-9)
            output = (completed.stdout or "") + (completed.stderr or "")
            if output_file.exists():
                output += "\n" + output_file.read_text(encoding="utf-8", errors="replace")

            found = verified_candidate(puzzle, chunk, output)
            reported_rate = parse_reported_rate(output)
            if found is not None:
                return EngineOutcome(
                    status="found",
                    checked=0,
                    elapsed_seconds=elapsed,
                    rate_keys_per_second=reported_rate or 0.0,
                    found_key=found,
                    returncode=completed.returncode,
                    message="Candidate independently verified by PuzzleForge.",
                )

            if completed.returncode != 0:
                return EngineOutcome(
                    status="error",
                    checked=0,
                    elapsed_seconds=elapsed,
                    rate_keys_per_second=reported_rate or 0.0,
                    returncode=completed.returncode,
                    message=(
                        f"BitCrack exited with code {completed.returncode}; range was not credited. "
                        f"{_tail(output)}"
                    ),
                )

            measured_rate = chunk.size / elapsed
            return EngineOutcome(
                status="complete",
                checked=chunk.size,
                elapsed_seconds=elapsed,
                rate_keys_per_second=reported_rate or measured_rate,
                returncode=completed.returncode,
                message="Entire leased range completed without a match.",
            )


def _coerce_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _tail(output: str, lines: int = 12, width: int = 2_000) -> str:
    compact = "\n".join(output.strip().splitlines()[-lines:])
    return compact[-width:]
