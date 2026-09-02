"""Order work toward keyspace that other public searchers are least likely to
have already covered.

Why this can help
-----------------

The chunk *set* is unchanged, so for a given number of unique keys the
coverage probability is exactly the same as any other order.  Two effects are
nevertheless real:

1. Less duplicated effort.  Public searchers concentrate on a small number of
   obvious regions.  Work placed away from those regions is far less likely to
   repeat a range somebody else is running right now.
2. A genuine posterior tilt.  A region that other people have already searched
   without solving the puzzle is *less* likely to hold the key.  Conditioning
   on "the puzzle is still unsolved" therefore shifts probability away from
   heavily searched regions.

The tilt is bounded by how much of the interval the public has actually
covered.  For puzzle #71 that fraction is negligible, so the honest claim is
the first effect, not a probability shortcut.  The density model below is a
documented behavioural prior, not measured telemetry from other searchers, and
it cannot be validated the way Hypothesis Lab validates its models.  The
default preset therefore keeps a uniform lane running alongside it.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache

from .mosaic import PrivatePermutationOrder, _assert_rank


DEFAULT_BANDS = 4096

_LOW_SWEEP_SCALE = 0.02
_HIGH_SWEEP_SCALE = 0.01
_CENTER_WIDTH = 0.01
_SOLVED_ECHO_WIDTH = 0.004
_ROUND_CAP = 10


@dataclass(frozen=True, slots=True)
class DensityComponent:
    name: str
    weight: float
    rationale: str


COMPONENTS: tuple[DensityComponent, ...] = (
    DensityComponent(
        "sequential-low",
        0.34,
        "Most public operators start at the interval start and walk upward, so "
        "the lowest keys are re-covered more often than anything else.",
    ),
    DensityComponent(
        "sequential-high",
        0.08,
        "A smaller group walks downward from the interval end.",
    ),
    DensityComponent(
        "round-boundary",
        0.10,
        "Hand-picked ranges cluster on hex-round boundaries such as 0x...000000.",
    ),
    DensityComponent(
        "center-split",
        0.05,
        "Splitting the interval in half is a common first guess.",
    ),
    DensityComponent(
        "solved-echo",
        0.28,
        "Pattern searchers aim at the normalized positions of already solved "
        "puzzles, so those offsets are repeatedly re-covered.",
    ),
    DensityComponent(
        "uniform-floor",
        0.15,
        "Random-mode searchers spread thinly and evenly over the whole interval.",
    ),
)


def _trailing_zeros(value: int, width: int) -> int:
    if value == 0:
        return width
    return (value & -value).bit_length() - 1


def _normalize(values: list[float]) -> tuple[float, ...]:
    total = math.fsum(values)
    if total <= 0:
        return tuple(1.0 for _ in values)
    scale = len(values) / total
    return tuple(value * scale for value in values)


@lru_cache(maxsize=8)
def _solved_positions(target_puzzle: int) -> tuple[float, ...]:
    from .hypothesis import solved_observations

    return tuple(
        observation.position
        for observation in solved_observations()
        if observation.number < target_puzzle
    )


@lru_cache(maxsize=8)
def component_tables(
    target_puzzle: int, bands: int = DEFAULT_BANDS
) -> dict[str, tuple[float, ...]]:
    """Per-component relative effort, each normalized to a mean of one."""

    if bands < 1:
        raise ValueError("band count must be positive")
    width = max(1, (bands - 1).bit_length())
    echoes = _solved_positions(target_puzzle)

    low: list[float] = []
    high: list[float] = []
    round_boundary: list[float] = []
    center: list[float] = []
    echo: list[float] = []

    for band in range(bands):
        position = (band + 0.5) / bands
        low.append(math.exp(-position / _LOW_SWEEP_SCALE))
        high.append(math.exp(-(1.0 - position) / _HIGH_SWEEP_SCALE))
        round_boundary.append(
            2.0 ** min(_trailing_zeros(band, width), _ROUND_CAP)
        )
        offset = (position - 0.5) / _CENTER_WIDTH
        center.append(math.exp(-0.5 * offset * offset))
        echo.append(
            math.fsum(
                math.exp(
                    -0.5
                    * ((position - target) / _SOLVED_ECHO_WIDTH)
                    * ((position - target) / _SOLVED_ECHO_WIDTH)
                )
                for target in echoes
            )
        )

    return {
        "sequential-low": _normalize(low),
        "sequential-high": _normalize(high),
        "round-boundary": _normalize(round_boundary),
        "center-split": _normalize(center),
        "solved-echo": _normalize(echo),
        "uniform-floor": tuple(1.0 for _ in range(bands)),
    }


@lru_cache(maxsize=8)
def search_density(
    target_puzzle: int, bands: int = DEFAULT_BANDS
) -> tuple[float, ...]:
    """Relative public search effort per band, normalized to a mean of one."""

    tables = component_tables(target_puzzle, bands)
    return tuple(
        math.fsum(
            component.weight * tables[component.name][band]
            for component in COMPONENTS
        )
        for band in range(bands)
    )


def dominant_component(target_puzzle: int, band: int, bands: int = DEFAULT_BANDS) -> str:
    tables = component_tables(target_puzzle, bands)
    return max(
        COMPONENTS,
        key=lambda component: component.weight * tables[component.name][band],
    ).name


@lru_cache(maxsize=8)
def _layout(
    size: int, bands: int, target_puzzle: int
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Cold-to-hot band order, band boundaries, and cumulative chunk counts."""

    bounds = tuple(size * band // bands for band in range(bands + 1))
    density = search_density(target_puzzle, bands)
    order = tuple(sorted(range(bands), key=lambda band: (density[band], band)))

    cumulative = [0]
    for band in order:
        cumulative.append(cumulative[-1] + bounds[band + 1] - bounds[band])
    return order, bounds, tuple(cumulative)


class ColdOrder:
    """Bijective chunk order that visits the least-searched bands first."""

    name = "cold"

    def __init__(
        self,
        size: int,
        seed: str,
        target_puzzle: int,
        bands: int = DEFAULT_BANDS,
    ) -> None:
        if size < 1:
            raise ValueError("strategy size must be positive")
        if not seed:
            raise ValueError("strategy seed must not be empty")
        if bands < 1:
            raise ValueError("band count must be positive")
        self.size = size
        self.seed = seed
        self.target_puzzle = target_puzzle
        self.bands = min(bands, size)
        self._order, self._bounds, self._cumulative = _layout(
            size, self.bands, target_puzzle
        )
        self._permutations: dict[int, PrivatePermutationOrder] = {}

    def _permutation(self, band: int, count: int) -> PrivatePermutationOrder:
        cached = self._permutations.get(band)
        if cached is None:
            cached = PrivatePermutationOrder(count, f"{self.seed}|cold|{band}")
            self._permutations[band] = cached
        return cached

    def chunk_id(self, rank: int) -> int:
        _assert_rank(rank, self.size)
        index = bisect_right(self._cumulative, rank) - 1
        band = self._order[index]
        low = self._bounds[band]
        offset = rank - self._cumulative[index]
        return low + self._permutation(band, self._bounds[band + 1] - low).chunk_id(
            offset
        )

    def band_report(self, count: int) -> tuple[dict[str, object], ...]:
        """Describe the coldest bands in the order they will be searched."""

        if count < 1:
            raise ValueError("report count must be positive")
        density = search_density(self.target_puzzle, self.bands)
        rows: list[dict[str, object]] = []
        for position, band in enumerate(self._order[:count]):
            rows.append(
                {
                    "rank": position,
                    "band": band,
                    "start_fraction": band / self.bands,
                    "end_fraction": (band + 1) / self.bands,
                    "relative_effort": density[band],
                    "chunks": self._bounds[band + 1] - self._bounds[band],
                    "dominant_component": dominant_component(
                        self.target_puzzle, band, self.bands
                    ),
                }
            )
        return tuple(rows)
