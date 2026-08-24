from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Collection

from .crypto import p2pkh_address_from_private_key
from .mosaic import AffineOrder


DATASET_SOURCE = (
    "roadhero/Bitcoin-Puzzle-Info@"
    "6bbd33dcefe2b4d039f96f437e86ea5f918de495/BTC-Solved-Unsolved.txt"
)
GRID_CELLS = 256
MIN_TRAINING_OBSERVATIONS = 16
MODEL_NAMES = (
    "histogram-8",
    "histogram-16",
    "kde-wide",
    "kde-narrow",
    "recent-kde",
    "lag-near",
    "lag-delta",
)


# Public solved challenge vectors.  Each line is puzzle,key_hex,address.  The
# loader verifies interval membership and independently derives every address
# before a vector can influence scheduling.
_SOLVED_VECTORS = """
1,1,1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH
2,3,1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb
3,7,19ZewH8Kk1PDbSNdJ97FP4EiCjTRaZMZQA
4,8,1EhqbyUMvvs7BfL8goY6qcPbD6YKfPqb7e
5,15,1E6NuFjCi27W5zoXg8TRdcSRq84zJeBW3k
6,31,1PitScNLyp2HCygzadCh7FveTnfmpPbfp8
7,4c,1McVt1vMtCC7yn5b9wgX1833yCcLXzueeC
8,e0,1M92tSqNmQLYw33fuBvjmeadirh1ysMBxK
9,1d3,1CQFwcjw1dwhtkVWBttNLDtqL7ivBonGPV
10,202,1LeBZP5QCwwgXRtmVUvTVrraqPUokyLHqe
11,483,1PgQVLmst3Z314JrQn5TNiys8Hc38TcXJu
12,a7b,1DBaumZxUkM4qMQRt2LVWyFJq5kDtSZQot
13,1460,1Pie8JkxBT6MGPz9Nvi3fsPkr2D8q3GBc1
14,2930,1ErZWg5cFCe4Vw5BzgfzB74VNLaXEiEkhk
15,68f3,1QCbW9HWnwQWiQqVo5exhAnmfqKRrCRsvW
16,c936,1BDyrQ6WoF8VN3g9SAS1iKZcPzFfnDVieY
17,1764f,1HduPEXZRdG26SUT5Yk83mLkPyjnZuJ7Bm
18,3080d,1GnNTmTVLZiqQfLbAdp9DVdicEnB5GoERE
19,5749f,1NWmZRpHH4XSPwsW6dsS3nrNWfL1yrJj4w
20,d2c55,1HsMJxNiV7TLxmoF6uJNkydxPFDog4NQum
21,1ba534,14oFNXucftsHiUMY8uctg6N487riuyXs4h
22,2de40f,1CfZWK1QTQE3eS9qn61dQjV89KDjZzfNcv
23,556e52,1L2GM8eE7mJWLdo3HZS6su1832NX2txaac
24,dc2a04,1rSnXMr63jdCuegJFuidJqWxUPV7AtUf7
25,1fa5ee5,15JhYXn6Mx3oF4Y7PcTAv2wVVAuCFFQNiP
26,340326e,1JVnST957hGztonaWK6FougdtjxzHzRMMg
27,6ac3875,128z5d7nN7PkCuX5qoA4Ys6pmxUYnEy86k
28,d916ce8,12jbtzBb54r97TCwW3G1gCFoumpckRAPdY
29,17e2551e,19EEC52krRUK1RkUAEZmQdjTyHT7Gp1TYT
30,3d94cd64,1LHtnpd8nU5VHEMkG2TMYYNUjjLc992bps
31,7d4fe747,1LhE6sCTuGae42Axu1L1ZB7L96yi9irEBE
32,b862a62e,1FRoHA9xewq7DjrZ1psWJVeTer8gHRqEvR
33,1a96ca8d8,187swFMjz1G54ycVU56B7jZFHFTNVQFDiu
34,34a65911d,1PWABE7oUahG2AFFQhhvViQovnCr4rEv7Q
35,4aed21170,1PWCx5fovoEaoBowAvF5k91m2Xat9bMgwb
36,9de820a7c,1Be2UF9NLfyLFbtm3TCbmuocc9N1Kduci1
37,1757756a93,14iXhn8bGajVWegZHJ18vJLHhntcpL4dex
38,22382facd0,1HBtApAFA9B2YZw3G2YKSMCtb3dVnjuNe2
39,4b5f8303e9,122AJhKLEfkFBaGAd84pLp1kfE7xK3GdT8
40,e9ae4933d6,1EeAxcprB2PpCnr34VfZdFrkUWuxyiNEFv
41,153869acc5b,1L5sU9qvJeuwQUdt4y1eiLmquFxKjtHr3E
42,2a221c58d8f,1E32GPWgDyeyQac4aJxm9HVoLrrEYPnM4N
43,6bd3b27c591,1PiFuqGpG8yGM5v6rNHWS3TjsG6awgEGA1
44,e02b35a358f,1CkR2uS7LmFwc3T2jV8C1BhWb5mQaoxedF
45,122fca143c05,1NtiLNGegHWE3Mp9g2JPkgx6wUg4TW7bbk
46,2ec18388d544,1F3JRMWudBaj48EhwcHDdpeuy2jwACNxjP
47,6cd610b53cba,1Pd8VvT49sHKsmqrQiP61RsVwmXCZ6ay7Z
48,ade6d7ce3b9b,1DFYhaB2J9q1LLZJWKTnscPWos9VBqDHzv
49,174176b015f4d,12CiUhYVTTH33w3SPUBqcpMoqnApAV4WCF
50,22bd43c2e9354,1MEzite4ReNuWaL5Ds17ePKt2dCxWEofwk
51,75070a1a009d4,1NpnQyZ7x24ud82b7WiRNvPm6N8bqGQnaS
52,efae164cb9e3c,15z9c9sVpu6fwNiK7dMAFgMYSK4GqsGZim
53,180788e47e326c,15K1YKJMiJ4fpesTVUcByoz334rHmknxmT
54,236fb6d5ad1f43,1KYUv7nSvXx4642TKeuC2SNdTk326uUpFy
55,6abe1f9b67e114,1LzhS3k3e9Ub8i2W1V8xQFdB8n2MYCHPCa
56,9d18b63ac4ffdf,17aPYR1m6pVAacXg1PTDDU7XafvK1dxvhi
57,1eb25c90795d61c,15c9mPGLku1HuW9LRtBf4jcHVpBUt8txKz
58,2c675b852189a21,1Dn8NF8qDyyfHMktmuoQLGyjWmZXgvosXf
59,7496cbb87cab44f,1HAX2n9Uruu9YDt4cqRgYcvtGvZj1rbUyt
60,fc07a1825367bbe,1Kn5h2qpgw9mWE5jKpk8PP4qvvJ1QVy8su
61,13c96a3742f64906,1AVJKwzs9AskraJLGHAZPiaZcrpDr1U6AB
62,363d541eb611abee,1Me6EfpwZK5kQziBwBfvLiHjaPGxCKLoJi
63,7cce5efdaccf6808,1NpYjtLira16LfGbGwZJ5JbDPh3ai9bjf4
64,f7051f27b09112d4,16jY7qLJnxb7CHZyqBP8qca9d51gAjyXQN
65,1a838b13505b26867,18ZMbwUFLMHoZBbfpCjUJQTCMCbktshgpe
66,2832ed74f2b5e35ee,13zb1hQbWVsc2S7ZTZnP2G4undNNpdh5so
67,730fc235c1942c1ae,1BY8GQbnueYofwSuFAT3USAhGjPrkxDdW9
68,bebb3940cd0fc1491,1MVDYgVaSN6iKKEsbzRUAYFrYJadLYZvvZ
69,101d83275fb2bc7e0c,19vkiEajfhuZ8bs8Zu2jgmC6oqZbWqhxhG
70,349b84b6431a6c4ef1,19YZECXj3SxEZMoUeJ1yiPsw8xANe7M7QR
""".strip()


@dataclass(frozen=True, slots=True)
class SolvedObservation:
    number: int
    key: int
    address: str

    @property
    def position(self) -> float:
        start = 1 << (self.number - 1)
        return (self.key - start) / start


@dataclass(frozen=True, slots=True)
class HypothesisScore:
    name: str
    holdouts: int
    mean_log_lift: float
    geometric_lift: float
    p_value: float
    adjusted_p_value: float
    validated: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HypothesisReport:
    target_puzzle: int
    dataset_source: str
    observations: int
    holdouts: int
    selected_model: str
    selected_model_validated: bool
    uniform_fallback: bool
    research_percent: int
    search_percent: int
    search_slots: int
    cycle: int
    scores: tuple[HypothesisScore, ...]
    selected_cells: tuple[int, ...] = ()
    warning: str = (
        "Experimental ordering only; measured historical lift does not prove "
        "future probability lift."
    )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["scores"] = [score.to_dict() for score in self.scores]
        payload["selected_cells"] = list(self.selected_cells)
        return payload


@dataclass(frozen=True, slots=True)
class HypothesisCandidate:
    chunk_id: int
    lane: str
    strategy_rank: int
    cycle: int
    model: str
    cell: int
    model_validated: bool
    analysis_performed: bool


def _parse_vectors() -> tuple[SolvedObservation, ...]:
    observations: list[SolvedObservation] = []
    for line in _SOLVED_VECTORS.splitlines():
        number_text, key_hex, address = line.split(",")
        observations.append(
            SolvedObservation(int(number_text), int(key_hex, 16), address)
        )
    return tuple(observations)


@lru_cache(maxsize=1)
def solved_observations() -> tuple[SolvedObservation, ...]:
    observations = _parse_vectors()
    numbers = [observation.number for observation in observations]
    if numbers != list(range(1, len(observations) + 1)):
        raise ValueError("solved-vector dataset must be consecutive from puzzle #1")
    for observation in observations:
        start = 1 << (observation.number - 1)
        end = (1 << observation.number) - 1
        if not start <= observation.key <= end:
            raise ValueError(
                f"solved vector #{observation.number} is outside its interval"
            )
        actual = p2pkh_address_from_private_key(observation.key)
        if actual != observation.address:
            raise ValueError(
                f"solved vector #{observation.number} failed address verification"
            )
    return observations


def analyze_hypotheses(
    target_puzzle: int,
    *,
    research_percent: int = 10,
    search_percent: int = 90,
    cycle: int = 0,
) -> HypothesisReport:
    _validate_ratio(research_percent, search_percent)
    training = tuple(
        observation
        for observation in solved_observations()
        if observation.number < target_puzzle
    )
    if len(training) <= MIN_TRAINING_OBSERVATIONS:
        raise ValueError(
            "Hypothesis Lab requires more solved observations before the target"
        )

    score_values = tuple(_backtest(name, training) for name in MODEL_NAMES)
    ranked = sorted(
        score_values,
        key=lambda score: (
            score.validated,
            score.mean_log_lift,
            -score.adjusted_p_value,
            score.name,
        ),
        reverse=True,
    )
    validated = [score for score in ranked if score.validated]
    selected = validated[0] if validated else ranked[0]
    selected_model = selected.name if validated else "uniform"
    search_slots = max(1, round(search_percent / research_percent))
    return HypothesisReport(
        target_puzzle=target_puzzle,
        dataset_source=DATASET_SOURCE,
        observations=len(training),
        holdouts=selected.holdouts,
        selected_model=selected_model,
        selected_model_validated=bool(validated),
        uniform_fallback=not validated,
        research_percent=research_percent,
        search_percent=search_percent,
        search_slots=search_slots,
        cycle=cycle,
        scores=tuple(ranked),
    )


def _backtest(
    name: str, observations: tuple[SolvedObservation, ...]
) -> HypothesisScore:
    log_lifts: list[float] = []
    for index in range(MIN_TRAINING_OBSERVATIONS, len(observations)):
        training = tuple(item.position for item in observations[:index])
        actual = observations[index].position
        density = max(_density(name, training, actual), 1e-12)
        log_lifts.append(math.log(density))

    mean_log_lift = statistics.fmean(log_lifts)
    if len(log_lifts) > 1:
        deviation = statistics.stdev(log_lifts)
        standard_error = deviation / math.sqrt(len(log_lifts))
    else:
        standard_error = math.inf
    z_score = mean_log_lift / standard_error if standard_error > 0 else 0.0
    p_value = 0.5 * math.erfc(z_score / math.sqrt(2))
    adjusted = min(1.0, p_value * len(MODEL_NAMES))
    return HypothesisScore(
        name=name,
        holdouts=len(log_lifts),
        mean_log_lift=mean_log_lift,
        geometric_lift=math.exp(mean_log_lift),
        p_value=p_value,
        adjusted_p_value=adjusted,
        validated=mean_log_lift > 0 and adjusted < 0.05,
    )


def _density(name: str, values: tuple[float, ...], position: float) -> float:
    if name == "uniform":
        return 1.0
    if not values:
        return 1.0
    if name == "histogram-8":
        return _histogram_density(values, position, 8)
    if name == "histogram-16":
        return _histogram_density(values, position, 16)
    if name == "kde-wide":
        return _kde_density(values, position, 0.10)
    if name == "kde-narrow":
        return _kde_density(values, position, 0.04)
    if name == "recent-kde":
        return _kde_density(values[-16:], position, 0.08)
    if name == "lag-near":
        return _kde_density((values[-1],), position, 0.12)
    if name == "lag-delta":
        recent = values[-17:]
        deltas = [right - left for left, right in zip(recent, recent[1:])]
        predicted = values[-1] + (statistics.median(deltas) if deltas else 0.0)
        predicted = min(1.0, max(0.0, predicted))
        return _kde_density((predicted,), position, 0.12)
    raise ValueError(f"unknown hypothesis model: {name}")


def _histogram_density(
    values: tuple[float, ...], position: float, bins: int
) -> float:
    counts = [0] * bins
    for value in values:
        counts[min(bins - 1, int(value * bins))] += 1
    index = min(bins - 1, max(0, int(position * bins)))
    alpha = 1.0
    return (counts[index] + alpha) / (len(values) + alpha * bins) * bins


def _kde_density(
    values: tuple[float, ...], position: float, bandwidth: float
) -> float:
    coefficient = 1.0 / (len(values) * bandwidth * math.sqrt(2 * math.pi))
    total = 0.0
    for value in values:
        for center in (value, -value, 2.0 - value):
            distance = (position - center) / bandwidth
            total += math.exp(-0.5 * distance * distance)
    return max(total * coefficient, 1e-12)


def _validate_ratio(research_percent: int, search_percent: int) -> None:
    if isinstance(research_percent, bool) or isinstance(search_percent, bool):
        raise ValueError("Hypothesis Lab percentages must be integers")
    if not 1 <= research_percent <= 50:
        raise ValueError("Hypothesis Lab research percent must be 1-50")
    if not 1 <= search_percent <= 99:
        raise ValueError("Hypothesis Lab search percent must be 1-99")
    if research_percent + search_percent != 100:
        raise ValueError("Hypothesis Lab percentages must total 100")


class HypothesisPlanner:
    """Persistent 10/90 research-to-search range planner.

    One CPU analysis phase selects a scored normalized cell and fills the next
    nine GPU work slots.  The global coordinator de-duplicates every proposal.
    No validated-lift claim is made when the forward holdout gate fails.
    """

    def __init__(
        self,
        total_chunks: int,
        *,
        target_puzzle: int,
        seed: str,
        research_percent: int = 10,
        search_percent: int = 90,
    ) -> None:
        if total_chunks < 1:
            raise ValueError("total_chunks must be positive")
        if target_puzzle < 2:
            raise ValueError("target puzzle must be at least #2")
        if not seed:
            raise ValueError("seed must not be empty")
        _validate_ratio(research_percent, search_percent)
        self.total_chunks = total_chunks
        self.target_puzzle = target_puzzle
        self.seed = seed
        self.research_percent = research_percent
        self.search_percent = search_percent
        self.search_slots = max(1, round(search_percent / research_percent))
        self.grid_cells = min(GRID_CELLS, total_chunks)
        self._cycle = 0
        self._queue: list[dict[str, object]] = []
        self._cell_cursors: dict[str, int] = {}
        self._last_report: dict[str, object] | None = None

    def state(self) -> dict[str, object]:
        return {
            "schema": 1,
            "total_chunks": self.total_chunks,
            "target_puzzle": self.target_puzzle,
            "seed": self.seed,
            "research_percent": self.research_percent,
            "search_percent": self.search_percent,
            "cycle": self._cycle,
            "queue": [dict(item) for item in self._queue],
            "cell_cursors": dict(self._cell_cursors),
            "last_report": self._last_report,
        }

    def restore(self, state: dict[str, object]) -> None:
        expected = {
            "schema": 1,
            "total_chunks": self.total_chunks,
            "target_puzzle": self.target_puzzle,
            "seed": self.seed,
            "research_percent": self.research_percent,
            "search_percent": self.search_percent,
        }
        for name, value in expected.items():
            if state.get(name) != value:
                raise ValueError("Hypothesis Lab state does not match this planner")

        cycle = state.get("cycle")
        queue = state.get("queue")
        cursors = state.get("cell_cursors")
        report = state.get("last_report")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            raise ValueError("invalid Hypothesis Lab cycle")
        if not isinstance(queue, list) or not isinstance(cursors, dict):
            raise ValueError("invalid Hypothesis Lab queue or cursors")
        parsed_queue: list[dict[str, object]] = []
        for item in queue:
            if not isinstance(item, dict):
                raise ValueError("invalid Hypothesis Lab queued candidate")
            chunk_id = item.get("chunk_id")
            if (
                isinstance(chunk_id, bool)
                or not isinstance(chunk_id, int)
                or not 0 <= chunk_id < self.total_chunks
            ):
                raise ValueError("invalid Hypothesis Lab queued chunk")
            parsed_queue.append(dict(item))
        parsed_cursors: dict[str, int] = {}
        for name, value in cursors.items():
            if (
                not isinstance(name, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("invalid Hypothesis Lab cell cursor")
            parsed_cursors[name] = value
        if report is not None and not isinstance(report, dict):
            raise ValueError("invalid Hypothesis Lab report")
        self._cycle = cycle
        self._queue = parsed_queue
        self._cell_cursors = parsed_cursors
        self._last_report = None if report is None else dict(report)

    @property
    def last_report(self) -> dict[str, object] | None:
        return None if self._last_report is None else dict(self._last_report)

    def next_unseen(self, seen: Collection[int]) -> HypothesisCandidate:
        analyzed = False
        while True:
            while self._queue:
                item = self._queue.pop(0)
                chunk_id = int(item["chunk_id"])
                if chunk_id in seen:
                    continue
                return HypothesisCandidate(
                    chunk_id=chunk_id,
                    lane=str(item["lane"]),
                    strategy_rank=int(item["strategy_rank"]),
                    cycle=int(item["cycle"]),
                    model=str(item["model"]),
                    cell=int(item["cell"]),
                    model_validated=bool(item["model_validated"]),
                    analysis_performed=analyzed,
                )
            if len(seen) >= self.total_chunks:
                raise IndexError("Hypothesis Lab plan is exhausted")
            self._prepare_cycle(seen)
            analyzed = True

    def _prepare_cycle(self, seen: Collection[int]) -> None:
        report = analyze_hypotheses(
            self.target_puzzle,
            research_percent=self.research_percent,
            search_percent=self.search_percent,
            cycle=self._cycle,
        )
        positions = tuple(
            observation.position
            for observation in solved_observations()
            if observation.number < self.target_puzzle
        )
        ranked_cells = sorted(
            range(self.grid_cells),
            key=lambda cell: (
                _density(
                    report.selected_model,
                    positions,
                    (cell + 0.5) / self.grid_cells,
                ),
                _tie_breaker(self.seed, report.selected_model, cell),
            ),
            reverse=True,
        )

        queued: set[int] = set()
        selected_cells: list[int] = []
        if report.uniform_fallback:
            while len(self._queue) < self.search_slots:
                chunk_id = self._next_uniform()
                if chunk_id is None:
                    break
                if chunk_id in seen or chunk_id in queued:
                    continue
                slot = len(self._queue)
                cell = min(
                    self.grid_cells - 1,
                    chunk_id * self.grid_cells // self.total_chunks,
                )
                self._queue.append(
                    {
                        "chunk_id": chunk_id,
                        "lane": "hypothesis:uniform-fallback",
                        "strategy_rank": self._cycle * self.search_slots + slot,
                        "cycle": self._cycle,
                        "model": "uniform",
                        "cell": cell,
                        "model_validated": False,
                    }
                )
                queued.add(chunk_id)
                if cell not in selected_cells:
                    selected_cells.append(cell)

        for cell in ranked_cells:
            if report.uniform_fallback:
                break
            while len(self._queue) < self.search_slots:
                chunk_id = self._next_in_cell(report.selected_model, cell)
                if chunk_id is None:
                    break
                if chunk_id in seen or chunk_id in queued:
                    continue
                slot = len(self._queue)
                self._queue.append(
                    {
                        "chunk_id": chunk_id,
                        "lane": f"hypothesis:{report.selected_model}",
                        "strategy_rank": self._cycle * self.search_slots + slot,
                        "cycle": self._cycle,
                        "model": report.selected_model,
                        "cell": cell,
                        "model_validated": report.selected_model_validated,
                    }
                )
                queued.add(chunk_id)
                if cell not in selected_cells:
                    selected_cells.append(cell)
            if len(self._queue) >= self.search_slots:
                break
        if not self._queue:
            raise IndexError("Hypothesis Lab cannot produce another unseen chunk")

        report = replace(report, selected_cells=tuple(selected_cells))
        self._last_report = report.to_dict()
        self._cycle += 1

    def _next_in_cell(self, model: str, cell: int) -> int | None:
        start = cell * self.total_chunks // self.grid_cells
        end = (cell + 1) * self.total_chunks // self.grid_cells
        size = end - start
        cursor_name = f"{model}:{cell}"
        cursor = self._cell_cursors.get(cursor_name, 0)
        if cursor >= size:
            return None
        order = AffineOrder(size, f"{self.seed}/hypothesis/{model}/{cell}")
        chunk_id = start + order.chunk_id(cursor)
        self._cell_cursors[cursor_name] = cursor + 1
        return chunk_id

    def _next_uniform(self) -> int | None:
        cursor_name = "uniform:global"
        cursor = self._cell_cursors.get(cursor_name, 0)
        if cursor >= self.total_chunks:
            return None
        order = AffineOrder(
            self.total_chunks,
            f"{self.seed}/hypothesis/uniform-fallback",
        )
        chunk_id = order.chunk_id(cursor)
        self._cell_cursors[cursor_name] = cursor + 1
        return chunk_id


def _tie_breaker(seed: str, model: str, cell: int) -> int:
    digest = hashlib.blake2b(
        f"PuzzleForge/Hypothesis/{seed}/{model}/{cell}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big")
