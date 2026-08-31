import hashlib
import tempfile
import unittest
from pathlib import Path

from puzzleforge.crypto import p2pkh_address_from_private_key
from puzzleforge.generator_lab import (
    GeneratorLab,
    GeneratorScheme,
    StaticSeedSource,
    _masked_key,
    generator_dashboard_status,
    load_generator_state,
)
from puzzleforge.hypothesis import SolvedObservation


def synthetic_scheme(seed: bytes, number: int) -> int:
    return int.from_bytes(
        hashlib.sha256(seed + number.to_bytes(4, "big")).digest(),
        "big",
    )


def synthetic_observations(seed: bytes, target: int) -> tuple[SolvedObservation, ...]:
    return tuple(
        SolvedObservation(
            number=number,
            key=_masked_key(synthetic_scheme(seed, number), number),
            address="synthetic",
        )
        for number in range(2, target)
    )


class GeneratorLabTests(unittest.TestCase):
    def test_exact_generator_is_validated_on_holdouts_and_finds_target(self) -> None:
        correct_seed = b"correct public-puzzle seed"
        target = 9
        target_key = _masked_key(synthetic_scheme(correct_seed, target), target)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "generator-lab.json"
            lab = GeneratorLab(
                state_path,
                target_puzzle=target,
                target_address=p2pkh_address_from_private_key(target_key),
                observations=synthetic_observations(correct_seed, target),
                sources=(
                    StaticSeedSource("test-seeds", (b"wrong", correct_seed)),
                ),
                schemes=(GeneratorScheme("synthetic", synthetic_scheme),),
            )
            state = lab.run_slice(max_seconds=5, max_candidates=10)
            restored = load_generator_state(state_path)
            found_key = lab.found_key()

        self.assertEqual(state.status, "found")
        self.assertEqual(int(state.found_key_hex, 16), target_key)
        self.assertEqual(state.validated_known_generators, 1)
        self.assertEqual(state.found_scheme, "synthetic")
        self.assertEqual(restored.status, "found")
        self.assertEqual(found_key, target_key)

    def test_cursor_resume_does_not_repeat_checked_candidates(self) -> None:
        correct_seed = b"not-present"
        target = 8
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "generator-lab.json"
            lab = GeneratorLab(
                state_path,
                target_puzzle=target,
                target_address="not-a-real-target",
                observations=synthetic_observations(correct_seed, target),
                sources=(StaticSeedSource("resume", (b"a", b"b", b"c")),),
                schemes=(GeneratorScheme("synthetic", synthetic_scheme),),
            )
            first = lab.run_slice(max_seconds=5, max_candidates=1)
            second = lab.run_slice(max_seconds=5, max_candidates=1)

        self.assertEqual(first.checked_candidates, 1)
        self.assertEqual(second.checked_candidates, 2)
        self.assertEqual(second.completed_seed_candidates, 2)
        self.assertEqual(second.candidate_index, 2)

    def test_partial_low_bit_match_is_diagnostic_not_validation(self) -> None:
        correct_seed = b"correct"
        target = 10
        with tempfile.TemporaryDirectory() as directory:
            lab = GeneratorLab(
                Path(directory) / "generator-lab.json",
                target_puzzle=target,
                target_address="not-a-real-target",
                observations=synthetic_observations(correct_seed, target),
                sources=(StaticSeedSource("wrong", (b"one", b"two")),),
                schemes=(GeneratorScheme("synthetic", synthetic_scheme),),
            )
            state = lab.run_slice(max_seconds=5, max_candidates=2)

        self.assertEqual(state.validated_known_generators, 0)
        self.assertEqual(state.exact_filter_matches, 0)
        self.assertLess(state.best_low_bits, state.best_low_bits_total)

    def test_dashboard_never_exposes_found_key_or_seed_material(self) -> None:
        correct_seed = b"correct"
        target = 9
        target_key = _masked_key(synthetic_scheme(correct_seed, target), target)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "campaign.sqlite3"
            state_path = database.with_name("generator-lab.json")
            lab = GeneratorLab(
                state_path,
                target_puzzle=target,
                target_address=p2pkh_address_from_private_key(target_key),
                observations=synthetic_observations(correct_seed, target),
                sources=(StaticSeedSource("test", (correct_seed,)),),
                schemes=(GeneratorScheme("synthetic", synthetic_scheme),),
            )
            lab.run_slice(max_seconds=5, max_candidates=1)
            payload = generator_dashboard_status(
                database,
                enabled=True,
                duty_percent=10,
            )

        self.assertEqual(payload["status"], "found")
        self.assertNotIn("found_key_hex", payload)
        self.assertNotIn("found_seed_base64", payload)
        self.assertEqual(payload["gpu_reserved_percent"], 0)


if __name__ == "__main__":
    unittest.main()
