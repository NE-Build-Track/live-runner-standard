"""Checking a request against its entry's schema, before the gateway pays for it (SPEC.md, gateway rule 8)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry as SchemaRegistry
from referencing import Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from ._json import ABSENT, escape, lookup, safe
from .registry import Operation, Registry

__all__ = [
    "RequestCheck",
    "SchemaCheck",
    "is_json_media_type",
    "media_type_matches",
    "response_schema",
    "schema_problems",
    "validate_request",
    "validate_response",
]

_ENTRY = "urn:livepeer-registry-entry"


@dataclass(frozen=True)
class RequestCheck:
    """The problems with a request; none means it may be sent."""

    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __bool__(self) -> bool:
        return self.ok


SchemaCheck = RequestCheck
"""The same result type, for checks of answers against an entry's response schemas."""


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_json_media_type(content_type: str) -> bool:
    media = _media_type(content_type)
    return media == "application/json" or media.endswith("+json")


def media_type_matches(declared: str, actual: str) -> bool:
    """Whether ``actual`` is one of ``declared``: an exact type, ``type/*`` or ``*/*``."""
    want, have = _media_type(declared), _media_type(actual)
    if want in ("*/*", have):
        return True
    return want.endswith("/*") and have.split("/", 1)[0] == want[:-2]


def _request_body(operation: Operation) -> tuple[dict[str, Any] | None, str]:
    """The operation's requestBody object, with its pointer in the entry document."""
    body = operation.spec.get("requestBody")
    pointer = f"{operation.pointer}/requestBody"
    if isinstance(body, dict) and isinstance(body.get("$ref"), str) and body["$ref"].startswith("#"):
        pointer = body["$ref"][1:]
        body = lookup(operation.document, pointer)
    return (body if isinstance(body, dict) else None), pointer


_MOST_VALUES = 12  # an enum's values a problem lists before it says how many more there are


def _what(error: ValidationError) -> str:
    """What the schema wants where ``error`` is, in the schema's words alone."""
    keyword, expected, instance = error.validator, cast(Any, error.validator_value), error.instance
    schema = error.schema if isinstance(error.schema, dict) else {}
    if keyword is None:  # a false schema
        return "isn't allowed"
    if "propertyNames" in error.relative_schema_path:
        return "has a member name that " + (f"doesn't match {expected}" if keyword == "pattern" else "isn't allowed")
    if keyword == "required" and isinstance(instance, dict):
        return "is missing " + ", ".join(k for k in expected if k not in instance)
    if keyword == "additionalProperties" and isinstance(instance, dict):
        extra = [safe(k, 40) for k in instance if k not in (schema.get("properties") or {})]
        return "has members the entry doesn't allow: " + ", ".join(extra[:5]) + (", …" if len(extra) > 5 else "")
    if keyword == "type":
        return f"must be of type {expected if isinstance(expected, str) else ' or '.join(expected)}"
    if keyword == "const":
        return f"must be {json.dumps(expected)}"
    if keyword == "enum":
        shown = ", ".join(json.dumps(v) for v in expected[:_MOST_VALUES])
        more = len(expected) - _MOST_VALUES
        return f"must be one of {shown}" + (f" or {more} more" if more > 0 else "")
    if keyword == "not":
        described = schema.get("description")
        return f"isn't allowed. {described}" if isinstance(described, str) else "isn't allowed"
    if keyword in ("anyOf", "oneOf"):
        if keyword == "oneOf" and not error.context:
            return "matches more than one of the shapes the entry describes"
        return "matches none of the shapes the entry describes"
    return _BOUNDS.get(str(keyword), "breaks the entry's {keyword} rule").format(expected=expected, keyword=keyword)


_BOUNDS = {
    "pattern": "doesn't match {expected}",
    "format": "isn't a valid {expected}",
    "minimum": "must be at least {expected}",
    "exclusiveMinimum": "must be more than {expected}",
    "maximum": "must be at most {expected}",
    "exclusiveMaximum": "must be less than {expected}",
    "multipleOf": "must be a multiple of {expected}",
    "minLength": "must be at least {expected} characters",
    "maxLength": "must be at most {expected} characters",
    "minItems": "must have at least {expected} items",
    "maxItems": "must have at most {expected} items",
    "uniqueItems": "must not repeat an item",
    "minProperties": "must have at least {expected} members",
    "maxProperties": "must have at most {expected} members",
}


def schema_problems(errors: Iterable[ValidationError], subject: str = "the body") -> tuple[str, ...]:
    """Schema errors as lines that say where and what the schema wants, never the value itself.

    A refused value can be a caller's whole conversation or a runner's whole answer, and these lines go
    to callers and into logs. Where a value matches none of a union's shapes, such as a block in a list
    keyed by ``type``, the line names the deepest cause, which is in the shape the value's type picks.
    """
    lines = []
    for error in sorted(errors, key=lambda e: [str(p) for p in e.absolute_path]):
        cause = best_match([error]) or error
        what = _what(cause)
        path = cause.absolute_path
        lines.append(f"at /{'/'.join(escape(str(p)) for p in path)}: {what}" if path else f"{subject} {what}")
    return tuple(dict.fromkeys(lines))


def _validator(registry: Registry, operation: Operation, pointer: str) -> Draft202012Validator:
    key = (operation.defined_in, operation.name, pointer)
    cached = registry._validators.get(key)
    if cached is None:
        resource: Resource[Any] = Resource.from_contents(operation.document, default_specification=DRAFT202012)
        schemas: SchemaRegistry[Any] = SchemaRegistry().with_resource(_ENTRY, resource)
        cached = Draft202012Validator({"$ref": f"{_ENTRY}#{pointer}"}, registry=schemas)
        registry._validators[key] = cached
    validator: Draft202012Validator = cached
    return validator


def validate_request(
    registry: Registry,
    profile: str,
    endpoint: str,
    body: Any,
    *,
    content_type: str = "application/json",
) -> RequestCheck:
    """Check ``body`` against the request schema of ``endpoint`` in ``profile``'s entry.

    For a JSON media type ``body`` is the decoded value, or ``bytes`` to decode; ``None`` means no
    body. For any other media type (multipart, binary) only the content type is checked against the
    ones the operation accepts. Raises ``UnknownProfile`` or ``UnknownEndpoint`` for a profile or
    endpoint the registry doesn't have.
    """
    operation = registry.operation(profile, endpoint)
    request_body, body_pointer = _request_body(operation)
    if request_body is None:
        return RequestCheck() if body is None else RequestCheck((f"{endpoint} takes no request body",))
    if body is None:
        return (
            RequestCheck((f"{endpoint} requires a request body",)) if request_body.get("required") else RequestCheck()
        )

    content = request_body.get("content") or {}
    declared = next((name for name in content if media_type_matches(name, content_type)), None)
    if declared is None:
        accepted = ", ".join(sorted(content)) or "nothing"
        return RequestCheck((f"{endpoint} doesn't accept {_media_type(content_type)}; it accepts {accepted}",))
    if not is_json_media_type(content_type):
        return RequestCheck()
    if isinstance(body, bytes | bytearray):
        try:
            body = json.loads(body)
        except ValueError as exc:
            return RequestCheck((f"the body isn't JSON: {exc}",))
    media = content[declared]
    if not isinstance(media, dict) or lookup(media, "/schema") is ABSENT:
        return RequestCheck()
    pointer = f"{body_pointer}/content/{escape(declared)}/schema"
    try:
        return RequestCheck(schema_problems(_validator(registry, operation, pointer).iter_errors(body)))
    except Unresolvable as exc:
        return RequestCheck((f"the entry's request schema can't be resolved: {exc}",))


def response_schema(operation: Operation, status: int, content_type: str | None) -> str | None:
    """The JSON pointer of the schema for an answer with this status and content type, or None.

    The response is the status's own, else its range's (``4XX``), else ``default``, as in OpenAPI.
    For ``sse`` and ``ndjson`` answers the schema describes one event's data or one line (SPEC.md).
    """
    responses = operation.spec.get("responses")
    if not isinstance(responses, dict):
        return None
    for code in (str(status), f"{status // 100}XX", "default"):
        if code in responses:
            break
    else:
        return None
    response = responses[code]
    pointer = f"{operation.pointer}/responses/{escape(code)}"
    if isinstance(response, dict) and isinstance(response.get("$ref"), str) and response["$ref"].startswith("#"):
        pointer = response["$ref"][1:]
        response = lookup(operation.document, pointer)
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, dict) or not content_type:
        return None
    media = next((name for name in content if media_type_matches(name, content_type)), None)
    if media is None or not isinstance(content[media], dict) or "schema" not in content[media]:
        return None
    return f"{pointer}/content/{escape(media)}/schema"


def validate_response(
    registry: Registry,
    profile: str,
    endpoint: str,
    value: Any,
    *,
    status: int,
    content_type: str | None,
) -> SchemaCheck | None:
    """Check an answer's decoded body (or one SSE event's data, or one NDJSON line) against its schema.

    None when the entry declares no schema for that status and content type.
    """
    operation = registry.operation(profile, endpoint)
    pointer = response_schema(operation, status, content_type)
    if pointer is None:
        return None
    try:
        return SchemaCheck(schema_problems(_validator(registry, operation, pointer).iter_errors(value)))
    except Unresolvable as exc:
        return SchemaCheck((f"the entry's response schema can't be resolved: {exc}",))
