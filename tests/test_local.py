import io
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

from puzzleforge.cli import command_hypothesis_enable, command_local_app
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

    def test_existing_profile_can_enable_hypothesis_without_losing_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = self.make_profile(directory)
            profile_path = Path(directory) / "profile.json"
            save_profile(profile_path, profile)
            coordinator = Coordinator(Path(profile.database))
            previous = coordinator.lease(
                "before-lab", lease_seconds=60, now_epoch=1000
            )
            with redirect_stdout(io.StringIO()):
                result = command_hypothesis_enable(Namespace(profile=profile_path))
            updated = load_profile(profile_path)
            next_lease = coordinator.lease(
                "after-lab", lease_seconds=60, now_epoch=1001
            )
            status = coordinator.status(now_epoch=1002)

        self.assertEqual(result, 0)
        self.assertTrue(updated.hypothesis_enabled)
        self.assertEqual(updated.planner_mode, "hypothesis")
        self.assertNotEqual(previous.chunk_id, next_lease.chunk_id)
        self.assertTrue(next_lease.strategy_lane.startswith("hypothesis:"))
        self.assertTrue(status["hypothesis_lab"]["enabled"])

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

    def test_local_app_runs_worker_and_dashboard_as_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fake-cuBitCrack"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
                "out.write_text('Private key: 0xe0\\n', encoding='utf-8')\n"
                "print('8.00 MKey/s')\n",
                encoding="utf-8",
            )
            os.chmod(binary, 0o755)
            database = root / "campaign.sqlite3"
            Coordinator.initialize(
                database,
                puzzle_number=8,
                chunk_size=128,
                seed="local-app-test",
            )
            profile = LocalProfile(
                schema=1,
                puzzle=8,
                binary=str(binary),
                tuning=EngineTuning(device=0),
                measured_rate_keys_per_second=8_000_000,
                benchmark_relative_spread=0.0,
                chunk_size=128,
                target_chunk_seconds=300,
                planner_mode="affine",
                seed="local-app-test",
                database=str(database),
                benchmark_report=str(root / "benchmark.json"),
                device_probe="Fake GPU",
                created_at="2026-08-24T00:00:00+00:00",
            )
            profile_path = root / "profile.json"
            save_profile(profile_path, profile)
            args = Namespace(
                profile=profile_path,
                worker="local-app-test",
                lease_seconds=60,
                max_chunks=1,
                engine_timeout=5.0,
                no_thermal_guard=True,
                host="127.0.0.1",
                port=0,
                no_open=True,
            )
            with redirect_stdout(io.StringIO()):
                result = command_local_app(args)
            state = Coordinator(database).status()["state"]
        self.assertEqual(result, 0)
        self.assertEqual(state, "found")


if __name__ == "__main__":
    unittest.main()
