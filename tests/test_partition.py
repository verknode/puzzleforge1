import unittest

from puzzleforge.model import Puzzle
from puzzleforge.partition import ChunkPlan


class PartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.puzzle = Puzzle(
            number=8,
            start=1 << 7,
            end=(1 << 8) - 1,
            address="1M92tSqNmQLYw33fuBvjmeadirh1ysMBxK",
            status="solved-test-vector",
        )

    def test_permutation_covers_every_key_once(self) -> None:
        plan = ChunkPlan(self.puzzle, chunk_size=11, seed="coverage")
        keys: list[int] = []
        chunk_ids: list[int] = []
        for sequence in range(plan.chunks_for_shard):
            chunk = plan.chunk_for_sequence(sequence)
            keys.extend(range(chunk.start, chunk.end + 1))
            chunk_ids.append(chunk.chunk_id)
        self.assertEqual(sorted(keys), list(range(self.puzzle.start, self.puzzle.end + 1)))
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))

    def test_shards_do_not_overlap(self) -> None:
        seen: set[int] = set()
        for shard_index in range(5):
            plan = ChunkPlan(
                self.puzzle,
                chunk_size=7,
                seed="team",
                shard_count=5,
                shard_index=shard_index,
            )
            for sequence in range(plan.chunks_for_shard):
                chunk = plan.chunk_for_sequence(sequence)
                self.assertNotIn(chunk.chunk_id, seen)
                seen.add(chunk.chunk_id)
        expected_chunks = (self.puzzle.size + 6) // 7
        self.assertEqual(len(seen), expected_chunks)

    def test_same_inputs_are_deterministic(self) -> None:
        first = ChunkPlan(self.puzzle, 13, "same")
        second = ChunkPlan(self.puzzle, 13, "same")
        self.assertEqual(first.manifest(), second.manifest())
        self.assertEqual(first.chunk_for_sequence(3), second.chunk_for_sequence(3))

    def test_invalid_shard_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChunkPlan(self.puzzle, 10, "seed", shard_count=2, shard_index=2)


if __name__ == "__main__":
    unittest.main()

