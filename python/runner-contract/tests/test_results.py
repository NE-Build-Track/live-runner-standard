"""read_result, classify and ResultReader: mappings first, then the success rule, for every framing."""

from __future__ import annotations

import json

import pytest

from livepeer_runner_contract import (
    ErrorKind,
    Framing,
    Registry,
    Result,
    ResultReader,
    Retry,
    classify,
    framing_of,
    read_result,
)
from livepeer_runner_contract._json import ABSENT, json_equal, lookup, lookup_all

SSE = "text/event-stream"
JSON = "application/json"
NDJSON = "application/x-ndjson"


def kind(result: Result) -> ErrorKind:
    assert result.error is not None, result
    return result.error.kind


def chat(registry: Registry, body: bytes | str, status: int = 200, content_type: str | None = SSE) -> Result:
    return read_result(
        registry, "openai.chat.v1", "chat_completions", status=status, content_type=content_type, body=body
    )


def test_json_success_and_errors(registry: Registry) -> None:
    ok = read_result(registry, "zio.echo.v1", "echo", status=200, content_type=JSON, body=b'{"text": "hi"}')
    assert ok.success and ok.framing is Framing.JSON and ok.value == {"text": "hi"} and ok.error is None

    bad = read_result(registry, "zio.echo.v1", "echo", status=400, content_type=JSON, body=b"{}")
    assert not bad.success and bad.error is not None
    assert (bad.error.kind, bad.error.retry) == (ErrorKind.INPUT_INVALID, Retry.NEVER)

    unmapped = read_result(registry, "zio.echo.v1", "echo", status=500, content_type=JSON, body=b"{}")
    assert unmapped.error is not None and unmapped.error.mapping is None
    assert (unmapped.error.kind, unmapped.error.retry) == (ErrorKind.INTERNAL, Retry.OWNER)
    assert unmapped.failure is not None and "500 isn't a success status" in unmapped.failure


def test_an_error_in_the_body_of_a_200_is_an_error(registry: Registry) -> None:
    mapped = read_result(
        registry, "zio.echo.v1", "echo", status=200, content_type=JSON, body=b'{"error": {"code": "upstream"}}'
    )
    assert not mapped.success and mapped.error is not None and kind(mapped) is ErrorKind.UPSTREAM_FAILED
    # No mapping matches, but the success rule says /error must be absent: internal.
    other = read_result(
        registry, "zio.echo.v1", "echo", status=200, content_type=JSON, body=b'{"error": {"code": "x"}}'
    )
    assert not other.success and other.error is not None and kind(other) is ErrorKind.INTERNAL
    assert other.failure is not None and "/error absent" in other.failure


def test_a_mapping_decides_whatever_the_status(registry: Registry) -> None:
    page = read_result(
        registry, "zio.echo.v1", "echo", status=200, content_type="text/html; charset=utf-8", body=b"<html>"
    )
    assert page.error is not None and kind(page) is ErrorKind.UNAVAILABLE


def test_sse_success_needs_the_final_event(registry: Registry) -> None:
    good = chat(registry, 'data: {"choices": []}\n\ndata: [DONE]\n\n')
    assert good.success and good.framing is Framing.SSE
    cut = chat(registry, 'data: {"choices": []}\n\n')
    assert not cut.success and kind(cut) is ErrorKind.INTERNAL
    assert 'last-event data "[DONE]"' in (cut.failure or "")


def test_the_final_condition_is_scoped_to_its_framing(registry: Registry) -> None:
    assert chat(registry, '{"choices": []}', content_type=JSON).success


def test_an_error_event_anywhere_in_a_stream_is_an_error(registry: Registry) -> None:
    body = 'data: {"choices": []}\n\ndata: {"error": {"message": "oom"}}\n\ndata: [DONE]\n\n'
    result = chat(registry, body)
    assert not result.success and kind(result) is ErrorKind.INTERNAL
    assert result.error is not None and result.error.mapping is not None


def test_json_errors_of_a_streaming_endpoint(registry: Registry) -> None:
    context = chat(registry, '{"error": {"type": "exceed_context_size_error"}}', status=400, content_type=JSON)
    assert kind(context) is ErrorKind.INPUT_UNSUPPORTED
    other = chat(registry, '{"error": {"type": "invalid_request_error"}}', status=400, content_type=JSON)
    assert kind(other) is ErrorKind.INPUT_INVALID
    busy = classify(registry, "openai.chat.v1", "chat_completions", status=429, content_type=JSON, body="{}")
    assert (busy.kind, busy.retry) == (ErrorKind.BUSY, Retry.ELSEWHERE)


@pytest.mark.parametrize(
    "body",
    [
        b'data: {"a": 1}\r\n\r\ndata: [DONE]\r\n\r\n',
        b'data: {"a": 1}\r\rdata: [DONE]\r\r',
        b'\xef\xbb\xbf: a comment\nevent: message\ndata: {"a": 1}\n\nid: 7\nretry: 10\ndata:[DONE]\n\n',
    ],
)
def test_sse_line_endings_comments_and_fields(registry: Registry, body: bytes) -> None:
    assert chat(registry, body).success


def test_an_unfinished_last_event_is_not_dispatched(registry: Registry) -> None:
    assert not chat(registry, 'data: {"a": 1}\n\ndata: [DONE]').success


def test_multi_line_data_joins_with_newlines(registry: Registry) -> None:
    reader = ResultReader(registry.operation("openai.chat.v1", "chat_completions"), status=200, content_type=SSE)
    reader.feed(b'data: {"a":\ndata: 1}\n\n')
    assert reader.finish().value == {"a": 1}


@pytest.mark.parametrize("size", [1, 2, 3, 7])
def test_chunking_doesnt_change_the_result(registry: Registry, size: int) -> None:
    body = 'data: {"choices": []}\r\n\r\ndata: {"error": {"m": "é"}}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
    whole = chat(registry, body)
    reader = ResultReader(registry.operation("openai.chat.v1", "chat_completions"), status=200, content_type=SSE)
    for i in range(0, len(body), size):
        reader.feed(body[i : i + size])
    assert reader.finish() == whole


def test_ndjson_last_line_and_any_line(registry: Registry) -> None:
    def run(body: str) -> Result:
        return read_result(registry, "zio.stream.v1", "transcode", status=200, content_type=NDJSON, body=body)

    assert run('{"progress": 0.5}\n{"done": true}\n').success
    assert run('{"progress": 0.5}\n{"done": true}').success  # no final newline
    assert run('{"done": true}\n\n\n').success
    not_done = run('{"progress": 0.5}\n')
    assert not not_done.success and kind(not_done) is ErrorKind.INTERNAL
    encoder = run('{"error": {"code": "encoder_unavailable"}}\n{"done": true}\n')
    assert kind(encoder) is ErrorKind.UNAVAILABLE
    last = run('{"progress": 0.5}\n{"error": "disk full"}\n')
    assert kind(last) is ErrorKind.OUTPUT_FAILED


def test_binary_answers(registry: Registry) -> None:
    def speech(status: int, content_type: str, body: bytes) -> Result:
        return read_result(registry, "openai.speech.v1", "speech", status=status, content_type=content_type, body=body)

    ok = speech(200, "audio/mpeg", b"\xff\xfb\x90")
    assert ok.success and ok.framing is Framing.BINARY and ok.value is None
    voice = speech(400, JSON, b'{"error": {"code": "unsupported_voice"}}')
    assert kind(voice) is ErrorKind.INPUT_UNSUPPORTED
    plain = speech(400, JSON, b'{"error": {"code": "bad_input"}}')
    assert kind(plain) is ErrorKind.INTERNAL
    wrong = speech(200, JSON, b"{}")
    assert not wrong.success and "the answer is json; speech answers with binary" in (wrong.failure or "")


def test_no_content_type_means_the_only_framing(registry: Registry) -> None:
    assert read_result(registry, "zio.echo.v1", "echo", status=200, content_type=None, body="{}").success
    several = chat(registry, "{}", content_type=None)
    assert not several.success and "several framings" in (several.failure or "")


def test_a_body_past_the_buffer_cant_be_checked(registry: Registry) -> None:
    reader = ResultReader(registry.operation("zio.echo.v1", "echo"), status=200, content_type=JSON, max_buffer=8)
    reader.feed(b'{"text": "longer than eight bytes"}')
    result = reader.finish()
    assert not result.success and "too large" in (result.failure or "")
    with pytest.raises(RuntimeError):
        reader.feed(b"more")
    assert reader.finish() is result


@pytest.mark.parametrize(
    ("content_type", "framing"),
    [
        ("text/event-stream; charset=utf-8", Framing.SSE),
        ("application/x-ndjson", Framing.NDJSON),
        ("Application/JSON", Framing.JSON),
        ("application/problem+json", Framing.JSON),
        ("audio/wav", Framing.BINARY),
    ],
)
def test_framing_of(content_type: str, framing: Framing) -> None:
    assert framing_of(content_type) is framing


def test_pointers_with_star_name_every_item() -> None:
    doc = {"data": [{"url": "a"}, {"b64": "x"}, {"url": "c"}], "a/b": {"~": 1}, "*": 2}
    assert lookup_all(doc, "/data/*/url") == ["a", "c"]
    assert lookup_all(doc, "/data/1/b64") == ["x"]
    assert lookup_all(doc, "/data/*/none") == []
    assert lookup_all(doc, "/*") == [doc["data"], doc["a/b"], 2]  # on an object, every member
    assert lookup_all({"answers": {"a": {"p": 1}, "b": {"p": 2}}}, "/answers/*/p") == [1, 2]
    assert lookup_all(doc, "/a~1b/~0") == [1]
    assert lookup_all(doc, "") == [doc]
    assert lookup(doc, "/data/01") is ABSENT and lookup(doc, "/data/2/url") == "c"


def test_json_equality_is_jsons() -> None:
    assert json_equal(1, 1.0) and json_equal({"a": [1, None]}, {"a": [1.0, None]})
    assert not json_equal(True, 1) and not json_equal(0, False) and not json_equal(None, 0)
    assert not json_equal([1], [1, 2]) and not json_equal({"a": 1}, {"b": 1})


def test_value_is_the_last_event_where_it_is_json(registry: Registry) -> None:
    reader = ResultReader(registry.operation("openai.chat.v1", "chat_completions"), status=200, content_type=SSE)
    reader.feed(json.dumps({"x": 1}).join(["data: ", "\n\n"]).encode())
    assert reader.finish().value == {"x": 1}
