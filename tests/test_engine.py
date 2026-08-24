import os
import tempfile
import unittest
from pathlib import Path

from puzzleforge.engine import (
    BitCrackEngine,
    EngineTuning,
    candidate_keys_from_output,
    parse_reported_rate,
)
from puzzleforge.partition import KeyChunk
from puzzleforge.registry import get_puzzle


class EngineTests(unittest.TestCase):
    def test_rate_parser_uses_last_report(self) -> None:
        output = "[00:01] 2.5 MKey/s\n[00:02] 1.25 GKey/s"
        self.assertEqual(parse_reported_rate(output), 1.25e9)

    def test_candidate_parser_accepts_labeled_short_hex(self) -> None:
        self.assertEqual(candidate_keys_from_output("Private key: 0xe0"), (0xE0,))

    def test_command_has_exact_reviewed_range_and_tuning(self) -> None:
        puzzle = get_puzzle(8)
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=0x80, end=0xFF)
        engine = BitCrackEngine(
            Path("/tmp/cuBitCrack"),
            EngineTuning(device=1, blocks=32, threads=256, points=1024),
        )
        command = engine.build_command(puzzle, chunk, Path("/tmp/matches.txt"))
        self.assertEqual(command[-1], puzzle.address)
        self.assertEqual(command[command.index("--keyspace") + 1], "80:ff")
        self.assertIn("compressed", command)
        self.assertEqual(command[command.index("--device") + 1], "1")

    def test_known_puzzle8_result_is_independently_verified(self) -> None:
        puzzle = get_puzzle(8)
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=puzzle.start, end=puzzle.end)
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fake-cuBitCrack"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
                "out.write_text('Private key: 0xe0\\n', encoding='utf-8')\n"
                "print('8.00 MKey/s')\n",
                encoding="utf-8",
            )
            os.chmod(binary, 0o755)
            outcome = BitCrackEngine(binary).scan(puzzle, chunk)
        self.assertEqual(outcome.status, "found")
        self.assertEqual(outcome.found_key, 0xE0)
        self.assertEqual(outcome.rate_keys_per_second, 8e6)

    def test_failed_process_never_credits_range(self) -> None:
        puzzle = get_puzzle(8)
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=puzzle.start, end=puzzle.end)
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fake-cuBitCrack"
            binary.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            os.chmod(binary, 0o755)
            outcome = BitCrackEngine(binary).scan(puzzle, chunk)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.checked, 0)


if __name__ == "__main__":
    unittest.main()
