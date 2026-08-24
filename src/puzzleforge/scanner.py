from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .checkpoint import Checkpoint, load_checkpoint, save_checkpoint
from .crypto import (
    GENERATOR,
    compressed_public_key,
    decode_p2pkh,
    hash160,
    point_add,
    scalar_multiply,
)
from .model import Puzzle
from .partition import ChunkPlan, KeyChunk


@dataclass(frozen=True, slots=True)
class ScanResult:
    checked: int
    found_key: int | None = None


@dataclass(frozen=True, slots=True)
class SessionResult:
    checked: int
    completed_chunks: int
    found_key: int | None
    exhausted: bool


def _scan_contiguous(start: int, end: int, target_hash160: bytes) -> ScanResult:
    if end < start:
        return ScanResult(checked=0)
    point = scalar_multiply(start)
    checked = 0
    for private_key in range(start, end + 1):
        if hash160(compressed_public_key(point)) == target_hash160:
            return ScanResult(checked=checked + 1, found_key=private_key)
        checked += 1
        if private_key != end:
            point = point_add(point, GENERATOR)
    return ScanResult(checked=checked)


def _split_interval(start: int, end: int, parts: int) -> list[tuple[int, int]]:
    size = end - start + 1
    parts = max(1, min(parts, size))
    base, extra = divmod(size, parts)
    intervals: list[tuple[int, int]] = []
    cursor = start
    for index in range(parts):
        width = base + (1 if index < extra else 0)
        intervals.append((cursor, cursor + width - 1))
        cursor += width
    return intervals


def scan_chunk(puzzle: Puzzle, chunk: KeyChunk, workers: int | None = None) -> ScanResult:
    if chunk.start < puzzle.start or chunk.end > puzzle.end:
        raise ValueError("chunk is outside the reviewed puzzle interval")
    target = decode_p2pkh(puzzle.address)
    worker_count = workers or (os.cpu_count() or 1)
    intervals = _split_interval(chunk.start, chunk.end, worker_count)
    if len(intervals) == 1:
        return _scan_contiguous(*intervals[0], target)

    checked = 0
    found: int | None = None
    with ProcessPoolExecutor(max_workers=len(intervals)) as executor:
        futures = [
            executor.submit(_scan_contiguous, start, end, target)
            for start, end in intervals
        ]
        for future in as_completed(futures):
            if future.cancelled():
                continue
            result = future.result()
            checked += result.checked
            if result.found_key is not None and found is None:
                found = result.found_key
                for pending in futures:
                    pending.cancel()
    return ScanResult(checked=checked, found_key=found)


def run_session(
    plan: ChunkPlan,
    chunks: int,
    workers: int | None,
    checkpoint_path: Path,
) -> SessionResult:
    if chunks < 1:
        raise ValueError("chunks must be positive")

    if checkpoint_path.exists():
        checkpoint = load_checkpoint(checkpoint_path)
        checkpoint.assert_matches(plan)
    else:
        checkpoint = Checkpoint.create(plan)
        save_checkpoint(checkpoint_path, checkpoint)

    if checkpoint.found_key_hex:
        return SessionResult(
            checked=0,
            completed_chunks=0,
            found_key=int(checkpoint.found_key_hex, 16),
            exhausted=False,
        )

    checked_this_run = 0
    completed_this_run = 0
    found: int | None = None

    for _ in range(chunks):
        try:
            chunk = plan.chunk_for_sequence(checkpoint.next_sequence)
        except IndexError:
            return SessionResult(
                checked=checked_this_run,
                completed_chunks=completed_this_run,
                found_key=None,
                exhausted=True,
            )

        result = scan_chunk(plan.puzzle, chunk, workers)
        checked_this_run += result.checked
        checkpoint.keys_scanned += result.checked

        if result.found_key is not None:
            found = result.found_key
            checkpoint.found_key_hex = f"{found:064x}"
            save_checkpoint(checkpoint_path, checkpoint)
            break

        checkpoint.next_sequence += 1
        checkpoint.completed_chunks += 1
        completed_this_run += 1
        save_checkpoint(checkpoint_path, checkpoint)

    return SessionResult(
        checked=checked_this_run,
        completed_chunks=completed_this_run,
        found_key=found,
        exhausted=checkpoint.next_sequence >= plan.chunks_for_shard,
    )
