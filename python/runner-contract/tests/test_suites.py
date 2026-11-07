"""CI check 6: every conformance suite passes against its reference runner, and fails against a broken one.

The suites are the repository's own (conformance/), read with the repository's registry, so a pull
request that changes a suite or an entry is checked as it would ship.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from livepeer_runner_contract import Registry, read_result
from livepeer_runner_contract.conform import Options, Report, Target, available_suites, run_suite
from livepeer_runner_contract.manifest import parse_manifest, render_envelope
from reference.profiles import BEHAVIOURS, manifest_for
from reference.server import ReferenceRunner, Settings

ROOT = Path(__file__).resolve().parents[3]
SUITES = available_suites(ROOT)
FAST = Options(cancel_settle_s=0.2)


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(ROOT)


@pytest.fixture(scope="module")
def report_schema() -> Draft202012Validator:
    return Draft202012Validator(json.loads((ROOT / "schemas/conformance-report.v1.schema.json").read_text()))


def conform(registry: Registry, profile: str, runner: ReferenceRunner, reasoning: str | None = "off") -> Report:
    """Run ``profile``'s suite against the runner. A chat runner declares ``reasoning`` as its default."""
    declared = reasoning if "chat" in registry.entry(profile).capabilities else None
    text = manifest_for([profile], runner.url, registry, reasoning=declared)
    manifest = parse_manifest(text, capabilities=registry.capabilities or {})
    assert manifest.model is not None
    target = Target(
        manifest.id, manifest.url, render_envelope(manifest), manifest.health_url, manifest.model.name, manifest
    )
    return run_suite(registry, target, profile, FAST)


@pytest.fixture
def reference(registry: Registry, request: pytest.FixtureRequest) -> Iterator[ReferenceRunner]:
    profile, faults = request.param
    with ReferenceRunner([BEHAVIOURS[profile]], registry, Settings(faults=frozenset(faults))) as runner:
        yield runner
    assert not runner.state.errors, runner.state.errors[0]


def test_the_first_suites_are_there() -> None:
    assert set(SUITES) >= {
        "openai.chat.v1",
        "openai.responses.stateless.v1",
        "anthropic.messages.v1",
        "typesafe.systemone.v1",
        "openai.embeddings.v1",
    }


@pytest.mark.parametrize("profile", SUITES)
def test_every_suite_has_a_reference_runner(profile: str) -> None:
    assert profile in BEHAVIOURS, (
        f"conformance/ has a suite for {profile}, but python/runner-contract/tests/reference/profiles.py has no "
        "reference runner for it; add a Behaviour so CI check 6 can run the suite"
    )


@pytest.mark.parametrize("reference", [(p, ()) for p in SUITES if p in BEHAVIOURS], indirect=True, ids=str)
def test_every_suite_passes_against_its_reference_runner(
    registry: Registry, reference: ReferenceRunner, report_schema: Draft202012Validator
) -> None:
    profile = next(b.profile for b, _ in reference.state.behaviours.values())
    report = conform(registry, profile, reference)
    failed = [f"{c.id}: {c.detail}" for c in report.checks if c.result == "fail"]
    assert report.passed, failed
    assert not list(report_schema.iter_errors(report.document))
    skipped = {c.id for c in report.checks if c.result == "skip"}
    unstreamed = {c.id for c in report.checks if c.id.endswith(".headers") and "not streamed" in c.detail}
    assert skipped - unstreamed == {"common.registration", "common.health.starting"}, skipped
    assert reference.state.cancelled, "the cancellation check closed no answer early"


# Each broken behaviour, and a check that must catch it.
BROKEN = [
    ("openai.chat.v1", "no_final", "chat_completions.sse.answer"),
    ("openai.chat.v1", "stream_error", "chat_completions.sse.answer"),
    ("openai.chat.v1", "accept_invalid", "chat_completions.invalid.answer"),
    ("openai.chat.v1", "no_usage", "chat_completions.json.work_reports"),
    ("openai.chat.v1", "no_usage", "chat_completions.json.schema"),
    ("openai.chat.v1", "slow_headers", "chat_completions.sse.headers"),
    ("openai.chat.v1", "health_starting", "common.health.ready"),
    ("openai.chat.v1", "die_on_cancel", "common.cancel"),
    ("openai.chat.v1", "ignore_limits", "chat_completions.context_tokens.answer"),
    ("openai.chat.v1", "ignore_limits", "chat_completions.inline_cap.answer"),
    ("openai.responses.stateless.v1", "no_final", "responses.sse.answer"),
    ("openai.responses.stateless.v1", "no_usage", "responses.sse.work_reports"),
    ("openai.responses.stateless.v2", "no_final", "responses.sse.answer"),
    ("openai.responses.stateless.v2", "no_usage", "responses.capped.json.work_reports"),
    ("openai.responses.stateless.v2", "stream_error", "responses.capped.sse.answer"),
    ("anthropic.messages.v1", "no_final", "messages.sse.answer"),
    ("anthropic.messages.v1", "stream_error", "messages.sse.answer"),
    ("openai.chat.v2", "no_final", "chat_completions.sse.answer"),
    ("openai.chat.v2", "accept_invalid", "chat_completions.invalid.answer"),
    ("openai.chat.v2", "no_usage", "chat_completions.json.work_reports"),
    ("anthropic.messages.v2", "no_final", "messages.sse.answer"),
    ("anthropic.messages.v2", "stream_error", "messages.sse.answer"),
    ("typesafe.systemone.v1", "bad_schema", "systemone.json.schema"),
    ("openai.embeddings.v1", "accept_invalid", "embeddings.invalid.answer"),
    ("openai.embeddings.v1", "ignore_limits", "embeddings.max_input_bytes.answer"),
]


@pytest.mark.parametrize(
    ("reference", "check_id"),
    [((profile, (fault,)), check_id) for profile, fault, check_id in BROKEN],
    indirect=["reference"],
    ids=[f"{p}-{f}-{c}" for p, f, c in BROKEN],
)
def test_a_broken_runner_fails_its_suite(registry: Registry, reference: ReferenceRunner, check_id: str) -> None:
    profile = next(b.profile for b, _ in reference.state.behaviours.values())
    report = conform(registry, profile, reference)
    assert not report.passed
    results = {c.id: c for c in report.checks}
    assert results[check_id].result == "fail", results[check_id]


def test_an_early_refusal_of_a_large_body_counts(registry: Registry) -> None:
    """A runner that answers 413 before reading the body, and closes, refused it before any work: that passes."""
    settings = Settings(faults=frozenset({"refuse_early"}))
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry, settings) as runner:
        report = conform(registry, "openai.chat.v1", runner)
    assert report.passed, [c for c in report.checks if c.result == "fail"]


# A model that spends 28 tokens thinking before it answers, as BlueClaw measured Qwen3.8 doing.
THINKS = 28
CHAT = [
    "openai.chat.v1",
    "openai.chat.v2",
    "anthropic.messages.v1",
    "anthropic.messages.v2",
    "openai.responses.stateless.v1",
    "openai.responses.stateless.v2",
]
OFF_CASES = {
    "openai.chat.v1": "chat_completions.reasoning_off",
    "openai.chat.v2": "chat_completions.reasoning_off",
    "anthropic.messages.v1": "messages.reasoning_off",
    "anthropic.messages.v2": "messages.reasoning_off",
    "openai.responses.stateless.v2": "responses.reasoning_off",
}


@pytest.mark.parametrize("profile", CHAT)
def test_a_reasoning_model_passes_its_suite(registry: Registry, profile: str) -> None:
    """The success cases leave a reasoning model room to think and still answer."""
    with ReferenceRunner([BEHAVIOURS[profile]], registry, Settings(thinks=THINKS)) as runner:
        report = conform(registry, profile, runner, reasoning="on")
    assert report.passed, [f"{c.id}: {c.detail}" for c in report.checks if c.result == "fail"]
    if profile in OFF_CASES:
        assert next(c for c in report.checks if c.id == OFF_CASES[profile]).result == "skip"


@pytest.mark.parametrize("profile", sorted(OFF_CASES))
def test_a_runner_that_says_it_answers_without_reasoning_but_thinks_fails(registry: Registry, profile: str) -> None:
    with ReferenceRunner([BEHAVIOURS[profile]], registry, Settings(thinks=THINKS)) as runner:
        report = conform(registry, profile, runner, reasoning="off")
    assert {c.id for c in report.checks if c.result == "fail"} == {OFF_CASES[profile] + ".content"}


def test_the_reasoning_off_case_skips_a_runner_that_declares_nothing(registry: Registry) -> None:
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry) as runner:
        report = conform(registry, "openai.chat.v1", runner, reasoning=None)
    check = next(c for c in report.checks if c.id == "chat_completions.reasoning_off")
    assert check.result == "skip" and "/reasoning/default" in check.detail


def _incomplete(streaming: bool) -> tuple[str, str]:
    response = {
        "id": "resp_1",
        "object": "response",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [],
        "usage": {"input_tokens": 9, "output_tokens": 16, "total_tokens": 25},
    }
    if not streaming:
        return "application/json", json.dumps(response)
    event = {"type": "response.incomplete", "sequence_number": 1, "response": response}
    return "text/event-stream", f"event: response.incomplete\ndata: {json.dumps(event)}\n\n"


@pytest.mark.parametrize("streaming", [False, True])
def test_v2_counts_an_answer_its_budget_cut_short_as_success_and_v1_doesnt(registry: Registry, streaming: bool) -> None:
    content_type, body = _incomplete(streaming)
    for profile, success in (("openai.responses.stateless.v1", False), ("openai.responses.stateless.v2", True)):
        result = read_result(registry, profile, "responses", status=200, content_type=content_type, body=body)
        assert result.success is success, (profile, result.failure)


def test_v2_still_fails_a_cut_stream_and_a_failed_response(registry: Registry) -> None:
    v2 = "openai.responses.stateless.v2"
    cut = 'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
    assert not read_result(registry, v2, "responses", status=200, content_type="text/event-stream", body=cut).success
    failed = json.dumps({"id": "resp_1", "status": "failed", "output": []})
    result = read_result(registry, v2, "responses", status=200, content_type="application/json", body=failed)
    assert not result.success and result.error is not None and result.error.kind.value == "internal"


def test_health_that_is_never_ready_fails_health_alone(registry: Registry) -> None:
    """The report shows readiness apart from the calls, and one health fault isn't counted against cancellation."""
    settings = Settings(faults=frozenset({"health_starting"}))
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry, settings) as runner:
        report = conform(registry, "openai.chat.v1", runner)
    assert report.groups == {"registration": "pass", "health": "fail", "cancellation": "pass", "calls": "pass"}


def test_reasoning_alone_isnt_an_answer(registry: Registry) -> None:
    """A model that spends the whole budget thinking returns reasoning in the answer's own shape (a
    reasoning item's content text, reasoning_text deltas); the content checks must not count it."""
    profile = "openai.responses.stateless.v2"
    with ReferenceRunner([BEHAVIOURS[profile]], registry, Settings(thinks=600)) as runner:
        report = conform(registry, profile, runner, reasoning="on")
    failed = {c.id for c in report.checks if c.result == "fail"}
    assert failed == {"responses.json.content", "responses.sse.content"}, failed
