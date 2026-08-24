import unittest

from puzzleforge.cloud import (
    BenchmarkRate,
    CloudOffer,
    CloudPolicy,
    normalize_gpu_model,
    plan_capacity,
)
from puzzleforge.cloud_providers import parse_runpod_offers, parse_vast_offers


class CloudTests(unittest.TestCase):
    def test_model_normalization_matches_provider_names(self) -> None:
        self.assertEqual(
            normalize_gpu_model("NVIDIA GeForce RTX 4090"),
            normalize_gpu_model("RTX_4090"),
        )

    def test_plan_selects_measured_value_under_hard_budgets(self) -> None:
        offers = [
            CloudOffer("vast", "cheap", "RTX 4090", 1, 0.30, 0.995, True, True),
            CloudOffer("vast", "costly", "RTX 4090", 1, 0.70, 0.999, True, True),
            CloudOffer("vast", "unknown", "RTX 5090", 1, 0.20, 0.999, True, True),
        ]
        policy = CloudPolicy(
            max_instances=2,
            max_total_hourly_usd=0.50,
            max_daily_usd=12.0,
            max_offer_hourly_usd=1.0,
            max_cost_per_quadrillion_usd=100.0,
        )
        plan = plan_capacity(
            puzzle_number=71,
            offers=offers,
            benchmarks=[BenchmarkRate("NVIDIA GeForce RTX 4090", 5e9)],
            policy=policy,
        )
        self.assertEqual(plan.selected_instances, 1)
        self.assertEqual(plan.decisions[0].offer.offer_id, "cheap")
        reasons = {item.offer.offer_id: item.reason for item in plan.decisions}
        self.assertIn("benchmark", reasons["unknown"])
        self.assertIn("budget", reasons["costly"])

    def test_vast_and_runpod_catalog_parsers(self) -> None:
        vast = parse_vast_offers(
            {
                "offers": [
                    {
                        "id": 123,
                        "gpu_name": "RTX 4090",
                        "num_gpus": 1,
                        "min_bid": 0.25,
                        "reliability": 0.997,
                        "verified": True,
                        "rentable": True,
                        "geolocation": "NO",
                        "cuda_vers": 12.8,
                    }
                ]
            },
            interruptible=True,
        )
        runpod = parse_runpod_offers(
            {
                "gpus": [
                    {
                        "id": "NVIDIA GeForce RTX 4090",
                        "name": "RTX 4090",
                        "price": {"secure": 0.44, "community": 0.31},
                        "availability": "HIGH",
                        "cudaVersions": ["12.8"],
                        "dataCenters": [{"id": "EUR-NO-1"}],
                    }
                ]
            },
            cloud="SECURE",
        )
        self.assertEqual(vast[0].hourly_usd, 0.25)
        self.assertTrue(vast[0].interruptible)
        self.assertEqual(runpod[0].hourly_usd, 0.44)
        self.assertEqual(runpod[0].region, "EUR-NO-1")


if __name__ == "__main__":
    unittest.main()
