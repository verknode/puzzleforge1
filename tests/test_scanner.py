import tempfile
import unittest
from pathlib import Path

from puzzleforge.checkpoint import load_checkpoint
from puzzleforge.model import Puzzle
from puzzleforge.partition import ChunkPlan, KeyChunk
from puzzleforge.scanner import scan_chunk, run_session


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.puzzle = Puzzle(
            number=8,
            start=1 << 7,
            end=(1 << 8) - 1,
            address="1M92tSqNmQLYw33fuBvjmeadirh1ysMBxK",
            status="solved-test-vector",
        )

    def test_finds_known_key(self) -> None:
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=220, end=230)
        result = scan_chunk(self.puzzle, chunk, workers=1)
        self.assertEqual(result.found_key, 224)

    def test_no_match(self) -> None:
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=128, end=140)
        result = scan_chunk(self.puzzle, chunk, workers=1)
        self.assertIsNone(result.found_key)
        self.assertEqual(result.checked, 13)

    def test_batch_size_does_not_change_the_result(self) -> None:
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=200, end=250)
        for batch_size in (1, 2, 7, 64, 4096):
            with self.subTest(batch_size=batch_size):
                result = scan_chunk(
                    self.puzzle, chunk, workers=1, batch_size=batch_size
                )
                self.assertEqual(result.found_key, 224)
                self.assertEqual(result.checked, 25)

    def test_parallel_workers_find_the_known_key(self) -> None:
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=128, end=255)
        result = scan_chunk(self.puzzle, chunk, workers=4)
        self.assertEqual(result.found_key, 224)

    def test_rejects_a_non_positive_batch_size(self) -> None:
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=128, end=140)
        with self.assertRaises(ValueError):
            scan_chunk(self.puzzle, chunk, workers=1, batch_size=0)

    def test_session_shares_one_pool_across_chunks(self) -> None:
        plan = ChunkPlan(self.puzzle, chunk_size=8, seed="pooled")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            result = run_session(plan, chunks=16, workers=2, checkpoint_path=path)
            self.assertEqual(result.found_key, 224)

    def test_session_resumes_from_atomic_checkpoint(self) -> None:
        plan = ChunkPlan(self.puzzle, chunk_size=8, seed="resume")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            first = run_session(plan, chunks=1, workers=1, checkpoint_path=path)
            checkpoint = load_checkpoint(path)
            if first.found_key is None:
                self.assertEqual(checkpoint.next_sequence, 1)
                second = run_session(plan, chunks=1, workers=1, checkpoint_path=path)
                self.assertGreaterEqual(second.checked, 1)
            else:
                self.assertIsNotNone(checkpoint.found_key_hex)


if __name__ == "__main__":
    unittest.main()

