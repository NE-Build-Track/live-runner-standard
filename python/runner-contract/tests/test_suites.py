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

from livepeer_runner_contract import Registry
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


def conform(registry: Registry, profile: str, runner: ReferenceRunner) -> Report:
    manifest = parse_manifest(manifest_for([profile], runner.url, registry), capabilities=registry.capabilities or {})
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
    ("anthropic.messages.v1", "no_final", "messages.sse.answer"),
    ("anthropic.messages.v1", "stream_error", "messages.sse.answer"),
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
