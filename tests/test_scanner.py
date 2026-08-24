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

