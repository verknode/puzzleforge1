"""Local worker liveness and performance; never a source of coverage credit."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def campaign_gpu_lock(database: Path):
    """OS-owned lock, automatically released even if a worker crashes."""
    path = database.with_suffix(".gpu.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another local worker or retune is using this campaign. Stop it first.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def runtime_path(database: Path) -> Path:
    return database.with_suffix(".runtime.json")


def read_runtime(database: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    try:
        value = json.loads(runtime_path(database).read_text(encoding="utf-8"))
        if value.get("schema") != 1:
            raise ValueError("unsupported runtime record")
        age = max(0.0, now - float(value["updated_at_epoch"]))
        value["heartbeat_age_seconds"] = age
        terminal = value["phase"] in {"stopped", "error", "found", "idle"}
        value["alive"] = age <= 15 and not terminal
        if age > 15 and not terminal:
            value["phase"] = "stale"
        rate_at = value.get("rate_at_epoch")
        value["rate_age_seconds"] = None if rate_at is None else max(0.0, now - rate_at)
        if not value["alive"] or rate_at is None or now - rate_at > 15 or value["phase"] != "scanning":
            value["reported_rate_keys_per_second"] = None
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"schema": 1, "phase": "unknown", "alive": False,
                "reported_rate_keys_per_second": None}


class RuntimeMonitor:
    def __init__(self, database: Path) -> None:
        self.path = runtime_path(database)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._warning = False
        self.data = {"schema": 1, "pid": os.getpid(), "phase": "starting",
                     "started_at_epoch": time.time(), "confirmed_keys": "0",
                     "completed_chunks": 0, "reported_rate_keys_per_second": None,
                     "rate_at_epoch": None, "thermal_retries": 0}
        self._thread = threading.Thread(target=self._loop, name="puzzleforge-runtime", daemon=True)

    def __enter__(self):
        self._publish()
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join(timeout=5)
        if exc_type is not None:
            self.phase("stopped" if issubclass(exc_type, KeyboardInterrupt) else "error")
        elif self.data["phase"] not in {"found", "error", "idle"}:
            self.phase("stopped")
        self._publish()

    def phase(self, value: str) -> None:
        with self._lock:
            self.data["phase"] = value
            if value != "scanning":
                self.data["reported_rate_keys_per_second"] = None
                self.data["rate_at_epoch"] = None

    def begin_chunk(self, chunk) -> None:
        self.phase("initializing")
        with self._lock:
            self.data.update(chunk_id=str(chunk.chunk_id), chunk_start_hex=f"{chunk.start:x}",
                             chunk_end_hex=f"{chunk.end:x}", chunk_started_at_epoch=time.time(), first_rate_seconds=None)

    def progress(self, event: dict) -> None:
        # This allowlist deliberately excludes output lines, candidates and seeds.
        with self._lock:
            if event.get("phase") in {"initializing", "scanning", "cooling"}:
                self.data["phase"] = event["phase"]
            for key in ("reported_rate_keys_per_second", "rate_at_epoch", "first_rate_seconds"):
                if key in event:
                    self.data[key] = event[key]
            if event.get("phase") in {"initializing", "cooling"}:
                self.data["reported_rate_keys_per_second"] = None
                self.data["rate_at_epoch"] = None
            if event.get("thermal_retry"):
                self.data["thermal_retries"] += 1

    def complete(self, outcome) -> None:
        with self._lock:
            self.data["confirmed_keys"] = str(int(self.data["confirmed_keys"]) + outcome.checked)
            self.data["completed_chunks"] += 1
            self.data["last_completed_at_epoch"] = time.time()
            self.data["last_chunk_seconds"] = outcome.elapsed_seconds
            self.data["last_chunk_rate_keys_per_second"] = outcome.checked / max(outcome.elapsed_seconds, 1e-9)
        self.phase("found" if outcome.status == "found" else "planning")
        self._publish()

    def _loop(self) -> None:
        while not self._stop.wait(2):
            self._publish()

    def _publish(self) -> None:
        with self._lock:
            elapsed = max(time.monotonic() - self._started, 1e-9)
            self.data.update(updated_at_epoch=time.time(), elapsed_seconds=elapsed,
                             session_rate_keys_per_second=int(self.data["confirmed_keys"]) / elapsed)
            payload = dict(self.data)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, suffix=".runtime.tmp")
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, allow_nan=False)
            os.replace(temporary, self.path)
        except OSError:
            if not self._warning:
                print("Live telemetry could not be saved; the campaign ledger is unaffected.", file=sys.stderr)
                self._warning = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
