import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from puzzleforge.benchmark import run_benchmark
from puzzleforge.coordinator import Coordinator
from puzzleforge.dashboard import dashboard_payload
from puzzleforge.engine import BitCrackEngine, EngineOutcome, EngineTuning
from puzzleforge.local import LocalProfile, load_profile, save_profile, run_local_once
from puzzleforge.partition import KeyChunk
from puzzleforge.registry import get_puzzle
from puzzleforge.retune import apply_retune
from puzzleforge.runtime import RuntimeMonitor, campaign_gpu_lock, read_runtime


def make_profile(root):
    return LocalProfile(schema=1, puzzle=71, binary=str(root / "engine"),
                        tuning=EngineTuning(blocks=16, threads=128, points=256),
                        measured_rate_keys_per_second=100, benchmark_relative_spread=.01,
                        chunk_size=256, target_chunk_seconds=300, planner_mode="affine",
                        seed="must-stay-private", database=str(root / "campaign.sqlite3"),
                        benchmark_report=str(root / "benchmark.json"), created_at="2026-09-07",
                        device_probe="test GPU")


class LiveEngineTests(unittest.TestCase):
    def run_child(self, source, progress=None):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "test-engine"
            binary.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
            binary.chmod(0o755)
            puzzle = get_puzzle(8)
            return BitCrackEngine(binary, poll_seconds=.01, timeout_seconds=3,
                                  progress=progress).scan(puzzle, KeyChunk(0, 0, 128, 255))

    def test_rate_arrives_while_scanning_and_is_not_counted_as_coverage(self):
        events = []
        outcome = self.run_child(
            "import os, time\n"
            "os.write(1, b'999.'); time.sleep(.05); os.write(1, b'99 MKey/s\\r')\n"
            "time.sleep(.15)\n"
            "os.write(2, b'Reached end of keyspace\\n')\n", events.append)
        self.assertTrue(any(e.get("reported_rate_keys_per_second") == 999.99e6 for e in events))
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.checked, 128)
        self.assertAlmostEqual(outcome.rate_keys_per_second, 128 / outcome.elapsed_seconds)
        self.assertLess(outcome.rate_keys_per_second, 10000)

    def test_silent_zero_exit_never_credits_an_unverified_completion(self):
        outcome = self.run_child("pass\n")
        self.assertEqual((outcome.status, outcome.checked), ("error", 0))
        self.assertIn("end-of-keyspace", outcome.message)

    def test_stderr_flood_does_not_deadlock_or_lose_earlier_verified_match(self):
        outcome = self.run_child(
            "import os\n"
            "os.write(1, b'Private key: 0xe0\\n')\n"
            "for i in range(128): os.write(2, b'x' * 4096 + b'\\n')\n")
        self.assertEqual(outcome.found_key, 0xE0)

    def test_unverified_key_is_redacted_from_error(self):
        outcome = self.run_child("print('Private key: 0x99')\n")
        self.assertNotIn("0x99", outcome.message)


class RuntimeTests(unittest.TestCase):
    def test_stale_and_stopped_workers_never_show_live_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "campaign.sqlite3"
            with RuntimeMonitor(database) as monitor:
                monitor.progress({"phase": "scanning", "reported_rate_keys_per_second": 123,
                                  "rate_at_epoch": time.time(), "private_key": "never publish"})
                monitor._publish()
                live = read_runtime(database)
                self.assertEqual(live["reported_rate_keys_per_second"], 123)
                self.assertNotIn("private_key", live)
                stale = read_runtime(database, now=time.time() + 60)
                self.assertEqual(stale["phase"], "stale")
                self.assertIsNone(stale["reported_rate_keys_per_second"])
            self.assertEqual(read_runtime(database)["phase"], "stopped")

    def test_campaign_lock_rejects_second_owner_and_releases_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "campaign.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with campaign_gpu_lock(database):
                    with self.assertRaisesRegex(RuntimeError, "Another local worker"):
                        with campaign_gpu_lock(database):
                            self.fail("second lock owner")
                    raise RuntimeError("interrupted")
            with campaign_gpu_lock(database):
                pass

    def test_dashboard_with_no_worker_does_not_expose_seed_or_claim_live_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root)
            Coordinator.initialize(Path(profile.database), puzzle_number=71, chunk_size=256, seed=profile.seed)
            payload = dashboard_payload(profile, {"available": False})
            self.assertNotIn(profile.seed, json.dumps(payload))
            self.assertNotIn("found_key_hex", payload["campaign"])
            self.assertEqual(payload["runtime"]["phase"], "unknown")


class RetuneTests(unittest.TestCase):
    def report(self, profile, sequence):
        current, faster = profile.tuning, replace(profile.tuning, blocks=32)
        rates = {current: iter(sequence[0]), faster: iter(sequence[1])}
        class Engine:
            def __init__(self, tuning):
                self.tuning = tuning
            def scan(self, puzzle, chunk):
                rate = next(rates[self.tuning])
                if rate is None:
                    return EngineOutcome("error", 0, 1, 10000000, message="failed")
                return EngineOutcome("complete", chunk.size, chunk.size / rate, 10000000)
        return run_benchmark(puzzle_number=71, chunk_size=256, seed="retune-test", sequence=0,
                             repeats=2, profiles=(current, faster), engine_factory=Engine,
                             binary_name="fake", device_probe="test", warmup_runs=1)

    def test_failure_disqualifies_fast_profile_and_warmup_is_not_measured(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory))
            report = self.report(profile, ((1, 100, 100), (10000, 1000, None)))
        self.assertEqual(report.best.tuning, profile.tuning)
        self.assertEqual(report.best.median_keys_per_second, 100)

    def test_retune_preserves_seed_grid_sweep_and_database_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root)
            profile = replace(profile, auto_sweep_enabled=True,
                              sweep_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
            path = root / "profile.json"
            save_profile(path, profile)
            coordinator = Coordinator.initialize(Path(profile.database), puzzle_number=71, chunk_size=256, seed=profile.seed)
            lease = coordinator.lease("test")
            coordinator.complete(lease.token, lease.worker, checked=lease.keys)
            before = Path(profile.database).read_bytes()
            result = apply_retune(path, self.report(profile, ((1, 100, 100), (1, 120, 121))), root / "retune.json")
            updated = load_profile(path)
            self.assertTrue(result["changed_tuning"])
            for key in ("seed", "chunk_size", "database", "sweep_address", "auto_sweep_enabled"):
                self.assertEqual(getattr(updated, key), getattr(profile, key))
            self.assertEqual(Path(profile.database).read_bytes(), before)
            self.assertEqual(load_profile(root / "retune.profile-backup.json"), profile)

    def test_overlapping_measurements_keep_original_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root)
            path = root / "profile.json"
            save_profile(path, profile)
            report = self.report(profile, ((1, 100, 110), (1, 108, 119)))
            result = apply_retune(path, report, root / "retune.json")
            self.assertFalse(result["changed_tuning"])
            self.assertEqual(load_profile(path).tuning, profile.tuning)


class ZoomTests(unittest.TestCase):
    def test_zoom_preserves_exact_boundaries_and_short_last_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = Coordinator.initialize(Path(directory) / "campaign.sqlite3", puzzle_number=8, chunk_size=17, seed="zoom")
            for _ in range(8):
                lease = coordinator.lease("zoom")
                coordinator.complete(lease.token, lease.worker, checked=lease.keys)
            data = coordinator.range_map(bins=64, first_chunk=7, after_chunk=8)
            self.assertEqual(data["bins"], 1)
            self.assertEqual(data["coverage"], [[0, "9"]])
            self.assertEqual(data["states"]["completed"], [[0, 1]])
            with self.assertRaises(ValueError):
                coordinator.range_map(first_chunk=-1)

    def test_zoom_of_huge_range_uses_integer_chunk_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = Coordinator.initialize(Path(directory) / "campaign.sqlite3", puzzle_number=71, chunk_size=256, seed="zoom")
            lease = coordinator.lease("zoom")
            coordinator.complete(lease.token, lease.worker, checked=lease.keys)
            index = lease.chunk.chunk_id
            data = coordinator.range_map(first_chunk=index, after_chunk=index + 1)
            self.assertEqual(data["coverage"], [[0, "256"]])
            self.assertEqual(data["window_first_chunk"], str(index))


if __name__ == "__main__":
    unittest.main()
