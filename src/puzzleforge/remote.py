from __future__ import annotations

import json
import ssl
import threading
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .coordinator import Completion, Lease
from .engine import BitCrackEngine, EngineOutcome
from .registry import get_puzzle


class RemoteError(RuntimeError):
    pass


class CoordinatorClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 30,
        allow_insecure_http: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("coordinator URL must be an absolute HTTP(S) URL")
        if (
            parsed.scheme != "https"
            and parsed.hostname not in local_hosts
            and not allow_insecure_http
        ):
            raise ValueError(
                "remote coordinator requires HTTPS; use --allow-insecure-http only on a trusted tunnel"
            )
        if len(api_token) < 24:
            raise ValueError("API token must contain at least 24 characters")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def lease(self, worker: str, lease_seconds: int) -> Lease | None:
        response = self._request(
            "POST",
            "/v1/lease",
            {"worker": worker, "lease_seconds": lease_seconds},
        )
        raw = response.get("lease")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RemoteError("coordinator returned an invalid lease")
        try:
            return Lease(**raw)
        except (TypeError, ValueError) as exc:
            raise RemoteError("coordinator returned an invalid lease") from exc

    def heartbeat(self, token: str, worker: str, lease_seconds: int) -> float:
        response = self._request(
            "POST",
            "/v1/heartbeat",
            {"token": token, "worker": worker, "lease_seconds": lease_seconds},
        )
        return float(response["expires_at"])

    def complete(
        self, lease: Lease, outcome: EngineOutcome
    ) -> Completion:
        response = self._request(
            "POST",
            "/v1/complete",
            {
                "token": lease.token,
                "worker": lease.worker,
                "checked": outcome.checked,
                "found_key_hex": (
                    None if outcome.found_key is None else f"{outcome.found_key:064x}"
                ),
                "elapsed_seconds": outcome.elapsed_seconds,
                "rate_keys_per_second": outcome.rate_keys_per_second,
            },
        )
        try:
            return Completion(**response)
        except (TypeError, ValueError) as exc:
            raise RemoteError("coordinator returned an invalid completion") from exc

    def fail(self, lease: Lease, error: str) -> None:
        self._request(
            "POST",
            "/v1/fail",
            {"token": lease.token, "worker": lease.worker, "error": error},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "PuzzleForge-worker/0.2",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = _error_detail(exc.read())
            raise RemoteError(f"coordinator HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RemoteError(f"coordinator request failed: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteError("coordinator returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RemoteError("coordinator response must be a JSON object")
        return parsed


@dataclass(frozen=True, slots=True)
class WorkerRun:
    outcome: str
    message: str
    found: bool = False


class GPUWorker:
    def __init__(
        self,
        client: CoordinatorClient,
        engine: BitCrackEngine,
        *,
        worker: str,
        lease_seconds: int = 900,
    ) -> None:
        self.client = client
        self.engine = engine
        self.worker = worker
        self.lease_seconds = lease_seconds

    def run_once(self) -> WorkerRun:
        lease = self.client.lease(self.worker, self.lease_seconds)
        if lease is None:
            state = str(self.client.status().get("state", "unknown"))
            return WorkerRun("idle", f"no work available; campaign state={state}")

        puzzle = get_puzzle(lease.puzzle)
        if lease.address != puzzle.address:
            raise RemoteError("lease target does not match the reviewed registry")
        chunk = lease.chunk
        if chunk.size != lease.keys:
            raise RemoteError("lease range size is inconsistent")

        stopped = threading.Event()
        heartbeat_errors: list[BaseException] = []
        interval = max(5.0, min(60.0, self.lease_seconds / 3))

        def heartbeat_loop() -> None:
            while not stopped.wait(interval):
                try:
                    self.client.heartbeat(
                        lease.token, lease.worker, self.lease_seconds
                    )
                except BaseException as exc:  # propagated in the worker thread
                    heartbeat_errors.append(exc)
                    return

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name="puzzleforge-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            outcome = self.engine.scan(puzzle, chunk)
        finally:
            stopped.set()
            heartbeat.join(timeout=interval + 1)

        if heartbeat_errors:
            raise RemoteError(f"lease heartbeat failed: {heartbeat_errors[0]}")
        if outcome.status == "error":
            self.client.fail(lease, outcome.message)
            return WorkerRun("error", outcome.message)

        completion = self.client.complete(lease, outcome)
        return WorkerRun(
            "found" if completion.found else "complete",
            (
                f"chunk {lease.sequence} checked {outcome.checked:,} keys at "
                f"{outcome.rate_keys_per_second:,.0f} keys/s"
            ),
            found=completion.found,
        )


def _error_detail(raw: bytes) -> str:
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
            return parsed["error"][:1_000]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "request rejected"
