from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Iterable


MODEL_ZOO_VERSION = 1
MIN_TRAINING_OBSERVATIONS = 16
DEFAULT_CALIBRATION_TRIALS = 128
REPORT_SCORE_LIMIT = 20
_DENSITY_FLOOR = 1e-12
_DENSITY_CEILING = 1e12
_UNIFORM_MIX = 0.05

ParameterValue = int | float | str


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    family: str
    parameters: tuple[tuple[str, ParameterValue], ...]
    complexity: int
    promotion_eligible: bool

    def parameter(self, name: str) -> ParameterValue:
        for key, value in self.parameters:
            if key == name:
                return value
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "family": self.family,
            "parameters": dict(self.parameters),
            "complexity": self.complexity,
            "promotion_eligible": self.promotion_eligible,
        }


@dataclass(frozen=True, slots=True)
class ForwardEvidence:
    holdouts: int
    mean_log_lift: float
    geometric_lift: float
    early_log_lift: float
    late_log_lift: float
    positive_holdout_rate: float


@dataclass(frozen=True, slots=True)
class ModelScore:
    name: str
    family: str
    parameters: tuple[tuple[str, ParameterValue], ...]
    holdouts: int
    mean_log_lift: float
    geometric_lift: float
    early_log_lift: float
    late_log_lift: float
    positive_holdout_rate: float
    raw_empirical_p: float
    adjusted_p_value: float
    q_value: float
    promotion_eligible: bool
    stable: bool
    validated: bool

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["parameters"] = dict(self.parameters)
        return payload


@dataclass(frozen=True, slots=True)
class ModelZooAnalysis:
    model_count: int
    eligible_model_count: int
    shadow_model_count: int
    validated_model_count: int
    registry_fingerprint: str
    calibration_trials: int
    familywise_alpha: float
    null_max_95pct: float
    best_candidate: str
    best_shadow_model: str | None
    selected_model: str
    selected_model_validated: bool
    scores: tuple[ModelScore, ...]


def _spec(
    name: str,
    family: str,
    *,
    complexity: int,
    eligible: bool,
    **parameters: ParameterValue,
) -> ModelSpec:
    return ModelSpec(
        name=name,
        family=family,
        parameters=tuple(sorted(parameters.items())),
        complexity=complexity,
        promotion_eligible=eligible,
    )


@lru_cache(maxsize=1)
def model_registry() -> tuple[ModelSpec, ...]:
    """Return the immutable, deterministic v1 model registry.

    Shadow models are measured and reported but cannot steer GPU work.  They
    cover deliberately broad data-mining families while the smaller promotion
    set is calibrated together against synthetic uniform sequences.
    """

    models: list[ModelSpec] = []

    for bins in (4, 8, 16, 32, 64, 128):
        models.append(
            _spec(
                f"histogram-{bins}",
                "histogram",
                complexity=bins - 1,
                eligible=bins <= 16,
                bins=bins,
            )
        )

    for bandwidth in (0.02, 0.03, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20):
        label = _decimal_label(bandwidth)
        models.append(
            _spec(
                f"kde-bw-{label}",
                "kde",
                complexity=1,
                eligible=True,
                bandwidth=bandwidth,
            )
        )

    for window in (8, 12, 16, 24, 32):
        for bandwidth in (0.04, 0.08, 0.12):
            label = _decimal_label(bandwidth)
            models.append(
                _spec(
                    f"recent-kde-w{window}-bw{label}",
                    "recent-kde",
                    complexity=2,
                    eligible=True,
                    bandwidth=bandwidth,
                    window=window,
                )
            )

    for lag in (1, 2, 3, 4, 8):
        for bandwidth in (0.06, 0.12):
            label = _decimal_label(bandwidth)
            models.append(
                _spec(
                    f"lag-{lag}-bw{label}",
                    "lag-kde",
                    complexity=2,
                    eligible=True,
                    bandwidth=bandwidth,
                    lag=lag,
                )
            )

    for reducer in ("mean", "median"):
        for window in (4, 8, 16, 32):
            for bandwidth in (0.06, 0.12):
                label = _decimal_label(bandwidth)
                models.append(
                    _spec(
                        f"delta-{reducer}-w{window}-bw{label}",
                        "delta",
                        complexity=3,
                        eligible=True,
                        bandwidth=bandwidth,
                        reducer=reducer,
                        window=window,
                    )
                )

    for window in (0, 16, 32):
        name = "beta-full" if window == 0 else f"beta-w{window}"
        models.append(
            _spec(
                name,
                "beta",
                complexity=2,
                eligible=True,
                window=window,
            )
        )

    for lag in (1, 2, 3):
        for bandwidth in (0.08, 0.15):
            label = _decimal_label(bandwidth)
            models.append(
                _spec(
                    f"ar-{lag}-bw{label}",
                    "autoregression",
                    complexity=3,
                    eligible=lag <= 2,
                    bandwidth=bandwidth,
                    lag=lag,
                )
            )

    for modulus in (3, 5, 7, 11, 13, 16, 31):
        models.append(
            _spec(
                f"residue-{modulus}",
                "residue",
                complexity=modulus - 1,
                eligible=modulus <= 7,
                bits=16,
                modulus=modulus,
            )
        )

    for bits in (8, 12, 16):
        eligible = bits <= 12
        models.extend(
            (
                _spec(
                    f"bit-weight-{bits}",
                    "bit-weight",
                    complexity=bits,
                    eligible=eligible,
                    bits=bits,
                ),
                _spec(
                    f"bit-runs-{bits}",
                    "bit-runs",
                    complexity=bits,
                    eligible=eligible,
                    bits=bits,
                ),
                _spec(
                    f"bit-independent-{bits}",
                    "bit-independent",
                    complexity=bits,
                    eligible=eligible,
                    bits=bits,
                ),
                _spec(
                    f"bit-markov-{bits}",
                    "bit-markov",
                    complexity=5,
                    eligible=eligible,
                    bits=bits,
                ),
            )
        )

    for bits in (8, 12, 16):
        for window in (8, 16, 32):
            models.append(
                _spec(
                    f"xor-mode-{bits}-w{window}",
                    "xor-mode",
                    complexity=3,
                    eligible=False,
                    bandwidth=0.08,
                    bits=bits,
                    window=window,
                )
            )

    for period in (3, 4, 5, 6, 8, 10, 12, 16):
        for bandwidth in (0.08, 0.15):
            label = _decimal_label(bandwidth)
            models.append(
                _spec(
                    f"spectral-p{period}-bw{label}",
                    "spectral",
                    complexity=3,
                    eligible=False,
                    bandwidth=bandwidth,
                    period=period,
                )
            )

    for bits in (8, 12, 16):
        for variant in ("nr", "glibc"):
            models.append(
                _spec(
                    f"lcg-{variant}-{bits}",
                    "lcg",
                    complexity=2,
                    eligible=False,
                    bits=bits,
                    variant=variant,
                )
            )
        for variant in ("a", "b"):
            models.append(
                _spec(
                    f"xorshift-{variant}-{bits}",
                    "xorshift",
                    complexity=3,
                    eligible=False,
                    bits=bits,
                    variant=variant,
                )
            )
        for algorithm in ("sha256", "blake2b"):
            models.append(
                _spec(
                    f"hash-chain-{algorithm}-{bits}",
                    "hash-chain",
                    complexity=1,
                    eligible=False,
                    algorithm=algorithm,
                    bits=bits,
                )
            )

    names = [model.name for model in models]
    if len(names) != len(set(names)):
        raise RuntimeError("Model Zoo names must be unique")
    return tuple(models)


@lru_cache(maxsize=1)
def registry_fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(f"model-zoo-v{MODEL_ZOO_VERSION}\n".encode())
    for model in model_registry():
        digest.update(repr(model).encode())
        digest.update(b"\n")
    return digest.hexdigest()[:16]


@lru_cache(maxsize=1)
def _registry_by_name() -> dict[str, ModelSpec]:
    return {model.name: model for model in model_registry()}


def model_density(
    name: str,
    values: tuple[float, ...],
    position: float,
) -> float:
    if name == "uniform":
        return 1.0
    try:
        spec = _registry_by_name()[name]
    except KeyError as exc:
        raise ValueError(f"unknown Model Zoo model: {name}") from exc
    return density(spec, values, position)


def density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    if not values:
        return 1.0
    position = min(math.nextafter(1.0, 0.0), max(0.0, position))
    family = spec.family

    if family == "histogram":
        result = _histogram_density(
            values,
            position,
            int(spec.parameter("bins")),
        )
    elif family == "kde":
        result = _kde_density(
            values,
            position,
            float(spec.parameter("bandwidth")),
        )
    elif family == "recent-kde":
        window = int(spec.parameter("window"))
        result = _kde_density(
            values[-window:],
            position,
            float(spec.parameter("bandwidth")),
        )
    elif family == "lag-kde":
        lag = min(len(values), int(spec.parameter("lag")))
        result = _kde_density(
            (values[-lag],),
            position,
            float(spec.parameter("bandwidth")),
        )
    elif family == "delta":
        result = _delta_density(spec, values, position)
    elif family == "beta":
        window = int(spec.parameter("window"))
        selected = values if window == 0 else values[-window:]
        result = _beta_density(selected, position)
    elif family == "autoregression":
        result = _autoregression_density(spec, values, position)
    elif family == "residue":
        result = _residue_density(spec, values, position)
    elif family == "bit-weight":
        result = _bit_class_density(spec, values, position, "weight")
    elif family == "bit-runs":
        result = _bit_class_density(spec, values, position, "runs")
    elif family == "bit-independent":
        result = _bit_independent_density(spec, values, position)
    elif family == "bit-markov":
        result = _bit_markov_density(spec, values, position)
    elif family == "xor-mode":
        result = _xor_mode_density(spec, values, position)
    elif family == "spectral":
        result = _spectral_density(spec, values, position)
    elif family == "lcg":
        result = _lcg_density(spec, values, position)
    elif family == "xorshift":
        result = _xorshift_density(spec, values, position)
    elif family == "hash-chain":
        result = _hash_chain_density(spec, values, position)
    else:
        raise ValueError(f"unknown Model Zoo family: {family}")
    return min(_DENSITY_CEILING, max(_DENSITY_FLOOR, result))


def forward_log_lifts(
    spec: ModelSpec,
    values: tuple[float, ...],
    *,
    minimum_training: int = MIN_TRAINING_OBSERVATIONS,
) -> tuple[float, ...]:
    if len(values) <= minimum_training:
        raise ValueError("not enough observations for forward holdouts")
    lifts: list[float] = []
    for index in range(minimum_training, len(values)):
        training = values[:index]
        observed = values[index]
        lifts.append(math.log(density(spec, training, observed)))
    return tuple(lifts)


def evaluate_forward(
    spec: ModelSpec,
    values: tuple[float, ...],
    *,
    minimum_training: int = MIN_TRAINING_OBSERVATIONS,
) -> ForwardEvidence:
    lifts = forward_log_lifts(
        spec,
        values,
        minimum_training=minimum_training,
    )
    midpoint = max(1, len(lifts) // 2)
    early = lifts[:midpoint]
    late = lifts[midpoint:]
    mean = statistics.fmean(lifts)
    return ForwardEvidence(
        holdouts=len(lifts),
        mean_log_lift=mean,
        geometric_lift=math.exp(mean),
        early_log_lift=statistics.fmean(early),
        late_log_lift=statistics.fmean(late) if late else mean,
        positive_holdout_rate=sum(value > 0 for value in lifts) / len(lifts),
    )


def analyze_model_zoo(
    values: tuple[float, ...],
    *,
    calibration_trials: int = DEFAULT_CALIBRATION_TRIALS,
    score_limit: int = REPORT_SCORE_LIMIT,
) -> ModelZooAnalysis:
    if len(values) <= MIN_TRAINING_OBSERVATIONS:
        raise ValueError("not enough observations for Model Zoo")
    if calibration_trials < 19:
        raise ValueError("calibration_trials must be at least 19")
    if score_limit < 1:
        raise ValueError("score_limit must be positive")

    specs = model_registry()
    eligible = tuple(model for model in specs if model.promotion_eligible)
    shadow = tuple(model for model in specs if not model.promotion_eligible)
    observed = {model.name: evaluate_forward(model, values) for model in specs}
    null_by_model, null_maxima = _null_calibration(
        len(values),
        calibration_trials,
    )

    raw_p_values = {
        model.name: _empirical_p(
            observed[model.name].mean_log_lift,
            null_by_model[model.name],
        )
        for model in eligible
    }
    q_values = _benjamini_hochberg(raw_p_values)
    scores: list[ModelScore] = []
    for model in specs:
        evidence = observed[model.name]
        is_eligible = model.promotion_eligible
        raw_p = raw_p_values.get(model.name, 1.0)
        adjusted_p = (
            _empirical_p(evidence.mean_log_lift, null_maxima)
            if is_eligible
            else 1.0
        )
        q_value = q_values.get(model.name, 1.0)
        stable = (
            evidence.early_log_lift > 0
            and evidence.late_log_lift > 0
        )
        validated = (
            is_eligible
            and evidence.mean_log_lift > 0
            and stable
            and adjusted_p <= 0.05
        )
        scores.append(
            ModelScore(
                name=model.name,
                family=model.family,
                parameters=model.parameters,
                holdouts=evidence.holdouts,
                mean_log_lift=evidence.mean_log_lift,
                geometric_lift=evidence.geometric_lift,
                early_log_lift=evidence.early_log_lift,
                late_log_lift=evidence.late_log_lift,
                positive_holdout_rate=evidence.positive_holdout_rate,
                raw_empirical_p=raw_p,
                adjusted_p_value=adjusted_p,
                q_value=q_value,
                promotion_eligible=is_eligible,
                stable=stable,
                validated=validated,
            )
        )

    ranked_eligible = sorted(
        (score for score in scores if score.promotion_eligible),
        key=_score_rank,
        reverse=True,
    )
    ranked_shadow = sorted(
        (score for score in scores if not score.promotion_eligible),
        key=_score_rank,
        reverse=True,
    )
    validated = [score for score in ranked_eligible if score.validated]
    selected_model = validated[0].name if validated else "uniform"

    report_scores: list[ModelScore] = []
    eligible_limit = score_limit
    if ranked_shadow and score_limit > 1:
        eligible_limit = max(1, score_limit * 3 // 4)
    for score in ranked_eligible[:eligible_limit]:
        report_scores.append(score)
    for score in ranked_shadow[: max(0, score_limit - len(report_scores))]:
        if score.name not in {item.name for item in report_scores}:
            report_scores.append(score)
    if validated and validated[0].name not in {item.name for item in report_scores}:
        report_scores[-1] = validated[0]

    return ModelZooAnalysis(
        model_count=len(specs),
        eligible_model_count=len(eligible),
        shadow_model_count=len(shadow),
        validated_model_count=len(validated),
        registry_fingerprint=registry_fingerprint(),
        calibration_trials=calibration_trials,
        familywise_alpha=0.05,
        null_max_95pct=_quantile(null_maxima, 0.95),
        best_candidate=ranked_eligible[0].name,
        best_shadow_model=ranked_shadow[0].name if ranked_shadow else None,
        selected_model=selected_model,
        selected_model_validated=bool(validated),
        scores=tuple(report_scores),
    )


def _score_rank(score: ModelScore) -> tuple[bool, bool, float, float, str]:
    return (
        score.validated,
        score.stable,
        score.mean_log_lift,
        -score.adjusted_p_value,
        score.name,
    )


@lru_cache(maxsize=8)
def _null_calibration(
    observations: int,
    trials: int,
) -> tuple[dict[str, tuple[float, ...]], tuple[float, ...]]:
    eligible = tuple(
        model for model in model_registry() if model.promotion_eligible
    )
    seed_material = (
        f"PuzzleForge/ModelZoo/null/{registry_fingerprint()}/"
        f"{observations}/{trials}"
    ).encode()
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    generator = random.Random(seed)
    by_model: dict[str, list[float]] = {model.name: [] for model in eligible}
    maxima: list[float] = []
    for _ in range(trials):
        sequence = tuple(generator.random() for _ in range(observations))
        trial_scores: list[float] = []
        for model in eligible:
            mean = evaluate_forward(model, sequence).mean_log_lift
            by_model[model.name].append(mean)
            trial_scores.append(mean)
        maxima.append(max(trial_scores))
    return (
        {name: tuple(values) for name, values in by_model.items()},
        tuple(maxima),
    )


def _empirical_p(observed: float, null_values: Iterable[float]) -> float:
    values = tuple(null_values)
    return (1 + sum(value >= observed for value in values)) / (len(values) + 1)


def _benjamini_hochberg(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    result: dict[str, float] = {}
    running = 1.0
    for rank in range(count, 0, -1):
        name, p_value = ordered[rank - 1]
        running = min(running, p_value * count / rank)
        result[name] = min(1.0, running)
    return result


def _quantile(values: tuple[float, ...], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _decimal_label(value: float) -> str:
    return f"{value:.2f}".replace(".", "p")


def _histogram_density(
    values: tuple[float, ...],
    position: float,
    bins: int,
) -> float:
    counts = [0] * bins
    for value in values:
        counts[min(bins - 1, int(value * bins))] += 1
    index = min(bins - 1, int(position * bins))
    alpha = 1.0
    return (counts[index] + alpha) / (len(values) + alpha * bins) * bins


def _kde_density(
    values: tuple[float, ...],
    position: float,
    bandwidth: float,
) -> float:
    coefficient = 1.0 / (len(values) * bandwidth * math.sqrt(2 * math.pi))
    total = 0.0
    for value in values:
        for center in (value, -value, 2.0 - value):
            distance = (position - center) / bandwidth
            total += math.exp(-0.5 * distance * distance)
    return max(total * coefficient, _DENSITY_FLOOR)


def _delta_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    window = int(spec.parameter("window"))
    recent = values[-(window + 1) :]
    deltas = tuple(right - left for left, right in zip(recent, recent[1:]))
    if not deltas:
        return 1.0
    reducer = str(spec.parameter("reducer"))
    delta = statistics.fmean(deltas) if reducer == "mean" else statistics.median(deltas)
    predicted = min(1.0, max(0.0, values[-1] + delta))
    return _kde_density(
        (predicted,),
        position,
        float(spec.parameter("bandwidth")),
    )


def _beta_density(values: tuple[float, ...], position: float) -> float:
    if len(values) < 3:
        return 1.0
    mean = (sum(values) + 1.0) / (len(values) + 2.0)
    variance = statistics.pvariance(values)
    maximum = mean * (1.0 - mean)
    if variance <= 1e-8 or variance >= maximum:
        return 1.0
    common = maximum / variance - 1.0
    alpha = min(100.0, max(0.2, mean * common))
    beta = min(100.0, max(0.2, (1.0 - mean) * common))
    x = min(1.0 - 1e-12, max(1e-12, position))
    log_density = (
        (alpha - 1.0) * math.log(x)
        + (beta - 1.0) * math.log1p(-x)
        - (math.lgamma(alpha) + math.lgamma(beta) - math.lgamma(alpha + beta))
    )
    return _mix_uniform(math.exp(min(math.log(_DENSITY_CEILING), log_density)))


def _autoregression_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    lag = int(spec.parameter("lag"))
    if len(values) <= lag + 2:
        return 1.0
    x_values = values[:-lag]
    y_values = values[lag:]
    x_mean = statistics.fmean(x_values)
    y_mean = statistics.fmean(y_values)
    variance = sum((value - x_mean) ** 2 for value in x_values)
    if variance <= 1e-12:
        predicted = y_mean
    else:
        covariance = sum(
            (x - x_mean) * (y - y_mean)
            for x, y in zip(x_values, y_values)
        )
        slope = min(2.0, max(-2.0, covariance / variance))
        predicted = y_mean + slope * (values[-lag] - x_mean)
    predicted = min(1.0, max(0.0, predicted))
    return _kde_density(
        (predicted,),
        position,
        float(spec.parameter("bandwidth")),
    )


def _quantize(value: float, bits: int) -> int:
    size = 1 << bits
    return min(size - 1, max(0, int(value * size)))


def _residue_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    modulus = int(spec.parameter("modulus"))
    size = 1 << bits
    counts = [0] * modulus
    for value in values:
        counts[_quantize(value, bits) % modulus] += 1
    residue = _quantize(position, bits) % modulus
    alpha = 1.0
    probability = (counts[residue] + alpha) / (len(values) + alpha * modulus)
    bucket_size = (size - 1 - residue) // modulus + 1
    uniform_probability = bucket_size / size
    return probability / uniform_probability


def _bit_class_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
    kind: str,
) -> float:
    bits = int(spec.parameter("bits"))
    classes = bits + 1 if kind == "weight" else bits
    counts = [0] * classes
    for value in values:
        index = _quantize(value, bits)
        category = index.bit_count() if kind == "weight" else _transition_count(index, bits)
        counts[category] += 1
    target = _quantize(position, bits)
    category = target.bit_count() if kind == "weight" else _transition_count(target, bits)
    alpha = 1.0
    class_probability = (counts[category] + alpha) / (len(values) + alpha * classes)
    if kind == "weight":
        class_size = math.comb(bits, category)
    else:
        class_size = 2 * math.comb(bits - 1, category)
    uniform_probability = class_size / (1 << bits)
    return _mix_uniform(class_probability / uniform_probability)


def _transition_count(value: int, bits: int) -> int:
    return ((value ^ (value >> 1)) & ((1 << (bits - 1)) - 1)).bit_count()


def _bit_independent_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    indexes = tuple(_quantize(value, bits) for value in values)
    target = _quantize(position, bits)
    log_probability = 0.0
    for shift in range(bits):
        ones = sum((value >> shift) & 1 for value in indexes)
        probability_one = (ones + 1.0) / (len(indexes) + 2.0)
        probability = probability_one if (target >> shift) & 1 else 1.0 - probability_one
        log_probability += math.log(probability)
    relative_density = math.exp(log_probability + bits * math.log(2.0))
    return _mix_uniform(relative_density)


def _bit_markov_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    indexes = tuple(_quantize(value, bits) for value in values)
    initial_ones = sum((value >> (bits - 1)) & 1 for value in indexes)
    initial_one_probability = (initial_ones + 1.0) / (len(indexes) + 2.0)
    transitions = [[1.0, 1.0], [1.0, 1.0]]
    for value in indexes:
        previous = (value >> (bits - 1)) & 1
        for shift in range(bits - 2, -1, -1):
            current = (value >> shift) & 1
            transitions[previous][current] += 1.0
            previous = current
    target = _quantize(position, bits)
    first = (target >> (bits - 1)) & 1
    probability = initial_one_probability if first else 1.0 - initial_one_probability
    previous = first
    for shift in range(bits - 2, -1, -1):
        current = (target >> shift) & 1
        row = transitions[previous]
        probability *= row[current] / (row[0] + row[1])
        previous = current
    return _mix_uniform(probability * (1 << bits))


def _xor_mode_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    window = int(spec.parameter("window"))
    indexes = tuple(_quantize(value, bits) for value in values[-(window + 1) :])
    if len(indexes) < 2:
        return 1.0
    counts: dict[int, int] = {}
    for left, right in zip(indexes, indexes[1:]):
        delta = left ^ right
        counts[delta] = counts.get(delta, 0) + 1
    xor_value = max(counts, key=lambda value: (counts[value], -value))
    predicted = indexes[-1] ^ xor_value
    center = (predicted + 0.5) / (1 << bits)
    return _kde_density(
        (center,),
        position,
        float(spec.parameter("bandwidth")),
    )


def _spectral_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    period = int(spec.parameter("period"))
    omega = 2.0 * math.pi / period
    mean = statistics.fmean(values)
    cosine = 0.0
    sine = 0.0
    cosine_norm = 0.0
    sine_norm = 0.0
    for index, value in enumerate(values):
        centered = value - mean
        c = math.cos(omega * index)
        s = math.sin(omega * index)
        cosine += centered * c
        sine += centered * s
        cosine_norm += c * c
        sine_norm += s * s
    next_index = len(values)
    predicted = mean
    if cosine_norm > 0:
        predicted += cosine / cosine_norm * math.cos(omega * next_index)
    if sine_norm > 0:
        predicted += sine / sine_norm * math.sin(omega * next_index)
    predicted = min(1.0, max(0.0, predicted))
    return _kde_density(
        (predicted,),
        position,
        float(spec.parameter("bandwidth")),
    )


def _lcg_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    modulus = 1 << bits
    variant = str(spec.parameter("variant"))
    if variant == "nr":
        multiplier, increment = 1_664_525, 1_013_904_223
    else:
        multiplier, increment = 1_103_515_245, 12_345
    state = _quantize(values[-1], bits)
    predicted = (multiplier * state + increment) % modulus
    return _kde_density(((predicted + 0.5) / modulus,), position, 0.08)


def _xorshift_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    variant = str(spec.parameter("variant"))
    shifts = (1, 3, 2) if variant == "a" else (3, 5, 1)
    mask = (1 << bits) - 1
    state = _quantize(values[-1], bits)
    state ^= (state << shifts[0]) & mask
    state ^= state >> shifts[1]
    state ^= (state << shifts[2]) & mask
    state &= mask
    return _kde_density(((state + 0.5) / (1 << bits),), position, 0.08)


def _hash_chain_density(
    spec: ModelSpec,
    values: tuple[float, ...],
    position: float,
) -> float:
    bits = int(spec.parameter("bits"))
    algorithm = str(spec.parameter("algorithm"))
    width = (bits + 7) // 8
    state = _quantize(values[-1], bits).to_bytes(width, "big")
    if algorithm == "sha256":
        digest = hashlib.sha256(state).digest()
    else:
        digest = hashlib.blake2b(state, digest_size=32).digest()
    predicted = int.from_bytes(digest[:width], "big") & ((1 << bits) - 1)
    return _kde_density(((predicted + 0.5) / (1 << bits),), position, 0.08)


def _mix_uniform(relative_density: float) -> float:
    return _UNIFORM_MIX + (1.0 - _UNIFORM_MIX) * relative_density
