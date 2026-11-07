"""``runner-contract ready``: the standard's readiness check, run beside an engine whose own health check isn't one.

SPEC.md ("How every runner behaves", Readiness): ``HealthURL`` answers 200 with ``{"status": "ready"}``
only after a self-test of every advertised profile passes, and 503 with ``starting`` or ``failed``
otherwise. A stock engine's health check (vLLM's and SGLang's ``/health``, Ollama's ``/``) answers 200
once its server is up and tests nothing, so an operator runs this beside it and registers this
service's URL as ``HealthURL``. It never proxies the node's traffic.

Where the node reaches the runner through one address (a tunnel with a single target), ``front``
serves the health check on one path and relays every other request to the engine unchanged,
streams included; a caller closing its connection closes the engine's, so cancellation still reaches
the engine.

The self-test is each advertised profile's conformance suite, its success cases only: the golden
requests ``conform`` sends, for the reasoning mode the envelope declares, judged by the entry's
success rule, with content where the suite looks for it. Timing, schema and work reports are left to
``conform``. After a pass, the engine's own health check decides: when it stops answering 2xx, this
answers ``failed``, and when it answers again the self-test runs again.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .conform import Options, Target, _exchange, _Run, load_common, load_suite, profiles_to_run
from .registry import Registry

__all__ = ["Readiness", "SelfTest", "answer", "engine_up", "front", "self_test", "serve"]

# The checks of a success case that say whether the profile works; the others are conform's to judge.
_SELF_TEST_CHECKS = ("request", "answer", "content")
_DETAIL_MAX = 200  # the health entry's limit on detail


@dataclass(frozen=True)
class SelfTest:
    passed: bool
    detail: str


def self_test(registry: Registry, target: Target, options: Options | None = None) -> SelfTest:
    """Each advertised profile's success cases, sent to the runner; the first that fails, or a pass."""
    options = options or Options()
    profiles = profiles_to_run(target, registry)
    if not profiles:
        return SelfTest(False, "the envelope advertises no profile")
    common = load_common(registry)
    sent = 0
    for profile in profiles:
        suite = load_suite(registry, profile)
        run = _Run(registry, target, suite, common, options)
        run.check_envelope()
        if not run.endpoints:
            return SelfTest(False, f"the envelope advertises no endpoint of {profile}")
        for case in suite.cases:
            if case.framing is None or case.limit is not None or not case.valid:
                continue
            if any(name not in options.fixtures for name in case.fixtures):
                continue  # minted inputs expire; a self-test runs only the cases it was given them for
            run.run_case(case)
        for check in run.checks:
            if check.id.startswith("common."):
                continue
            if check.id.rsplit(".", 1)[-1] in _SELF_TEST_CHECKS and check.result == "fail":
                return SelfTest(False, f"{check.id}: {check.detail}")
            sent += check.id.endswith(".answer")
    if not sent:
        return SelfTest(False, "no success case of the advertised profiles applies to this runner")
    return SelfTest(True, f"{sent} self-test request(s) of {', '.join(profiles)} succeeded")


def engine_up(url: str, timeout: float = 10.0) -> bool:
    """Whether the engine's own health check answers 2xx."""
    got = _exchange("GET", url, None, accept="*/*", timeout=timeout)
    return got.error is None and 200 <= got.status < 300


class Readiness:
    """``starting`` until the first self-test passes, then ``ready``. A runner that isn't a stock engine
    uses it too, with its own self-test as ``test`` and its own check as ``engine``, calling ``step`` on
    a timer and ``fail`` when a call shows it broken; ``answer`` gives its health check's body.

    A failed self-test before that leaves it ``starting``, since an engine still loading fails it. Once it
    has been ready, the engine's health failing, or a later self-test failing, makes it ``failed``; when
    the engine is up again the self-test runs again. ``retest`` seconds, when set, repeats the self-test
    while ready, for an engine whose health check stays 2xx after it loses its model.
    """

    def __init__(
        self,
        test: Callable[[], SelfTest],
        engine: Callable[[], bool],
        *,
        retest: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._test, self._engine, self._retest, self._clock = test, engine, retest, clock
        self._lock = threading.Lock()
        self.state = "starting"
        self.detail = "the self-test hasn't run yet"
        self._tested_at: float | None = None

    def recheck(self) -> None:
        """Run the self-test again on the next step, as after a call the engine failed."""
        self._tested_at = None

    def fail(self, detail: str) -> None:
        """Answer ``failed`` now, as when a runner's own call finds its encoder or GPU gone, and self-test
        again on the next step. Before the first pass it stays ``starting``."""
        self._tested_at = None
        self._set("failed" if self.state != "starting" else "starting", detail)

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self.state, self.detail

    def _set(self, state: str, detail: str) -> None:
        with self._lock:
            self.state, self.detail = state, detail[:_DETAIL_MAX]

    def step(self) -> str:
        """Check the engine, and run the self-test when it is due. Returns the new state."""
        try:
            up = self._engine()
        except Exception:
            up = False
        if not up:
            self._tested_at = None
            self._set("failed" if self.state != "starting" else "starting", "the engine's own health check fails")
            return self.state
        due = self._tested_at is None or (self._retest > 0 and self._clock() - self._tested_at >= self._retest)
        if not due:
            return self.state
        try:
            result = self._test()
        except Exception as exc:
            result = SelfTest(False, f"the self-test couldn't run: {type(exc).__name__}")
        if result.passed:
            self._tested_at = self._clock()
            self._set("ready", result.detail)
        else:
            self._tested_at = None  # try again on the next step
            self._set("starting" if self.state == "starting" else "failed", result.detail)
        return self.state


def answer(state: str, detail: str = "") -> tuple[int, bytes]:
    """The ``livepeer.health.v1`` answer for a state: 200 only when ready."""
    body: dict[str, Any] = {"status": state}
    if detail and state != "ready":
        body["detail"] = detail[:_DETAIL_MAX]
    return (200 if state == "ready" else 503), json.dumps(body).encode()


def serve(readiness: Readiness, host: str, port: int) -> ThreadingHTTPServer:
    """An HTTP server that answers every GET with the readiness state. The caller runs ``serve_forever``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = answer(*readiness.snapshot())
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # quiet: the node polls often
            pass

    return ThreadingHTTPServer((host, port), Handler)


# Headers about one hop's connection, which the front's own connections replace (RFC 9110, 7.6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
_CHUNK = 65536


def _end_to_end(headers: Any) -> list[tuple[str, str]]:
    named = {v.strip().lower() for k, v in headers.items() if k.lower() == "connection" for v in v.split(",")}
    return [(k, v) for k, v in headers.items() if k.lower() not in _HOP_BY_HOP and k.lower() not in named]


def front(
    readiness: Readiness, engine_url: str, host: str, port: int, *, health_path: str = "/healthz"
) -> ThreadingHTTPServer:
    """An HTTP server that answers ``health_path`` with the readiness state and relays every other
    request to ``engine_url`` unchanged, refusing with 503 until ready. The caller runs ``serve_forever``."""
    engine = urlsplit(engine_url)
    connection_class = HTTPSConnection if engine.scheme == "https" else HTTPConnection
    base = engine.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _plain(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                parts: list[bytes] = []
                while True:
                    size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline().strip():
                            pass  # trailers
                        return b"".join(parts)
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _relay(self) -> None:
            if self.command == "GET" and self.path.split("?", 1)[0] == health_path:
                self._plain(*answer(*readiness.snapshot()))
                return
            body = self._body()
            if readiness.snapshot()[0] != "ready":
                self.close_connection = True
                self._plain(
                    503, json.dumps({"error": {"message": "the runner isn't ready", "type": "unavailable"}}).encode()
                )
                return
            upstream = connection_class(engine.hostname or "", engine.port)
            done = threading.Event()

            def watch() -> None:
                # The caller has sent its whole request, so its socket reads empty only once it hangs up.
                try:
                    gone = self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    gone = True
                if gone and not done.is_set() and upstream.sock is not None:
                    with contextlib.suppress(OSError):
                        upstream.sock.shutdown(socket.SHUT_RDWR)

            try:
                upstream.putrequest(self.command, base + self.path, skip_accept_encoding=True)
                for name, value in _end_to_end(self.headers):
                    if name.lower() != "content-length":
                        upstream.putheader(name, value)
                upstream.putheader("Content-Length", str(len(body)))
                upstream.endheaders(body)
                threading.Thread(target=watch, name="front-watch", daemon=True).start()
                response = upstream.getresponse()
            except (OSError, HTTPException):
                done.set()
                upstream.close()
                self.close_connection = True
                self._plain(
                    502,
                    json.dumps({"error": {"message": "the engine can't be reached", "type": "unavailable"}}).encode(),
                )
                return
            if response.status >= 500 and response.status != 503:
                readiness.recheck()
            self.close_connection = True
            try:
                self.send_response_only(response.status, response.reason)
                for name, value in _end_to_end(response.headers):
                    self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                while chunk := response.read1(_CHUNK):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, HTTPException):
                pass  # the caller went away, or the engine did: either way the other side is closed below
            finally:
                done.set()
                upstream.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _relay

    return ThreadingHTTPServer((host, port), Handler)
