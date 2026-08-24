import threading
import tempfile
import unittest
from pathlib import Path

from puzzleforge.coordinator import Coordinator
from puzzleforge.coordinator_http import CoordinatorHTTPServer
from puzzleforge.engine import EngineOutcome
from puzzleforge.remote import CoordinatorClient, GPUWorker


class NoMatchEngine:
    def scan(self, puzzle, chunk):
        return EngineOutcome(
            status="complete",
            checked=chunk.size,
            elapsed_seconds=0.25,
            rate_keys_per_second=chunk.size * 4,
            message="test completion",
        )


class RemoteTests(unittest.TestCase):
    def test_http_worker_completes_one_unique_chunk(self) -> None:
        token = "test-token-that-is-long-enough-123456"
        with tempfile.TemporaryDirectory() as directory:
            coordinator = Coordinator.initialize(
                Path(directory) / "campaign.sqlite3",
                puzzle_number=71,
                chunk_size=256,
                seed="remote-test",
            )
            server = CoordinatorHTTPServer(("127.0.0.1", 0), coordinator, token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = CoordinatorClient(
                    f"http://127.0.0.1:{server.server_port}", token
                )
                result = GPUWorker(
                    client,
                    NoMatchEngine(),
                    worker="gpu-http-test",
                    lease_seconds=30,
                ).run_once()
                self.assertEqual(result.outcome, "complete")
                status = client.status()
                self.assertEqual(status["checked_keys"], "256")
                self.assertEqual(status["completed_chunks"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
