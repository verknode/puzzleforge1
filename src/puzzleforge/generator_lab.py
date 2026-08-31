from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import random
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .crypto import GROUP_N, p2pkh_address_from_private_key
from .hypothesis import SolvedObservation, solved_observations
from .registry import get_puzzle


GENERATOR_LAB_SCHEMA = 1
GENERATOR_LAB_VERSION = 1
HARDENED = 1 << 31
FILTER_HOLDOUTS = 5
DEFAULT_TIMESTAMP_CENTER = 1_421_280_000  # 2015-01-15 00:00:00 UTC
DEFAULT_TIMESTAMP_RADIUS = 365 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class SeedCandidate:
    source: str
    ordinal: int
    material: bytes

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.material).hexdigest()[:16]


class SeedSource(Protocol):
    name: str
    size: int
    timestamp_like: bool

    def candidate(self, ordinal: int) -> SeedCandidate: ...


@dataclass(frozen=True, slots=True)
class StaticSeedSource:
    name: str
    values: tuple[bytes, ...]
    timestamp_like: bool = False

    def __init__(
        self,
        name: str,
        values: Sequence[str | bytes],
        *,
        timestamp_like: bool = False,
    ) -> None:
        normalized = tuple(
            value.encode("utf-8") if isinstance(value, str) else bytes(value)
            for value in values
        )
        if not name or not normalized:
            raise ValueError("static seed source requires a name and candidates")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "values", normalized)
        object.__setattr__(self, "timestamp_like", timestamp_like)

    @property
    def size(self) -> int:
        return len(self.values)

    def candidate(self, ordinal: int) -> SeedCandidate:
        if not 0 <= ordinal < self.size:
            raise IndexError("seed candidate is outside the source")
        return SeedCandidate(self.name, ordinal, self.values[ordinal])


class ContextSeedSource:
    name = "public-context-phrases"
    timestamp_like = False
    _bases = (
        "bitcoin",
        "Bitcoin",
        "BITCOIN",
        "satoshi",
        "Satoshi",
        "saatoshi",
        "saatoshi_rising",
        "satoshi_rising",
        "bitcoinpuzzle",
        "BitcoinPuzzle",
        "bitcoin puzzle",
        "puzzle",
        "wallet",
        "deterministicwallet",
        "largebitcoinCollider",
        "crackingstrength",
    )
    _separators = ("", "-", "_", " ")
    _suffixes = 1_000
    _per_base = 1 + len(_separators) * _suffixes
    size = len(_bases) * _per_base

    def candidate(self, ordinal: int) -> SeedCandidate:
        if not 0 <= ordinal < self.size:
            raise IndexError("seed candidate is outside the source")
        base_index, variant = divmod(ordinal, self._per_base)
        base = self._bases[base_index]
        if variant == 0:
            text = base
        else:
            separator_index, number = divmod(variant - 1, self._suffixes)
            text = f"{base}{self._separators[separator_index]}{number}"
        return SeedCandidate(self.name, ordinal, text.encode("utf-8"))


class CalendarSeedSource:
    name = "calendar-phrases-2014-2017"
    timestamp_like = False
    _first = date(2014, 1, 1)
    _last = date(2017, 12, 31)
    _variants = 7
    size = ((_last - _first).days + 1) * _variants

    def candidate(self, ordinal: int) -> SeedCandidate:
        if not 0 <= ordinal < self.size:
            raise IndexError("seed candidate is outside the source")
        day_offset, variant = divmod(ordinal, self._variants)
        value = self._first + timedelta(days=day_offset)
        compact = value.strftime("%Y%m%d")
        options = (
            compact,
            value.isoformat(),
            value.strftime("%d%m%Y"),
            value.strftime("%d.%m.%Y"),
            f"bitcoin{compact}",
            f"satoshi{compact}",
            f"puzzle{compact}",
        )
        return SeedCandidate(self.name, ordinal, options[variant].encode("ascii"))


@dataclass(frozen=True, slots=True)
class TimestampSeedSource:
    encoding: str
    center: int = DEFAULT_TIMESTAMP_CENTER
    radius: int = DEFAULT_TIMESTAMP_RADIUS
    timestamp_like: bool = True

    def __post_init__(self) -> None:
        if self.encoding not in {"ascii", "big", "little"}:
            raise ValueError("timestamp encoding must be ascii, big, or little")
        if not 0 <= self.center <= 0xFFFFFFFF or not 0 <= self.radius:
            raise ValueError("timestamp source bounds are invalid")
        if self.center - self.radius < 0 or self.center + self.radius > 0xFFFFFFFF:
            raise ValueError("timestamp source exceeds uint32")

    @property
    def name(self) -> str:
        return f"unix-time-zigzag-{self.encoding}"

    @property
    def size(self) -> int:
        return self.radius * 2 + 1

    def candidate(self, ordinal: int) -> SeedCandidate:
        if not 0 <= ordinal < self.size:
            raise IndexError("seed candidate is outside the source")
        if ordinal == 0:
            value = self.center
        else:
            distance = (ordinal + 1) // 2
            value = self.center + distance if ordinal & 1 else self.center - distance
        if self.encoding == "ascii":
            material = str(value).encode("ascii")
        else:
            material = value.to_bytes(4, self.encoding)
        return SeedCandidate(self.name, ordinal, material)


@dataclass(frozen=True, slots=True)
class GeneratorScheme:
    name: str
    derive: Callable[[bytes, int], int | None]
    timestamp_safe: bool = True


def _valid_scalar(raw: bytes) -> int | None:
    value = int.from_bytes(raw, "big")
    return value if 1 <= value < GROUP_N else None


def _index(number: int, origin: int) -> int:
    value = number - 1 + origin
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("derived-wallet index exceeds uint32")
    return value


def _sha_seed_index(seed: bytes, number: int, *, origin: int) -> int | None:
    index = _index(number, origin).to_bytes(4, "big")
    return _valid_scalar(hashlib.sha256(seed + index).digest())


def _sha_index_seed(seed: bytes, number: int, *, origin: int) -> int | None:
    index = _index(number, origin).to_bytes(4, "big")
    return _valid_scalar(hashlib.sha256(index + seed).digest())


def _sha_seed_ascii_index(seed: bytes, number: int, *, origin: int) -> int | None:
    index = str(_index(number, origin)).encode("ascii")
    return _valid_scalar(hashlib.sha256(seed + index).digest())


def _hmac_seed_index(seed: bytes, number: int, *, origin: int) -> int | None:
    index = _index(number, origin).to_bytes(4, "big")
    return _valid_scalar(hmac.new(seed, index, hashlib.sha256).digest())


def _hash_chain(seed: bytes, number: int, *, origin: int) -> int | None:
    rounds = _index(number, origin) + 1
    value = seed
    for _ in range(rounds):
        value = hashlib.sha256(value).digest()
    return _valid_scalar(value)


def _python_mt(seed: bytes, number: int, *, origin: int) -> int | None:
    generator = random.Random()
    generator.seed(seed, version=2)
    value = 0
    for _ in range(_index(number, origin) + 1):
        value = generator.getrandbits(256)
    return value if 1 <= value < GROUP_N else None


def _compressed_public_key(private_key: int) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.derive_private_key(private_key, ec.SECP256K1())
    return key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )


def _bip32_master(seed: bytes) -> tuple[int, bytes] | None:
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    private_key = int.from_bytes(digest[:32], "big")
    if not 1 <= private_key < GROUP_N:
        return None
    return private_key, digest[32:]


def _bip32_child(
    private_key: int,
    chain_code: bytes,
    child_index: int,
) -> tuple[int, bytes] | None:
    if not 0 <= child_index <= 0xFFFFFFFF:
        raise ValueError("BIP32 child index exceeds uint32")
    if child_index & HARDENED:
        data = b"\x00" + private_key.to_bytes(32, "big")
    else:
        data = _compressed_public_key(private_key)
    digest = hmac.new(
        chain_code,
        data + child_index.to_bytes(4, "big"),
        hashlib.sha512,
    ).digest()
    left = int.from_bytes(digest[:32], "big")
    if left >= GROUP_N:
        return None
    child = (left + private_key) % GROUP_N
    if child == 0:
        return None
    return child, digest[32:]


def _bip32_path(seed: bytes, path: Sequence[int]) -> int | None:
    node = _bip32_master(seed)
    if node is None:
        return None
    private_key, chain_code = node
    for child_index in path:
        node = _bip32_child(private_key, chain_code, child_index)
        if node is None:
            return None
        private_key, chain_code = node
    return private_key


def _bip32_direct(seed: bytes, number: int, *, hardened: bool) -> int | None:
    child = _index(number, 0) | (HARDENED if hardened else 0)
    return _bip32_path(seed, (child,))


def _bip32_external(seed: bytes, number: int) -> int | None:
    return _bip32_path(seed, (0, _index(number, 0)))


def _bip39_bip44(seed_phrase: bytes, number: int) -> int | None:
    try:
        phrase = unicodedata.normalize("NFKD", seed_phrase.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    seed = hashlib.pbkdf2_hmac(
        "sha512",
        phrase.encode("utf-8"),
        b"mnemonic",
        2_048,
    )
    return _bip32_path(
        seed,
        (44 | HARDENED, 0 | HARDENED, 0 | HARDENED, 0, _index(number, 0)),
    )


DEFAULT_SCHEMES: tuple[GeneratorScheme, ...] = (
    GeneratorScheme(
        "sha256(seed||u32be(index0))",
        lambda seed, number: _sha_seed_index(seed, number, origin=0),
    ),
    GeneratorScheme(
        "sha256(seed||u32be(index1))",
        lambda seed, number: _sha_seed_index(seed, number, origin=1),
    ),
    GeneratorScheme(
        "sha256(u32be(index0)||seed)",
        lambda seed, number: _sha_index_seed(seed, number, origin=0),
    ),
    GeneratorScheme(
        "sha256(seed||ascii(index0))",
        lambda seed, number: _sha_seed_ascii_index(seed, number, origin=0),
    ),
    GeneratorScheme(
        "hmac-sha256(seed,u32be(index0))",
        lambda seed, number: _hmac_seed_index(seed, number, origin=0),
    ),
    GeneratorScheme(
        "hmac-sha256(seed,u32be(index1))",
        lambda seed, number: _hmac_seed_index(seed, number, origin=1),
    ),
    GeneratorScheme(
        "sha256-hash-chain-index0",
        lambda seed, number: _hash_chain(seed, number, origin=0),
    ),
    GeneratorScheme(
        "python-mt19937-index0",
        lambda seed, number: _python_mt(seed, number, origin=0),
    ),
    GeneratorScheme(
        "bip32-raw-m/index0",
        lambda seed, number: _bip32_direct(seed, number, hardened=False),
        timestamp_safe=False,
    ),
    GeneratorScheme(
        "bip32-raw-m/index0h",
        lambda seed, number: _bip32_direct(seed, number, hardened=True),
        timestamp_safe=False,
    ),
    GeneratorScheme(
        "bip32-raw-m/0/index0",
        _bip32_external,
        timestamp_safe=False,
    ),
    GeneratorScheme(
        "bip39-m/44h/0h/0h/0/index0",
        _bip39_bip44,
        timestamp_safe=False,
    ),
)


@dataclass(slots=True)
class GeneratorLabState:
    schema: int
    version: int
    target_puzzle: int
    strategy_fingerprint: str
    status: str = "ready"
    source_index: int = 0
    candidate_index: int = 0
    scheme_index: int = 0
    checked_candidates: int = 0
    completed_seed_candidates: int = 0
    exact_filter_matches: int = 0
    validated_known_generators: int = 0
    best_low_bits: int = 0
    best_low_bits_total: int = 0
    best_scheme: str = ""
    best_source: str = ""
    best_seed_fingerprint: str = ""
    current_scheme: str = ""
    current_source: str = ""
    cycles: int = 0
    total_active_seconds: float = 0.0
    last_slice_seconds: float = 0.0
    last_error: str = ""
    found_key_hex: str = ""
    found_scheme: str = ""
    found_source: str = ""
    found_seed_fingerprint: str = ""
    found_seed_base64: str = ""
    found_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "GeneratorLabState":
        if not isinstance(payload, dict):
            raise ValueError("Generator Lab state must be a JSON object")
        try:
            fields = {
                field: payload[field]
                for field in cls.__dataclass_fields__
                if field in payload
            }
            state = cls(**fields)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid Generator Lab state: {exc}") from exc
        if state.schema != GENERATOR_LAB_SCHEMA:
            raise ValueError("unsupported Generator Lab state schema")
        if state.version != GENERATOR_LAB_VERSION:
            raise ValueError("unsupported Generator Lab strategy version")
        if state.status not in {
            "ready",
            "running",
            "sleeping",
            "stopped",
            "exhausted",
            "found",
            "error",
        }:
            raise ValueError("invalid Generator Lab status")
        for value in (
            state.source_index,
            state.candidate_index,
            state.scheme_index,
            state.checked_candidates,
            state.completed_seed_candidates,
            state.cycles,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Generator Lab counters must be non-negative integers")
        return state


def generator_state_path(database: str | Path) -> Path:
    return Path(database).expanduser().resolve().with_name("generator-lab.json")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _save_state(path: Path, state: GeneratorLabState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = _utc_now()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
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


def load_generator_state(path: Path) -> GeneratorLabState | None:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Generator Lab state contains invalid JSON: {exc}") from exc
    return GeneratorLabState.from_dict(payload)


def _load_wordlist(path: Path) -> StaticSeedSource:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Generator Lab wordlist not found: {resolved}")
    if resolved.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Generator Lab wordlist exceeds 64 MiB")
    values: list[bytes] = []
    seen: set[bytes] = set()
    with resolved.open("rb") as handle:
        for raw in handle:
            value = raw.rstrip(b"\r\n")
            if not value or len(value) > 512 or value in seen:
                continue
            seen.add(value)
            values.append(value)
            if len(values) > 5_000_000:
                raise ValueError("Generator Lab wordlist exceeds 5,000,000 entries")
    if not values:
        raise ValueError("Generator Lab wordlist has no usable entries")
    return StaticSeedSource(f"wordlist:{resolved.name}", values)


def default_seed_sources(wordlist: Path | None = None) -> tuple[SeedSource, ...]:
    sources: list[SeedSource] = []
    if wordlist is not None:
        sources.append(_load_wordlist(wordlist))
    sources.extend((ContextSeedSource(), CalendarSeedSource()))
    sources.extend(
        TimestampSeedSource(encoding)
        for encoding in ("ascii", "big", "little")
    )
    return tuple(sources)


def _masked_key(raw_key: int, puzzle_number: int) -> int:
    if puzzle_number < 1:
        raise ValueError("puzzle number must be positive")
    retained = puzzle_number - 1
    return (1 << retained) | (raw_key & ((1 << retained) - 1))


def _matching_low_bits(left: int, right: int, width: int) -> int:
    if width <= 0:
        return 0
    difference = (left ^ right) & ((1 << width) - 1)
    if difference == 0:
        return width
    return (difference & -difference).bit_length() - 1


def _source_descriptor(source: SeedSource) -> dict[str, object]:
    descriptor: dict[str, object] = {"name": source.name, "size": source.size}
    if isinstance(source, StaticSeedSource):
        digest = hashlib.sha256()
        for value in source.values:
            digest.update(len(value).to_bytes(4, "big"))
            digest.update(value)
        descriptor["content"] = digest.hexdigest()
    return descriptor


class GeneratorLab:
    def __init__(
        self,
        state_path: Path,
        *,
        target_puzzle: int,
        target_address: str | None = None,
        observations: Sequence[SolvedObservation] | None = None,
        sources: Sequence[SeedSource] | None = None,
        schemes: Sequence[GeneratorScheme] | None = None,
        wordlist: Path | None = None,
    ) -> None:
        puzzle = get_puzzle(target_puzzle) if target_address is None else None
        self.state_path = state_path.expanduser().resolve()
        self.target_puzzle = target_puzzle
        self.target_address = puzzle.address if puzzle is not None else target_address
        self.observations = tuple(
            observation
            for observation in (
                solved_observations() if observations is None else observations
            )
            if observation.number < target_puzzle
        )
        if not self.observations:
            raise ValueError("Generator Lab requires solved observations before target")
        self.sources = tuple(
            default_seed_sources(wordlist) if sources is None else sources
        )
        self.schemes = tuple(DEFAULT_SCHEMES if schemes is None else schemes)
        if not self.sources or not self.schemes:
            raise ValueError("Generator Lab requires seed sources and schemes")
        if any(source.size < 1 for source in self.sources):
            raise ValueError("Generator Lab seed sources must not be empty")
        if len({scheme.name for scheme in self.schemes}) != len(self.schemes):
            raise ValueError("Generator Lab scheme names must be unique")
        self._schemes_by_source = tuple(
            tuple(
                scheme
                for scheme in self.schemes
                if not source.timestamp_like or scheme.timestamp_safe
            )
            for source in self.sources
        )
        if any(not schemes_for_source for schemes_for_source in self._schemes_by_source):
            raise ValueError("every Generator Lab source requires at least one scheme")
        fingerprint_material = {
            "version": GENERATOR_LAB_VERSION,
            "target": target_puzzle,
            "observations": [item.number for item in self.observations],
            "sources": [_source_descriptor(source) for source in self.sources],
            "schemes": [scheme.name for scheme in self.schemes],
        }
        self.strategy_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_material, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()

    def ensure_state(self) -> GeneratorLabState:
        state = load_generator_state(self.state_path)
        if state is None:
            state = GeneratorLabState(
                schema=GENERATOR_LAB_SCHEMA,
                version=GENERATOR_LAB_VERSION,
                target_puzzle=self.target_puzzle,
                strategy_fingerprint=self.strategy_fingerprint,
            )
            _save_state(self.state_path, state)
            return state
        if state.target_puzzle != self.target_puzzle:
            raise ValueError("Generator Lab state belongs to a different puzzle")
        if state.strategy_fingerprint != self.strategy_fingerprint:
            raise ValueError(
                "Generator Lab strategy set changed; move generator-lab.json "
                "aside to restart the experiment"
            )
        return state

    def _normalize_cursor(self, state: GeneratorLabState) -> bool:
        while state.source_index < len(self.sources):
            source = self.sources[state.source_index]
            schemes = self._schemes_by_source[state.source_index]
            if state.candidate_index >= source.size:
                state.source_index += 1
                state.candidate_index = 0
                state.scheme_index = 0
                continue
            if state.scheme_index >= len(schemes):
                state.scheme_index = 0
                state.candidate_index += 1
                state.completed_seed_candidates += 1
                continue
            return True
        state.status = "exhausted"
        state.current_source = ""
        state.current_scheme = ""
        return False

    def _advance_cursor(self, state: GeneratorLabState) -> None:
        state.scheme_index += 1
        self._normalize_cursor(state)

    def _candidate_matches_known(
        self,
        scheme: GeneratorScheme,
        seed: bytes,
    ) -> bool:
        for observation in self.observations[-FILTER_HOLDOUTS:]:
            raw = scheme.derive(seed, observation.number)
            if raw is None or _masked_key(raw, observation.number) != observation.key:
                return False
        return True

    def run_slice(
        self,
        *,
        max_seconds: float = 1.0,
        max_candidates: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> GeneratorLabState:
        if not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError("Generator Lab slice duration must be positive")
        if max_candidates is not None and (
            isinstance(max_candidates, bool) or max_candidates < 1
        ):
            raise ValueError("Generator Lab candidate limit must be positive")
        state = self.ensure_state()
        if state.status in {"found", "exhausted"}:
            return state

        started = time.monotonic()
        deadline = started + max_seconds
        processed = 0
        state.status = "running"
        state.last_error = ""
        _save_state(self.state_path, state)
        filter_observation = self.observations[-1]
        filter_width = filter_observation.number - 1

        try:
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    break
                if max_candidates is not None and processed >= max_candidates:
                    break
                if not self._normalize_cursor(state):
                    break
                source = self.sources[state.source_index]
                schemes = self._schemes_by_source[state.source_index]
                scheme = schemes[state.scheme_index]
                candidate = source.candidate(state.candidate_index)
                state.current_source = source.name
                state.current_scheme = scheme.name

                raw = scheme.derive(candidate.material, filter_observation.number)
                state.checked_candidates += 1
                processed += 1
                if raw is not None:
                    predicted = _masked_key(raw, filter_observation.number)
                    matched_bits = _matching_low_bits(
                        predicted,
                        filter_observation.key,
                        filter_width,
                    )
                    if matched_bits > state.best_low_bits:
                        state.best_low_bits = matched_bits
                        state.best_low_bits_total = filter_width
                        state.best_scheme = scheme.name
                        state.best_source = source.name
                        state.best_seed_fingerprint = candidate.fingerprint
                    if predicted == filter_observation.key:
                        state.exact_filter_matches += 1
                        if self._candidate_matches_known(
                            scheme,
                            candidate.material,
                        ):
                            state.validated_known_generators += 1
                            target_raw = scheme.derive(
                                candidate.material,
                                self.target_puzzle,
                            )
                            if target_raw is not None:
                                target_key = _masked_key(
                                    target_raw,
                                    self.target_puzzle,
                                )
                                if (
                                    p2pkh_address_from_private_key(target_key)
                                    == self.target_address
                                ):
                                    state.status = "found"
                                    state.found_key_hex = f"{target_key:064x}"
                                    state.found_scheme = scheme.name
                                    state.found_source = source.name
                                    state.found_seed_fingerprint = candidate.fingerprint
                                    state.found_seed_base64 = base64.b64encode(
                                        candidate.material
                                    ).decode("ascii")
                                    state.found_at = _utc_now()
                if state.status == "found":
                    break
                self._advance_cursor(state)
        except BaseException as exc:
            state.status = "error"
            state.last_error = " ".join(str(exc).split())[:2_000]
            raise
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            state.last_slice_seconds = elapsed
            state.total_active_seconds += elapsed
            state.cycles += 1
            if state.status == "running":
                state.status = "sleeping"
            _save_state(self.state_path, state)
        return state

    def mark_stopped(self) -> GeneratorLabState:
        state = self.ensure_state()
        if state.status not in {"found", "exhausted", "error"}:
            state.status = "stopped"
            _save_state(self.state_path, state)
        return state

    def found_key(self) -> int | None:
        state = self.ensure_state()
        if state.status != "found" or not state.found_key_hex:
            return None
        return int(state.found_key_hex, 16)


class GeneratorLabWorker:
    def __init__(
        self,
        lab: GeneratorLab,
        *,
        duty_percent: int = 10,
        window_seconds: float = 10.0,
    ) -> None:
        if isinstance(duty_percent, bool) or not 1 <= duty_percent <= 50:
            raise ValueError("Generator Lab CPU duty must be 1-50 percent")
        if not math.isfinite(window_seconds) or window_seconds < 1:
            raise ValueError("Generator Lab duty window must be at least one second")
        self.lab = lab
        self.duty_percent = duty_percent
        self.window_seconds = window_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._loop,
            name="puzzleforge-generator-lab",
            daemon=True,
        )

    def start(self) -> None:
        self.lab.ensure_state()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=self.window_seconds + 2)
        self.lab.mark_stopped()

    def found_key(self) -> int | None:
        return self.lab.found_key()

    def _loop(self) -> None:
        active_seconds = self.window_seconds * self.duty_percent / 100
        try:
            while not self.stop_event.is_set():
                window_started = time.monotonic()
                state = self.lab.run_slice(
                    max_seconds=active_seconds,
                    stop_event=self.stop_event,
                )
                if state.status in {"found", "exhausted", "error"}:
                    return
                remaining = self.window_seconds - (
                    time.monotonic() - window_started
                )
                if remaining > 0 and self.stop_event.wait(remaining):
                    return
        except BaseException:
            return


def default_generator_lab(
    database: str | Path,
    *,
    target_puzzle: int,
    wordlist: str = "",
) -> GeneratorLab:
    return GeneratorLab(
        generator_state_path(database),
        target_puzzle=target_puzzle,
        wordlist=Path(wordlist) if wordlist else None,
    )


def generator_dashboard_status(
    database: str | Path,
    *,
    enabled: bool,
    duty_percent: int,
) -> dict[str, object]:
    state = load_generator_state(generator_state_path(database))
    if state is None:
        return {
            "enabled": enabled,
            "status": "ready" if enabled else "disabled",
            "cpu_duty_percent": duty_percent,
            "gpu_reserved_percent": 0,
            "checked_candidates": 0,
            "completed_seed_candidates": 0,
            "best_low_bits": 0,
            "best_low_bits_total": 0,
            "validated_known_generators": 0,
            "current_source": "",
            "current_scheme": "",
            "updated_at": None,
        }
    payload = state.to_dict()
    payload.update(
        {
            "enabled": enabled,
            "cpu_duty_percent": duty_percent,
            "gpu_reserved_percent": 0,
        }
    )
    if not enabled and payload["status"] not in {"found", "exhausted"}:
        payload["status"] = "disabled"
    payload.pop("found_seed_base64", None)
    payload.pop("found_key_hex", None)
    return payload
