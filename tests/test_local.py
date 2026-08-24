import tempfile
import unittest
from pathlib import Path

from puzzleforge.coordinator import Coordinator
from puzzleforge.engine import EngineOutcome, EngineTuning
from puzzleforge.local import (
    CHUNK_ALIGNMENT,
    LocalProfile,
    find_bitcrack_binary,
    load_profile,
    recommended_chunk_size,
    run_local_once,
    save_profile,
)


class CompleteEngine:
    def __init__(self, rate: float = 2_000_000.0) -> None:
        self.rate = rate

    def scan(self, puzzle, chunk):
        return EngineOutcome(
            status="complete",
            checked=chunk.size,
            elapsed_seconds=chunk.size / self.rate,
            rate_keys_per_second=self.rate,
            message="complete",
        )


class ErrorEngine:
    def scan(self, puzzle, chunk):
        return EngineOutcome(
            status="error",
            checked=0,
            elapsed_seconds=0.1,
            rate_keys_per_second=0.0,
            message="device reset",
        )


class LocalTests(unittest.TestCase):
    def make_profile(self, directory: str) -> LocalProfile:
        root = Path(directory)
        database = root / "campaign.sqlite3"
        Coordinator.initialize(
            database,
            puzzle_number=71,
            chunk_size=256,
            seed="local-tests",
        )
        return LocalProfile(
            schema=1,
            puzzle=71,
            binary=str(root / "cuBitCrack"),
            tuning=EngineTuning(device=0, blocks=32, threads=256, points=512),
            measured_rate_keys_per_second=2_000_000.0,
            benchmark_relative_spread=0.01,
            chunk_size=256,
            target_chunk_seconds=300,
            planner_mode="affine",
            seed="local-tests",
            database=str(database),
            benchmark_report=str(root / "benchmark.json"),
            device_probe="Fake GPU",
            created_at="2026-08-24T00:00:00+00:00",
        )

    def test_chunk_size_uses_measured_rate_and_alignment(self) -> None:
        size = recommended_chunk_size(6_100_000_000, 300)
        self.assertEqual(size % CHUNK_ALIGNMENT, 0)
        self.assertLessEqual(size, 6_100_000_000 * 300)
        self.assertGreater(size, 1_800_000_000_000)

    def test_profile_round_trip_is_atomic_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = self.make_profile(directory)
            path = Path(directory) / "profile.json"
            save_profile(path, profile)
            loaded = load_profile(path)
        self.assertEqual(loaded, profile)
        self.assertEqual(loaded.tuning.threads, 256)

    def test_explicit_binary_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cuBitCrack"
            binary.touch()
            self.assertEqual(find_bitcrack_binary(binary), binary.resolve())

    def test_completed_local_chunk_is_durable_and_next_chunk_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = self.make_profile(directory)
            first = run_local_once(
                profile, CompleteEngine(), worker="local-test", lease_seconds=60
            )
            second = run_local_once(
                profile, CompleteEngine(), worker="local-test", lease_seconds=60
            )
            status = Coordinator(Path(profile.database)).status()
        self.assertEqual(first.outcome, "complete")
        self.assertEqual(second.outcome, "complete")
        self.assertEqual(status["completed_chunks"], 2)
        self.assertEqual(status["checked_keys"], "512")

    def test_failed_local_chunk_returns_to_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = self.make_profile(directory)
            failed = run_local_once(
                profile, ErrorEngine(), worker="local-test", lease_seconds=60
            )
            after_failure = Coordinator(Path(profile.database)).status()
            completed = run_local_once(
                profile, CompleteEngine(), worker="local-test", lease_seconds=60
            )
            after_retry = Coordinator(Path(profile.database)).status()
        self.assertEqual(failed.outcome, "error")
        self.assertEqual(after_failure["retry_queue"], 1)
        self.assertEqual(completed.outcome, "complete")
        self.assertEqual(after_retry["completed_chunks"], 1)
        self.assertEqual(after_retry["allocated_chunks"], 1)


if __name__ == "__main__":
    unittest.main()
