import unittest

from puzzleforge.hypothesis import (
    HypothesisPlanner,
    analyze_hypotheses,
    solved_observations,
)


class HypothesisTests(unittest.TestCase):
    def test_public_dataset_is_complete_and_independently_verified(self) -> None:
        observations = solved_observations()
        self.assertEqual(len(observations), 70)
        self.assertEqual(observations[7].key, 0xE0)
        self.assertEqual(observations[-1].number, 70)
        self.assertTrue(all(0 <= item.position < 1 for item in observations))

    def test_forward_holdout_report_is_explicit_about_evidence(self) -> None:
        report = analyze_hypotheses(71)
        self.assertEqual(report.observations, 70)
        self.assertEqual(report.holdouts, 54)
        self.assertEqual(report.search_slots, 9)
        self.assertEqual(report.model_count, 126)
        self.assertEqual(report.eligible_model_count, 70)
        self.assertEqual(report.shadow_model_count, 56)
        self.assertLessEqual(len(report.scores), 20)
        self.assertEqual(report.selected_model, "uniform")
        self.assertTrue(report.uniform_fallback)
        self.assertFalse(report.selected_model_validated)
        self.assertEqual(report.validated_model_count, 0)
        self.assertIn(report.best_candidate, {score.name for score in report.scores})
        self.assertIn("does not prove", report.warning)

    def test_one_analysis_phase_feeds_nine_unique_search_slots(self) -> None:
        planner = HypothesisPlanner(
            10_000,
            target_puzzle=71,
            seed="hypothesis-cycle",
        )
        seen: set[int] = set()
        candidates = []
        for _ in range(18):
            candidate = planner.next_unseen(seen)
            seen.add(candidate.chunk_id)
            candidates.append(candidate)

        self.assertEqual(len(seen), 18)
        self.assertTrue(candidates[0].analysis_performed)
        self.assertFalse(any(item.analysis_performed for item in candidates[1:9]))
        self.assertTrue(candidates[9].analysis_performed)
        self.assertEqual({item.cycle for item in candidates[:9]}, {0})
        self.assertEqual({item.cycle for item in candidates[9:]}, {1})

    def test_state_restore_keeps_the_exact_next_range(self) -> None:
        first = HypothesisPlanner(20_000, target_puzzle=71, seed="restore")
        seen: set[int] = set()
        for _ in range(11):
            seen.add(first.next_unseen(seen).chunk_id)
        state = first.state()
        expected = first.next_unseen(seen)

        restored = HypothesisPlanner(20_000, target_puzzle=71, seed="restore")
        restored.restore(state)
        self.assertEqual(restored.next_unseen(seen), expected)

    def test_invalid_ratio_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HypothesisPlanner(
                100,
                target_puzzle=71,
                seed="ratio",
                research_percent=20,
                search_percent=90,
            )


if __name__ == "__main__":
    unittest.main()
