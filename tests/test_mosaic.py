import unittest

from puzzleforge.mosaic import (
    AffineOrder,
    BitSpreadOrder,
    CenterOrder,
    EdgeOrder,
    MosaicPlanner,
    PrivatePermutationOrder,
)


class MosaicTests(unittest.TestCase):
    def test_every_lane_is_a_permutation_for_non_power_of_two_sizes(self) -> None:
        for size in (1, 2, 3, 7, 17, 63, 127):
            orders = (
                AffineOrder(size, "test"),
                PrivatePermutationOrder(size, "test"),
                BitSpreadOrder(size, "test"),
                EdgeOrder(size),
                CenterOrder(size),
            )
            for order in orders:
                actual = [order.chunk_id(rank) for rank in range(size)]
                self.assertEqual(sorted(actual), list(range(size)), order.name)

    def test_global_filter_produces_complete_unique_coverage(self) -> None:
        planner = MosaicPlanner(128, seed="coverage")
        candidates = planner.preview(128)
        ids = [candidate.chunk_id for candidate in candidates]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), list(range(128)))
        self.assertGreater(len({candidate.lane for candidate in candidates}), 1)

    def test_state_restore_is_deterministic(self) -> None:
        first = MosaicPlanner(257, seed="resume")
        seen: set[int] = set()
        for _ in range(25):
            seen.add(first.next_unseen(seen).chunk_id)
        state = first.state()
        expected = first.next_unseen(seen)

        restored = MosaicPlanner(257, seed="resume")
        restored.restore(state)
        self.assertEqual(restored.next_unseen(seen), expected)

    def test_edge_and_center_orders_are_explicit(self) -> None:
        self.assertEqual([EdgeOrder(8).chunk_id(i) for i in range(8)], [0, 7, 1, 6, 2, 5, 3, 4])
        self.assertEqual([CenterOrder(8).chunk_id(i) for i in range(8)], [3, 4, 2, 5, 1, 6, 0, 7])

    def test_private_orders_are_seeded_and_non_affine(self) -> None:
        size = 4093
        first = [
            PrivatePermutationOrder(size, "one").chunk_id(rank)
            for rank in range(64)
        ]
        again = [
            PrivatePermutationOrder(size, "one").chunk_id(rank)
            for rank in range(64)
        ]
        second = [
            PrivatePermutationOrder(size, "two").chunk_id(rank)
            for rank in range(64)
        ]
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        deltas = {
            (first[index + 1] - first[index]) % size
            for index in range(len(first) - 1)
        }
        self.assertGreater(len(deltas), 50)


if __name__ == "__main__":
    unittest.main()
