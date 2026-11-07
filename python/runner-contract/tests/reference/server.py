"""A reference runner: a small stdlib HTTP server that answers profiles as their registry entries say.

CI check 6 runs every conformance suite against one, so a suite that a correct runner fails, or that
a broken runner passes, can't merge. The server does what every runner must (SPEC.md, "How every
runner behaves"): health answers 200 only while ready, requests are checked against the entry, limits
are refused with declared errors before any work, and streams end as the success rule says. Each
profile's answers come from a ``Behaviour`` in profiles.py. ``faults`` make it misbehave on purpose.

Run one by hand, to try ``runner-contract conform`` against it:

    python python/runner-contract/tests/reference/server.py openai.chat.v1 --port 8765
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from livepeer_runner_contract import Registry, validate_request
from livepeer_runner_contract._json import ABSENT, lookup

FAULTS = frozenset(
    {
        "slow_headers",  # streamed answers' headers after 1.2 s
        "health_starting",  # health answers 503 starting, always
        "accept_invalid",  # answers a request that breaks the schema
        "ignore_limits",  # works past every limit
        "die_on_cancel",  # a closed stream makes health fail
        "no_final",  # a stream ends without its final event
        "no_usage",  # no usage anywhere: no work reports
        "bad_schema",  # a JSON answer without its model
        "stream_error",  # an error event in the middle of a stream
        "refuse_early",  # answers a body past max_input_bytes without reading it, and closes
    }
)


@dataclass
class Answer:
    status: int
    body: Any = None
    """The JSON body, for a JSON answer."""
    events: list[tuple[str | None, Any]] | None = None
    """An SSE answer's events: (event name, data as JSON or text)."""
    content_type: str = "application/json"


class Behaviour:
    """How one profile answers. Subclasses in profiles.py fill these in from the profile's entry."""

    profile = ""

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
        """The answer to a valid request. ``thinks``: output tokens spent reasoning before any content."""
        raise NotImplementedError

    def invalid(self, endpoint: str, problem: str) -> Answer:
        raise NotImplementedError

    def too_long(self, endpoint: str, limit: int) -> Answer:
        raise NotImplementedError

    def too_large(self, endpoint: str) -> Answer:
        raise NotImplementedError

    def error_event(self) -> tuple[str | None, Any]:
        raise NotImplementedError


def count_tokens(value: Any) -> int:
    """A rough token count: the words of every string in the request."""
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, dict):
        return sum(count_tokens(v) for k, v in value.items() if k != "model")
    if isinstance(value, list):
        return sum(count_tokens(v) for v in value)
    return 0


@dataclass
class Settings:
    context_tokens: int = 4096
    max_input_bytes: int = 9 * 1024 * 1024
    faults: frozenset[str] = frozenset()
    event_gap_s: float = 0.01
    thinks: int = 0
    """Output tokens the engine spends reasoning before it answers, as a reasoning model does; 0 for none."""


def _strip(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_strip(v, key) for v in value]
    return value


@dataclass
class _State:
    behaviours: dict[tuple[str, str], tuple[Behaviour, str]]
    """By (method, path): the behaviour and endpoint name."""
    registry: Registry
    settings: Settings
    healthy: bool = True
    cancelled: list[str] = field(default_factory=list)
    """Paths whose answer the client closed before it ended."""
    errors: list[str] = field(default_factory=list)
    """Anything else that went wrong in a handler: a bug in the reference runner."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    state: _State

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        import traceback

        error = sys.exc_info()[1]
        if isinstance(error, BrokenPipeError | ConnectionResetError):
            self.state.cancelled.append("(closed early)")
        else:
            self.state.errors.append("".join(traceback.format_exception(error)))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        return

    def _json(self, status: int, body: Any, *, slow: bool = False) -> None:
        data = json.dumps(body).encode()
        if slow:
            time.sleep(1.2)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        state = self.server.state
        if self.path.split("?", 1)[0] != "/health":
            self._json(404, {"error": {"message": "not found"}})
        elif "health_starting" in state.settings.faults:
            self._json(503, {"status": "starting"})
        elif not state.healthy:
            self._json(503, {"status": "failed", "detail": "a cancelled stream broke the runner"})
        else:
            self._json(200, {"status": "ready"})

    def do_POST(self) -> None:
        state = self.server.state
        faults = state.settings.faults
        found = state.behaviours.get(("POST", self.path))
        length = int(self.headers.get("Content-Length") or 0)
        if found is None:
            self.rfile.read(length)
            self._json(404, {"error": {"message": "not found"}})
            return
        behaviour, endpoint = found
        if length > state.settings.max_input_bytes and "ignore_limits" not in faults:
            if "refuse_early" in faults:
                self.close_connection = True
            else:
                while length > 0:  # drain, then refuse, before any work
                    length -= len(self.rfile.read(min(length, 1 << 16)))
            self._send(behaviour.too_large(endpoint), faults)
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
        except ValueError:
            self._send(behaviour.invalid(endpoint, "the body isn't JSON"), faults)
            return
        check = validate_request(state.registry, behaviour.profile, endpoint, request)
        if not check.ok and "accept_invalid" not in faults:
            self._send(behaviour.invalid(endpoint, check.problems[0]), faults)
            return
        if "ignore_limits" not in faults:
            operation = state.registry.operation(behaviour.profile, endpoint)
            for item in operation.x.get("inputs", []):
                value = lookup(request, item.get("field", ""))
                if value is ABSENT or not isinstance(item.get("max_bytes"), int):
                    continue
                if len(json.dumps(value).encode()) > item["max_bytes"]:
                    self._send(behaviour.too_large(endpoint), faults)
                    return
            if count_tokens(request) > state.settings.context_tokens:
                self._send(behaviour.too_long(endpoint, state.settings.context_tokens), faults)
                return
        answer = behaviour.answer(endpoint, request if isinstance(request, dict) else {}, state.settings.thinks)
        self._send(answer, faults, behaviour)

    def _send(self, answer: Answer, faults: frozenset[str], behaviour: Behaviour | None = None) -> None:
        body, events = answer.body, answer.events
        if "no_usage" in faults:
            body = _strip(body, "usage")
            events = None if events is None else [(name, _strip(data, "usage")) for name, data in events]
        if events is None:
            if "bad_schema" in faults and answer.status == 200 and isinstance(body, dict):
                body = {k: v for k, v in body.items() if k != "model"}
            self._json(answer.status, body)
            return
        if "no_final" in faults:
            events = events[:-1]
        if "stream_error" in faults and behaviour is not None:
            events = [*events[:2], behaviour.error_event(), *events[2:]]
        if "slow_headers" in faults and answer.status == 200:
            time.sleep(1.2)
        self.send_response(answer.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for name, data in events:
                text = data if isinstance(data, str) else json.dumps(data)
                chunk = (f"event: {name}\n" if name else "") + f"data: {text}\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
                time.sleep(self.server.state.settings.event_gap_s)
        except (BrokenPipeError, ConnectionResetError):
            self.server.state.cancelled.append(self.path)
            if "die_on_cancel" in faults:
                self.server.state.healthy = False


class ReferenceRunner:
    """A reference runner on 127.0.0.1, on a free port, for as long as the ``with`` block lasts."""

    def __init__(self, behaviours: Sequence[Behaviour], registry: Registry, settings: Settings | None = None) -> None:
        routes: dict[tuple[str, str], tuple[Behaviour, str]] = {}
        for behaviour in behaviours:
            for name, operation in registry.entry(behaviour.profile).operations.items():
                routes[(operation.method, operation.path)] = (behaviour, name)
        self.state = _State(routes, registry, settings or Settings())
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.state = self.state
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def health_url(self) -> str:
        return f"{self.url}/health"

    def __enter__(self) -> ReferenceRunner:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from reference.profiles import BEHAVIOURS

    parser = argparse.ArgumentParser(description="Serve reference runners for profiles, until interrupted.")
    parser.add_argument("profile", nargs="+", choices=sorted(BEHAVIOURS))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--registry", default=str(Path(__file__).resolve().parents[4]))
    parser.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS))
    args = parser.parse_args()
    reference = ReferenceRunner(
        [BEHAVIOURS[p] for p in args.profile], Registry.load(args.registry), Settings(faults=frozenset(args.fault))
    )
    reference._server.server_close()
    server = _Server(("127.0.0.1", args.port), _Handler)
    server.state = reference.state
    print(f"reference runner for {', '.join(args.profile)} at http://127.0.0.1:{args.port}", flush=True)  # noqa: T201
    server.serve_forever()
