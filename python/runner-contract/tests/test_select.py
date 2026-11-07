"""parse_discovery and select: matching by capability, model, profile, attributes, limits and admission."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import REVISION, envelope_text
from livepeer_runner_contract import (
    Admission,
    ApprovalKey,
    Envelope,
    IneligibleReason,
    Reasoning,
    Registry,
    Runner,
    RunnerKey,
    parse_discovery,
    parse_envelope,
    select,
)

ORCH = "https://orch.example.com:8935"


def record(runner_id: str, metadata: str | None, app: str = "chat", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "runner_id": runner_id,
        "observed_at": "2026-11-05T12:00:00Z",
        "url": f"{ORCH}/apps/{runner_id}/app",
        "app": app,
        "mode": "single-shot",
        "capacity": 4,
        "capacity_used": 1,
        "capacity_available": 3,
        "price_info": {"price": "277777778", "price_usd": "0.000277", "currency": "wei", "unit": "seconds"},
        **extra,
    }
    if metadata is not None:
        out["metadata"] = metadata
    return out


def runner(runner_id: str = "r1", app: str = "chat", orchestrator: str = ORCH, **changes: Any) -> Runner:
    envelope = parse_envelope(envelope_text(**changes))
    assert isinstance(envelope, Envelope), envelope
    return Runner(orchestrator=orchestrator, runner_id=runner_id, app=app, envelope=envelope)


def test_parse_discovery_reads_the_nodes_answer() -> None:
    answer = [
        {"nodes": [{"url": ORCH, "queried_at": "2026-11-05T12:00:00Z"}]},
        {
            "address": ORCH,
            "runners": [
                record("good", envelope_text()),
                record("old", '{"model": "qwen", "pricing": "gold"}'),
                record("big", envelope_text(attributes={"x": "y" * 2000})),
                record("bare", None),
                "junk",
            ],
        },
        {"address": "", "runners": [record("orphan", envelope_text())]},
        "junk",
    ]
    discovery = parse_discovery(json.dumps(answer))
    assert [r.runner_id for r in discovery.runners] == ["good"]
    good = discovery.runners[0]
    assert good.key == RunnerKey(ORCH, "good")
    assert (good.app, good.mode, good.capacity, good.capacity_available) == ("chat", "single-shot", 4, 3)
    assert good.price is not None and good.price.unit == "seconds" and good.price.price_usd == "0.000277"
    reasons = {i.key.runner_id: i.reason.reason for i in discovery.ignored}
    assert reasons == {
        "old": IneligibleReason.NOT_V1,
        "big": IneligibleReason.TOO_LARGE,
        "bare": IneligibleReason.NO_METADATA,
    }


def test_a_runner_the_node_reports_without_an_id_uses_the_envelopes() -> None:
    item = record("", envelope_text(runner_id="from-envelope"))
    del item["runner_id"]
    assert parse_discovery([{"address": ORCH, "runners": [item]}]).runners[0].runner_id == "from-envelope"


@pytest.mark.parametrize("answer", [None, 5, "", b"[", "{}", [None], [{"address": ORCH, "runners": "x"}]])
def test_parse_discovery_never_raises(answer: Any) -> None:
    assert parse_discovery(answer).runners == ()


def names(selections: list[Any]) -> list[tuple[str, str]]:
    return [(s.runner.runner_id, s.name) for s in selections]


def test_select_by_capability_model_and_profile(registry: Registry) -> None:
    runners = [
        runner("chat"),
        runner("other-model", model={"name": "llama", "source": "hf:meta/llama"}),
        runner("tts", app="text-to-speech", endpoints={"speech": {"profile": "openai.speech.v1"}}),
    ]
    assert names(select(runners, "chat", registry=registry)) == [
        ("chat", "chat_completions"),
        ("other-model", "chat_completions"),
    ]
    assert names(select(runners, "chat", model="qwen3.8-27b", registry=registry)) == [("chat", "chat_completions")]
    assert names(select(runners, "chat", model="hf:meta/llama", registry=registry)) == [
        ("other-model", "chat_completions")
    ]
    assert names(select(runners, "chat", profile="openai.chatcompletions.v1", registry=registry)) == names(
        select(runners, "chat", profile="openai.chat.v1", registry=registry)
    )
    assert select(runners, "chat", profile="openai.nothing.v1", registry=registry) == []
    assert names(select(runners, "text-to-speech", registry=registry)) == [("tts", "speech")]


def test_only_endpoints_the_registry_backs_are_selected(registry: Registry) -> None:
    r = runner(
        endpoints={
            "chat_completions": {"profile": "openai.chat.v1"},
            "list_models": {"profile": "openai.models.v1"},
            "speech": {"profile": "openai.speech.v1"},  # doesn't serve chat
            "responses": {"profile": "openai.chat.v1"},  # not an operation of the entry
            "thing": {"profile": "openai.unknown.v1"},  # not in the release
            "errors": {"profile": "livepeer.errors.v1"},  # a shared entry
        }
    )
    assert names(select([r], "chat", registry=registry)) == [("r1", "chat_completions"), ("r1", "list_models")]


def test_select_by_attributes_and_limits(registry: Registry) -> None:
    r = runner(attributes={"languages": ["en", "fr"], "preset": "fast", "hdr": True})
    assert select([r], "chat", attributes={"languages": "fr"}, registry=registry)
    assert select([r], "chat", attributes={"languages": ["en", "fr"], "preset": "fast"}, registry=registry)
    assert not select([r], "chat", attributes={"languages": ["en", "de"]}, registry=registry)
    assert not select([r], "chat", attributes={"region": "eu"}, registry=registry)
    assert not select([r], "chat", attributes={"hdr": 1}, registry=registry)  # true is not 1
    assert select([r], "chat", limits={"context_tokens": 100000}, registry=registry)
    assert not select([r], "chat", limits={"context_tokens": 200000}, registry=registry)
    assert not select([r], "chat", limits={"max_input_bytes": 1}, registry=registry)


def test_private_runners_need_an_approval(registry: Registry) -> None:
    private = runner("p", admission="private")
    unknown = runner("u", admission="trusted-by-zio")
    key = ApprovalKey(ORCH, "p", REVISION, "openai.chat.v1")
    assert select([private, unknown], "chat", registry=registry) == []
    chosen = select([private, unknown], "chat", approvals={key}, registry=registry)
    assert names(chosen) == [("p", "chat_completions")]
    assert chosen[0].admission is Admission.PRIVATE and chosen[0].approved and chosen[0].approval == key
    # An approval names one deployment: a new revision needs a new one.
    moved = runner("p", admission="private", deployment_revision="sha256:" + "ee" * 32)
    assert select([moved], "chat", approvals={key}, registry=registry) == []
    assert select([unknown], "chat", approvals=lambda k: k.runner_id == "u", registry=registry)


def test_a_private_profile_needs_an_approval_whatever_the_envelope_says(registry: Registry) -> None:
    r = runner(
        "t",
        app="video-transcode",
        model=None,
        admission="candidate",
        endpoints={"transcode": {"profile": "zio.stream.v1"}},
    )
    assert select([r], "video-transcode", registry=registry) == []
    key = ApprovalKey(ORCH, "t", REVISION, "zio.stream.v1")
    chosen = select([r], "video-transcode", approvals={key}, registry=registry)
    assert chosen and chosen[0].admission is Admission.PRIVATE


def test_approved_only_requires_approvals_for_candidates_too(registry: Registry) -> None:
    a, b = runner("a"), runner("b")
    approvals = {ApprovalKey(ORCH, "b", REVISION, "openai.chat.v1")}
    assert names(select([a, b], "chat", approvals=approvals, approved_only=True, registry=registry)) == [
        ("b", "chat_completions")
    ]
    plain = select([a, b], "chat", approvals=approvals, registry=registry)
    assert [s.approved for s in plain] == [False, True]


def test_selection_gives_the_path_and_method_to_call(registry: Registry) -> None:
    default, moved = (
        runner("d"),
        runner(
            "m",
            endpoints={"chat_completions": {"profile": "openai.chat.v1", "path": "/openai/v1/chat", "method": "PUT"}},
        ),
    )
    first, second = select([default, moved], "chat", registry=registry)
    assert (first.method, first.path) == ("POST", "/v1/chat/completions")
    assert (second.method, second.path) == ("PUT", "/openai/v1/chat")
    assert first.operation.name == "chat_completions" and first.profile == "openai.chat.v1"


def test_an_alias_in_the_envelope_is_resolved(registry: Registry) -> None:
    r = runner(endpoints={"chat_completions": {"profile": "openai.chatcompletions.v1"}})
    (chosen,) = select([r], "chat", profile="openai.chat.v1", registry=registry)
    assert chosen.profile == "openai.chat.v1" and chosen.approval.profile == "openai.chat.v1"


def test_select_by_declared_reasoning(registry: Registry) -> None:
    thinks, answers, says_nothing = (
        runner("on", reasoning={"default": "on"}),
        runner("off", reasoning={"default": "off"}),
        runner("none"),
    )
    runners = [thinks, answers, says_nothing]
    assert [s.runner.runner_id for s in select(runners, "chat", reasoning="off", registry=registry)] == ["off"]
    assert [s.runner.runner_id for s in select(runners, "chat", reasoning=Reasoning.ON, registry=registry)] == ["on"]
    assert len(select(runners, "chat", registry=registry)) == 3


def test_select_by_model_revision(registry: Registry) -> None:
    pinned = runner("pinned")  # the fixture's model revision is "a" * 40
    other = runner("other", model={"name": "qwen3.8-27b", "source": "hf:Qwen/Qwen3.8-27B-AWQ", "revision": "b" * 40})
    picked = select(
        [pinned, other], "chat", model="hf:Qwen/Qwen3.8-27B-AWQ", model_revision="a" * 40, registry=registry
    )
    assert [s.runner.runner_id for s in picked] == ["pinned"]
