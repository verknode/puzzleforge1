from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .model import Puzzle


@dataclass(frozen=True, slots=True)
class KeyChunk:
    ordinal: int
    chunk_id: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


class ChunkPlan:
    """Bijective chunk order with deterministic multi-machine sharding."""

    def __init__(
        self,
        puzzle: Puzzle,
        chunk_size: int,
        seed: str,
        shard_count: int = 1,
        shard_index: int = 0,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk size must be positive")
        if shard_count < 1:
            raise ValueError("shard count must be positive")
        if not 0 <= shard_index < shard_count:
            raise ValueError("shard index must be in [0, shard_count)")
        if not seed:
            raise ValueError("seed must not be empty")

        self.puzzle = puzzle
        self.chunk_size = chunk_size
        self.seed = seed
        self.shard_count = shard_count
        self.shard_index = shard_index
        self.total_chunks = (puzzle.size + chunk_size - 1) // chunk_size
        self._multiplier, self._offset = self._derive_affine_parameters()

    def _derive_affine_parameters(self) -> tuple[int, int]:
        if self.total_chunks == 1:
            return 0, 0
        digest = hashlib.blake2b(
            f"PuzzleForge/v1/{self.puzzle.number}/{self.seed}".encode(),
            digest_size=32,
        ).digest()
        candidate = int.from_bytes(digest[:16], "big") % self.total_chunks
        candidate = candidate or 1
        while math.gcd(candidate, self.total_chunks) != 1:
            candidate = (candidate + 1) % self.total_chunks
            candidate = candidate or 1
        offset = int.from_bytes(digest[16:], "big") % self.total_chunks
        return candidate, offset

    @property
    def chunks_for_shard(self) -> int:
        remaining = self.total_chunks - self.shard_index
        return 0 if remaining <= 0 else (remaining + self.shard_count - 1) // self.shard_count

    def ordinal_for_sequence(self, sequence: int) -> int:
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        ordinal = self.shard_index + sequence * self.shard_count
        if ordinal >= self.total_chunks:
            raise IndexError("shard has no more chunks")
        return ordinal

    def chunk_for_sequence(self, sequence: int) -> KeyChunk:
        ordinal = self.ordinal_for_sequence(sequence)
        chunk_id = (
            (self._multiplier * ordinal + self._offset) % self.total_chunks
            if self.total_chunks > 1
            else 0
        )
        start = self.puzzle.start + chunk_id * self.chunk_size
        end = min(start + self.chunk_size - 1, self.puzzle.end)
        return KeyChunk(ordinal=ordinal, chunk_id=chunk_id, start=start, end=end)

    def manifest(self) -> dict[str, int | str]:
        return {
            "puzzle": self.puzzle.number,
            "seed": self.seed,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "shard_count": self.shard_count,
            "shard_index": self.shard_index,
            "chunks_for_shard": self.chunks_for_shard,
            "permutation_multiplier": self._multiplier,
            "permutation_offset": self._offset,
        }

