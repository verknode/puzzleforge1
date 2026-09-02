import unittest

from puzzleforge.crypto import (
    FIELD_P,
    GROUP_N,
    batch_inverse,
    generator_multiples,
    iter_sequential_points,
    scalar_multiply,
)


class BatchInverseTests(unittest.TestCase):
    def test_inverts_every_non_zero_entry(self) -> None:
        values = [1, 2, 3, FIELD_P - 1, 0x1234567890ABCDEF, FIELD_P - 7]
        for value, inverse in zip(values, batch_inverse(values)):
            self.assertEqual(value * inverse % FIELD_P, 1)

    def test_zero_entries_return_zero(self) -> None:
        self.assertEqual(batch_inverse([0, 0, 0]), [0, 0, 0])

    def test_mixed_batch_keeps_the_other_inverses_correct(self) -> None:
        values = [0, 5, 0, 9, 0]
        results = batch_inverse(values)
        self.assertEqual(results[0], 0)
        self.assertEqual(results[2], 0)
        self.assertEqual(results[4], 0)
        self.assertEqual(5 * results[1] % FIELD_P, 1)
        self.assertEqual(9 * results[3] % FIELD_P, 1)

    def test_empty_batch(self) -> None:
        self.assertEqual(batch_inverse([]), [])


class SequentialWalkTests(unittest.TestCase):
    def test_matches_the_scalar_multiply_oracle(self) -> None:
        cases = (
            (1, 40, 7),
            (1, 1, 1),
            (128, 128, 1024),
            (0xE0, 20, 5),
            (255, 3, 4),
            (1 << 70, 300, 64),
            ((1 << 70) + 1, 65, 64),
        )
        for start, count, batch in cases:
            with self.subTest(start=start, count=count, batch=batch):
                points = list(iter_sequential_points(start, count, batch))
                self.assertEqual(len(points), count)
                for offset, point in enumerate(points):
                    self.assertEqual(point, scalar_multiply(start + offset))

    def test_batch_size_does_not_change_the_result(self) -> None:
        reference = list(iter_sequential_points(1 << 20, 200, 1))
        for batch in (2, 3, 16, 64, 512):
            with self.subTest(batch=batch):
                self.assertEqual(
                    list(iter_sequential_points(1 << 20, 200, batch)), reference
                )

    def test_handles_a_walk_that_passes_its_own_generator_multiple(self) -> None:
        # Starting at scalar 1 makes ``current`` equal ``1*G`` on the first
        # block, which forces the zero-denominator doubling fallback.
        points = list(iter_sequential_points(1, 8, 8))
        self.assertEqual(points[1], scalar_multiply(2))

    def test_empty_and_invalid_requests(self) -> None:
        self.assertEqual(list(iter_sequential_points(1, 0, 16)), [])
        with self.assertRaises(ValueError):
            list(iter_sequential_points(1, 4, 0))
        with self.assertRaises(ValueError):
            list(iter_sequential_points(0, 4, 16))
        with self.assertRaises(ValueError):
            list(iter_sequential_points(GROUP_N - 1, 4, 16))

    def test_generator_multiple_table(self) -> None:
        table = generator_multiples(6)
        for index, point in enumerate(table, start=1):
            self.assertEqual(point, scalar_multiply(index))
        with self.assertRaises(ValueError):
            generator_multiples(0)


if __name__ == "__main__":
    unittest.main()
