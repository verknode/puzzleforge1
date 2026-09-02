import tempfile
import unittest
from pathlib import Path

from puzzleforge.benchmark import (
    run_benchmark,
    save_report,
    tuning_flags,
    tuning_profiles,
)
from puzzleforge.engine import EngineOutcome, EngineTuning


class RateEngine:
    def __init__(self, tuning: EngineTuning) -> None:
        self.tuning = tuning

    def scan(self, puzzle, chunk):
        rate = float((self.tuning.blocks or 1) * (self.tuning.points or 1))
        return EngineOutcome(
            status="complete",
            checked=chunk.size,
            elapsed_seconds=chunk.size / rate,
            rate_keys_per_second=rate,
            message="complete",
        )


class BenchmarkTests(unittest.TestCase):
    def test_quick_profile_is_bounded_and_valid(self) -> None:
        profiles = tuning_profiles("quick", device=2)
        self.assertEqual(len(profiles), 6)
        self.assertTrue(all(profile.device == 2 for profile in profiles))
        self.assertTrue(
            all(
                profile.threads is None or profile.threads % 32 == 0
                for profile in profiles
            )
        )

    def test_every_profile_set_benchmarks_the_engine_defaults(self) -> None:
        # Without a flag-free candidate the search cannot discover that the
        # binary's own device detection beats every fixed grid.
        for name in ("quick", "balanced", "full"):
            with self.subTest(profile=name):
                profiles = tuning_profiles(name, device=0)
                bare = [
                    profile
                    for profile in profiles
                    if profile.blocks is None
                    and profile.threads is None
                    and profile.points is None
                ]
                self.assertEqual(len(bare), 1)
                self.assertEqual(bare[0].device, 0)

    def test_fastest_profile_is_recommended_and_report_is_atomic(self) -> None:
        profiles = (
            EngineTuning(device=0, blocks=16, threads=128, points=256),
            EngineTuning(device=0, blocks=64, threads=256, points=1024),
        )
        report = run_benchmark(
            puzzle_number=71,
            chunk_size=256,
            seed="benchmark-test",
            sequence=0,
            repeats=2,
            profiles=profiles,
            engine_factory=RateEngine,
            binary_name="fake",
            device_probe="fake GPU",
        )
        self.assertEqual(report.best.tuning, profiles[1])
        self.assertIn("--blocks 64", tuning_flags(report.best.tuning))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            save_report(path, report)
            payload = path.read_text(encoding="utf-8")
        self.assertIn('"recommended_flags": "--device 0 --blocks 64', payload)


if __name__ == "__main__":
    unittest.main()
