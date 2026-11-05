"""validate_request: a body checked against the entry's request schema before paying."""

from __future__ import annotations

from pathlib import Path

import pytest

from livepeer_runner_contract import Registry, UnknownEndpoint, UnknownProfile, validate_request, validate_response

CHAT = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}]}


def test_a_valid_body_passes(registry: Registry) -> None:
    check = validate_request(registry, "openai.chat.v1", "chat_completions", CHAT)
    assert check.ok and bool(check) and check.problems == ()


def test_each_problem_is_one_line_with_where_it_is(registry: Registry) -> None:
    body = {"messages": [{"role": "robot", "content": "hi"}], "stream": "yes"}
    check = validate_request(registry, "openai.chat.v1", "chat_completions", body)
    assert not check.ok
    assert "'model' is a required property" in check.problems
    assert any(p.startswith("at /messages/0/role: 'robot' is not one of") for p in check.problems)
    assert any(p.startswith("at /stream:") for p in check.problems)


def test_refs_resolve_within_the_entry(registry: Registry) -> None:
    assert validate_request(registry, "zio.echo.v1", "echo", {"text": "hi", "times": 2}).ok
    check = validate_request(registry, "zio.echo.v1", "echo", {"text": "far too long here", "times": 0, "x": 1})
    assert len(check.problems) == 3


def test_bytes_are_decoded_as_json(registry: Registry) -> None:
    assert validate_request(registry, "zio.echo.v1", "echo", b'{"text": "hi"}').ok
    assert validate_request(registry, "zio.echo.v1", "echo", b"{nope").problems[0].startswith("the body isn't JSON")


def test_content_types(registry: Registry) -> None:
    assert validate_request(
        registry, "zio.echo.v1", "echo", CHAT, content_type="application/json; charset=utf-8"
    ).problems
    check = validate_request(registry, "zio.echo.v1", "echo", b"hi", content_type="text/plain")
    assert check.problems == ("echo doesn't accept text/plain; it accepts application/json",)
    # Media bodies: only the content type is checked.
    assert validate_request(registry, "openai.speech.v1", "upload", b"\xff\xfb", content_type="audio/mpeg").ok
    assert not validate_request(registry, "openai.speech.v1", "upload", b"\xff\xfb", content_type="video/mp4").ok


def test_bodies_where_none_is_expected_or_one_is_required(registry: Registry) -> None:
    assert validate_request(registry, "openai.models.v1", "list_models", None).ok
    assert validate_request(registry, "openai.models.v1", "list_models", {}).problems == (
        "list_models takes no request body",
    )
    assert validate_request(registry, "zio.echo.v1", "echo", None).problems == ("echo requires a request body",)


def test_aliases_and_extends(registry: Registry) -> None:
    assert validate_request(registry, "openai.chatcompletions.v1", "chat_completions", CHAT).ok
    assert validate_request(registry, "fal.flux.v1", "run", {"prompt": "a cat"}).ok
    assert not validate_request(registry, "fal.flux.v1", "run", {}).ok


def test_unknown_profiles_and_endpoints_raise(registry: Registry) -> None:
    with pytest.raises(UnknownProfile):
        validate_request(registry, "openai.nothing.v1", "x", {})
    with pytest.raises(UnknownEndpoint):
        validate_request(registry, "openai.chat.v1", "completions", {})


def test_answers_check_against_the_entrys_response_schemas() -> None:
    real = Registry.load(Path(__file__).resolve().parents[3])
    chat = {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    check = validate_response(
        real, "openai.chat.v1", "chat_completions", chat, status=200, content_type="application/json"
    )
    assert check is not None and check.ok
    bad = validate_response(real, "openai.chat.v1", "chat_completions", {}, status=200, content_type="application/json")
    assert bad is not None and "'id' is a required property" in bad.problems
    # One SSE event's data: [DONE] isn't JSON, and the event schema allows it.
    done = validate_response(
        real, "openai.chat.v1", "chat_completions", "[DONE]", status=200, content_type="text/event-stream"
    )
    assert done is not None and done.ok
    # An error status falls back to the default response.
    error = {"error": {"message": "no", "type": "BadRequestError"}}
    assert validate_response(
        real, "openai.chat.v1", "chat_completions", error, status=400, content_type="application/json"
    )
    assert (
        validate_response(real, "openai.chat.v1", "chat_completions", "x", status=200, content_type="text/html") is None
    )
