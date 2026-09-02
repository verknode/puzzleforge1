import unittest

from puzzleforge.coldzone import (
    COMPONENTS,
    ColdOrder,
    dominant_component,
    search_density,
)
from puzzleforge.mosaic import COLD_LANES, MosaicPlanner, preset_name


class ColdDensityTests(unittest.TestCase):
    def test_component_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(item.weight for item in COMPONENTS), 1.0)

    def test_density_is_normalized_to_mean_one(self) -> None:
        density = search_density(71, 512)
        self.assertAlmostEqual(sum(density) / len(density), 1.0, places=9)

    def test_interval_start_is_hotter_than_the_coldest_band(self) -> None:
        density = search_density(71, 512)
        self.assertGreater(density[0], min(density))
        self.assertEqual(density[0], max(density))

    def test_solved_positions_attract_effort(self) -> None:
        # A band centred on a solved puzzle's normalized position must score
        # above the quietest band in the same interval.
        density = search_density(71, 4096)
        self.assertGreater(max(density), 10 * min(density))

    def test_dominant_component_of_the_lowest_band(self) -> None:
        self.assertEqual(dominant_component(71, 0, 512), "sequential-low")


class ColdOrderTests(unittest.TestCase):
    def test_order_is_a_permutation(self) -> None:
        for size in (1, 2, 3, 17, 64, 255, 1000):
            order = ColdOrder(size, "test", 71, bands=16)
            actual = [order.chunk_id(rank) for rank in range(size)]
            self.assertEqual(sorted(actual), list(range(size)), size)

    def test_order_is_a_permutation_when_bands_exceed_size(self) -> None:
        order = ColdOrder(5, "test", 71)
        self.assertEqual(
            sorted(order.chunk_id(rank) for rank in range(5)), list(range(5))
        )

    def test_prefix_is_unique_on_a_large_domain(self) -> None:
        order = ColdOrder(1 << 30, "test", 71)
        ids = [order.chunk_id(rank) for rank in range(5000)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_first_chunks_land_in_the_coldest_band(self) -> None:
        size = 1 << 20
        order = ColdOrder(size, "test", 71)
        report = order.band_report(1)[0]
        low = int(report["start_fraction"] * size)
        high = int(report["end_fraction"] * size)
        for rank in range(64):
            self.assertTrue(low <= order.chunk_id(rank) < high)

    def test_coldest_band_is_quieter_than_the_interval_start(self) -> None:
        order = ColdOrder(1 << 20, "test", 71)
        coldest = order.band_report(1)[0]
        self.assertLess(
            coldest["relative_effort"], search_density(71, order.bands)[0]
        )

    def test_out_of_range_rank_is_rejected(self) -> None:
        order = ColdOrder(64, "test", 71, bands=8)
        with self.assertRaises(IndexError):
            order.chunk_id(64)

    def test_different_seeds_change_the_order_inside_a_band(self) -> None:
        first = ColdOrder(1 << 20, "seed-a", 71)
        second = ColdOrder(1 << 20, "seed-b", 71)
        self.assertNotEqual(
            [first.chunk_id(rank) for rank in range(32)],
            [second.chunk_id(rank) for rank in range(32)],
        )

    def test_same_seed_is_deterministic(self) -> None:
        first = ColdOrder(1 << 20, "seed-a", 71)
        second = ColdOrder(1 << 20, "seed-a", 71)
        self.assertEqual(
            [first.chunk_id(rank) for rank in range(32)],
            [second.chunk_id(rank) for rank in range(32)],
        )

    def test_rejects_invalid_construction(self) -> None:
        with self.assertRaises(ValueError):
            ColdOrder(0, "test", 71)
        with self.assertRaises(ValueError):
            ColdOrder(16, "", 71)
        with self.assertRaises(ValueError):
            ColdOrder(16, "test", 71, bands=0)


class ColdLaneTests(unittest.TestCase):
    def test_cold_preset_is_named(self) -> None:
        self.assertEqual(preset_name(COLD_LANES), "cold")
        self.assertEqual(preset_name(MosaicPlanner.DEFAULT_LANES), "mosaic")

    def test_planner_uses_both_lanes_without_repeating_a_chunk(self) -> None:
        planner = MosaicPlanner(
            1 << 16, seed="test", lanes=COLD_LANES, target_puzzle=71
        )
        candidates = planner.preview(40)
        self.assertEqual(len({item.chunk_id for item in candidates}), 40)
        lanes = {item.lane for item in candidates}
        self.assertEqual(lanes, {"cold", "uniform"})

    def test_cold_lane_wins_the_majority_of_slots(self) -> None:
        planner = MosaicPlanner(
            1 << 16, seed="test", lanes=COLD_LANES, target_puzzle=71
        )
        candidates = planner.preview(100)
        cold = sum(1 for item in candidates if item.lane == "cold")
        self.assertEqual(cold, 60)

    def test_cold_lane_requires_a_target_puzzle(self) -> None:
        with self.assertRaises(ValueError):
            MosaicPlanner(1 << 16, seed="test", lanes=COLD_LANES)

    def test_lane_set_survives_a_state_round_trip(self) -> None:
        planner = MosaicPlanner(
            1 << 16, seed="test", lanes=COLD_LANES, target_puzzle=71
        )
        planner.preview(8)
        state = planner.state()
        lanes = MosaicPlanner.lanes_from_state(state)
        self.assertEqual(lanes, COLD_LANES)
        restored = MosaicPlanner(
            1 << 16, seed="test", lanes=lanes, target_puzzle=71
        )
        restored.restore(state)
        self.assertEqual(restored.state(), state)

    def test_invalid_lane_states_are_rejected(self) -> None:
        for state in (
            {},
            {"lanes": []},
            {"lanes": [{"name": "cold"}]},
            {"lanes": [{"name": "cold", "weight": True}]},
            {"lanes": [{"name": 1, "weight": 2}]},
            {"lanes": [{"name": "cold", "weight": "6"}]},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                MosaicPlanner.lanes_from_state(state)


if __name__ == "__main__":
    unittest.main()
