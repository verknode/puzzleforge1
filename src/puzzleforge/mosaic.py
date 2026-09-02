from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Collection, Final, Protocol


class ChunkOrder(Protocol):
    name: str

    def chunk_id(self, rank: int) -> int: ...


def _assert_rank(rank: int, size: int) -> None:
    if rank < 0 or rank >= size:
        raise IndexError("strategy rank is outside the chunk domain")


@dataclass(frozen=True, slots=True)
class AffineOrder:
    size: int
    seed: str
    name: str = "uniform"
    _multiplier: int = field(init=False, repr=False)
    _offset: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("strategy size must be positive")
        digest = hashlib.blake2b(
            f"PuzzleForge/MOSAIC/affine/{self.size}/{self.seed}".encode(),
            digest_size=32,
        ).digest()
        if self.size == 1:
            multiplier, offset = 0, 0
        else:
            multiplier = int.from_bytes(digest[:16], "big") % self.size or 1
            while math.gcd(multiplier, self.size) != 1:
                multiplier = (multiplier + 1) % self.size or 1
            offset = int.from_bytes(digest[16:], "big") % self.size
        object.__setattr__(self, "_multiplier", multiplier)
        object.__setattr__(self, "_offset", offset)

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        if self.size == 1:
            return 0
        return (self._multiplier * rank + self._offset) % self.size


@dataclass(frozen=True, slots=True)
class PrivatePermutationOrder:
    """Keyed format-preserving permutation over ``range(size)``."""

    size: int
    seed: str
    name: str = "private-prp"
    _half_bits: int = field(init=False, repr=False)
    _mask: int = field(init=False, repr=False)
    _key: bytes = field(init=False, repr=False)

    _ROUNDS = 10

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("strategy size must be positive")
        if not self.seed:
            raise ValueError("strategy seed must not be empty")
        bits = max(2, (self.size - 1).bit_length())
        if bits % 2:
            bits += 1
        half_bits = bits // 2
        key = hashlib.blake2b(
            f"PuzzleForge/private-prp/v1/{self.size}/{self.seed}".encode(),
            digest_size=32,
        ).digest()
        object.__setattr__(self, "_half_bits", half_bits)
        object.__setattr__(self, "_mask", (1 << half_bits) - 1)
        object.__setattr__(self, "_key", key)

    def _round_function(self, right: int, round_number: int) -> int:
        width = (self._half_bits + 7) // 8
        payload = bytes((round_number,)) + right.to_bytes(width, "big")
        digest = hashlib.blake2b(payload, key=self._key, digest_size=16).digest()
        return int.from_bytes(digest, "big") & self._mask

    def _permutation(self, value: int) -> int:
        left = value >> self._half_bits
        right = value & self._mask
        for round_number in range(self._ROUNDS):
            left, right = right, left ^ self._round_function(right, round_number)
        return (left << self._half_bits) | right

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        if self.size == 1:
            return 0
        candidate = self._permutation(rank)
        while candidate >= self.size:
            candidate = self._permutation(candidate)
        return candidate


@dataclass(frozen=True, slots=True)
class EdgeOrder:
    size: int
    name: str = "edges"

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("strategy size must be positive")

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        return rank // 2 if rank % 2 == 0 else self.size - 1 - rank // 2


@dataclass(frozen=True, slots=True)
class CenterOrder:
    size: int
    name: str = "center"

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("strategy size must be positive")

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        middle = (self.size - 1) // 2
        if rank == 0:
            return middle
        if rank % 2:
            return middle + (rank + 1) // 2
        return middle - rank // 2


@dataclass(frozen=True, slots=True)
class BitSpreadOrder:
    size: int
    seed: str
    name: str = "spread"
    _bits: int = field(init=False, repr=False)
    _domain: int = field(init=False, repr=False)
    _mask: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("strategy size must be positive")
        bits = max(1, (self.size - 1).bit_length())
        domain = 1 << bits
        digest = hashlib.blake2b(
            f"PuzzleForge/MOSAIC/spread/{self.size}/{self.seed}".encode(),
            digest_size=16,
        ).digest()
        object.__setattr__(self, "_bits", bits)
        object.__setattr__(self, "_domain", domain)
        object.__setattr__(self, "_mask", int.from_bytes(digest, "big") % domain)

    def _permutation(self, value: int) -> int:
        mixed = value ^ self._mask
        reversed_bits = int(f"{mixed:0{self._bits}b}"[::-1], 2)
        return reversed_bits ^ self._mask

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        candidate = self._permutation(rank)
        while candidate >= self.size:
            candidate = self._permutation(candidate)
        return candidate


@dataclass(frozen=True, slots=True)
class Lane:
    name: str
    weight: int

    def __post_init__(self) -> None:
        if self.weight < 1:
            raise ValueError("lane weight must be positive")


DEFAULT_LANES: Final[tuple[Lane, ...]] = (
    Lane("uniform", 8),
    Lane("spread", 4),
    Lane("edges", 2),
    Lane("center", 2),
)

# Six of every ten chunks go to the least-searched bands; the remaining four
# keep an unbiased private permutation running, because the cold prior is a
# behavioural model rather than a validated measurement.
COLD_LANES: Final[tuple[Lane, ...]] = (
    Lane("cold", 6),
    Lane("uniform", 4),
)

LANE_PRESETS: Final[dict[str, tuple[Lane, ...]]] = {
    "mosaic": DEFAULT_LANES,
    "cold": COLD_LANES,
}


def preset_name(lanes: tuple[Lane, ...]) -> str:
    for name, preset in LANE_PRESETS.items():
        if lanes == preset:
            return name
    return "custom"


@dataclass(frozen=True, slots=True)
class MosaicCandidate:
    lane: str
    lane_rank: int
    chunk_id: int

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


class MosaicPlanner:
    """Experimental multi-order planner with one global de-duplication set.

    Every lane is itself a complete permutation. Interleaving changes ordering,
    never the set of chunks. This can only improve early success when a tested
    non-uniform prior is real; it has no probability advantage for uniform data.
    """

    DEFAULT_LANES = DEFAULT_LANES
    COLD_LANES = COLD_LANES

    def __init__(
        self,
        total_chunks: int,
        *,
        seed: str,
        lanes: tuple[Lane, ...] | None = None,
        target_puzzle: int | None = None,
    ) -> None:
        if total_chunks < 1:
            raise ValueError("total_chunks must be positive")
        if not seed:
            raise ValueError("seed must not be empty")
        self.total_chunks = total_chunks
        self.seed = seed
        self.target_puzzle = target_puzzle
        self.lanes = lanes or self.DEFAULT_LANES
        if len({lane.name for lane in self.lanes}) != len(self.lanes):
            raise ValueError("lane names must be unique")
        self._orders: dict[str, ChunkOrder] = {
            "uniform": PrivatePermutationOrder(total_chunks, seed),
            "spread": BitSpreadOrder(total_chunks, seed),
            "edges": EdgeOrder(total_chunks),
            "center": CenterOrder(total_chunks),
        }
        if any(lane.name == "cold" for lane in self.lanes):
            if target_puzzle is None:
                raise ValueError("the cold lane requires a target puzzle number")
            from .coldzone import ColdOrder

            self._orders["cold"] = ColdOrder(total_chunks, seed, target_puzzle)
        unknown = [lane.name for lane in self.lanes if lane.name not in self._orders]
        if unknown:
            raise ValueError(f"unknown MOSAIC lanes: {', '.join(unknown)}")

        self._wheel = _weighted_wheel(self.lanes)
        self._slot = 0
        self._cursors = {lane.name: 0 for lane in self.lanes}

    @staticmethod
    def lanes_from_state(state: dict[str, object]) -> tuple[Lane, ...]:
        """Recover the lane set a stored campaign was created with."""

        raw = state.get("lanes") if isinstance(state, dict) else None
        if not isinstance(raw, list) or not raw:
            raise ValueError("MOSAIC state has no lane list")
        lanes: list[Lane] = []
        for entry in raw:
            if not isinstance(entry, dict) or set(entry) != {"name", "weight"}:
                raise ValueError("MOSAIC lane entry is invalid")
            name = entry["name"]
            weight = entry["weight"]
            if (
                not isinstance(name, str)
                or isinstance(weight, bool)
                or not isinstance(weight, int)
            ):
                raise ValueError("MOSAIC lane entry is invalid")
            lanes.append(Lane(name, weight))
        return tuple(lanes)

    def state(self) -> dict[str, object]:
        return {
            "schema": 1,
            "total_chunks": self.total_chunks,
            "seed": self.seed,
            "lanes": [asdict(lane) for lane in self.lanes],
            "slot": self._slot,
            "cursors": dict(self._cursors),
        }

    def restore(self, state: dict[str, object]) -> None:
        expected = {
            "schema": 1,
            "total_chunks": self.total_chunks,
            "seed": self.seed,
            "lanes": [asdict(lane) for lane in self.lanes],
        }
        for name, value in expected.items():
            if state.get(name) != value:
                raise ValueError("MOSAIC state does not match this planner")
        slot = state.get("slot")
        cursors = state.get("cursors")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("invalid MOSAIC slot")
        if not isinstance(cursors, dict) or set(cursors) != set(self._cursors):
            raise ValueError("invalid MOSAIC cursors")
        parsed: dict[str, int] = {}
        for name, value in cursors.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("invalid MOSAIC cursor")
            if not 0 <= value <= self.total_chunks:
                raise ValueError("invalid MOSAIC cursor")
            parsed[name] = value
        self._slot = slot
        self._cursors = parsed

    def next_unseen(self, seen: Collection[int]) -> MosaicCandidate:
        if len(seen) >= self.total_chunks:
            raise IndexError("MOSAIC plan is exhausted")
        remaining_proposals = sum(
            self.total_chunks - cursor for cursor in self._cursors.values()
        )
        maximum_slots = remaining_proposals * len(self._wheel) + len(self._wheel)
        for _ in range(maximum_slots):
            lane_name = self._wheel[self._slot % len(self._wheel)]
            self._slot += 1
            rank = self._cursors[lane_name]
            if rank >= self.total_chunks:
                continue
            self._cursors[lane_name] = rank + 1
            chunk_id = self._orders[lane_name].chunk_id(rank)
            if chunk_id in seen:
                continue
            return MosaicCandidate(lane=lane_name, lane_rank=rank, chunk_id=chunk_id)
        raise IndexError("MOSAIC lanes cannot produce another unseen chunk")

    def preview(self, count: int) -> tuple[MosaicCandidate, ...]:
        if count < 1:
            raise ValueError("preview count must be positive")
        seen: set[int] = set()
        values: list[MosaicCandidate] = []
        for _ in range(min(count, self.total_chunks)):
            candidate = self.next_unseen(seen)
            seen.add(candidate.chunk_id)
            values.append(candidate)
        return tuple(values)


def _weighted_wheel(lanes: tuple[Lane, ...]) -> tuple[str, ...]:
    total = sum(lane.weight for lane in lanes)
    current = {lane.name: 0 for lane in lanes}
    wheel: list[str] = []
    for _ in range(total):
        for lane in lanes:
            current[lane.name] += lane.weight
        chosen = max(lanes, key=lambda lane: current[lane.name])
        current[chosen.name] -= total
        wheel.append(chosen.name)
    return tuple(wheel)
