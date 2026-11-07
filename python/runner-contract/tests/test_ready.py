"""runner-contract ready: the self-test, the states, the health answer and the command's refusals."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry as SchemaRegistry
from referencing import Resource
from referencing.jsonschema import DRAFT202012

from livepeer_runner_contract import Registry
from livepeer_runner_contract.cli import main
from livepeer_runner_contract.conform import Options, Target, run_suite
from livepeer_runner_contract.manifest import parse_manifest, render_envelope
from livepeer_runner_contract.ready import Readiness, SelfTest, answer, engine_up, front, self_test, serve
from reference.profiles import BEHAVIOURS, manifest_for
from reference.server import ReferenceRunner, Settings

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(ROOT)


def target(registry: Registry, runner: ReferenceRunner, profiles: list[str], reasoning: str | None = None) -> Target:
    text = manifest_for(profiles, runner.url, registry, reasoning=reasoning)
    manifest = parse_manifest(text, capabilities=registry.capabilities or {})
    assert manifest.model is not None
    return Target(manifest.id, manifest.url, render_envelope(manifest), None, manifest.model.name, manifest)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_starting_until_the_first_self_test_passes() -> None:
    results = iter([SelfTest(False, "the model is loading"), SelfTest(True, "ok")])
    readiness = Readiness(lambda: next(results), lambda: True)
    assert readiness.step() == "starting"
    assert readiness.detail == "the model is loading"
    assert readiness.step() == "ready"


def test_an_engine_down_at_start_stays_starting_and_runs_no_self_test() -> None:
    calls: list[int] = []

    def test() -> SelfTest:
        calls.append(1)
        return SelfTest(True, "ok")

    readiness = Readiness(test, lambda: False)
    assert readiness.step() == "starting"
    assert not calls


def test_the_engine_failing_after_ready_is_failed_and_its_return_runs_the_self_test_again() -> None:
    up = [True]
    calls: list[int] = []

    def test() -> SelfTest:
        calls.append(1)
        return SelfTest(True, "ok")

    readiness = Readiness(test, lambda: up[0])
    assert readiness.step() == "ready"
    assert readiness.step() == "ready" and len(calls) == 1  # no retest while the engine stays up
    up[0] = False
    assert readiness.step() == "failed"
    up[0] = True
    assert readiness.step() == "ready" and len(calls) == 2


def test_a_retest_that_fails_after_ready_is_failed() -> None:
    clock = Clock()
    results = iter([SelfTest(True, "ok"), SelfTest(False, "chat_completions.json.answer: status 500")])
    readiness = Readiness(lambda: next(results), lambda: True, retest=60, clock=clock)
    assert readiness.step() == "ready"
    clock.now = 59
    assert readiness.step() == "ready"
    clock.now = 60
    assert readiness.step() == "failed"


def test_a_self_test_that_raises_is_a_failed_self_test() -> None:
    def broken() -> SelfTest:
        raise RuntimeError("boom")

    readiness = Readiness(broken, lambda: True)
    assert readiness.step() == "starting"
    assert "couldn't run" in readiness.detail


def test_the_answer_is_the_health_entrys(registry: Registry) -> None:
    entry = registry.entry("livepeer.health.v1")
    resource: Resource[Any] = Resource.from_contents(entry.document, default_specification=DRAFT202012)
    validator = Draft202012Validator(
        {"$ref": "urn:health#/components/schemas/Health"},
        registry=SchemaRegistry().with_resource("urn:health", resource),
    )
    for state, status in (("ready", 200), ("starting", 503), ("failed", 503)):
        code, body = answer(state, "x" * 500)
        document = json.loads(body)
        assert code == status and document["status"] == state
        assert not list(validator.iter_errors(document))
        assert ("detail" in document) is (state != "ready")


@pytest.mark.parametrize(
    "profiles",
    [["openai.chat.v1"], ["openai.chat.v2", "anthropic.messages.v2", "openai.responses.stateless.v2"]],
)
def test_the_self_test_passes_a_working_runner(registry: Registry, profiles: list[str]) -> None:
    with ReferenceRunner([BEHAVIOURS[p] for p in profiles], registry) as runner:
        result = self_test(registry, target(registry, runner, profiles), Options(timeout=10))
    assert result.passed, result.detail


def test_the_self_test_passes_a_reasoning_model_and_fails_one_that_says_it_doesnt_reason(registry: Registry) -> None:
    profiles = ["openai.chat.v1"]
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry, Settings(thinks=28)) as runner:
        assert self_test(registry, target(registry, runner, profiles, reasoning="on")).passed
        result = self_test(registry, target(registry, runner, profiles, reasoning="off"))
    assert not result.passed and result.detail.startswith("chat_completions.reasoning_off.content")


@pytest.mark.parametrize(("fault", "check"), [("no_final", "chat_completions.sse.answer"), ("health_starting", None)])
def test_the_self_test_judges_answers_not_the_runners_own_health(
    registry: Registry, fault: str, check: str | None
) -> None:
    settings = Settings(faults=frozenset({fault}))
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry, settings) as runner:
        result = self_test(registry, target(registry, runner, ["openai.chat.v1"]))
    if check is None:
        assert result.passed, result.detail
    else:
        assert not result.passed and result.detail.startswith(check)


def test_engine_up_is_any_2xx(registry: Registry) -> None:
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry) as runner:
        assert engine_up(runner.health_url)
        assert not engine_up(runner.url + "/missing")
    assert not engine_up(runner.health_url, timeout=0.5)  # closed


@pytest.fixture
def served() -> Iterator[tuple[Readiness, str]]:
    readiness = Readiness(lambda: SelfTest(True, "ok"), lambda: True)
    server = serve(readiness, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield readiness, f"http://{host!s}:{port}/healthz"
    server.shutdown()
    server.server_close()


def test_serve_answers_every_get_with_the_state(served: tuple[Readiness, str]) -> None:
    readiness, url = served
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(url, timeout=5)  # noqa: S310 (a local http URL)
    assert caught.value.code == 503 and json.loads(caught.value.read())["status"] == "starting"
    readiness.step()
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 (a local http URL)
        assert response.status == 200 and json.loads(response.read()) == {"status": "ready"}


def test_the_command_refuses_what_it_cant_serve(
    registry: Registry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(["openai.chat.v1"], "http://127.0.0.1:9", registry), encoding="utf-8")
    base = ["ready", "--manifest", str(manifest), "--registry", str(ROOT), "--engine-health", "http://127.0.0.1:9/"]
    assert main([*base, "--listen", "nowhere"]) == 2
    assert "--listen nowhere" in capsys.readouterr().err
    images = tmp_path / "images.toml"
    images.write_text(manifest_for(["openai.images.v1"], "http://127.0.0.1:9", registry), encoding="utf-8")
    assert main(["ready", "--manifest", str(images), "--registry", str(ROOT), "--engine-health", "http://x/"]) == 2
    assert "no conformance suite" in capsys.readouterr().err


@pytest.fixture
def fronted(registry: Registry) -> Iterator[tuple[ReferenceRunner, Readiness, str]]:
    """A reference engine behind a front, on one address, as a runner reached through a one-target tunnel."""
    profiles = ["openai.chat.v1", "openai.responses.stateless.v2"]
    with ReferenceRunner([BEHAVIOURS[p] for p in profiles], registry) as engine:
        engine_target = target(registry, engine, profiles, reasoning="off")
        readiness = Readiness(lambda: self_test(registry, engine_target, Options(timeout=10)), lambda: True)
        server = front(readiness, engine.url, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        yield engine, readiness, f"http://{host!s}:{port}"
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict[str, Any]) -> tuple[int, bytes]:
    request = urllib.request.Request(  # noqa: S310 (a local http URL)
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (a local http URL)
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_the_front_refuses_calls_until_ready_then_relays_them_unchanged(
    fronted: tuple[ReferenceRunner, Readiness, str],
) -> None:
    engine, readiness, url = fronted
    call = {"model": "reference-model", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 64}
    assert _post(url + "/v1/chat/completions", call)[0] == 503
    assert readiness.step() == "ready"
    direct = _post(engine.url + "/v1/chat/completions", call)
    assert _post(url + "/v1/chat/completions", call) == direct
    assert _post(url + "/v1/chat/completions", {"model": "reference-model", "messages": []})[0] == 400


def test_a_runner_behind_the_front_conforms_on_one_address(
    registry: Registry, fronted: tuple[ReferenceRunner, Readiness, str]
) -> None:
    """Health and calls share the front's address; a stream closed mid-answer closes the engine's too."""
    engine, readiness, url = fronted
    readiness.step()
    for profile in ("openai.chat.v1", "openai.responses.stateless.v2"):
        text = manifest_for([profile], url, registry, reasoning="off")
        manifest = parse_manifest(text, capabilities=registry.capabilities or {})
        assert manifest.model is not None
        runner = Target(manifest.id, url, render_envelope(manifest), url + "/healthz", manifest.model.name, manifest)
        report = run_suite(registry, runner, profile, Options(cancel_settle_s=0.2))
        assert report.passed, [f"{c.id}: {c.detail}" for c in report.checks if c.result == "fail"]
    assert any(path.startswith("/v1/") for path in engine.state.cancelled), engine.state.cancelled


def test_an_engine_error_makes_the_front_self_test_again() -> None:
    """A 500 from the engine runs the self-test again; the caller's own mistakes and 503s don't."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Engine(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status = int(self.path.rsplit("/", 1)[-1])
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    engine = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=engine.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    tests: list[int] = []

    def test() -> SelfTest:
        tests.append(1)
        return SelfTest(True, "ok")

    readiness = Readiness(test, lambda: True)
    host, port = engine.server_address[:2]
    server = front(readiness, f"http://{host!s}:{port}", "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        readiness.step()
        for status in (400, 503):
            assert _post(f"{url}/status/{status}", {})[0] == status
            readiness.step()
        assert len(tests) == 1
        assert _post(f"{url}/status/500", {})[0] == 500
        readiness.step()
        assert len(tests) == 2
    finally:
        server.shutdown()
        server.server_close()
        engine.shutdown()
        engine.server_close()


def test_a_runner_that_finds_itself_broken_fails_at_once_and_tests_again() -> None:
    results = iter([SelfTest(True, "ok"), SelfTest(False, "the encoder is gone"), SelfTest(True, "ok")])
    readiness = Readiness(lambda: next(results), lambda: True)
    readiness.fail("the GPU fell off the bus")
    assert readiness.state == "starting"  # nothing has passed yet
    assert readiness.step() == "ready"
    readiness.fail("the encoder failed a call")
    assert readiness.snapshot() == ("failed", "the encoder failed a call")
    assert readiness.step() == "failed"
    assert readiness.step() == "ready"
