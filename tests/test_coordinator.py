import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from puzzleforge.coordinator import Coordinator, LeaseRejected


class CoordinatorTests(unittest.TestCase):
    def make_coordinator(
        self,
        directory: str,
        *,
        puzzle: int = 71,
        chunk_size: int = 256,
        planner_mode: str = "affine",
    ) -> Coordinator:
        return Coordinator.initialize(
            Path(directory) / "campaign.sqlite3",
            puzzle_number=puzzle,
            chunk_size=chunk_size,
            seed="coordinator-tests",
            planner_mode=planner_mode,
        )

    def test_cold_mode_reports_its_preset_and_uses_both_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(
                directory, chunk_size=1 << 40, planner_mode="cold"
            )
            self.assertEqual(coordinator.status()["planner_mode"], "cold")
            leases = [
                coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000 + index)
                for index in range(10)
            ]
            self.assertEqual(
                {lease.strategy_lane for lease in leases}, {"cold", "uniform"}
            )
            self.assertEqual(len({lease.chunk_id for lease in leases}), 10)

    def test_cold_campaign_restores_its_lane_set_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            first = Coordinator.initialize(
                path,
                puzzle_number=71,
                chunk_size=1 << 40,
                seed="coordinator-tests",
                planner_mode="cold",
            )
            # Far-future epochs keep the first lease live, so the status call
            # below cannot reclaim it and hand the same chunk back.
            first.lease("gpu-a", lease_seconds=600, now_epoch=4_000_000_000)
            reopened = Coordinator(path)
            self.assertEqual(reopened.status()["planner_mode"], "cold")
            self.assertEqual(
                reopened.lease(
                    "gpu-b", lease_seconds=600, now_epoch=4_000_000_001
                ).strategy_lane,
                "uniform",
            )

    def test_cold_campaign_keeps_its_lanes_across_a_reseed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(
                directory, chunk_size=1 << 40, planner_mode="cold"
            )
            coordinator.reseed("a-new-private-order")
            self.assertEqual(coordinator.status()["planner_mode"], "cold")

    def test_rejects_an_unknown_planner_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.make_coordinator(directory, planner_mode="warm")

    def test_consecutive_leases_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            first = coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000)
            second = coordinator.lease("gpu-b", lease_seconds=60, now_epoch=1000)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.chunk_id, second.chunk_id)
            self.assertTrue(
                first.chunk.end < second.chunk.start
                or second.chunk.end < first.chunk.start
            )

    def test_expired_lease_is_reissued_with_new_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            first = coordinator.lease("gpu-a", lease_seconds=10, now_epoch=1000)
            second = coordinator.lease("gpu-b", lease_seconds=10, now_epoch=1011)
            self.assertEqual(first.sequence, second.sequence)
            self.assertNotEqual(first.token, second.token)
            with self.assertRaises(LeaseRejected):
                coordinator.heartbeat(
                    first.token, "gpu-a", lease_seconds=10, now_epoch=1012
                )

    def test_partial_no_match_is_rejected_then_full_result_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            lease = coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000)
            with self.assertRaises(LeaseRejected):
                coordinator.complete(
                    lease.token,
                    "gpu-a",
                    checked=lease.keys - 1,
                    now_epoch=1001,
                )
            completion = coordinator.complete(
                lease.token,
                "gpu-a",
                checked=lease.keys,
                elapsed_seconds=1.0,
                rate_keys_per_second=lease.keys,
                now_epoch=1002,
            )
            self.assertTrue(completion.accepted)
            status = coordinator.status(now_epoch=1003)
            self.assertEqual(status["checked_keys"], str(lease.keys))
            self.assertGreater(Decimal(status["no_repeat_advantage"]), Decimal(0))

    def test_solved_puzzle8_result_and_retry_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory, puzzle=8, chunk_size=128)
            lease = coordinator.lease("gpu-test", lease_seconds=60, now_epoch=1000)
            completion = coordinator.complete(
                lease.token,
                "gpu-test",
                checked=97,
                found_key_hex="e0",
                now_epoch=1001,
            )
            replay = coordinator.complete(
                lease.token,
                "gpu-test",
                checked=97,
                found_key_hex="e0",
                now_epoch=1002,
            )
            self.assertTrue(completion.found)
            self.assertTrue(replay.idempotent)
            self.assertEqual(coordinator.status(now_epoch=1003)["state"], "found")

    def test_generator_candidate_is_verified_without_fake_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory, puzzle=8, chunk_size=128)
            completion = coordinator.record_verified_candidate("e0")
            replay = coordinator.record_verified_candidate("e0")
            status = coordinator.status(now_epoch=1000)

        self.assertTrue(completion.found)
        self.assertTrue(replay.idempotent)
        self.assertEqual(status["state"], "found")
        self.assertEqual(status["checked_keys"], "0")
        self.assertEqual(status["completed_chunks"], 0)

    def test_generator_candidate_must_match_registered_puzzle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory, puzzle=8, chunk_size=128)
            with self.assertRaises(LeaseRejected):
                coordinator.record_verified_candidate("e1")
            status = coordinator.status(now_epoch=1000)

        self.assertEqual(status["state"], "running")

    def test_failed_work_returns_to_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            first = coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000)
            coordinator.fail(first.token, "gpu-a", error="device reset")
            second = coordinator.lease("gpu-b", lease_seconds=60, now_epoch=1001)
            self.assertEqual(first.sequence, second.sequence)
            status = coordinator.status(now_epoch=1002)
            self.assertEqual(status["worker_failures"], 1)

    def test_mosaic_campaign_persists_unique_multi_lane_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            coordinator = Coordinator.initialize(
                path,
                puzzle_number=71,
                chunk_size=256,
                seed="mosaic-coordinator-test",
                planner_mode="mosaic",
            )
            leases = [
                coordinator.lease(
                    f"gpu-{index}", lease_seconds=60, now_epoch=1000
                )
                for index in range(24)
            ]
            reopened = Coordinator(path)
            leases.append(
                reopened.lease("gpu-reopened", lease_seconds=60, now_epoch=1001)
            )
            chunk_ids = [lease.chunk_id for lease in leases]
            self.assertEqual(len(chunk_ids), len(set(chunk_ids)))
            self.assertGreater(len({lease.strategy_lane for lease in leases}), 1)
            status = reopened.status(now_epoch=1002)
            self.assertEqual(status["planner_mode"], "mosaic")
            self.assertEqual(status["allocated_chunks"], 25)
            self.assertGreater(len(status["strategy_lanes"]), 1)

    def test_hypothesis_campaign_persists_10_90_cycles_without_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            coordinator = Coordinator.initialize(
                path,
                puzzle_number=71,
                chunk_size=1 << 40,
                seed="hypothesis-coordinator-test",
                planner_mode="hypothesis",
            )
            leases = [
                coordinator.lease(
                    f"lab-{index}", lease_seconds=60, now_epoch=1000
                )
                for index in range(12)
            ]
            reopened = Coordinator(path)
            leases.append(
                reopened.lease("lab-reopened", lease_seconds=60, now_epoch=1001)
            )
            chunk_ids = [lease.chunk_id for lease in leases]
            status = reopened.status(now_epoch=1002)

        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))
        self.assertTrue(
            all(lease.strategy_lane.startswith("hypothesis:") for lease in leases)
        )
        self.assertEqual(status["planner_mode"], "hypothesis")
        self.assertEqual(status["hypothesis_lab"]["research_percent"], 10)
        self.assertEqual(status["hypothesis_lab"]["search_percent"], 90)
        self.assertEqual(status["hypothesis_lab"]["cycle"], 2)
        self.assertIsNotNone(status["hypothesis_lab"]["report"])

    def test_hypothesis_can_take_over_existing_campaign_without_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(
                directory,
                chunk_size=1 << 40,
            )
            before = [
                coordinator.lease(
                    f"affine-{index}", lease_seconds=60, now_epoch=1000
                )
                for index in range(3)
            ]
            coordinator.enable_hypothesis()
            after = [
                coordinator.lease(
                    f"hypothesis-{index}", lease_seconds=60, now_epoch=1001
                )
                for index in range(12)
            ]
            status = coordinator.status(now_epoch=1002)

        chunk_ids = [lease.chunk_id for lease in before + after]
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))
        self.assertEqual(status["planner_mode"], "hypothesis")
        self.assertTrue(
            all(lease.strategy_lane.startswith("hypothesis:") for lease in after)
        )

    def test_v1_database_is_migrated_without_losing_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE campaign (
                    id INTEGER PRIMARY KEY,
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
                    state TEXT NOT NULL,
                    found_key_hex TEXT,
                    reclaimed_leases INTEGER NOT NULL,
                    total_failures INTEGER NOT NULL,
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
                    state TEXT NOT NULL,
                    worker TEXT,
                    lease_token TEXT UNIQUE,
                    lease_expires_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_checked INTEGER,
                    result_kind TEXT,
                    found_key_hex TEXT,
                    elapsed_seconds REAL,
                    rate_keys_per_second REAL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )
            connection.execute(
                """
                INSERT INTO campaign VALUES (
                    1, 1, 71, '1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU',
                    '400000000000000000', '7fffffffffffffffff', 'legacy',
                    256, 4611686018427387904, 0, 0, '0', 'running', NULL,
                    0, 0, 'created', 'updated'
                )
                """
            )
            connection.commit()
            connection.close()

            coordinator = Coordinator(path)
            status = coordinator.status(now_epoch=1000)
            lease = coordinator.lease("gpu-migrated", lease_seconds=60, now_epoch=1000)
            self.assertEqual(status["schema_version"], 3)
            self.assertEqual(status["planner_mode"], "affine")
            self.assertEqual(lease.strategy_lane, "affine")


if __name__ == "__main__":
    unittest.main()
