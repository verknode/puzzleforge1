from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Puzzle:
    """One reviewed target from the public Bitcoin Puzzle challenge."""

    number: int
    start: int
    end: int
    address: str
    status: str = "unsolved"

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("puzzle number must be positive")
        if self.start < 1 or self.end < self.start:
            raise ValueError("invalid key interval")
        expected_start = 1 << (self.number - 1)
        expected_end = (1 << self.number) - 1
        if (self.start, self.end) != (expected_start, expected_end):
            raise ValueError(
                f"puzzle #{self.number} must use its published power-of-two interval"
            )

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def start_hex(self) -> str:
        return f"{self.start:x}"

    @property
    def end_hex(self) -> str:
        return f"{self.end:x}"

