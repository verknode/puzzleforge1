import threading
import unittest

from puzzleforge.engine import EngineOutcome
from puzzleforge.partition import KeyChunk
from puzzleforge.thermal import ThermalGuardedEngine, ThermalPolicy


class AbortAwareEngine:
    def __init__(self, abort: threading.Event) -> None:
        self.abort = abort
        self.calls = 0

    def scan(self, puzzle, chunk):
        self.calls += 1
        if self.calls == 1:
            self.abort.wait(timeout=1)
            return EngineOutcome(
                status="error",
                checked=0,
                elapsed_seconds=0.1,
                rate_keys_per_second=0.0,
                message="aborted",
            )
        return EngineOutcome(
            status="complete",
            checked=chunk.size,
            elapsed_seconds=1.0,
            rate_keys_per_second=chunk.size,
            message="complete",
        )


class ThermalTests(unittest.TestCase):
    def test_invalid_hysteresis_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ThermalPolicy(maximum_c=80, resume_c=80)

    def test_overheat_aborts_cools_and_retries_same_chunk(self) -> None:
        abort = threading.Event()
        inner = AbortAwareEngine(abort)
        temperatures = iter((70.0, 85.0, 75.0, 70.0))
        lock = threading.Lock()

        def snapshot(device):
            with lock:
                try:
                    temperature = next(temperatures)
                except StopIteration:
                    temperature = 70.0
            return {"available": True, "temperature_c": temperature}

        guarded = ThermalGuardedEngine(
            inner,
            abort,
            device=0,
            policy=ThermalPolicy(
                maximum_c=82,
                resume_c=72,
                poll_seconds=0.01,
                max_retries=2,
            ),
            snapshot=snapshot,
            sleep=lambda _: None,
        )
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=1, end=128)
        outcome = guarded.scan(object(), chunk)
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.checked, chunk.size)
        self.assertEqual(inner.calls, 2)

    def test_missing_telemetry_fails_closed(self) -> None:
        abort = threading.Event()
        inner = AbortAwareEngine(abort)
        guarded = ThermalGuardedEngine(
            inner,
            abort,
            device=0,
            snapshot=lambda _: {"available": False, "error": "no driver"},
        )
        chunk = KeyChunk(ordinal=0, chunk_id=0, start=1, end=128)
        outcome = guarded.scan(object(), chunk)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(inner.calls, 0)
        self.assertIn("no telemetry", outcome.message)


if __name__ == "__main__":
    unittest.main()
