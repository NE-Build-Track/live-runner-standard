"""JSON helpers: RFC 6901 pointers, JSON equality, strict parsing and safe text for logs."""

from __future__ import annotations

import json
import re
from typing import Any, Final

_INDEX = re.compile(r"^(0|[1-9][0-9]*)$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class _Absent:
    """The marker for a value that isn't there, distinct from JSON null."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT: Final = _Absent()


def escape(token: str) -> str:
    """One JSON pointer reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def lookup(value: Any, pointer: str) -> Any:
    """The value at ``pointer`` within ``value``, or ``ABSENT``. The empty pointer is the whole value."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        return ABSENT
    node = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return ABSENT
            node = node[token]
        elif isinstance(node, list):
            if not _INDEX.match(token) or int(token) >= len(node):
                return ABSENT
            node = node[int(token)]
        else:
            return ABSENT
    return node


def lookup_all(value: Any, pointer: str) -> list[Any]:
    """Every value ``pointer`` names, where a ``*`` token stands for every item of an array or every
    member of an object (SPEC.md). Without a ``*`` this is ``lookup``, as a list of none or one.
    """
    if pointer == "":
        return [value]
    if not pointer.startswith("/"):
        return []
    nodes = [value]
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        found: list[Any] = []
        for node in nodes:
            if isinstance(node, dict):
                if key == "*":
                    found.extend(node.values())
                elif key in node:
                    found.append(node[key])
            elif isinstance(node, list):
                if key == "*":
                    found.extend(node)
                elif _INDEX.match(key) and int(key) < len(node):
                    found.append(node[int(key)])
        nodes = found
        if not nodes:
            break
    return nodes


def json_equal(a: Any, b: Any) -> bool:
    """Equality as JSON means it: true is not 1, and 1 equals 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return False


class StrictJSONError(ValueError):
    """The text isn't one unambiguous JSON value."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise StrictJSONError(f"key {safe(key)} appears twice")
        out[key] = value
    return out


def _no_constants(name: str) -> Any:
    raise StrictJSONError(f"{name} isn't JSON")


def loads_strict(text: str | bytes) -> Any:
    """Parse JSON, refusing duplicate keys and NaN or Infinity, which readers would disagree on."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except RecursionError as exc:
        raise StrictJSONError("nested too deeply") from exc
    except StrictJSONError:
        raise
    except ValueError as exc:
        raise StrictJSONError(str(exc)) from exc


def safe(text: object, limit: int = 64) -> str:
    """A runner-supplied value made safe for a log line: plain tokens as they are, anything else quoted."""
    value = str(text)
    if _SAFE_TOKEN.match(value):
        return value
    quoted = json.dumps(value[:limit], ensure_ascii=True)
    return quoted if len(value) <= limit else quoted[:-1] + '…"'
