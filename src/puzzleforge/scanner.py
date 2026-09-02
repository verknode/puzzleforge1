from __future__ import annotations

import hashlib
import os
from concurrent.futures import Executor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .checkpoint import Checkpoint, load_checkpoint, save_checkpoint
from .crypto import DEFAULT_BATCH_SIZE, decode_p2pkh, iter_sequential_points
from .model import Puzzle
from .partition import ChunkPlan, KeyChunk


_COMPRESSED_PREFIXES = (b"\x02", b"\x03")


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


def _scan_contiguous(
    start: int,
    end: int,
    target_hash160: bytes,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ScanResult:
    if end < start:
        return ScanResult(checked=0)
    try:
        hashlib.new("ripemd160")
    except ValueError as exc:
        raise RuntimeError(
            "this Python/OpenSSL build does not provide RIPEMD-160"
        ) from exc

    sha256 = hashlib.sha256
    ripemd160 = hashlib.new
    prefixes = _COMPRESSED_PREFIXES
    private_key = start
    checked = 0
    for x, y in iter_sequential_points(start, end - start + 1, batch_size):
        digest = ripemd160(
            "ripemd160",
            sha256(prefixes[y & 1] + x.to_bytes(32, "big")).digest(),
        ).digest()
        checked += 1
        if digest == target_hash160:
            return ScanResult(checked=checked, found_key=private_key)
        private_key += 1
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


def _worker_count(workers: int | None) -> int:
    return max(1, workers or (os.cpu_count() or 1))


def _scan_parallel(
    executor: Executor,
    intervals: list[tuple[int, int]],
    target: bytes,
    batch_size: int,
) -> ScanResult:
    checked = 0
    found: int | None = None
    futures = [
        executor.submit(_scan_contiguous, start, end, target, batch_size)
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


def scan_chunk(
    puzzle: Puzzle,
    chunk: KeyChunk,
    workers: int | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    executor: Executor | None = None,
) -> ScanResult:
    if chunk.start < puzzle.start or chunk.end > puzzle.end:
        raise ValueError("chunk is outside the reviewed puzzle interval")
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    target = decode_p2pkh(puzzle.address)
    intervals = _split_interval(chunk.start, chunk.end, _worker_count(workers))
    if len(intervals) == 1:
        start, end = intervals[0]
        return _scan_contiguous(start, end, target, batch_size)

    if executor is not None:
        return _scan_parallel(executor, intervals, target, batch_size)
    with ProcessPoolExecutor(max_workers=len(intervals)) as pool:
        return _scan_parallel(pool, intervals, target, batch_size)


def run_session(
    plan: ChunkPlan,
    chunks: int,
    workers: int | None,
    checkpoint_path: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SessionResult:
    if chunks < 1:
        raise ValueError("chunks must be positive")
    if batch_size < 1:
        raise ValueError("batch size must be positive")

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

    worker_count = _worker_count(workers)
    if worker_count > 1:
        # One pool for the whole session; a fresh pool per chunk pays the
        # process-spawn cost again for every checkpoint.
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            return _run_chunks(
                plan, chunks, workers, checkpoint_path, checkpoint, batch_size, pool
            )
    return _run_chunks(
        plan, chunks, workers, checkpoint_path, checkpoint, batch_size, None
    )


def _run_chunks(
    plan: ChunkPlan,
    chunks: int,
    workers: int | None,
    checkpoint_path: Path,
    checkpoint: Checkpoint,
    batch_size: int,
    executor: Executor | None,
) -> SessionResult:
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

        result = scan_chunk(
            plan.puzzle, chunk, workers, batch_size=batch_size, executor=executor
        )
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
