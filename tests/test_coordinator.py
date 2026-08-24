import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from puzzleforge.coordinator import Coordinator, LeaseRejected


class CoordinatorTests(unittest.TestCase):
    def make_coordinator(
        self, directory: str, *, puzzle: int = 71, chunk_size: int = 256
    ) -> Coordinator:
        return Coordinator.initialize(
            Path(directory) / "campaign.sqlite3",
            puzzle_number=puzzle,
            chunk_size=chunk_size,
            seed="coordinator-tests",
        )

    def test_consecutive_leases_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            first = coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000)
            second = coordinator.lease("gpu-b", lease_seconds=60, now_epoch=1000)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.chunk_id, second.chunk_id)
            self.assertTrue(first.chunk.end < second.chunk.start or second.chunk.end < first.chunk.start)

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

    def test_failed_work_returns_to_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.make_coordinator(directory)
            first = coordinator.lease("gpu-a", lease_seconds=60, now_epoch=1000)
            coordinator.fail(first.token, "gpu-a", error="device reset")
            second = coordinator.lease("gpu-b", lease_seconds=60, now_epoch=1001)
            self.assertEqual(first.sequence, second.sequence)
            status = coordinator.status(now_epoch=1002)
            self.assertEqual(status["worker_failures"], 1)


if __name__ == "__main__":
    unittest.main()
