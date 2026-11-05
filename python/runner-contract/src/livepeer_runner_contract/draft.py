"""``runner-contract draft``: a registry entry skeleton from a runner's own OpenAPI or pydantic models.

"Derive to draft, review to publish" (SPEC.md): the draft is for a steward to review, never for a
gateway to read. Every value the tool can't know is a string starting ``TODO``, so the draft fails
CI until the steward fills it. The guesses it does make (framing from content types, error kinds
from statuses) carry a TODO note to confirm.
"""

from __future__ import annotations

import copy
import importlib
import re
import sys
from collections.abc import Iterator, Mapping
from typing import Any

from .registry import HTTP_METHODS

__all__ = ["draft_from_models", "draft_from_openapi"]

# Usual meanings; each still gets a note for the steward to confirm. 401 and 403 are left out:
# access is the gateway's to classify, not a runner error.
GUESSED_KINDS: Mapping[int, str] = {
    400: "input_invalid",
    404: "input_invalid",
    413: "input_invalid",
    415: "input_unsupported",
    422: "input_invalid",
    429: "busy",
    500: "internal",
    502: "upstream_failed",
    503: "unavailable",
    504: "deadline_exceeded",
}
REVIEW_NOTE = (
    "TODO: a steward reviews every description and summary, since registry text becomes tool and form "
    "descriptions; remove any text written to steer a model."
)


def _snake(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()).strip("_")
    return out if re.match(r"^[a-z]", out) else f"op_{out}"


def _framing(content_type: str) -> str:
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "text/event-stream":
        return "sse"
    if media in ("application/x-ndjson", "application/ndjson"):
        return "ndjson"
    if media == "application/json" or media.endswith("+json"):
        return "json"
    return "binary"


def _root_x(profile: str | None) -> dict[str, Any]:
    return {
        "registry_entry": 1,
        "profile": profile or "TODO: <namespace>.<name>.v<major>",
        "kind": "profile",
        "mode": "TODO: single-shot or persistent",
        "capabilities": ["TODO: capabilities from capabilities.json"],
        "admission": {"default": "candidate"},
        "attempts": "TODO: caller or gateway",
    }


def _operation_x(responses: Mapping[str, Any]) -> dict[str, Any]:
    success, framings, errors = [], [], []
    for code, response in responses.items():
        if not str(code).isdigit():
            continue
        status = int(code)
        if 200 <= status < 300:
            success.append(status)
            content = response.get("content") if isinstance(response, dict) else None
            for content_type in content or {}:
                framing = _framing(content_type)
                if framing not in framings:
                    framings.append(framing)
        elif status >= 400:
            kind = GUESSED_KINDS.get(status)
            errors.append(
                {"status": status, "kind": kind, "note": "TODO: confirm this kind"}
                if kind
                else {"status": status, "kind": "TODO: a base kind", "note": "TODO: map this error"}
            )
    if not errors:
        errors = [{"kind": "TODO: a base kind", "note": "TODO: list every error the runner returns, with its status"}]
    x: dict[str, Any] = {
        "framing": (framings[0] if len(framings) == 1 else framings)
        if framings
        else "TODO: json, sse, ndjson or binary",
        "success": {"status": sorted(success) or [200]},
        "errors": errors,
        "work_reports": [],
        "inputs": [],
        "outputs": [],
    }
    if len(framings) > 1:
        x["success"]["final"] = [{"framing": f, "in": "TODO: body, last-line or last-event"} for f in framings]
    return x


def _operations(doc: Mapping[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for route, item in (doc.get("paths") or {}).items():
        if isinstance(item, dict):
            for method in HTTP_METHODS:
                if isinstance(item.get(method), dict):
                    yield route, method, item[method]


def draft_from_openapi(source: Mapping[str, Any], profile: str | None = None) -> dict[str, Any]:
    """A draft entry from an OpenAPI document (3.0 or 3.1), keeping its paths, schemas and descriptions."""
    doc = copy.deepcopy(dict(source))
    original = str(doc.get("openapi", ""))
    notes = [REVIEW_NOTE]
    if not original.startswith("3.1"):
        notes.append(
            f"TODO: converted from OpenAPI {original or '(no version)'}; check nullable, exclusiveMinimum, "
            "exclusiveMaximum and example, which 3.1 writes differently."
        )
    draft: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": profile or "TODO: the profile ID", "version": "1", "description": " ".join(notes)},
        "paths": {},
    }
    seen: set[str] = set()
    for route, method, op in _operations(doc):
        name = op.get("operationId")
        name = _snake(name) if isinstance(name, str) and name else _snake(f"{method}_{route}")
        while name in seen:
            name += "_2"
        seen.add(name)
        op = {k: v for k, v in op.items() if not k.startswith("x-") and k not in ("security", "callbacks")}
        op["operationId"] = name
        op["x-livepeer"] = _operation_x(op.get("responses") or {})
        draft["paths"].setdefault(route, {})[method] = op
    components = doc.get("components")
    if isinstance(components, dict):
        draft["components"] = {k: v for k, v in components.items() if k != "securitySchemes"}
    draft["x-livepeer"] = _root_x(profile)
    return draft


def draft_from_models(specs: list[str], profile: str | None = None) -> dict[str, Any]:
    """A draft entry from pydantic models, ``module:Model`` each: the first is the request, the second the response.

    Further models join the components. The operation's path and name are TODOs, since models don't say them.
    """
    try:
        json_schema = importlib.import_module("pydantic.json_schema")
    except ImportError as exc:
        raise RuntimeError("draft --pydantic needs pydantic installed alongside runner-contract") from exc
    if "" not in sys.path:
        sys.path.insert(0, "")  # import the builder's modules from the working directory
    models = []
    for spec in specs:
        module_name, _, attr = spec.partition(":")
        if not module_name or not attr:
            raise ValueError(f"{spec!r} isn't module:Model")
        models.append(getattr(importlib.import_module(module_name), attr))
    modes = ["validation"] + ["serialization"] * (len(models) - 1)
    refs, defs = json_schema.models_json_schema(
        list(zip(models, modes, strict=True)), ref_template="#/components/schemas/{model}"
    )
    request = refs[(models[0], "validation")]
    responses: dict[str, Any] = {"200": {"description": "TODO: describe the answer"}}
    if len(models) > 1:
        responses["200"]["content"] = {"application/json": {"schema": refs[(models[1], "serialization")]}}
    op = {
        "operationId": "TODO: the endpoint's name, ^[a-z][a-z0-9_]*$",
        "requestBody": {"required": True, "content": {"application/json": {"schema": request}}},
        "responses": responses,
    }
    op["x-livepeer"] = _operation_x(responses)
    return {
        "openapi": "3.1.0",
        "info": {"title": profile or "TODO: the profile ID", "version": "1", "description": REVIEW_NOTE},
        "paths": {"/TODO": {"post": op}},
        "components": {"schemas": defs.get("$defs", {})},
        "x-livepeer": _root_x(profile),
    }
