import tempfile
import unittest
from pathlib import Path

from puzzleforge.coordinator import Coordinator
from puzzleforge.dashboard import DASHBOARD_HTML, dashboard_payload
from puzzleforge.engine import EngineTuning
from puzzleforge.local import LocalProfile
from puzzleforge.telemetry import nvidia_snapshot, parse_nvidia_csv


class CompletedProcess:
    def __init__(self, stdout: str, *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class DashboardTests(unittest.TestCase):
    def test_nvidia_csv_fields_are_typed(self) -> None:
        snapshot = parse_nvidia_csv(
            "0, NVIDIA GeForce RTX 4090, 64, 97, 280.5, 450, 1024, 24576, 2505\n"
        )
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["device"], 0)
        self.assertEqual(snapshot["temperature_c"], 64.0)
        self.assertAlmostEqual(snapshot["memory_percent"], 1024 / 24576 * 100)

    def test_nvidia_failure_is_nonfatal(self) -> None:
        def runner(*args, **kwargs):
            return CompletedProcess("", returncode=9, stderr="driver unavailable")

        snapshot = nvidia_snapshot(0, runner=runner)
        self.assertFalse(snapshot["available"])
        self.assertIn("driver unavailable", snapshot["error"])

    def test_dashboard_payload_combines_profile_progress_and_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "campaign.sqlite3"
            coordinator = Coordinator.initialize(
                database,
                puzzle_number=71,
                chunk_size=256,
                seed="dashboard-tests",
                planner_mode="hypothesis",
            )
            lease = coordinator.lease("dashboard-test", lease_seconds=60)
            coordinator.complete(
                lease.token,
                lease.worker,
                checked=lease.keys,
                elapsed_seconds=1.0,
                rate_keys_per_second=lease.keys,
            )
            profile = LocalProfile(
                schema=1,
                puzzle=71,
                binary=str(root / "cuBitCrack"),
                tuning=EngineTuning(
                    device=0, blocks=32, threads=256, points=512
                ),
                measured_rate_keys_per_second=6_000_000_000,
                benchmark_relative_spread=0.02,
                chunk_size=256,
                target_chunk_seconds=300,
                planner_mode="hypothesis",
                seed="dashboard-tests",
                database=str(database),
                benchmark_report=str(root / "benchmark.json"),
                device_probe="RTX 4090",
                created_at="2026-08-24T00:00:00+00:00",
                hypothesis_enabled=True,
            )
            payload = dashboard_payload(
                profile,
                {"available": True, "temperature_c": 62.0},
            )
        self.assertEqual(payload["campaign"]["checked_keys"], "256")
        self.assertGreater(float(payload["derived"]["coverage_percent"]), 0)
        self.assertEqual(payload["telemetry"]["temperature_c"], 62.0)
        self.assertTrue(payload["campaign"]["hypothesis_lab"]["enabled"])
        self.assertEqual(
            payload["campaign"]["hypothesis_lab"]["report"]["search_slots"], 9
        )
        self.assertIn(b"PUZZLE", DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
