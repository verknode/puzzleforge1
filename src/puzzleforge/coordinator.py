from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .crypto import p2pkh_address_from_private_key
from .mosaic import MosaicPlanner
from .partition import ChunkPlan, KeyChunk
from .registry import get_puzzle


SCHEMA_VERSION = 3
SQLITE_MAX_INTEGER = (1 << 63) - 1
_WORKER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


class CoordinatorError(RuntimeError):
    pass


class LeaseRejected(CoordinatorError):
    pass


class _SQLiteSeen:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __len__(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM work").fetchone()[0]

    def __contains__(self, chunk_id: object) -> bool:
        if isinstance(chunk_id, bool) or not isinstance(chunk_id, int):
            return False
        return (
            self.connection.execute(
                "SELECT 1 FROM work WHERE chunk_id = ? LIMIT 1", (chunk_id,)
            ).fetchone()
            is not None
        )


@dataclass(frozen=True, slots=True)
class Lease:
    token: str
    worker: str
    puzzle: int
    address: str
    sequence: int
    ordinal: int
    chunk_id: int
    start_hex: str
    end_hex: str
    keys: int
    strategy_lane: str
    strategy_rank: int
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def chunk(self) -> KeyChunk:
        return KeyChunk(
            ordinal=self.ordinal,
            chunk_id=self.chunk_id,
            start=int(self.start_hex, 16),
            end=int(self.end_hex, 16),
        )


@dataclass(frozen=True, slots=True)
class Completion:
    accepted: bool
    idempotent: bool
    campaign_state: str
    found: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_worker(worker: str) -> str:
    if not isinstance(worker, str) or not _WORKER_PATTERN.fullmatch(worker):
        raise ValueError(
            "worker must be 1-128 safe characters: letters, digits, . _ : @ / -"
        )
    return worker


def _validate_lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 86_400:
        raise ValueError("lease_seconds must be an integer from 10 to 86400")
    return value


def _normalize_key_hex(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("found_key_hex must be a hexadecimal string")
    compact = value.strip().lower()
    if compact.startswith("0x"):
        compact = compact[2:]
    if not compact or len(compact) > 64 or any(c not in "0123456789abcdef" for c in compact):
        raise ValueError("found_key_hex must contain 1-64 hexadecimal digits")
    candidate = int(compact, 16)
    if candidate == 0:
        raise ValueError("private key must be non-zero")
    return f"{candidate:064x}"


class Coordinator:
    """Transactional, single-campaign lease coordinator.

    SQLite is intentionally enough for one coordinator process and many remote
    workers. Every state transition uses ``BEGIN IMMEDIATE`` so a chunk cannot
    be leased to two workers at once.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"coordinator database not found: {self.path}")
        with self._connect() as connection:
            self._prepare_schema(connection)
            self._load_campaign(connection)

    @classmethod
    def initialize(
        cls,
        path: Path,
        *,
        puzzle_number: int,
        chunk_size: int,
        seed: str,
        planner_mode: str = "affine",
    ) -> "Coordinator":
        if path.exists():
            raise FileExistsError(f"refusing to overwrite coordinator database: {path}")
        if isinstance(chunk_size, bool) or not 1 <= chunk_size <= SQLITE_MAX_INTEGER:
            raise ValueError("chunk_size must fit a positive SQLite integer")
        if not seed or len(seed.encode("utf-8")) > 512:
            raise ValueError("seed must contain 1-512 UTF-8 bytes")
        if planner_mode not in {"affine", "mosaic", "hypothesis"}:
            raise ValueError("planner_mode must be affine, mosaic, or hypothesis")

        hypothesis_enabled = planner_mode == "hypothesis"
        stored_planner_mode = "affine" if hypothesis_enabled else planner_mode
        if hypothesis_enabled and puzzle_number < 18:
            raise ValueError(
                "Hypothesis Lab requires a target after the training observations"
            )

        puzzle = get_puzzle(puzzle_number)
        plan = ChunkPlan(puzzle=puzzle, chunk_size=chunk_size, seed=seed)
        if plan.total_chunks > SQLITE_MAX_INTEGER:
            minimum = (puzzle.size + SQLITE_MAX_INTEGER - 1) // SQLITE_MAX_INTEGER
            raise ValueError(
                f"chunk_size is too small for SQLite; use at least {minimum} keys"
            )
        planner_state = (
            json.dumps(
                MosaicPlanner(plan.total_chunks, seed=seed).state(),
                separators=(",", ":"),
                sort_keys=True,
            )
            if stored_planner_mode == "mosaic"
            else None
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE campaign (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_version INTEGER NOT NULL,
                    puzzle INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    start_hex TEXT NOT NULL,
                    end_hex TEXT NOT NULL,
                    seed TEXT NOT NULL,
                    chunk_size INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    next_sequence INTEGER NOT NULL,
                    completed_chunks INTEGER NOT NULL,
                    checked_keys TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('running', 'found', 'exhausted')),
                    found_key_hex TEXT,
                    reclaimed_leases INTEGER NOT NULL,
                    total_failures INTEGER NOT NULL,
                    planner_mode TEXT NOT NULL CHECK (planner_mode IN ('affine', 'mosaic')),
                    planner_state TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE work (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence INTEGER NOT NULL UNIQUE,
                    ordinal INTEGER NOT NULL,
                    chunk_id INTEGER NOT NULL UNIQUE,
                    start_hex TEXT NOT NULL,
                    end_hex TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('available', 'leased', 'completed')),
                    worker TEXT,
                    lease_token TEXT UNIQUE,
                    lease_expires_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_checked INTEGER,
                    result_kind TEXT CHECK (
                        result_kind IS NULL OR result_kind IN ('nomatch', 'found')
                    ),
                    found_key_hex TEXT,
                    elapsed_seconds REAL,
                    rate_keys_per_second REAL,
                    strategy_lane TEXT NOT NULL,
                    strategy_rank INTEGER NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX work_state_id ON work(state, id);
                CREATE INDEX work_expiry ON work(state, lease_expires_at);

                CREATE TABLE hypothesis_lab (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    research_percent INTEGER NOT NULL,
                    search_percent INTEGER NOT NULL,
                    state_json TEXT,
                    report_json TEXT,
                    analyzed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO campaign (
                    id, schema_version, puzzle, address, start_hex, end_hex,
                    seed, chunk_size, total_chunks, next_sequence,
                    completed_chunks, checked_keys, state, found_key_hex,
                    reclaimed_leases, total_failures, planner_mode,
                    planner_state, created_at, updated_at
                ) VALUES (
                    1, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '0', 'running', NULL,
                    0, 0, ?, ?, ?, ?
                )
                """,
                (
                    SCHEMA_VERSION,
                    puzzle.number,
                    puzzle.address,
                    puzzle.start_hex,
                    puzzle.end_hex,
                    seed,
                    chunk_size,
                    plan.total_chunks,
                    stored_planner_mode,
                    planner_state,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO hypothesis_lab (
                    id, enabled, research_percent, search_percent,
                    state_json, report_json, analyzed_at, updated_at
                ) VALUES (1, ?, 10, 90, NULL, NULL, NULL, ?)
                """,
                (1 if hypothesis_enabled else 0, now),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            connection.close()
            path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()
        return cls(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "ALTER TABLE campaign ADD COLUMN planner_mode "
                    "TEXT NOT NULL DEFAULT 'affine'"
                )
                connection.execute(
                    "ALTER TABLE campaign ADD COLUMN planner_state TEXT"
                )
                connection.execute(
                    "ALTER TABLE work ADD COLUMN strategy_lane "
                    "TEXT NOT NULL DEFAULT 'affine'"
                )
                connection.execute(
                    "ALTER TABLE work ADD COLUMN strategy_rank INTEGER"
                )
                connection.execute(
                    "UPDATE work SET strategy_rank = sequence "
                    "WHERE strategy_rank IS NULL"
                )
                connection.execute(
                    "UPDATE campaign SET schema_version = ? WHERE id = 1",
                    (2,),
                )
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
                version = 2
            except BaseException:
                connection.rollback()
                raise
        if version == 2:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE hypothesis_lab (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        research_percent INTEGER NOT NULL,
                        search_percent INTEGER NOT NULL,
                        state_json TEXT,
                        report_json TEXT,
                        analyzed_at TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO hypothesis_lab (
                        id, enabled, research_percent, search_percent,
                        state_json, report_json, analyzed_at, updated_at
                    ) VALUES (1, 0, 10, 90, NULL, NULL, NULL, ?)
                    """,
                    (utc_now(),),
                )
                connection.execute(
                    "UPDATE campaign SET schema_version = ? WHERE id = 1",
                    (SCHEMA_VERSION,),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
                version = SCHEMA_VERSION
            except BaseException:
                connection.rollback()
                raise
        if version != SCHEMA_VERSION:
            raise CoordinatorError(
                f"unsupported coordinator schema {version}; expected {SCHEMA_VERSION}"
            )

    @staticmethod
    def _load_campaign(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM campaign WHERE id = 1").fetchone()
        if row is None:
            raise CoordinatorError("coordinator campaign is missing")
        puzzle = get_puzzle(row["puzzle"])
        if (
            row["address"] != puzzle.address
            or int(row["start_hex"], 16) != puzzle.start
            or int(row["end_hex"], 16) != puzzle.end
        ):
            raise CoordinatorError("database target does not match the reviewed registry")
        if row["schema_version"] != SCHEMA_VERSION:
            raise CoordinatorError("campaign schema marker is inconsistent")
        if row["planner_mode"] not in {"affine", "mosaic"}:
            raise CoordinatorError("campaign planner mode is invalid")
        if row["planner_mode"] == "mosaic":
            try:
                state = json.loads(row["planner_state"])
                planner = MosaicPlanner(row["total_chunks"], seed=row["seed"])
                planner.restore(state)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CoordinatorError("campaign MOSAIC state is invalid") from exc
        return row

    @staticmethod
    def _load_hypothesis(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM hypothesis_lab WHERE id = 1"
        ).fetchone()
        if row is None:
            raise CoordinatorError("Hypothesis Lab state is missing")
        research = row["research_percent"]
        search = row["search_percent"]
        if (
            isinstance(research, bool)
            or isinstance(search, bool)
            or not 1 <= research <= 50
            or not 1 <= search <= 99
            or research + search != 100
        ):
            raise CoordinatorError("Hypothesis Lab ratio is invalid")
        for name in ("state_json", "report_json"):
            if row[name] is not None:
                try:
                    payload = json.loads(row[name])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CoordinatorError(
                        f"Hypothesis Lab {name} is invalid"
                    ) from exc
                if not isinstance(payload, dict):
                    raise CoordinatorError(f"Hypothesis Lab {name} must be an object")
        return row

    def enable_hypothesis(
        self,
        *,
        research_percent: int = 10,
        search_percent: int = 90,
    ) -> None:
        if (
            isinstance(research_percent, bool)
            or isinstance(search_percent, bool)
            or not 1 <= research_percent <= 50
            or not 1 <= search_percent <= 99
            or research_percent + search_percent != 100
        ):
            raise ValueError("Hypothesis Lab percentages must total 100")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_hypothesis(connection)
                same_ratio = (
                    row["research_percent"] == research_percent
                    and row["search_percent"] == search_percent
                )
                connection.execute(
                    """
                    UPDATE hypothesis_lab
                       SET enabled = 1, research_percent = ?, search_percent = ?,
                           state_json = CASE WHEN ? THEN state_json ELSE NULL END,
                           report_json = CASE WHEN ? THEN report_json ELSE NULL END,
                           analyzed_at = CASE WHEN ? THEN analyzed_at ELSE NULL END,
                           updated_at = ?
                     WHERE id = 1
                    """,
                    (
                        research_percent,
                        search_percent,
                        1 if same_ratio else 0,
                        1 if same_ratio else 0,
                        1 if same_ratio else 0,
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _plan(campaign: sqlite3.Row) -> ChunkPlan:
        return ChunkPlan(
            puzzle=get_puzzle(campaign["puzzle"]),
            chunk_size=campaign["chunk_size"],
            seed=campaign["seed"],
        )

    @staticmethod
    def _allocate_chunk(
        connection: sqlite3.Connection,
        campaign: sqlite3.Row,
        sequence: int,
    ) -> tuple[KeyChunk, str, int, str | None]:
        hypothesis = Coordinator._load_hypothesis(connection)
        if hypothesis["enabled"]:
            from .hypothesis import HypothesisPlanner

            planner = HypothesisPlanner(
                campaign["total_chunks"],
                target_puzzle=campaign["puzzle"],
                seed=campaign["seed"],
                research_percent=hypothesis["research_percent"],
                search_percent=hypothesis["search_percent"],
            )
            if hypothesis["state_json"]:
                try:
                    planner.restore(json.loads(hypothesis["state_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CoordinatorError("Hypothesis Lab state is invalid") from exc
            candidate = planner.next_unseen(_SQLiteSeen(connection))
            now = utc_now()
            connection.execute(
                """
                UPDATE hypothesis_lab
                   SET state_json = ?, report_json = ?, analyzed_at = ?, updated_at = ?
                 WHERE id = 1
                """,
                (
                    json.dumps(
                        planner.state(), separators=(",", ":"), sort_keys=True
                    ),
                    json.dumps(
                        planner.last_report,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now if candidate.analysis_performed else hypothesis["analyzed_at"],
                    now,
                ),
            )
            puzzle = get_puzzle(campaign["puzzle"])
            start = puzzle.start + candidate.chunk_id * campaign["chunk_size"]
            end = min(start + campaign["chunk_size"] - 1, puzzle.end)
            chunk = KeyChunk(
                ordinal=sequence,
                chunk_id=candidate.chunk_id,
                start=start,
                end=end,
            )
            return (
                chunk,
                candidate.lane,
                candidate.strategy_rank,
                campaign["planner_state"],
            )

        if campaign["planner_mode"] == "affine":
            chunk = Coordinator._plan(campaign).chunk_for_sequence(sequence)
            return chunk, "affine", sequence, None

        planner = MosaicPlanner(campaign["total_chunks"], seed=campaign["seed"])
        planner.restore(json.loads(campaign["planner_state"]))
        candidate = planner.next_unseen(_SQLiteSeen(connection))
        puzzle = get_puzzle(campaign["puzzle"])
        start = puzzle.start + candidate.chunk_id * campaign["chunk_size"]
        end = min(start + campaign["chunk_size"] - 1, puzzle.end)
        chunk = KeyChunk(
            ordinal=sequence,
            chunk_id=candidate.chunk_id,
            start=start,
            end=end,
        )
        state = json.dumps(
            planner.state(), separators=(",", ":"), sort_keys=True
        )
        return chunk, candidate.lane, candidate.lane_rank, state

    @staticmethod
    def _reclaim_expired(
        connection: sqlite3.Connection, *, now_epoch: float, now_text: str
    ) -> int:
        cursor = connection.execute(
            """
            UPDATE work
               SET state = 'available', worker = NULL, lease_token = NULL,
                   lease_expires_at = NULL, updated_at = ?
             WHERE state = 'leased' AND lease_expires_at <= ?
            """,
            (now_text, now_epoch),
        )
        reclaimed = cursor.rowcount
        if reclaimed:
            connection.execute(
                """
                UPDATE campaign
                   SET reclaimed_leases = reclaimed_leases + ?, updated_at = ?
                 WHERE id = 1
                """,
                (reclaimed, now_text),
            )
        return reclaimed

    @staticmethod
    def _mark_exhausted_if_done(
        connection: sqlite3.Connection, campaign: sqlite3.Row, now_text: str
    ) -> str:
        if campaign["state"] != "running" or campaign["next_sequence"] < campaign["total_chunks"]:
            return campaign["state"]
        remaining = connection.execute(
            "SELECT COUNT(*) FROM work WHERE state != 'completed'"
        ).fetchone()[0]
        if remaining:
            return campaign["state"]
        connection.execute(
            "UPDATE campaign SET state = 'exhausted', updated_at = ? WHERE id = 1",
            (now_text,),
        )
        return "exhausted"

    def lease(
        self,
        worker: str,
        *,
        lease_seconds: int = 900,
        now_epoch: float | None = None,
    ) -> Lease | None:
        worker = _validate_worker(worker)
        lease_seconds = _validate_lease_seconds(lease_seconds)
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now_text = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reclaim_expired(
                    connection, now_epoch=now_epoch, now_text=now_text
                )
                campaign = self._load_campaign(connection)
                if campaign["state"] != "running":
                    connection.commit()
                    return None

                row = connection.execute(
                    "SELECT * FROM work WHERE state = 'available' ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    sequence = campaign["next_sequence"]
                    if sequence >= campaign["total_chunks"]:
                        self._mark_exhausted_if_done(connection, campaign, now_text)
                        connection.commit()
                        return None
                    chunk, strategy_lane, strategy_rank, planner_state = (
                        self._allocate_chunk(connection, campaign, sequence)
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO work (
                            sequence, ordinal, chunk_id, start_hex, end_hex, size,
                            state, attempts, strategy_lane, strategy_rank,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'available', 0, ?, ?, ?, ?)
                        """,
                        (
                            sequence,
                            chunk.ordinal,
                            chunk.chunk_id,
                            f"{chunk.start:x}",
                            f"{chunk.end:x}",
                            chunk.size,
                            strategy_lane,
                            strategy_rank,
                            now_text,
                            now_text,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE campaign
                           SET next_sequence = ?, planner_state = ?, updated_at = ?
                         WHERE id = 1
                        """,
                        (sequence + 1, planner_state, now_text),
                    )
                    row = connection.execute(
                        "SELECT * FROM work WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()

                token = secrets.token_urlsafe(32)
                expires_at = now_epoch + lease_seconds
                connection.execute(
                    """
                    UPDATE work
                       SET state = 'leased', worker = ?, lease_token = ?,
                           lease_expires_at = ?, attempts = attempts + 1,
                           last_error = NULL, updated_at = ?
                     WHERE id = ? AND state = 'available'
                    """,
                    (worker, token, expires_at, now_text, row["id"]),
                )
                campaign = self._load_campaign(connection)
                connection.commit()
                return Lease(
                    token=token,
                    worker=worker,
                    puzzle=campaign["puzzle"],
                    address=campaign["address"],
                    sequence=row["sequence"],
                    ordinal=row["ordinal"],
                    chunk_id=row["chunk_id"],
                    start_hex=row["start_hex"],
                    end_hex=row["end_hex"],
                    keys=row["size"],
                    strategy_lane=row["strategy_lane"],
                    strategy_rank=row["strategy_rank"],
                    expires_at=expires_at,
                )
            except BaseException:
                connection.rollback()
                raise

    def heartbeat(
        self,
        token: str,
        worker: str,
        *,
        lease_seconds: int = 900,
        now_epoch: float | None = None,
    ) -> float:
        worker = _validate_worker(worker)
        lease_seconds = _validate_lease_seconds(lease_seconds)
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now_text = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM work WHERE lease_token = ?", (token,)
                ).fetchone()
                if row is None or row["state"] != "leased" or row["worker"] != worker:
                    raise LeaseRejected("lease is unknown, stale, or owned by another worker")
                if row["lease_expires_at"] <= now_epoch:
                    connection.execute(
                        """
                        UPDATE work SET state = 'available', worker = NULL,
                               lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                         WHERE id = ?
                        """,
                        (now_text, row["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE campaign SET reclaimed_leases = reclaimed_leases + 1,
                               updated_at = ? WHERE id = 1
                        """,
                        (now_text,),
                    )
                    connection.commit()
                    raise LeaseRejected("lease expired before heartbeat")
                expires_at = now_epoch + lease_seconds
                connection.execute(
                    "UPDATE work SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                    (expires_at, now_text, row["id"]),
                )
                connection.commit()
                return expires_at
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def complete(
        self,
        token: str,
        worker: str,
        *,
        checked: int,
        found_key_hex: str | None = None,
        elapsed_seconds: float | None = None,
        rate_keys_per_second: float | None = None,
        now_epoch: float | None = None,
    ) -> Completion:
        worker = _validate_worker(worker)
        if isinstance(checked, bool) or not isinstance(checked, int) or checked < 0:
            raise ValueError("checked must be a non-negative integer")
        normalized_key = _normalize_key_hex(found_key_hex)
        elapsed = _finite_nonnegative(elapsed_seconds, "elapsed_seconds")
        rate = _finite_nonnegative(rate_keys_per_second, "rate_keys_per_second")
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now_text = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM work WHERE lease_token = ?", (token,)
                ).fetchone()
                if row is None or row["worker"] != worker:
                    raise LeaseRejected("lease is unknown, stale, or owned by another worker")

                result_kind = "found" if normalized_key else "nomatch"
                if row["state"] == "completed":
                    same = (
                        row["result_checked"] == checked
                        and row["result_kind"] == result_kind
                        and row["found_key_hex"] == normalized_key
                    )
                    if not same:
                        raise LeaseRejected("lease was already completed with a different result")
                    campaign = self._load_campaign(connection)
                    connection.commit()
                    return Completion(
                        accepted=True,
                        idempotent=True,
                        campaign_state=campaign["state"],
                        found=result_kind == "found",
                    )

                if row["state"] != "leased":
                    raise LeaseRejected("lease is not active")
                if row["lease_expires_at"] <= now_epoch:
                    connection.execute(
                        """
                        UPDATE work SET state = 'available', worker = NULL,
                               lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                         WHERE id = ?
                        """,
                        (now_text, row["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE campaign SET reclaimed_leases = reclaimed_leases + 1,
                               updated_at = ? WHERE id = 1
                        """,
                        (now_text,),
                    )
                    connection.commit()
                    raise LeaseRejected("lease expired before completion")

                if normalized_key is None:
                    if checked != row["size"]:
                        raise LeaseRejected(
                            "a no-match result must account for the entire leased range"
                        )
                else:
                    if checked > row["size"]:
                        raise LeaseRejected("checked exceeds the leased range")
                    candidate = int(normalized_key, 16)
                    start = int(row["start_hex"], 16)
                    end = int(row["end_hex"], 16)
                    campaign = self._load_campaign(connection)
                    puzzle = get_puzzle(campaign["puzzle"])
                    if not start <= candidate <= end:
                        raise LeaseRejected("reported key is outside the leased range")
                    if p2pkh_address_from_private_key(candidate) != puzzle.address:
                        raise LeaseRejected("reported key failed independent address verification")

                connection.execute(
                    """
                    UPDATE work
                       SET state = 'completed', result_checked = ?, result_kind = ?,
                           found_key_hex = ?, elapsed_seconds = ?,
                           rate_keys_per_second = ?, lease_expires_at = NULL,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        checked,
                        result_kind,
                        normalized_key,
                        elapsed,
                        rate,
                        now_text,
                        row["id"],
                    ),
                )
                campaign = self._load_campaign(connection)
                new_checked = int(campaign["checked_keys"]) + checked
                completed_increment = 1 if result_kind == "nomatch" else 0
                state = "found" if result_kind == "found" else campaign["state"]
                found_value = (
                    normalized_key
                    if result_kind == "found"
                    else campaign["found_key_hex"]
                )
                connection.execute(
                    """
                    UPDATE campaign
                       SET checked_keys = ?, completed_chunks = completed_chunks + ?,
                           state = ?, found_key_hex = ?, updated_at = ?
                     WHERE id = 1
                    """,
                    (str(new_checked), completed_increment, state, found_value, now_text),
                )
                campaign = self._load_campaign(connection)
                if result_kind == "nomatch":
                    state = self._mark_exhausted_if_done(connection, campaign, now_text)
                connection.commit()
                return Completion(
                    accepted=True,
                    idempotent=False,
                    campaign_state=state,
                    found=result_kind == "found",
                )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def fail(self, token: str, worker: str, *, error: str) -> None:
        worker = _validate_worker(worker)
        clean_error = " ".join(str(error).split())[:2_000]
        if not clean_error:
            clean_error = "worker reported an unspecified error"
        now_text = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM work WHERE lease_token = ?", (token,)
                ).fetchone()
                if row is None or row["state"] != "leased" or row["worker"] != worker:
                    raise LeaseRejected("lease is unknown, stale, or owned by another worker")
                connection.execute(
                    """
                    UPDATE work
                       SET state = 'available', worker = NULL, lease_token = NULL,
                           lease_expires_at = NULL, last_error = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (clean_error, now_text, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE campaign SET total_failures = total_failures + 1,
                           updated_at = ? WHERE id = 1
                    """,
                    (now_text,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def record_verified_candidate(self, found_key_hex: str) -> Completion:
        """Record a challenge key recovered outside a leased range scan.

        Generator research never receives coverage credit.  A candidate can
        stop the campaign only after the coordinator independently verifies
        its interval and registered public-puzzle address.
        """

        normalized = _normalize_key_hex(found_key_hex)
        if normalized is None:
            raise ValueError("a recovered candidate key is required")
        now_text = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                campaign = self._load_campaign(connection)
                if campaign["state"] == "found":
                    if campaign["found_key_hex"] == normalized:
                        connection.commit()
                        return Completion(
                            accepted=True,
                            idempotent=True,
                            campaign_state="found",
                            found=True,
                        )
                    raise LeaseRejected("campaign already found a different key")

                puzzle = get_puzzle(campaign["puzzle"])
                candidate = int(normalized, 16)
                if not puzzle.start <= candidate <= puzzle.end:
                    raise LeaseRejected("candidate is outside the published interval")
                if p2pkh_address_from_private_key(candidate) != puzzle.address:
                    raise LeaseRejected(
                        "candidate failed independent address verification"
                    )
                connection.execute(
                    """
                    UPDATE campaign
                       SET state = 'found', found_key_hex = ?, updated_at = ?
                     WHERE id = 1
                    """,
                    (normalized, now_text),
                )
                connection.commit()
                return Completion(
                    accepted=True,
                    idempotent=False,
                    campaign_state="found",
                    found=True,
                )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def scrub_found_key(self, expected_key_hex: str) -> bool:
        """Remove a recovered key after a signed sweep is durably broadcast."""

        normalized = _normalize_key_hex(expected_key_hex)
        if normalized is None:
            raise ValueError("expected key is required")
        now_text = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                campaign = self._load_campaign(connection)
                if campaign["state"] != "found":
                    raise LeaseRejected("campaign has not found a key")
                stored = campaign["found_key_hex"]
                if stored is None:
                    connection.commit()
                    return False
                if stored != normalized:
                    raise LeaseRejected("found key does not match the expected key")
                connection.execute(
                    "UPDATE work SET found_key_hex = NULL, updated_at = ? "
                    "WHERE result_kind = 'found' AND found_key_hex = ?",
                    (now_text, normalized),
                )
                connection.execute(
                    "UPDATE campaign SET found_key_hex = NULL, updated_at = ? WHERE id = 1",
                    (now_text,),
                )
                connection.commit()
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def range_map(self, *, bins: int = 4096) -> dict[str, Any]:
        """Return a sparse, coarse map of allocated chunks across the keyspace.

        A map cell represents a contiguous group of chunk ids.  Sparse state
        lists keep the dashboard response small while preserving exact work
        locations.  A marked cell means that it contains one or more chunks in
        that state; it does not claim that the whole cell has been searched.
        """

        if (
            isinstance(bins, bool)
            or not isinstance(bins, int)
            or not 64 <= bins <= 16_384
        ):
            raise ValueError("range-map bins must be an integer from 64 to 16384")

        with self._connect() as connection:
            campaign = self._load_campaign(connection)
            total_chunks = int(campaign["total_chunks"])
            actual_bins = min(bins, total_chunks)
            bucket_span = (total_chunks + actual_bins - 1) // actual_bins
            rows = connection.execute(
                """
                SELECT CAST(chunk_id / ? AS INTEGER) AS bucket,
                       state,
                       COUNT(*) AS count
                  FROM work
                 GROUP BY bucket, state
                 ORDER BY bucket, state
                """,
                (bucket_span,),
            ).fetchall()

        states: dict[str, list[list[int]]] = {
            "completed": [],
            "active": [],
            "retry": [],
        }
        state_names = {
            "completed": "completed",
            "leased": "active",
            "available": "retry",
        }
        for row in rows:
            states[state_names[row["state"]]].append(
                [int(row["bucket"]), int(row["count"])]
            )

        return {
            "schema": 1,
            "puzzle": int(campaign["puzzle"]),
            "start_hex": str(campaign["start_hex"]),
            "end_hex": str(campaign["end_hex"]),
            "chunk_size": str(campaign["chunk_size"]),
            "total_chunks": str(total_chunks),
            "bins": actual_bins,
            "bucket_span_chunks": str(bucket_span),
            "checked_keys": str(campaign["checked_keys"]),
            "states": states,
            "updated_at": str(campaign["updated_at"]),
        }

    def status(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now_text = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reclaim_expired(
                    connection, now_epoch=now_epoch, now_text=now_text
                )
                campaign = self._load_campaign(connection)
                state = self._mark_exhausted_if_done(connection, campaign, now_text)
                if state != campaign["state"]:
                    campaign = self._load_campaign(connection)
                counts = {
                    row["state"]: row["count"]
                    for row in connection.execute(
                        "SELECT state, COUNT(*) AS count FROM work GROUP BY state"
                    )
                }
                metrics = connection.execute(
                    """
                    SELECT COALESCE(SUM(attempts), 0) AS attempts,
                           AVG(rate_keys_per_second) AS average_rate
                      FROM work
                    """
                ).fetchone()
                strategy_rows = connection.execute(
                    """
                    SELECT strategy_lane, state, COUNT(*) AS count
                      FROM work
                     GROUP BY strategy_lane, state
                     ORDER BY strategy_lane, state
                    """
                ).fetchall()
                hypothesis = self._load_hypothesis(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        puzzle = get_puzzle(campaign["puzzle"])
        checked = int(campaign["checked_keys"])
        with localcontext() as context:
            context.prec = 80
            total_decimal = Decimal(puzzle.size)
            checked_decimal = Decimal(checked)
            unique_probability = checked_decimal / total_decimal
            random_probability = Decimal(1) - (
                Decimal(1) - (Decimal(1) / total_decimal)
            ) ** checked
            no_repeat_advantage = unique_probability - random_probability
        strategy_lanes: dict[str, dict[str, int]] = {}
        for row in strategy_rows:
            lane = strategy_lanes.setdefault(
                row["strategy_lane"],
                {"available": 0, "leased": 0, "completed": 0},
            )
            lane[row["state"]] = row["count"]
        hypothesis_state = (
            json.loads(hypothesis["state_json"])
            if hypothesis["state_json"]
            else None
        )
        hypothesis_report = (
            json.loads(hypothesis["report_json"])
            if hypothesis["report_json"]
            else None
        )
        hypothesis_status = {
            "enabled": bool(hypothesis["enabled"]),
            "research_percent": hypothesis["research_percent"],
            "search_percent": hypothesis["search_percent"],
            "cycle": 0 if hypothesis_state is None else hypothesis_state["cycle"],
            "queued_search_slots": (
                0 if hypothesis_state is None else len(hypothesis_state["queue"])
            ),
            "analyzed_at": hypothesis["analyzed_at"],
            "report": hypothesis_report,
        }
        return {
            "schema_version": campaign["schema_version"],
            "puzzle": puzzle.number,
            "address": puzzle.address,
            "state": campaign["state"],
            "planner_mode": (
                "hypothesis" if hypothesis["enabled"] else campaign["planner_mode"]
            ),
            "base_planner_mode": campaign["planner_mode"],
            "seed": campaign["seed"],
            "chunk_size": campaign["chunk_size"],
            "total_chunks": campaign["total_chunks"],
            "allocated_chunks": campaign["next_sequence"],
            "completed_chunks": campaign["completed_chunks"],
            "checked_keys": str(checked),
            "total_keys": str(puzzle.size),
            "exact_unique_probability": str(unique_probability),
            "random_with_replacement_probability": str(random_probability),
            "no_repeat_advantage": str(no_repeat_advantage),
            "active_leases": counts.get("leased", 0),
            "retry_queue": counts.get("available", 0),
            "work_attempts": metrics["attempts"],
            "reclaimed_leases": campaign["reclaimed_leases"],
            "worker_failures": campaign["total_failures"],
            "average_reported_rate": metrics["average_rate"] or 0.0,
            "strategy_lanes": strategy_lanes,
            "hypothesis_lab": hypothesis_status,
            "found_key_hex": campaign["found_key_hex"],
            "created_at": campaign["created_at"],
            "updated_at": campaign["updated_at"],
        }


def _finite_nonnegative(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed
