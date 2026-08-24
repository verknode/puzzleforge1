from __future__ import annotations

import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .coordinator import Coordinator, CoordinatorError, LeaseRejected


MAX_BODY_BYTES = 64 * 1024
LOGGER = logging.getLogger("puzzleforge.coordinator")


class CoordinatorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        coordinator: Coordinator,
        api_token: str,
    ) -> None:
        if len(api_token) < 24:
            raise ValueError("API token must contain at least 24 characters")
        self.coordinator = coordinator
        self.api_token = api_token
        super().__init__(address, CoordinatorRequestHandler)


class CoordinatorRequestHandler(BaseHTTPRequestHandler):
    server: CoordinatorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/v1/status":
            if not self._authorized():
                return
            self._send(HTTPStatus.OK, self.server.coordinator.status())
            return
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        try:
            body = self._read_json()
            if path == "/v1/lease":
                lease = self.server.coordinator.lease(
                    _required_string(body, "worker"),
                    lease_seconds=_optional_integer(body, "lease_seconds", 900),
                )
                self._send(
                    HTTPStatus.OK,
                    {"lease": None if lease is None else lease.to_dict()},
                )
                return
            if path == "/v1/heartbeat":
                expires_at = self.server.coordinator.heartbeat(
                    _required_string(body, "token"),
                    _required_string(body, "worker"),
                    lease_seconds=_optional_integer(body, "lease_seconds", 900),
                )
                self._send(HTTPStatus.OK, {"expires_at": expires_at})
                return
            if path == "/v1/complete":
                completion = self.server.coordinator.complete(
                    _required_string(body, "token"),
                    _required_string(body, "worker"),
                    checked=_required_integer(body, "checked"),
                    found_key_hex=_optional_string(body, "found_key_hex"),
                    elapsed_seconds=_optional_number(body, "elapsed_seconds"),
                    rate_keys_per_second=_optional_number(
                        body, "rate_keys_per_second"
                    ),
                )
                self._send(HTTPStatus.OK, completion.to_dict())
                return
            if path == "/v1/fail":
                self.server.coordinator.fail(
                    _required_string(body, "token"),
                    _required_string(body, "worker"),
                    error=_required_string(body, "error"),
                )
                self._send(HTTPStatus.OK, {"accepted": True})
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except LeaseRejected as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except CoordinatorError as exc:
            LOGGER.exception("coordinator state error")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        except Exception:
            LOGGER.exception("unhandled coordinator request error")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal coordinator error"},
            )

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.api_token}"
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, expected):
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"error": "missing or invalid bearer token"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _send(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def serve(
    coordinator: Coordinator,
    *,
    api_token: str,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    with CoordinatorHTTPServer((host, port), coordinator, api_token) as server:
        LOGGER.warning("PuzzleForge coordinator listening on %s:%d", host, port)
        server.serve_forever()


def _required_string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _required_integer(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _optional_integer(body: dict[str, Any], name: str, default: int) -> int:
    if name not in body:
        return default
    return _required_integer(body, name)


def _optional_number(body: dict[str, Any], name: str) -> float | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or null")
    return float(value)
