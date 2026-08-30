from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Protocol

from .coordinator import Coordinator, LeaseRejected, SQLITE_MAX_INTEGER
from .engine import BitCrackEngine, EngineOutcome, EngineTuning
from .registry import get_puzzle

if TYPE_CHECKING:
    from .sweep import SweepNetwork, SweepReceipt


PROFILE_SCHEMA = 1
CHUNK_ALIGNMENT = 1 << 20


class LocalEngine(Protocol):
    def scan(self, puzzle, chunk) -> EngineOutcome: ...


@dataclass(frozen=True, slots=True)
class LocalProfile:
    schema: int
    puzzle: int
    binary: str
    tuning: EngineTuning
    measured_rate_keys_per_second: float
    benchmark_relative_spread: float
    chunk_size: int
    target_chunk_seconds: int
    planner_mode: str
    seed: str
    database: str
    benchmark_report: str
    device_probe: str
    created_at: str
    max_temperature_c: float = 82.0
    resume_temperature_c: float = 72.0
    thermal_poll_seconds: float = 3.0
    thermal_max_retries: int = 3
    hypothesis_enabled: bool = False
    hypothesis_research_percent: int = 10
    hypothesis_search_percent: int = 90
    auto_sweep_enabled: bool = False
    sweep_address: str = ""
    sweep_fee_floor_sat_vb: int = 25
    sweep_fee_cap_sat_vb: int = 500

    def __post_init__(self) -> None:
        if self.schema != PROFILE_SCHEMA:
            raise ValueError(f"unsupported local profile schema: {self.schema}")
        get_puzzle(self.puzzle)
        if not self.binary:
            raise ValueError("local profile binary path is empty")
        if not math.isfinite(self.measured_rate_keys_per_second) or (
            self.measured_rate_keys_per_second <= 0
        ):
            raise ValueError("local profile measured rate must be positive")
        if not math.isfinite(self.benchmark_relative_spread) or not (
            0 <= self.benchmark_relative_spread
        ):
            raise ValueError("local profile benchmark spread is invalid")
        if not 1 <= self.chunk_size <= SQLITE_MAX_INTEGER:
            raise ValueError("local profile chunk size is invalid")
        if not 10 <= self.target_chunk_seconds <= 86_400:
            raise ValueError("local profile target chunk duration is invalid")
        if self.planner_mode not in {"affine", "mosaic", "hypothesis"}:
            raise ValueError("local profile planner mode is invalid")
        if not self.seed:
            raise ValueError("local profile seed is empty")
        if not self.database or not self.benchmark_report:
            raise ValueError("local profile state paths are empty")
        if not 40 <= self.max_temperature_c <= 100:
            raise ValueError("local profile maximum temperature is invalid")
        if not 20 <= self.resume_temperature_c < self.max_temperature_c:
            raise ValueError("local profile resume temperature is invalid")
        if not 0.01 <= self.thermal_poll_seconds <= 60:
            raise ValueError("local profile thermal poll interval is invalid")
        if not 0 <= self.thermal_max_retries <= 100:
            raise ValueError("local profile thermal retry count is invalid")
        if not isinstance(self.hypothesis_enabled, bool):
            raise ValueError("local profile Hypothesis Lab flag is invalid")
        if (
            isinstance(self.hypothesis_research_percent, bool)
            or isinstance(self.hypothesis_search_percent, bool)
            or not 1 <= self.hypothesis_research_percent <= 50
            or not 1 <= self.hypothesis_search_percent <= 99
            or self.hypothesis_research_percent
            + self.hypothesis_search_percent
            != 100
        ):
            raise ValueError("local profile Hypothesis Lab ratio is invalid")
        if self.planner_mode == "hypothesis" and not self.hypothesis_enabled:
            raise ValueError("hypothesis planner requires Hypothesis Lab")
        if self.hypothesis_enabled and self.puzzle < 18:
            raise ValueError(
                "Hypothesis Lab requires a target after the training observations"
            )
        if not isinstance(self.auto_sweep_enabled, bool):
            raise ValueError("local profile auto-sweep flag is invalid")
        if self.sweep_address:
            from .sweep import decode_mainnet_p2wpkh

            decode_mainnet_p2wpkh(self.sweep_address)
        if self.auto_sweep_enabled and not self.sweep_address:
            raise ValueError("auto-sweep requires a destination address")
        if (
            isinstance(self.sweep_fee_floor_sat_vb, bool)
            or isinstance(self.sweep_fee_cap_sat_vb, bool)
            or not 1
            <= self.sweep_fee_floor_sat_vb
            <= self.sweep_fee_cap_sat_vb
            <= 10_000
        ):
            raise ValueError("local profile sweep fee bounds are invalid")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "LocalProfile":
        if not isinstance(payload, dict):
            raise ValueError("local profile must be a JSON object")
        tuning = payload.get("tuning")
        if not isinstance(tuning, dict):
            raise ValueError("local profile tuning is missing")
        try:
            planner_mode = str(payload["planner_mode"])
            return cls(
                schema=int(payload["schema"]),
                puzzle=int(payload["puzzle"]),
                binary=str(payload["binary"]),
                tuning=EngineTuning(
                    device=_optional_int(tuning.get("device")),
                    blocks=_optional_int(tuning.get("blocks")),
                    threads=_optional_int(tuning.get("threads")),
                    points=_optional_int(tuning.get("points")),
                ),
                measured_rate_keys_per_second=float(
                    payload["measured_rate_keys_per_second"]
                ),
                benchmark_relative_spread=float(
                    payload["benchmark_relative_spread"]
                ),
                chunk_size=int(payload["chunk_size"]),
                target_chunk_seconds=int(payload["target_chunk_seconds"]),
                planner_mode=planner_mode,
                seed=str(payload["seed"]),
                database=str(payload["database"]),
                benchmark_report=str(payload["benchmark_report"]),
                device_probe=str(payload.get("device_probe", "")),
                created_at=str(payload["created_at"]),
                max_temperature_c=float(payload.get("max_temperature_c", 82.0)),
                resume_temperature_c=float(payload.get("resume_temperature_c", 72.0)),
                thermal_poll_seconds=float(payload.get("thermal_poll_seconds", 3.0)),
                thermal_max_retries=int(payload.get("thermal_max_retries", 3)),
                hypothesis_enabled=_optional_bool(
                    payload.get(
                        "hypothesis_enabled", planner_mode == "hypothesis"
                    )
                ),
                hypothesis_research_percent=int(
                    payload.get("hypothesis_research_percent", 10)
                ),
                hypothesis_search_percent=int(
                    payload.get("hypothesis_search_percent", 90)
                ),
                auto_sweep_enabled=_optional_bool(
                    payload.get("auto_sweep_enabled", False)
                ),
                sweep_address=str(payload.get("sweep_address", "")),
                sweep_fee_floor_sat_vb=int(
                    payload.get("sweep_fee_floor_sat_vb", 25)
                ),
                sweep_fee_cap_sat_vb=int(
                    payload.get("sweep_fee_cap_sat_vb", 500)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid local profile: {exc}") from exc


@dataclass(frozen=True, slots=True)
class LocalRun:
    outcome: str
    message: str
    found: bool = False
    sweep_state: str | None = None
    sweep_txid: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def recommended_chunk_size(
    rate_keys_per_second: float,
    target_seconds: int,
    *,
    maximum_keys: int = SQLITE_MAX_INTEGER,
) -> int:
    rate = float(rate_keys_per_second)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("measured rate must be positive")
    if isinstance(target_seconds, bool) or not 10 <= target_seconds <= 86_400:
        raise ValueError("target chunk duration must be 10-86400 seconds")
    if isinstance(maximum_keys, bool) or not 1 <= maximum_keys <= SQLITE_MAX_INTEGER:
        raise ValueError("maximum_keys must fit a positive SQLite integer")

    raw = max(1, int(rate * target_seconds))
    if raw < CHUNK_ALIGNMENT:
        return min(raw, maximum_keys)
    aligned = (raw // CHUNK_ALIGNMENT) * CHUNK_ALIGNMENT
    return min(max(aligned, CHUNK_ALIGNMENT), maximum_keys)


def find_bitcrack_binary(
    explicit: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    working_directory = Path.cwd() if working_directory is None else working_directory

    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"BitCrack binary not found: {resolved}")
        return resolved

    candidates: list[Path] = []
    configured = environment.get("PUZZLEFORGE_BITCRACK")
    if configured:
        candidates.append(Path(configured).expanduser())

    names = ("cuBitCrack", "cuBitCrack.exe", "clBitCrack", "clBitCrack.exe")
    for name in names:
        candidates.extend(
            (
                working_directory / name,
                working_directory / ".puzzleforge" / "bin" / name,
            )
        )
        located = shutil.which(name, path=environment.get("PATH"))
        if located:
            candidates.append(Path(located))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(
        "BitCrack binary was not found; use --binary or PUZZLEFORGE_BITCRACK"
    )


def save_profile(path: Path, profile: LocalProfile) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n"
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


def load_profile(path: Path) -> LocalProfile:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"local profile contains invalid JSON: {exc}") from exc
    return LocalProfile.from_dict(payload)


def engine_from_profile(
    profile: LocalProfile,
    *,
    timeout_seconds: float | None = None,
    thermal_guard: bool = True,
) -> LocalEngine:
    abort_event = threading.Event() if thermal_guard else None
    engine = BitCrackEngine(
        Path(profile.binary),
        profile.tuning,
        timeout_seconds=timeout_seconds,
        abort_event=abort_event,
    )
    if not thermal_guard:
        return engine

    from .thermal import ThermalGuardedEngine, ThermalPolicy

    return ThermalGuardedEngine(
        engine,
        abort_event,
        device=profile.tuning.device,
        policy=ThermalPolicy(
            maximum_c=profile.max_temperature_c,
            resume_c=profile.resume_temperature_c,
            poll_seconds=profile.thermal_poll_seconds,
            max_retries=profile.thermal_max_retries,
        ),
    )


def run_local_once(
    profile: LocalProfile,
    engine: LocalEngine,
    *,
    worker: str,
    lease_seconds: int = 3_600,
    sweep_network: SweepNetwork | None = None,
) -> LocalRun:
    coordinator = Coordinator(Path(profile.database))
    lease = coordinator.lease(worker, lease_seconds=lease_seconds)
    if lease is None:
        state = coordinator.status()["state"]
        return LocalRun("idle", f"no work available; campaign state={state}")

    puzzle = get_puzzle(lease.puzzle)
    stopped = threading.Event()
    heartbeat_errors: list[BaseException] = []
    interval = max(5.0, min(60.0, lease_seconds / 3))

    def heartbeat_loop() -> None:
        while not stopped.wait(interval):
            try:
                coordinator.heartbeat(
                    lease.token,
                    lease.worker,
                    lease_seconds=lease_seconds,
                )
            except BaseException as exc:
                heartbeat_errors.append(exc)
                return

    heartbeat = threading.Thread(
        target=heartbeat_loop,
        name="puzzleforge-local-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        outcome = engine.scan(puzzle, lease.chunk)
    except BaseException:
        stopped.set()
        heartbeat.join(timeout=interval + 1)
        _release_failed_lease(coordinator, lease.token, lease.worker, "local run interrupted")
        raise
    finally:
        stopped.set()
    heartbeat.join(timeout=interval + 1)

    if heartbeat_errors:
        _release_failed_lease(
            coordinator,
            lease.token,
            lease.worker,
            f"local lease heartbeat failed: {heartbeat_errors[0]}",
        )
        raise RuntimeError(f"local lease heartbeat failed: {heartbeat_errors[0]}")

    if outcome.status == "error":
        coordinator.fail(lease.token, lease.worker, error=outcome.message)
        return LocalRun("error", outcome.message)

    completion = coordinator.complete(
        lease.token,
        lease.worker,
        checked=outcome.checked,
        found_key_hex=(
            None if outcome.found_key is None else f"{outcome.found_key:064x}"
        ),
        elapsed_seconds=outcome.elapsed_seconds,
        rate_keys_per_second=outcome.rate_keys_per_second,
    )
    sweep: SweepReceipt | None = None
    if completion.found and outcome.found_key is not None and profile.auto_sweep_enabled:
        sweep = _attempt_sweep(profile, outcome.found_key, coordinator, sweep_network)

    message = (
        f"chunk {lease.sequence} checked {outcome.checked:,} keys at "
        f"{outcome.rate_keys_per_second:,.0f} keys/s"
    )
    if completion.found:
        message = "MATCH VERIFIED. " + message
    if sweep is not None:
        if sweep.broadcast:
            message += f"; AUTO-SWEEP BROADCAST txid={sweep.txid}"
        else:
            message += f"; AUTO-SWEEP PENDING: {sweep.detail}"
    return LocalRun(
        "found" if completion.found else "complete",
        message,
        found=completion.found,
        sweep_state=None if sweep is None else sweep.state,
        sweep_txid=None if sweep is None else sweep.txid,
    )


def resume_pending_sweep(
    profile: LocalProfile,
    *,
    sweep_network: SweepNetwork | None = None,
) -> SweepReceipt | None:
    if not profile.auto_sweep_enabled:
        return None
    coordinator = Coordinator(Path(profile.database))
    status = coordinator.status()
    if status["state"] != "found":
        return None

    from .sweep import load_sweep_record

    record_path = _sweep_record_path(profile)
    record = load_sweep_record(record_path)
    if record and record.get("state") == "broadcast":
        from .sweep import SweepReceipt

        return SweepReceipt(
            state="broadcast",
            destination_address=str(record["destination_address"]),
            txid=str(record["txid"]),
            output_value_sats=int(record["output_value_sats"]),
            fee_sats=int(record["fee_sats"]),
            detail=str(record.get("detail", "")),
        )

    found_key_hex = status.get("found_key_hex")
    if not found_key_hex:
        return None
    return _attempt_sweep(
        profile,
        int(str(found_key_hex), 16),
        coordinator,
        sweep_network,
    )


def _attempt_sweep(
    profile: LocalProfile,
    private_key: int,
    coordinator: Coordinator,
    sweep_network: SweepNetwork | None,
) -> SweepReceipt:
    from .sweep import SweepError, SweepReceipt, execute_sweep

    puzzle = get_puzzle(profile.puzzle)
    try:
        receipt = execute_sweep(
            private_key,
            puzzle.address,
            profile.sweep_address,
            _sweep_record_path(profile),
            fee_floor=profile.sweep_fee_floor_sat_vb,
            fee_cap=profile.sweep_fee_cap_sat_vb,
            network=sweep_network,
        )
    except (OSError, RuntimeError, ValueError, SweepError) as exc:
        return SweepReceipt(
            state="pending",
            destination_address=profile.sweep_address,
            detail=str(exc),
        )
    if receipt.broadcast:
        coordinator.scrub_found_key(f"{private_key:064x}")
    return receipt


def _sweep_record_path(profile: LocalProfile) -> Path:
    return Path(profile.database).with_name("sweep.json")


def _release_failed_lease(
    coordinator: Coordinator, token: str, worker: str, error: str
) -> None:
    try:
        coordinator.fail(token, worker, error=error)
    except LeaseRejected:
        pass


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid tuning integer")
    return int(value)


def _optional_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("value must be a boolean")
    return value
