from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .partition import ChunkPlan


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Checkpoint:
    schema: int
    puzzle: int
    seed: str
    chunk_size: int
    shard_count: int
    shard_index: int
    next_sequence: int
    completed_chunks: int
    keys_scanned: int
    created_at: str
    updated_at: str
    found_key_hex: str | None = None

    @classmethod
    def create(cls, plan: ChunkPlan) -> "Checkpoint":
        now = utc_now()
        return cls(
            schema=1,
            puzzle=plan.puzzle.number,
            seed=plan.seed,
            chunk_size=plan.chunk_size,
            shard_count=plan.shard_count,
            shard_index=plan.shard_index,
            next_sequence=0,
            completed_chunks=0,
            keys_scanned=0,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Checkpoint":
        checkpoint = cls(**raw)
        if checkpoint.schema != 1:
            raise ValueError(f"unsupported checkpoint schema: {checkpoint.schema}")
        return checkpoint

    def assert_matches(self, plan: ChunkPlan) -> None:
        expected = (
            plan.puzzle.number,
            plan.seed,
            plan.chunk_size,
            plan.shard_count,
            plan.shard_index,
        )
        actual = (
            self.puzzle,
            self.seed,
            self.chunk_size,
            self.shard_count,
            self.shard_index,
        )
        if actual != expected:
            raise ValueError("checkpoint does not match this scan plan")


def load_checkpoint(path: Path) -> Checkpoint:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("checkpoint root must be a JSON object")
    return Checkpoint.from_dict(raw)


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.updated_at = utc_now()
    payload = json.dumps(asdict(checkpoint), indent=2, sort_keys=True) + "\n"
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

