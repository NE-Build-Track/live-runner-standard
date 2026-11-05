"""Work reports: read from the body, a line, an event, a header or the request, and added up by measure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from conftest import ROOT, write_release
from livepeer_runner_contract import Aggregate, Location, Registry, read_result

REPORTS: list[dict[str, Any]] = [
    {"framing": "json", "measure": "input_tokens", "in": "body", "pointer": "/usage/in"},
    {"framing": "json", "measure": "images", "in": "body", "pointer": "/data/*", "aggregate": "count"},
    {"framing": "json", "measure": "output_tokens", "in": "body", "pointer": "/parts/*/n", "aggregate": "sum"},
    {"measure": "characters", "in": "request", "pointer": "/text", "aggregate": "length"},
    {"measure": "media_seconds", "in": "header", "header": "X-Media-Seconds"},
    {
        "framing": "sse",
        "measure": "output_tokens",
        "in": "any-event",
        "pointer": "/usage/out",
        "request": {"pointer": "/stream_options/include_usage", "value": True},
    },
    {"framing": "sse", "measure": "input_tokens", "in": "last-event", "event": "done", "pointer": "/in"},
    {"framing": "ndjson", "measure": "audio_seconds", "in": "any-line", "pointer": "/seconds"},
    {"framing": "ndjson", "measure": "audio_seconds", "in": "last-line", "pointer": "/extra"},
]

ENTRY: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "zio.report.v1", "version": "1"},
    "paths": {
        "/v1/report": {
            "post": {
                "operationId": "report",
                "responses": {"200": {"description": "The answer"}},
                "x-livepeer": {
                    "framing": ["json", "sse", "ndjson"],
                    "success": {"status": [200]},
                    "errors": [{"status": 400, "kind": "input_invalid"}],
                    "work_reports": REPORTS,
                },
            }
        }
    },
    "x-livepeer": {
        "registry_entry": 1,
        "profile": "zio.report.v1",
        "kind": "profile",
        "mode": "single-shot",
        "capabilities": ["chat"],
        "admission": {"default": "candidate"},
        "attempts": "gateway",
    },
}


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> Registry:
    return Registry.load(write_release(tmp_path_factory.mktemp("reports"), {"zio/report.v1.json": ENTRY}, {}))


def read(registry: Registry, content_type: str, body: str, **extra: Any) -> dict[str, int | float]:
    result = read_result(registry, "zio.report.v1", "report", status=200, content_type=content_type, body=body, **extra)
    assert result.success
    return dict(result.work)


def test_the_entry_follows_the_schema() -> None:
    schema = json.loads((ROOT / "schemas/registry-entry.v1.schema.json").read_text())
    operation = Draft202012Validator({"$ref": "#/$defs/operation", "$defs": schema["$defs"]})
    assert not [e.message for e in operation.iter_errors(ENTRY["paths"]["/v1/report"]["post"]["x-livepeer"])]


def test_reports_are_parsed(registry: Registry) -> None:
    reports = registry.operation("zio.report.v1", "report").work_reports
    assert [r.location for r in reports][:5] == [
        Location.BODY,
        Location.BODY,
        Location.BODY,
        Location.REQUEST,
        Location.HEADER,
    ]
    assert reports[1].aggregate is Aggregate.COUNT and reports[0].aggregate is Aggregate.VALUE
    assert not reports[5].asked({}) and reports[5].asked({"stream_options": {"include_usage": True}})
    assert reports[0].asked({})


def test_a_json_body_a_header_and_the_request(registry: Registry) -> None:
    body = json.dumps({"usage": {"in": 12}, "data": [{"url": "a"}, {"url": "b"}], "parts": [{"n": 2}, {"n": 3}]})
    work = read(registry, "application/json", body, request={"text": "héllo"}, headers={"x-media-seconds": "2.5"})
    assert work == {"input_tokens": 12, "images": 2, "output_tokens": 5, "characters": 5, "media_seconds": 2.5}


def test_what_an_answer_doesnt_carry_isnt_reported(registry: Registry) -> None:
    work = read(registry, "application/json", json.dumps({"usage": {"in": "twelve"}, "data": []}))
    assert work == {"images": 0}  # a count of nothing is zero; a number that isn't one is no report


def test_any_event_takes_the_last_match_and_last_event_its_event(registry: Registry) -> None:
    body = (
        'data: {"usage": {"out": 1}}\n\n'
        'data: {"usage": {"out": 7}}\n\n'
        'data: {"other": true}\n\n'
        'event: done\ndata: {"in": 3}\n\n'
    )
    assert read(registry, "text/event-stream", body) == {"output_tokens": 7, "input_tokens": 3}
    assert read(registry, "text/event-stream", body.replace("event: done", "event: other")) == {"output_tokens": 7}


def test_reports_of_one_measure_add_up(registry: Registry) -> None:
    body = '{"seconds": 1.5}\n{"seconds": 2}\n{"extra": 0.5}\n'
    assert read(registry, "application/x-ndjson", body) == {"audio_seconds": 2.5}


def test_the_real_entries_report_work() -> None:
    real = Registry.load(ROOT)
    chat = json.dumps({"usage": {"prompt_tokens": 9, "completion_tokens": 4}})
    result = read_result(
        real, "openai.chat.v1", "chat_completions", status=200, content_type="application/json", body=chat
    )
    assert dict(result.work) == {"input_tokens": 9, "output_tokens": 4}
    sse = 'data: {"choices": []}\n\ndata: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}\n\ndata: [DONE]\n\n'
    result = read_result(
        real, "openai.chat.v1", "chat_completions", status=200, content_type="text/event-stream", body=sse
    )
    assert dict(result.work) == {"input_tokens": 9, "output_tokens": 4}


def test_a_malformed_report_refuses_its_entry(tmp_path: Path) -> None:
    entry = json.loads(json.dumps(ENTRY))
    entry["paths"]["/v1/report"]["post"]["x-livepeer"]["work_reports"] = [{"measure": "minutes", "in": "body"}]
    registry = Registry.load(write_release(tmp_path, {"zio/report.v1.json": entry}, {}), strict=False)
    assert "work report" in registry.refused["zio.report.v1"]
