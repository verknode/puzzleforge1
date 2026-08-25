import json
import unittest

from puzzleforge.model_zoo import (
    analyze_model_zoo,
    forward_log_lifts,
    model_registry,
    registry_fingerprint,
)


class ModelZooTests(unittest.TestCase):
    def test_registry_is_deterministic_unique_and_split_by_evidence_gate(self) -> None:
        models = model_registry()
        self.assertEqual(len(models), 126)
        self.assertEqual(len({model.name for model in models}), len(models))
        self.assertEqual(sum(model.promotion_eligible for model in models), 70)
        self.assertEqual(sum(not model.promotion_eligible for model in models), 56)
        self.assertEqual(len(registry_fingerprint()), 16)

    def test_forward_scoring_does_not_leak_a_holdout_into_earlier_folds(self) -> None:
        model = next(item for item in model_registry() if item.name == "kde-bw-0p06")
        values = tuple(((index * 37) % 101) / 101 for index in range(30))
        changed = values[:-1] + (0.999,)
        original_lifts = forward_log_lifts(model, values)
        changed_lifts = forward_log_lifts(model, changed)
        self.assertEqual(original_lifts[:-1], changed_lifts[:-1])
        self.assertNotEqual(original_lifts[-1], changed_lifts[-1])

    def test_planted_non_uniform_signal_can_pass_the_empirical_gate(self) -> None:
        values = tuple(
            0.08 + 0.16 * (((index * 37) % 101) / 101)
            for index in range(70)
        )
        report = analyze_model_zoo(values, calibration_trials=32)
        self.assertTrue(report.selected_model_validated)
        self.assertNotEqual(report.selected_model, "uniform")
        selected = next(
            score for score in report.scores if score.name == report.selected_model
        )
        self.assertTrue(selected.promotion_eligible)
        self.assertTrue(selected.stable)
        self.assertLessEqual(selected.adjusted_p_value, 0.05)

    def test_report_is_bounded_and_shadow_models_never_validate(self) -> None:
        values = tuple(((index * 53) % 127) / 127 for index in range(70))
        report = analyze_model_zoo(values, calibration_trials=32, score_limit=9)
        self.assertLessEqual(len(report.scores), 9)
        self.assertTrue(
            all(
                not score.validated
                for score in report.scores
                if not score.promotion_eligible
            )
        )
        payload = json.dumps([score.to_dict() for score in report.scores])
        self.assertLess(len(payload), 25_000)


if __name__ == "__main__":
    unittest.main()
