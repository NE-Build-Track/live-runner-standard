"""A small registry release built in a temporary directory, so tests don't depend on the real registry.

Each fixture entry is shaped like the real ones and validates against the repository's
registry-entry schema (test_fixtures_follow_the_schema checks).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from livepeer_runner_contract import Registry

ROOT = Path(__file__).resolve().parents[3]
DIGEST = "sha256:" + "ab" * 32
REVISION = "sha256:" + "cd" * 32


def _op(
    op_id: str,
    x: dict[str, Any],
    *,
    request: str | None = None,
    response: dict[str, Any] | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": op_id,
        "responses": {"200": {"description": "The answer", **({"content": response} if response else {})}},
        "x-livepeer": x,
    }
    if request is not None:
        op["requestBody"] = {
            "required": True,
            "content": {content_type: {"schema": {"$ref": f"#/components/schemas/{request}"}}},
        }
    return op


def _entry(
    profile: str,
    paths: dict[str, Any],
    *,
    schemas: dict[str, Any] | None = None,
    **x: Any,
) -> dict[str, Any]:
    root = {"registry_entry": 1, "profile": profile, "kind": "profile", **x}
    doc: dict[str, Any] = {"openapi": "3.1.0", "info": {"title": profile, "version": "1"}, "paths": paths}
    if schemas:
        doc["components"] = {"schemas": schemas}
    doc["x-livepeer"] = root
    return doc


ENTRIES: dict[str, dict[str, Any]] = {
    "zio/echo.v1.json": _entry(
        "zio.echo.v1",
        {
            "/v1/echo": {
                "post": _op(
                    "echo",
                    {
                        "framing": "json",
                        "success": {"status": [200], "final": [{"in": "body", "pointer": "/error", "present": False}]},
                        "errors": [
                            {"status": 400, "kind": "input_invalid"},
                            {"status": 503, "kind": "unavailable"},
                            {
                                "status": 200,
                                "in": "body",
                                "pointer": "/error/code",
                                "equals": "upstream",
                                "kind": "upstream_failed",
                            },
                            {"content_type": "text/html", "kind": "unavailable", "note": "a proxy's error page"},
                        ],
                    },
                    request="EchoRequest",
                    response={"application/json": {"schema": {"type": "object"}}},
                )
            }
        },
        schemas={
            "EchoRequest": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "maxLength": 10},
                    "times": {"$ref": "#/components/schemas/Times"},
                },
                "additionalProperties": False,
            },
            "Times": {"type": "integer", "minimum": 1},
        },
        mode="single-shot",
        capabilities=["chat"],
        admission={"default": "candidate"},
        attempts="gateway",
    ),
    "openai/chat.v1.json": _entry(
        "openai.chat.v1",
        {
            "/v1/chat/completions": {
                "post": _op(
                    "chat_completions",
                    {
                        "framing": ["json", "sse"],
                        "success": {
                            "status": [200],
                            "final": [{"framing": "sse", "in": "last-event", "data": "[DONE]"}],
                        },
                        "errors": [
                            {
                                "framing": "json",
                                "status": 400,
                                "in": "body",
                                "pointer": "/error/type",
                                "equals": "exceed_context_size_error",
                                "kind": "input_unsupported",
                            },
                            {"status": 400, "kind": "input_invalid"},
                            {"status": 429, "kind": "busy"},
                            {"framing": "sse", "in": "any-event", "pointer": "/error", "kind": "internal"},
                        ],
                    },
                    request="ChatRequest",
                )
            }
        },
        schemas={
            "ChatRequest": {
                "type": "object",
                "required": ["model", "messages"],
                "properties": {
                    "model": {"type": "string"},
                    "messages": {"type": "array", "minItems": 1, "items": {"$ref": "#/components/schemas/Message"}},
                    "stream": {"type": "boolean"},
                },
            },
            "Message": {
                "type": "object",
                "required": ["role", "content"],
                "properties": {"role": {"enum": ["system", "user", "assistant"]}, "content": {"type": "string"}},
            },
        },
        mode="single-shot",
        capabilities=["chat"],
        admission={"default": "candidate"},
        attempts="gateway",
    ),
    "openai/models.v1.json": _entry(
        "openai.models.v1",
        {
            "/v1/models": {
                "get": _op("list_models", {"framing": "json", "success": {"status": [200]}, "errors": []}),
            }
        },
        mode="single-shot",
        capabilities=["chat", "embeddings"],
        admission={"default": "candidate"},
        attempts="gateway",
    ),
    "openai/speech.v1.json": _entry(
        "openai.speech.v1",
        {
            "/v1/audio/speech": {
                "post": _op(
                    "speech",
                    {
                        "framing": "binary",
                        "success": {"status": [200]},
                        "errors": [
                            {
                                "status": 400,
                                "in": "body",
                                "pointer": "/error/code",
                                "matches": "^unsupported_",
                                "kind": "input_unsupported",
                            }
                        ],
                    },
                    request="SpeechRequest",
                    response={"audio/mpeg": {"schema": {"type": "string", "contentMediaType": "audio/mpeg"}}},
                ),
            },
            "/v1/audio/upload": {
                "post": _op(
                    "upload",
                    {"framing": "json", "success": {"status": [200]}, "errors": []},
                    request="SpeechRequest",
                    content_type="audio/*",
                )
            },
        },
        schemas={
            "SpeechRequest": {"type": "object", "required": ["input"], "properties": {"input": {"type": "string"}}}
        },
        mode="single-shot",
        capabilities=["text-to-speech"],
        admission={"default": "candidate"},
        attempts="gateway",
    ),
    "zio/stream.v1.json": _entry(
        "zio.stream.v1",
        {
            "/v1/transcode": {
                "post": _op(
                    "transcode",
                    {
                        "framing": "ndjson",
                        "success": {
                            "status": [200],
                            "final": [{"in": "last-line", "pointer": "/done", "equals": True}],
                        },
                        "errors": [
                            {"in": "any-line", "pointer": "/error/code", "matches": "^encoder_", "kind": "unavailable"},
                            {"in": "last-line", "pointer": "/error", "kind": "output_failed"},
                        ],
                    },
                )
            }
        },
        mode="single-shot",
        capabilities=["video-transcode"],
        admission={"default": "private", "reason": "it reads only the deployment's own storage"},
        attempts="caller",
    ),
    "zio/live.v1.json": _entry(
        "zio.live.v1",
        {
            "/v1/live/start": {
                "post": _op(
                    "session",
                    {
                        "framing": "json",
                        "success": {"status": [200]},
                        "errors": [{"status": 503, "kind": "busy"}],
                        "session": {
                            "channels": {"source": {"direction": "in", "media_type": "video/mp2t"}},
                            "idle_timeout_s": 30,
                            "stop": "stop",
                        },
                    },
                )
            },
            "/v1/live/stop": {
                "post": _op("stop", {"framing": "json", "success": {"status": [200]}, "errors": []}),
            },
        },
        mode="persistent",
        capabilities=["live-transcode"],
        admission={"default": "candidate"},
        attempts="gateway",
    ),
    "fal/queue.v1.json": _entry(
        "fal.queue.v1",
        {
            "/": {
                "post": _op(
                    "run",
                    {
                        "framing": "json",
                        "success": {"status": [200]},
                        "errors": [{"status": 504, "kind": "deadline_exceeded"}],
                    },
                )
            },
            "/requests/{request_id}": {
                "parameters": [{"name": "request_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "get": _op(
                    "result", {"framing": "json", "success": {"status": [200]}, "errors": [], "attempts": "gateway"}
                ),
            },
        },
        mode="single-shot",
        capabilities=["text-to-image", "image-edit"],
        admission={"default": "candidate"},
        attempts="caller",
        readiness="The adapter holds the operator's fal key",
    ),
    "fal/flux.v1.json": _entry(
        "fal.flux.v1",
        {
            "/": {
                "post": _op(
                    "run",
                    {
                        "framing": "json",
                        "success": {"status": [200]},
                        "errors": [{"status": 504, "kind": "deadline_exceeded"}],
                    },
                    request="FluxInput",
                )
            }
        },
        schemas={"FluxInput": {"type": "object", "required": ["prompt"], "properties": {"prompt": {"type": "string"}}}},
        extends="fal.queue.v1",
        mode="single-shot",
        capabilities=["text-to-image"],
        admission={"default": "candidate"},
        attempts="caller",
    ),
    "livepeer/errors.v1.json": {
        "openapi": "3.1.0",
        "info": {"title": "livepeer.errors.v1", "version": "1"},
        "components": {"schemas": {"ErrorKind": {"type": "string"}}},
        "x-livepeer": {"registry_entry": 1, "profile": "livepeer.errors.v1", "kind": "shared"},
    },
}
ALIASES = {"openai.chatcompletions.v1": "openai.chat.v1"}


def write_release(
    root: Path,
    entries: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    *,
    capabilities: bool = True,
) -> Path:
    """Write a release directory: registry/ with its index, and the repository's capability list."""
    entries = ENTRIES if entries is None else entries
    aliases = ALIASES if aliases is None else aliases
    index: dict[str, Any] = {}
    for rel, doc in entries.items():
        path = root / "registry" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        path.write_bytes(raw)
        entry_id = doc["x-livepeer"]["profile"]
        index[entry_id] = {
            "aliases": sorted(a for a, t in aliases.items() if t == entry_id),
            "path": f"registry/{rel}",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": "active",
            "steward": ["@NE-Build-Track/ne-spe"],
        }
    (root / "registry" / "index.json").write_text(json.dumps({"entries": index, "registry_index": 1}, indent=2))
    if capabilities:
        (root / "capabilities.json").write_bytes((ROOT / "capabilities.json").read_bytes())
    return root


@pytest.fixture(scope="session")
def release_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_release(tmp_path_factory.mktemp("release"))


@pytest.fixture(scope="session")
def registry(release_dir: Path) -> Registry:
    return Registry.load(release_dir)


@pytest.fixture
def entries() -> dict[str, dict[str, Any]]:
    """A copy of the fixture entries to change."""
    return copy.deepcopy(ENTRIES)


EnvelopeFactory = Callable[..., str]


def envelope_text(**changes: Any) -> str:
    """A valid envelope as compact JSON, with top-level keys changed (None removes one)."""
    doc: dict[str, Any] = {
        "runner_contract": 1,
        "deployment_revision": REVISION,
        "model": {"name": "qwen3.8-27b", "source": "hf:Qwen/Qwen3.8-27B-AWQ", "revision": "a" * 40},
        "endpoints": {"chat_completions": {"profile": "openai.chat.v1"}},
        "limits": {"context_tokens": 131072},
    }
    for key, value in changes.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
