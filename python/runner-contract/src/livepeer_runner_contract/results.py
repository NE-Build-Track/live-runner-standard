"""Reading a runner's answer, as SPEC.md ("How a gateway reads an answer") says.

The entry's error mappings are checked in order, whatever the status, and the first that matches
decides the kind. If none matches and the success rule holds, the answer succeeded; otherwise it
failed, as ``internal``. So an error inside a stream, or in the body of a 200, is still an error.

A gateway that streams an answer through feeds it to a ``ResultReader`` chunk by chunk. The reader
keeps only what the rules look at: the last NDJSON line or SSE event (testing ``any-line`` and
``any-event`` mappings as each one passes), and a JSON or error body up to ``max_buffer`` bytes.
``read_result`` and ``classify`` do the same for an answer already in memory.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ._json import ABSENT, json_equal, lookup_all
from .registry import (
    Aggregate,
    ErrorKind,
    ErrorMapping,
    FinalCondition,
    Framing,
    Location,
    Operation,
    Registry,
    Retry,
    WorkReport,
)

__all__ = ["Classification", "Result", "ResultReader", "classify", "framing_of", "read_result", "split_stream"]

DEFAULT_MAX_BUFFER = 16 * 1024 * 1024
_NEWLINE = re.compile(r"\r\n|\r|\n")
_UNIT = {Framing.JSON: Location.BODY, Framing.NDJSON: Location.LAST_LINE, Framing.SSE: Location.LAST_EVENT}


@dataclass(frozen=True)
class Classification:
    """A runner error's base kind and the retry rule that comes with it."""

    kind: ErrorKind
    retry: Retry
    mapping: ErrorMapping | None = None
    """The entry's mapping that matched; None when none did, which makes the error ``internal``."""


@dataclass(frozen=True)
class Result:
    success: bool
    status: int
    framing: Framing | None
    """The answer's framing, from its content type."""
    value: Any = None
    """The decoded JSON body, last NDJSON line or last SSE event's data, where it is JSON; else None."""
    failure: str | None = None
    """Why the answer isn't success."""
    error: Classification | None = None
    """The error's base kind and retry rule, when the answer isn't success."""
    work: Mapping[str, int | float] = field(default_factory=lambda: MappingProxyType({}))
    """What the runner reported about the work, by measure, with reports of the same measure added up."""


def framing_of(content_type: str | None, declared: tuple[Framing, ...] = ()) -> Framing | None:
    """The framing an answer's content type says. With no content type, the endpoint's only framing, if it has one."""
    if not content_type:
        return declared[0] if len(declared) == 1 else None
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "text/event-stream":
        return Framing.SSE
    if media in ("application/x-ndjson", "application/ndjson"):
        return Framing.NDJSON
    if media == "application/json" or media.endswith("+json"):
        return Framing.JSON
    return Framing.BINARY


class _Lines:
    """Splits a byte stream into lines at CRLF, LF or CR, decoding UTF-8 with replacement."""

    def __init__(self, limit: int) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._limit = limit
        self.overflow = False

    def feed(self, data: bytes, final: bool = False) -> list[str]:
        text = self._partial + self._decoder.decode(data, final)
        held = ""
        if not final and text.endswith("\r"):
            text, held = text[:-1], "\r"  # its LF may come in the next chunk
        parts = _NEWLINE.split(text)
        self._partial = parts.pop() + held
        if len(self._partial) > self._limit:
            self._partial, self.overflow = "", True
        if final and self._partial:
            parts.append(self._partial)
            self._partial = ""
        return parts


class _Events:
    """The server-sent events parsing rules (WHATWG HTML, "Parsing an event stream")."""

    def __init__(self) -> None:
        self._type = ""
        self._data: list[str] = []
        self._first = True

    def line(self, line: str) -> tuple[str, str] | None:
        """Take one line; return the event it completes, as (type, data), if it completes one."""
        if self._first:
            line, self._first = line.removeprefix("﻿"), False
        if line == "":
            event = (self._type or "message", "\n".join(self._data)) if self._data else None
            self._type, self._data = "", []
            return event
        if line.startswith(":"):
            return None
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._type = value
        elif name == "data":
            self._data.append(value)
        return None


class _Unit:
    """One thing a condition tests: the body, one NDJSON line, or one SSE event."""

    __slots__ = ("_value", "event", "text")

    def __init__(self, text: str, event: str | None = None) -> None:
        self.text = text
        self.event = event
        self._value: Any = _UNDECODED

    @property
    def value(self) -> Any:
        """The text as JSON, or ABSENT; decoded once, when a condition first asks."""
        if self._value is _UNDECODED:
            try:
                self._value = json.loads(self.text)
            except ValueError:
                self._value = ABSENT
        return self._value


_UNDECODED = object()


def _as_string(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _test(condition: FinalCondition | ErrorMapping, unit: _Unit) -> bool:
    """Whether ``unit`` passes the condition's event, data and pointer tests."""
    if condition.event is not None and unit.event != condition.event:
        return False
    if condition.data is not None and unit.text != condition.data:
        return False
    if condition.pointer is None:
        return True
    value = unit.value
    values = [] if value is ABSENT else lookup_all(value, condition.pointer)
    if condition.present is False:
        return not values
    if not values:
        return False
    if condition.equals is not ABSENT and not any(json_equal(v, condition.equals) for v in values):
        return False
    pattern = condition.matches
    return pattern is None or any(pattern.search(_as_string(v)) for v in values)


def _measure(report: WorkReport, value: Any) -> int | float | None:
    """What a work report reads from one value, or None when the value doesn't carry it."""
    if value is ABSENT or report.pointer is None:
        return None
    values = lookup_all(value, report.pointer)
    if report.aggregate is Aggregate.COUNT:
        return len(values)
    if report.aggregate is Aggregate.LENGTH:
        strings = [v for v in values if isinstance(v, str)]
        return sum(len(v) for v in strings) if strings else None
    numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return sum(numbers) if numbers else None


def _header_number(headers: Mapping[str, str], name: str) -> int | float | None:
    text = headers.get(name.lower())
    if text is None:
        return None
    try:
        number = float(text.strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def split_stream(framing: Framing | None, body: bytes) -> list[tuple[str | None, str]]:
    """A whole NDJSON or SSE answer as its lines or events: (event type, text), the type None for a line."""
    if framing not in (Framing.SSE, Framing.NDJSON):
        return []
    lines = _Lines(len(body) + 1)
    out: list[tuple[str | None, str]] = []
    events = _Events() if framing is Framing.SSE else None
    for line in lines.feed(body, final=True):
        if events is not None:
            event = events.line(line)
            if event is not None:
                out.append(event)
        elif line.strip():
            out.append((None, line))
    return out  # as in SSE parsing, an event the stream doesn't finish with a blank line isn't one


def _describe(condition: FinalCondition) -> str:
    parts = [condition.location.value]
    if condition.event is not None:
        parts.append(f"event {json.dumps(condition.event)}")
    if condition.data is not None:
        parts.append(f"data {json.dumps(condition.data)}")
    if condition.pointer is not None:
        if condition.present is False:
            parts.append(f"{condition.pointer} absent")
        elif condition.equals is not ABSENT:
            parts.append(f"{condition.pointer} = {json.dumps(condition.equals)}")
        else:
            parts.append(f"{condition.pointer} present")
    return " ".join(parts)


class ResultReader:
    """Reads one answer as it streams, then applies the operation's error mappings and success rule.

    Feed the body with ``feed`` as it arrives and call ``finish`` once it ends. A JSON body, or any
    body of a status that isn't success, is kept up to ``max_buffer`` bytes; past that, body
    conditions can't be checked, so they don't hold.
    """

    def __init__(
        self,
        operation: Operation,
        *,
        status: int,
        content_type: str | None,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        request: Any = ABSENT,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.operation = operation
        self.status = status
        self.content_type = content_type
        self.framing = framing_of(content_type, operation.framings)
        self._request = request
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._max = max_buffer
        self._body = bytearray()
        self._overflow = False
        self._keep_body = self.framing in (Framing.JSON, None) or status not in operation.success.status
        self._lines = _Lines(max_buffer) if self.framing in (Framing.SSE, Framing.NDJSON) else None
        self._events = _Events() if self.framing is Framing.SSE else None
        self._last: _Unit | None = None
        stream = {Framing.SSE: Location.ANY_EVENT, Framing.NDJSON: Location.ANY_LINE}.get(self.framing or Framing.JSON)
        self._any = [m for m in operation.errors if stream is not None and m.location is stream]
        self._matched: set[int] = set()
        self._reports = [r for r in operation.work_reports if r.framing is None or r.framing is self.framing]
        self._reported: dict[int, int | float] = {}  # any-line and any-event reports: the last match
        self._result: Result | None = None
        self._classification: Classification | None = None

    def feed(self, chunk: bytes) -> None:
        if self._result is not None:
            raise RuntimeError("the answer is already finished")
        if self._lines is not None:
            self._take(self._lines.feed(bytes(chunk)))
        elif self._keep_body and not self._overflow:
            if len(self._body) + len(chunk) > self._max:
                self._overflow, self._body = True, bytearray()
            else:
                self._body += chunk

    def _take(self, lines: list[str]) -> None:
        for line in lines:
            if self._events is not None:
                event = self._events.line(line)
                if event is not None:
                    self._unit(_Unit(event[1], event[0]))
            elif line.strip():
                self._unit(_Unit(line))

    def _unit(self, unit: _Unit) -> None:
        self._last = unit
        for i, mapping in enumerate(self._any):
            if i not in self._matched and _test(mapping, unit):
                self._matched.add(i)
        stream = Location.ANY_EVENT if self.framing is Framing.SSE else Location.ANY_LINE
        for i, report in enumerate(self._reports):
            if report.location is stream and (report.event is None or report.event == unit.event):
                values = [] if unit.value is ABSENT else lookup_all(unit.value, report.pointer or "")
                number = _measure(report, unit.value) if values else None
                if number is not None:
                    self._reported[i] = number

    def _work(self, body: _Unit | None) -> dict[str, int | float]:
        """Every work report the answer carries, added up by measure."""
        work: dict[str, int | float] = {}
        for i, report in enumerate(self._reports):
            if report.location in (Location.ANY_LINE, Location.ANY_EVENT):
                number = self._reported.get(i)
            elif report.location is Location.HEADER:
                number = None if report.header is None else _header_number(self._headers, report.header)
            elif report.location is Location.REQUEST:
                number = _measure(report, self._request)
            else:
                unit = body if report.location is Location.BODY else self._at(report.location)
                if unit is not None and report.event is not None and unit.event != report.event:
                    unit = None
                number = None if unit is None else _measure(report, unit.value)
            if number is not None:
                work[report.measure] = work.get(report.measure, 0) + number
        return work

    def _at(self, location: Location | None) -> _Unit | None:
        if location is Location.BODY:
            if not self._keep_body or self._overflow or self._lines is not None:
                return None
            return _Unit(bytes(self._body).decode("utf-8", "replace"))
        if location is Location.LAST_LINE and self.framing is Framing.NDJSON:
            return self._last
        if location is Location.LAST_EVENT and self.framing is Framing.SSE:
            return self._last
        return None

    def _mapping_matches(self, mapping: ErrorMapping, body: _Unit | None) -> bool:
        if mapping.status is not None and mapping.status != self.status:
            return False
        if mapping.framing is not None and mapping.framing is not self.framing:
            return False
        if mapping.content_type is not None and not (self.content_type or "").lower().startswith(
            mapping.content_type.lower()
        ):
            return False
        if mapping.location is None:
            return mapping.event is None and mapping.pointer is None
        if mapping.location in (Location.ANY_LINE, Location.ANY_EVENT):
            return any(m is mapping for i, m in enumerate(self._any) if i in self._matched)
        unit = body if mapping.location is Location.BODY else self._at(mapping.location)
        return unit is not None and _test(mapping, unit)

    def finish(self) -> Result:
        """Apply the rules to the whole answer. Calling it again returns the same result."""
        if self._result is not None:
            return self._result
        if self._lines is not None:
            self._take(self._lines.feed(b"", final=True))
            self._overflow = self._overflow or self._lines.overflow
        op = self.operation
        body = self._at(Location.BODY)
        unit = body if self.framing is Framing.JSON else self._at(_UNIT.get(self.framing or Framing.BINARY))
        value = None if unit is None or unit.value is ABSENT else unit.value

        work = MappingProxyType(self._work(body))
        for number, mapping in enumerate(op.errors, 1):
            if self._mapping_matches(mapping, body):
                self._classification = Classification(mapping.kind, mapping.kind.retry, mapping)
                why = f"the answer matches error mapping #{number} of {op.name}: {mapping.kind.value}"
                self._result = Result(False, self.status, self.framing, value, why, self._classification, work)
                return self._result
        self._classification = Classification(ErrorKind.INTERNAL, ErrorKind.INTERNAL.retry)
        failure = self._success_failure(body)
        if failure is None:
            self._result = Result(True, self.status, self.framing, value, work=work)
        else:
            self._result = Result(False, self.status, self.framing, value, failure, self._classification, work)
        return self._result

    def _success_failure(self, body: _Unit | None) -> str | None:
        """Why the success rule doesn't hold, or None when it does."""
        op = self.operation
        if self.status not in op.success.status:
            return f"status {self.status} isn't a success status for {op.name}, and no error mapping matches"
        if self.framing is None:
            return f"the answer has no content type, and {op.name} answers with several framings"
        if self.framing not in op.framings:
            declared = ", ".join(f.value for f in op.framings)
            return f"the answer is {self.framing.value}; {op.name} answers with {declared}"
        for condition in op.success.final:
            if condition.framing is not None and condition.framing is not self.framing:
                continue
            unit = body if condition.location is Location.BODY else self._at(condition.location)
            if unit is None and self._overflow:
                return "the answer is too large to check against the success rule"
            if unit is None or not _test(condition, unit):
                return f"the answer doesn't end as the success rule says: {_describe(condition)}"
        return None

    def classify(self) -> Classification:
        """The answer's base kind: the first error mapping that matches, else ``internal``."""
        self.finish()
        assert self._classification is not None
        return self._classification


def read_result(
    registry: Registry,
    profile: str,
    endpoint: str,
    *,
    status: int,
    content_type: str | None,
    body: bytes | str,
    request: Any = ABSENT,
    headers: Mapping[str, str] | None = None,
) -> Result:
    """Read a whole answer from ``endpoint`` of ``profile``: success, or the error's kind and retry rule.

    Give the ``request`` that was sent and the answer's ``headers`` to read work reports found there.
    """
    reader = ResultReader(
        registry.operation(profile, endpoint),
        status=status,
        content_type=content_type,
        request=request,
        headers=headers,
    )
    reader.feed(body.encode("utf-8") if isinstance(body, str) else body)
    return reader.finish()


def classify(
    registry: Registry,
    profile: str,
    endpoint: str,
    *,
    status: int,
    content_type: str | None = None,
    body: bytes | str = b"",
) -> Classification:
    """Map a runner's error answer to its base kind: the first of the entry's mappings that matches, else ``internal``.

    ``read_result`` says whether an answer is an error at all; this only names the kind.
    """
    reader = ResultReader(registry.operation(profile, endpoint), status=status, content_type=content_type)
    reader.feed(body.encode("utf-8") if isinstance(body, str) else body)
    return reader.classify()
