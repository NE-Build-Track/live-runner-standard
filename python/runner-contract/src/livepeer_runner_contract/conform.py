"""``runner-contract conform``: one profile's conformance suite, run against a running runner.

SPEC.md ("Conformance, interim"): a builder runs it on the runner's own host, which needs no payment
and no node, and attaches the report it writes to an approval request. A suite is data, in a
registry release at ``conformance/<namespace>/<name>.v<major>/suite.json``: golden requests per
endpoint and what each answer must be. ``conformance/common/suite.json`` holds the thresholds of the
checks every suite runs (the envelope, health, streamed headers within 1 s, keepalive, cancellation).

Every check is black-box: the tool sees only what the runner answers.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Registry as SchemaRegistry
from referencing import Resource
from referencing.jsonschema import DRAFT202012

from ._json import json_equal, lookup_all
from .envelope import Endpoint, Envelope, Ineligible, _parse
from .manifest import Manifest, revision_of
from .registry import EntryKind, ErrorKind, Framing, Location, Operation, Registry
from .requests import schema_problems, validate_request, validate_response
from .results import Result, ResultReader, split_stream
from .standard import MAX_METADATA_BYTES, PROFILE_ID, conformance_report_validator

__all__ = [
    "GROUPS",
    "Case",
    "Check",
    "Common",
    "Content",
    "Options",
    "Report",
    "Suite",
    "SuiteError",
    "Target",
    "available_suites",
    "group_of",
    "load_common",
    "load_suite",
    "profiles_to_run",
    "run_suite",
]

SUITE_FILE = "suite.json"
TOOL = "runner-contract"
STREAMS = (Framing.SSE, Framing.NDJSON)
_CASE_KEYS = (
    "id",
    "endpoint",
    "request",
    "valid",
    "expect",
    "content",
    "work_reports",
    "limit",
    "when",
    "fetch",
    "note",
)
FIXTURE = "$fixture."
"""A request string ``"$fixture.<name>"`` becomes the value the builder supplies under that name."""
_FIXTURE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ENVELOPE_LIMITS = ("context_tokens", "max_input_bytes")
_CONTENT_LOCATIONS = (Location.BODY, Location.LAST_LINE, Location.ANY_LINE, Location.LAST_EVENT, Location.ANY_EVENT)
_DETAIL_MAX = 2000


class SuiteError(ValueError):
    """A suite (or the common suite) that can't run. ``problems`` holds one line per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class Limit:
    """What a limit case exceeds: an envelope limit by name, or the entry's ``max_bytes`` for an input field."""

    name: str | None = None
    input: str | None = None


@dataclass(frozen=True)
class Content:
    """Where a success case's answer must carry non-empty content.

    ``pointer`` names candidates in a unit (the body, a line or an event); ``where`` keeps those whose
    relative pointers name the given values, such as ``{"/type": "output_text"}``; and ``value`` is the
    relative pointer to the content within each, the candidate itself when empty. ``where`` is how a
    suite tells an answer's text from reasoning that arrives in the same shape.
    """

    location: Location
    pointer: str
    where: tuple[tuple[str, Any], ...] = ()
    value: str = ""

    def describe(self) -> str:
        filters = "".join(f" where {k} = {json.dumps(v)}" for k, v in self.where)
        at = f" at {self.value}" if self.value else ""
        return f"{self.location.value} {self.pointer or '(the whole unit)'}{filters}{at}"

    def found(self, unit: Any) -> bool:
        """Whether the unit carries non-empty content here."""
        for candidate in lookup_all(unit, self.pointer):
            kept = all(any(json_equal(v, want) for v in lookup_all(candidate, at)) for at, want in self.where)
            if kept and any(_nonempty(v) for v in lookup_all(candidate, self.value)):
                return True
        return False


@dataclass(frozen=True)
class Case:
    """One golden request and what its answer must be.

    ``framing`` set: the answer must succeed with that framing. ``kinds`` set: it must be an error of
    one of those base kinds. ``valid`` false: the request must break the entry's request schema.
    """

    id: str
    endpoint: str
    request: Any
    framing: Framing | None = None
    kinds: tuple[ErrorKind, ...] = ()
    valid: bool = True
    content: tuple[Content, ...] = ()
    work_reports: bool = False
    limit: Limit | None = None
    when: tuple[tuple[str, Any], ...] = ()
    """Envelope pointers and values: the case runs only for a runner whose envelope declares each one."""
    fixtures: tuple[str, ...] = ()
    """The fixture names the request and ``fetch`` use, which the builder supplies."""
    fetch: tuple[str, ...] = ()
    """Fixture names of URLs that must answer 2xx with a body once the answer succeeds, such as outputs."""


@dataclass(frozen=True)
class Suite:
    profile: str
    version: str
    cases: tuple[Case, ...]
    cancel: str | None
    """The case to start, then abandon, for the cancellation check."""
    path: Path


@dataclass(frozen=True)
class Common:
    version: str
    headers_within_s: float
    keepalive_s: float
    cancel_within_s: float
    cancel_settle_s: float
    health_schema: tuple[str, str] | None
    """The registry entry and component that a health answer must match."""


@dataclass(frozen=True)
class Target:
    """The runner under test, as its manifest, block or envelope describes it."""

    runner_id: str
    url: str
    metadata: str
    health_url: str | None = None
    model: str | None = None
    manifest: Manifest | None = None
    metadata_from: str = "manifest"
    """Where ``metadata`` came from: ``manifest``, ``block`` or ``envelope``."""
    registration_problems: tuple[str, ...] | None = None
    """``runner-contract check``'s problems with a given block; None when no block was given."""


@dataclass(frozen=True)
class Options:
    timeout: float = 120.0
    """Seconds to wait on each request's socket."""
    health_timeout: float = 10.0
    cancel_settle_s: float | None = None
    """Seconds to wait after abandoning a request before probing; None takes the common suite's."""
    fixtures: Mapping[str, Any] = field(default_factory=dict)
    """Values the builder minted for this run, by name: input URLs, digests, storage grants. Never reported."""


@dataclass(frozen=True)
class Check:
    id: str
    result: str
    """``pass``, ``fail`` or ``skip``."""
    detail: str = ""


@dataclass(frozen=True)
class Report:
    document: dict[str, Any]
    """The report, valid against ``conformance-report.v1.schema.json``."""

    @property
    def passed(self) -> bool:
        return bool(self.document["pass"])

    @property
    def checks(self) -> list[Check]:
        return [Check(c["id"], c["result"], c.get("detail", "")) for c in self.document["checks"]]

    @property
    def groups(self) -> dict[str, str]:
        """Each group's result, ``pass``, ``fail`` or ``skip``, by the check IDs in it (``group_of``)."""
        results: dict[str, set[str]] = {}
        for check in self.checks:
            results.setdefault(group_of(check.id), set()).add(check.result)
        return {
            group: "fail" if "fail" in got else "pass" if "pass" in got else "skip"
            for group in GROUPS
            if (got := results.get(group))
        }


GROUPS = ("registration", "health", "cancellation", "calls")
"""What a report's checks are about: the registration, readiness, cancellation, and the suite's calls."""


def group_of(check_id: str) -> str:
    """The group a check ID belongs to: ``common.health.*`` is health, ``common.cancel`` cancellation, the other
    ``common.*`` checks the registration, and every case's checks the calls."""
    if check_id.startswith("common.health."):
        return "health"
    if check_id == "common.cancel":
        return "cancellation"
    if check_id.startswith("common."):
        return "registration"
    return "calls"


def _suite_dir(root: Path, profile: str) -> Path:
    namespace, _, name = profile.partition(".")
    return root / "conformance" / namespace / name


def available_suites(root: Path) -> list[str]:
    """The profile IDs that have a suite under ``root/conformance``."""
    base = root / "conformance"
    found = []
    for path in sorted(base.glob(f"*/*/{SUITE_FILE}")) if base.is_dir() else []:
        namespace, name = path.parent.parent.name, path.parent.name
        if namespace != "common" and PROFILE_ID.match(f"{namespace}.{name}"):
            found.append(f"{namespace}.{name}")
    return found


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SuiteError([f"{path}: doesn't exist"]) from exc
    except (OSError, ValueError) as exc:
        raise SuiteError([f"{path}: can't be read as JSON: {exc}"]) from exc


def _number(doc: Mapping[str, Any], key: str, where: Path, problems: list[str]) -> float:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        problems.append(f"{where}: {key} must be a positive number")
        return 1.0
    return float(value)


def load_common(registry: Registry) -> Common:
    """The common suite of the registry release: ``conformance/common/suite.json``."""
    if registry.root is None:
        raise SuiteError(["the registry has no release directory, so it has no conformance suites"])
    path = registry.root / "conformance" / "common" / SUITE_FILE
    doc = _read(path)
    problems: list[str] = []
    if not isinstance(doc, dict) or doc.get("conformance_common") != 1 or not isinstance(doc.get("version"), str):
        raise SuiteError([f'{path}: must be {{"conformance_common": 1, "version": "…", …}}'])
    health = doc.get("health")
    schema = None
    if isinstance(health, dict) and isinstance(health.get("entry"), str) and isinstance(health.get("schema"), str):
        schema = (health["entry"], health["schema"])
    common = Common(
        version=doc["version"],
        headers_within_s=_number(doc, "headers_within_s", path, problems),
        keepalive_s=_number(doc, "keepalive_s", path, problems),
        cancel_within_s=_number(doc, "cancel_within_s", path, problems),
        cancel_settle_s=_number(doc, "cancel_settle_s", path, problems),
        health_schema=schema,
    )
    if problems:
        raise SuiteError(problems)
    return common


def _fixtures_in(value: Any) -> list[str]:
    """The fixture names a request uses, in order, once each."""
    if isinstance(value, str):
        return [value[len(FIXTURE) :]] if value.startswith(FIXTURE) else []
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return list(dict.fromkeys(name for item in items for name in _fixtures_in(item)))


def _contains(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return value == marker
    if isinstance(value, dict):
        return any(_contains(v, marker) for v in value.values())
    if isinstance(value, list):
        return any(_contains(v, marker) for v in value)
    return False


def _case(item: Any, where: str, registry: Registry, profile: str, problems: list[str]) -> Case | None:
    if not isinstance(item, dict):
        problems.append(f"{where}: must be an object")
        return None
    for key in item:
        if key not in _CASE_KEYS:
            problems.append(f"{where}: {key} isn't a case key (expected one of {', '.join(_CASE_KEYS)})")
    case_id, endpoint = item.get("id"), item.get("endpoint")
    if not isinstance(case_id, str) or not case_id or not all(c.isalnum() or c in "._-" for c in case_id):
        problems.append(f"{where}: id must be letters, digits, '.', '_' or '-'")
        return None
    where = f"{where} ({case_id})"
    operation = registry.entry(profile).operations.get(endpoint) if isinstance(endpoint, str) else None
    if operation is None:
        problems.append(f"{where}: endpoint {endpoint!r} isn't an operation of {profile}")
        return None
    if "request" not in item:
        problems.append(f"{where}: needs a request")
    expect = item.get("expect")
    framing: Framing | None = None
    kinds: tuple[ErrorKind, ...] = ()
    if not isinstance(expect, dict) or len(expect) != 1 or not ({"framing", "kinds"} & set(expect)):
        problems.append(f'{where}: expect must be {{"framing": …}} for a success or {{"kinds": […]}} for an error')
    elif "framing" in expect:
        if expect["framing"] not in {f.value for f in operation.framings}:
            problems.append(f"{where}: {endpoint} doesn't answer with framing {expect['framing']!r}")
        else:
            framing = Framing(expect["framing"])
    else:
        values = expect["kinds"]
        if not isinstance(values, list) or not values or not all(v in set(ErrorKind) for v in values):
            problems.append(f"{where}: kinds must be a list of base kinds")
        else:
            kinds = tuple(ErrorKind(v) for v in values)
    content: list[Content] = []
    for c in item.get("content", []):
        pointers_ok = isinstance(c, dict) and isinstance(c.get("pointer"), str)
        where_ok = isinstance(c, dict) and isinstance(c.get("where", {}), dict)
        if (
            not pointers_ok
            or not where_ok
            or c.get("in") not in {loc.value for loc in _CONTENT_LOCATIONS}
            or not isinstance(c.get("value", ""), str)
            or set(c) - {"in", "pointer", "where", "value"}
        ):
            problems.append(
                f"{where}: each content item needs `in` (body, last-line, any-line…) and a pointer, and may add "
                "`where` (relative pointers to values) and `value` (a relative pointer)"
            )
        else:
            content.append(
                Content(Location(c["in"]), c["pointer"], tuple(c.get("where", {}).items()), c.get("value", ""))
            )
    limit = None
    if "limit" in item:
        raw = item["limit"]
        if isinstance(raw, dict) and raw.get("name") in _ENVELOPE_LIMITS and set(raw) == {"name"}:
            limit = Limit(name=raw["name"])
        elif isinstance(raw, dict) and isinstance(raw.get("input"), str) and set(raw) == {"input"}:
            inputs = [
                i for i in operation.x.get("inputs", []) if isinstance(i, dict) and i.get("field") == raw["input"]
            ]
            if not inputs or not isinstance(inputs[0].get("max_bytes"), int):
                problems.append(f"{where}: {endpoint} has no input at {raw['input']} with a max_bytes")
            limit = Limit(input=raw["input"])
        else:
            problems.append(
                f'{where}: limit must be {{"name": "context_tokens" | "max_input_bytes"}} or {{"input": …}}'
            )
        if not _contains(item.get("request"), "$fill"):
            problems.append(f'{where}: a limit case\'s request needs a "$fill" string to grow')
    for key in ("valid", "work_reports"):
        if key in item and not isinstance(item[key], bool):
            problems.append(f"{where}: {key} must be true or false")
    fetch = item.get("fetch", [])
    if not isinstance(fetch, list) or not all(isinstance(f, str) and f.startswith(FIXTURE) for f in fetch):
        problems.append(f'{where}: fetch must list fixtures, such as ["$fixture.descriptor_url"]')
        fetch = []
    names = _fixtures_in(item.get("request")) + [f[len(FIXTURE) :] for f in fetch]
    for name in names:
        if not _FIXTURE_NAME.match(name):
            problems.append(f"{where}: fixture {name!r} must be lowercase letters, digits and underscores")
    when = item.get("when", {})
    if not isinstance(when, dict) or not all(isinstance(k, str) and k.startswith("/") for k in when):
        problems.append(f'{where}: when must map envelope pointers to values, such as {{"/reasoning/default": "off"}}')
        when = {}
    assert isinstance(endpoint, str)  # an operation was found for it
    return Case(
        id=case_id,
        endpoint=endpoint,
        request=item.get("request"),
        framing=framing,
        kinds=kinds,
        valid=item.get("valid", True) is not False,
        content=tuple(content),
        work_reports=item.get("work_reports", False) is True,
        limit=limit,
        when=tuple(when.items()),
        fixtures=tuple(dict.fromkeys(names)),
        fetch=tuple(f[len(FIXTURE) :] for f in fetch),
    )


def load_suite(registry: Registry, profile: str) -> Suite:
    """The suite for ``profile`` in the registry release, checked against its entry."""
    entry = registry.entry(profile)
    if registry.root is None:
        raise SuiteError(["the registry has no release directory, so it has no conformance suites"])
    path = _suite_dir(registry.root, entry.id) / SUITE_FILE
    doc = _read(path)
    problems: list[str] = []
    if not isinstance(doc, dict) or doc.get("conformance_suite") != 1:
        raise SuiteError([f'{path}: must be {{"conformance_suite": 1, …}}'])
    if doc.get("profile") != entry.id:
        problems.append(f"{path}: profile must be {entry.id}, the entry its directory names")
    if entry.kind is not EntryKind.PROFILE:
        problems.append(f"{path}: {entry.id} is a shared entry; only wire profiles have suites")
    if not isinstance(doc.get("version"), str) or not doc["version"]:
        problems.append(f"{path}: version must be a non-empty string")
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError([*problems, f"{path}: cases must be a non-empty list"])
    cases = [
        c
        for i, item in enumerate(raw_cases)
        if (c := _case(item, f"{path}: case #{i + 1}", registry, entry.id, problems))
    ]
    ids = [c.id for c in cases]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"{path}: case id {dup} is used twice")
    cancel = doc.get("cancel")
    if cancel is not None and cancel not in ids:
        problems.append(f"{path}: cancel names {cancel!r}, which isn't a case")
    for key in doc:
        if key not in ("conformance_suite", "profile", "version", "cases", "cancel", "note"):
            problems.append(f"{path}: {key} isn't a suite key")
    if problems:
        raise SuiteError(problems)
    return Suite(entry.id, doc["version"], tuple(cases), cancel, path)


@dataclass
class _Exchange:
    """One HTTP request and its answer, with the timings the checks need."""

    status: int = 0
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    headers_s: float = 0.0
    """From the request's last byte sent to the answer's headers."""
    longest_gap_s: float = 0.0
    """The longest wait between bytes of the answer, from its headers to its end."""
    error: str | None = None


def _exchange(
    method: str,
    url: str,
    body: bytes | None,
    *,
    accept: str,
    timeout: float,
    abandon: str | None = None,
) -> _Exchange:
    """Send one request and read its answer, with timings.

    ``abandon``: ``after_send`` closes the connection before the answer; ``after_first_chunk`` after its first bytes.
    """
    out = _Exchange()
    parts = urlsplit(url)
    connection_class = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    connection = connection_class(parts.hostname or "", parts.port, timeout=timeout)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {"Accept": accept, "User-Agent": f"{TOOL} conform"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        try:
            connection.request(method, path, body=body, headers=headers)
        except (BrokenPipeError, ConnectionResetError):
            # A runner may answer a body it refuses (413) and close before it all arrives; read that answer.
            if abandon is not None or body is None or connection.sock is None:
                raise
        sent = time.monotonic()
        if abandon == "after_send":
            return out
        response = connection.getresponse()
        last = time.monotonic()
        out.headers_s = last - sent
        out.status = response.status
        out.content_type = response.getheader("Content-Type")
        out.headers = {k.lower(): v for k, v in response.getheaders()}
        chunks = []
        while True:
            chunk = response.read1(65536)
            now = time.monotonic()
            out.longest_gap_s = max(out.longest_gap_s, now - last)
            last = now
            if not chunk:
                break
            chunks.append(chunk)
            if abandon == "after_first_chunk":
                break
        out.body = b"".join(chunks)
    except (OSError, HTTPException) as exc:
        out.error = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    return out


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict):
        return bool(value)
    return value is not None


def _decoded(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


class _Run:
    """One suite's run against one runner."""

    def __init__(self, registry: Registry, target: Target, suite: Suite, common: Common, options: Options) -> None:
        self.registry = registry
        self.target = target
        self.suite = suite
        self.common = common
        self.options = options
        self.checks: list[Check] = []
        self.envelope: Envelope | None = None
        self.endpoints: dict[str, Endpoint] = {}
        self.health_ready: bool | None = None
        """Whether health answered ready before the cases ran; None until it was probed."""

    def add(self, check_id: str, result: str, detail: str = "") -> None:
        self.checks.append(Check(check_id, result, detail[:_DETAIL_MAX]))

    # The checks every suite runs

    def check_envelope(self) -> None:
        parsed = _parse(self.target.metadata, limit=None)
        size = len(self.target.metadata.encode("utf-8", "replace"))
        if isinstance(parsed, Ineligible):
            self.add("common.envelope", "fail", parsed.detail)
            return
        self.envelope = parsed
        if size > MAX_METADATA_BYTES:
            self.add("common.envelope", "fail", f"the envelope is {size:,} bytes; the limit is 1,024")
        else:
            self.add("common.envelope", "pass", f"a valid runner_contract 1 envelope, {size:,} of 1,024 bytes")

        manifest = self.target.manifest
        if manifest is None:
            self.add("common.revision", "skip", "no manifest given, so the revision can't be recomputed")
        elif self.target.metadata_from == "manifest":
            self.add("common.revision", "pass", "the envelope was built from the manifest, by the SPEC's recipe")
        elif parsed.deployment_revision == revision_of(manifest):
            self.add("common.revision", "pass", "the manifest recomputes the envelope's deployment_revision")
        else:
            self.add(
                "common.revision",
                "fail",
                f"the envelope's deployment_revision is {parsed.deployment_revision}; "
                f"the manifest makes {revision_of(manifest)}",
            )

        for endpoint in parsed.endpoints.values():
            if self.registry.canonical(endpoint.profile) == self.suite.profile:
                self.endpoints[endpoint.name] = endpoint
        if self.endpoints:
            names = ", ".join(sorted(self.endpoints))
            self.add("common.profile", "pass", f"the envelope advertises {self.suite.profile} on {names}")
        else:
            self.add("common.profile", "fail", f"the envelope advertises no endpoint of {self.suite.profile}")

        problems = self.target.registration_problems
        if problems is None:
            self.add("common.registration", "skip", "no registration block given")
        elif problems:
            self.add("common.registration", "fail", "; ".join(problems))
        else:
            self.add("common.registration", "pass", "the registration fields read as the node reads them")

    def _health_validator(self) -> Draft202012Validator | None:
        if self.common.health_schema is None:
            return None
        entry_id, component = self.common.health_schema
        entry = self.registry.get(entry_id)
        if entry is None:
            return None
        resource: Resource[Any] = Resource.from_contents(entry.document, default_specification=DRAFT202012)
        schemas: SchemaRegistry[Any] = SchemaRegistry().with_resource("urn:health", resource)
        return Draft202012Validator({"$ref": f"urn:health#/components/schemas/{component}"}, registry=schemas)

    def probe_health(self) -> tuple[bool, str]:
        """Whether health answers 200 with a ready body of the health entry's shape, and what it said."""
        url = self.target.health_url
        if not url:
            return False, "no health URL: pass --health-url, or give a manifest or block that has one"
        answer = _exchange("GET", url, None, accept="application/json", timeout=self.options.health_timeout)
        if answer.error:
            return False, f"the health check couldn't be reached: {answer.error}"
        body = _decoded(answer.body.decode("utf-8", "replace"))
        problems = []
        if answer.status != 200:
            problems.append(f"it answers {answer.status}")
        validator = self._health_validator()
        if validator is not None:
            problems += list(schema_problems(validator.iter_errors(body), "its body"))[:3]
        if not isinstance(body, dict) or body.get("status") != "ready":
            problems.append('its body isn\'t {"status": "ready"}')
        if answer.headers_s > self.common.headers_within_s:
            problems.append(f"its headers took {answer.headers_s:.2f} s")
        if problems:
            return False, "; ".join(problems)
        return True, f'200 with {{"status": "ready"}}, headers in {answer.headers_s:.2f} s'

    def check_health(self, check_id: str) -> None:
        ok, detail = self.probe_health()
        if self.health_ready is None:
            self.health_ready = ok
        self.add(check_id, "pass" if ok else "fail", detail)

    # The suite's cases

    def _filler(self, case: Case, operation: Operation) -> str | None:
        """The text that makes a limit case exceed its limit, or None when the runner advertises no such limit."""
        if case.limit is None:
            return ""
        assert self.envelope is not None
        if case.limit.name == "context_tokens":
            tokens = self.envelope.limits.get("context_tokens")
            # "a " is one token or more in common tokenizers, so this is past the limit with room to spare.
            return None if tokens is None else "a " * (int(tokens) + 256)
        if case.limit.name == "max_input_bytes":
            size = self.envelope.limits.get("max_input_bytes")
            return None if size is None else "a" * (int(size) + 1024)
        inputs = [
            i for i in operation.x.get("inputs", []) if isinstance(i, dict) and i.get("field") == case.limit.input
        ]
        return "a" * (int(inputs[0]["max_bytes"]) + 1024)

    def _request(self, case: Case, filler: str) -> Any:
        model = self.target.model

        def fill(value: Any) -> Any:
            if value == "$model":
                return model
            if value == "$fill":
                return filler
            if isinstance(value, str) and value.startswith(FIXTURE):
                return self.options.fixtures[value[len(FIXTURE) :]]
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v) for v in value]
            return value

        return fill(case.request)

    def _send(self, case: Case, operation: Operation, request: Any, abandon: str | None = None) -> _Exchange:
        endpoint = self.endpoints[case.endpoint]
        path = endpoint.path or operation.path
        url = self.target.url.rstrip("/") + path
        accept = {Framing.SSE: "text/event-stream", Framing.NDJSON: "application/x-ndjson"}.get(
            case.framing or Framing.JSON, "application/json"
        )
        body = None if request is None else json.dumps(request, ensure_ascii=False).encode("utf-8")
        method = endpoint.method or operation.method
        return _exchange(method, url, body, accept=accept, timeout=self.options.timeout, abandon=abandon)

    def _undeclared(self, case: Case) -> str | None:
        """The first of the case's ``when`` values the envelope doesn't declare, described; None when all hold."""
        document = _decoded(self.target.metadata)
        for pointer, value in case.when:
            found = lookup_all(document, pointer) if isinstance(document, dict) else []
            if not any(json_equal(v, value) for v in found):
                return f"the runner's envelope doesn't declare {pointer} = {json.dumps(value)}"
        return None

    def run_case(self, case: Case) -> None:
        prefix = case.id
        operation = self.registry.operation(self.suite.profile, case.endpoint)
        if self.envelope is None:
            self.add(prefix, "skip", "no valid envelope, so the runner's endpoints aren't known")
            return
        if case.endpoint not in self.endpoints:
            self.add(prefix, "skip", f"the runner doesn't advertise {case.endpoint}")
            return
        undeclared = self._undeclared(case)
        if undeclared:
            self.add(prefix, "skip", f"{undeclared}, so this case doesn't apply")
            return
        missing = [n for n in case.fixtures if n not in self.options.fixtures]
        if missing:
            self.add(prefix, "fail", f"the case needs fixture(s) {', '.join(missing)}: pass --fixtures with them")
            return
        if _contains(case.request, "$model") and not self.target.model:
            self.add(
                prefix, "fail", "the suite sends a model name: pass --model, or give a manifest or envelope with one"
            )
            return
        filler = self._filler(case, operation)
        if filler is None:
            assert case.limit is not None
            self.add(prefix, "skip", f"the runner advertises no limits.{case.limit.name}")
            return
        request = self._request(case, filler)

        check = validate_request(self.registry, self.suite.profile, case.endpoint, request)
        problems = list(check.problems)  # never the values, so never a fixture's signed URL or grant
        if case.valid and not check.ok:
            self.add(f"{prefix}.request", "fail", "the suite's request breaks the entry: " + "; ".join(problems[:3]))
            return
        if not case.valid and check.ok:
            self.add(f"{prefix}.request", "fail", "the suite's invalid request is valid against the entry")
            return
        detail = "valid against the entry" if case.valid else f"invalid as intended: {problems[0]}"
        self.add(f"{prefix}.request", "pass", detail)

        answer = self._send(case, operation, request)
        if answer.error:
            self.add(f"{prefix}.answer", "fail", f"no answer: {answer.error}")
            return
        reader = ResultReader(
            operation,
            status=answer.status,
            content_type=answer.content_type,
            request=request,
            headers=answer.headers,
            max_buffer=max(len(answer.body) + 1, 1),
        )
        reader.feed(answer.body)
        result = reader.finish()
        within = self.common.headers_within_s
        if result.framing in STREAMS:
            self.add(
                f"{prefix}.headers",
                "pass" if answer.headers_s <= within else "fail",
                f"headers in {answer.headers_s:.2f} s (at most {within:g} s for a streamed answer)",
            )
        else:
            self.add(
                f"{prefix}.headers",
                "skip",
                f"not streamed, so headers may come when the answer is done (they took {answer.headers_s:.2f} s)",
            )
        self._check_answer(prefix, case, result, answer)
        self._check_schema(prefix, case, result, answer)
        if case.framing is not None and result.success:
            self._check_content(prefix, case, result, answer)
            self._check_work(prefix, case, operation, request, result)
            self._check_fetch(prefix, case)
            if result.framing in STREAMS:
                keepalive = self.common.keepalive_s
                ok = answer.longest_gap_s <= keepalive
                self.add(
                    f"{prefix}.keepalive",
                    "pass" if ok else "fail",
                    f"the longest wait between bytes was {answer.longest_gap_s:.2f} s (at most {keepalive:g} s)",
                )

    def _check_answer(self, prefix: str, case: Case, result: Result, answer: _Exchange) -> None:
        got = (
            f"success ({result.framing.value if result.framing else 'no framing'})"
            if result.success
            else f"{result.error.kind.value if result.error else 'an error'} ({result.failure})"
        )
        if case.framing is not None:
            ok = result.success and result.framing is case.framing
            want = f"success as {case.framing.value}"
        else:
            ok = not result.success and result.error is not None and result.error.kind in case.kinds
            want = "an error of kind " + " or ".join(k.value for k in case.kinds)
        detail = f"status {answer.status}: {got}" if ok else f"wanted {want}; got status {answer.status}, {got}"
        self.add(f"{prefix}.answer", "pass" if ok else "fail", detail)

    def _units(self, result: Result, answer: _Exchange) -> list[tuple[str | None, Any]]:
        if result.framing in STREAMS:
            return [(kind, _decoded(text)) for kind, text in split_stream(result.framing, answer.body)]
        if result.framing is Framing.BINARY:
            return []
        return [(None, _decoded(answer.body.decode("utf-8", "replace")))]

    def _check_schema(self, prefix: str, case: Case, result: Result, answer: _Exchange) -> None:
        units = self._units(result, answer)
        if not units:
            self.add(f"{prefix}.schema", "skip", "the answer carries no JSON to check")
            return
        problems: list[str] = []
        checked = 0
        for i, (_, value) in enumerate(units):
            outcome = validate_response(
                self.registry,
                self.suite.profile,
                case.endpoint,
                value,
                status=answer.status,
                content_type=answer.content_type,
            )
            if outcome is None:
                content_type = answer.content_type or "no content type"
                self.add(f"{prefix}.schema", "skip", f"the entry declares no schema for {answer.status} {content_type}")
                return
            checked += 1
            where = f"event or line {i + 1}: " if len(units) > 1 else ""
            problems += [where + p for p in outcome.problems]
        what = f"all {checked} events or lines match" if checked > 1 else "the body matches"
        if problems:
            self.add(f"{prefix}.schema", "fail", f"{len(problems)} problem(s): " + "; ".join(problems[:5]))
        else:
            self.add(f"{prefix}.schema", "pass", f"{what} the entry's schema")

    def _check_content(self, prefix: str, case: Case, result: Result, answer: _Exchange) -> None:
        if not case.content:
            return
        units = [value for _, value in self._units(result, answer)]
        missing = []
        for content in case.content:
            pool = units[-1:] if content.location in (Location.LAST_LINE, Location.LAST_EVENT) else units
            if not any(content.found(unit) for unit in pool):
                missing.append(content.describe())
        if missing:
            self.add(f"{prefix}.content", "fail", "nothing non-empty at " + ", ".join(missing))
        else:
            self.add(f"{prefix}.content", "pass", "the answer carries content where the suite looks")

    def _check_fetch(self, prefix: str, case: Case) -> None:
        """Each URL the case names answers 2xx with a body. Details name fixtures, never their values."""
        if not case.fetch:
            return
        problems = []
        for name in case.fetch:
            url = self.options.fixtures.get(name)
            if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
                problems.append(f"fixture {name} isn't an http(s) URL")
                continue
            got = _exchange("GET", url, None, accept="*/*", timeout=self.options.timeout)
            if got.error:
                problems.append(f"{name} couldn't be fetched ({got.error.split(':', 1)[0]})")
            elif not 200 <= got.status < 300 or not got.body:
                problems.append(f"{name} answered {got.status}" + (" with no body" if not got.body else ""))
        if problems:
            self.add(f"{prefix}.fetch", "fail", "; ".join(problems))
        else:
            self.add(f"{prefix}.fetch", "pass", f"{', '.join(case.fetch)} answered 2xx with a body")

    def _check_work(self, prefix: str, case: Case, operation: Operation, request: Any, result: Result) -> None:
        if not case.work_reports:
            return
        applicable = [r for r in operation.work_reports if r.framing is None or r.framing is result.framing]
        expected = sorted({r.measure for r in applicable if r.asked(request)})
        unasked = sorted({r.measure for r in applicable if not r.asked(request)} - set(expected))
        if not expected:
            self.add(f"{prefix}.work_reports", "skip", f"the entry reports no work for {result.framing} answers")
            return
        missing = [m for m in expected if m not in result.work]
        found = ", ".join(f"{m} {result.work[m]:g}" for m in expected if m in result.work)
        note = f" (not asked for: {', '.join(unasked)})" if unasked else ""
        if missing:
            self.add(f"{prefix}.work_reports", "fail", f"missing {', '.join(missing)}; found {found or 'none'}{note}")
        else:
            self.add(f"{prefix}.work_reports", "pass", f"{found}{note}")

    def check_cancel(self) -> None:
        check_id = "common.cancel"
        if self.suite.cancel is None:
            self.add(check_id, "skip", "the suite names no case to cancel")
            return
        case = next(c for c in self.suite.cases if c.id == self.suite.cancel)
        operation = self.registry.operation(self.suite.profile, case.endpoint)
        needs_model = _contains(case.request, "$model") and not self.target.model
        if self.envelope is None or case.endpoint not in self.endpoints or needs_model:
            self.add(check_id, "skip", f"{case.id} can't run against this runner")
            return
        request = self._request(case, "")
        stream = case.framing in STREAMS
        started = self._send(case, operation, request, abandon="after_first_chunk" if stream else "after_send")
        if started.error:
            self.add(check_id, "fail", f"{case.id} couldn't start: {started.error}")
            return
        settle = self.options.cancel_settle_s
        time.sleep(self.common.cancel_settle_s if settle is None else settle)
        health = "health stayed ready"
        if self.health_ready:
            ready, detail = self.probe_health()
            if not ready:
                self.add(check_id, "fail", f"after {case.id} was closed mid-answer, health failed: {detail}")
                return
        else:
            # common.health.ready already failed; failing cancellation for it too would count one fault twice.
            health = "health wasn't ready before, so only the next request was checked"
        follow = self._send(case, operation, request)
        reader = ResultReader(operation, status=follow.status, content_type=follow.content_type, request=request)
        reader.feed(follow.body)
        result = reader.finish()
        if follow.error or not result.success:
            why = follow.error or result.failure
            self.add(check_id, "fail", f"after {case.id} was closed mid-answer, the next request failed: {why}")
            return
        how = "after its first bytes" if stream else "before its answer"
        self.add(
            check_id,
            "pass",
            f"{case.id} was closed {how}; {health}, and the next request succeeded. Black-box: that "
            f"the work stopped within {self.common.cancel_within_s:g} s can't be seen from outside",
        )


def run_suite(
    registry: Registry,
    target: Target,
    profile: str,
    options: Options | None = None,
    *,
    tool_version: str | None = None,
) -> Report:
    """Run ``profile``'s suite against the runner and return its report. Raises ``SuiteError`` for a broken suite."""
    from . import __version__

    options = options or Options()
    suite = load_suite(registry, profile)
    common = load_common(registry)
    started = datetime.now(UTC)
    run = _Run(registry, target, suite, common, options)
    run.check_envelope()
    run.check_health("common.health.ready")
    run.add(
        "common.health.starting",
        "skip",
        'black-box: conform can\'t make the runner unready; health must answer 503 with {"status": "starting"} '
        "until the self-test of every profile passes",
    )
    for case in suite.cases:
        run.run_case(case)
    run.check_cancel()
    run.check_health("common.health.after")
    finished = datetime.now(UTC)

    revision = run.envelope.deployment_revision if run.envelope else None
    if revision is None and target.manifest is not None:
        revision = revision_of(target.manifest)
    if revision is None:
        raise ValueError("no deployment_revision: the envelope doesn't parse, and no manifest gives one")
    document: dict[str, Any] = {
        "conformance_report": 1,
        "runner_id": target.runner_id,
        "deployment_revision": revision,
        "profile": suite.profile,
    }
    if registry.release:
        document["registry_release"] = registry.release
    document.update(
        {
            "suite": {"id": suite.profile, "version": f"{suite.version}+common.{common.version}"},
            "tool": {"name": TOOL, "version": tool_version or __version__},
            "started_at": _timestamp(started),
            "finished_at": _timestamp(finished),
            "checks": [
                {"id": c.id, "result": c.result, **({"detail": c.detail} if c.detail else {})} for c in run.checks
            ],
            "pass": not any(c.result == "fail" for c in run.checks),
        }
    )
    problems = [e.message for e in conformance_report_validator().iter_errors(document)]
    if problems:
        raise AssertionError(f"conform built a report its schema refuses: {problems}")
    return Report(document)


def _timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _profiles_of(envelope: Envelope, registry: Registry) -> list[str]:
    """Each profile the envelope's endpoints speak, as entry IDs, once."""
    out: list[str] = []
    for endpoint in envelope.endpoints.values():
        canonical = registry.canonical(endpoint.profile) or endpoint.profile
        if canonical not in out:
            out.append(canonical)
    return out


def profiles_to_run(target: Target, registry: Registry, wanted: Iterable[str] = ()) -> list[str]:
    """The profiles named, or else every profile the target's envelope advertises."""
    names = [registry.canonical(p) or p for p in wanted]
    if names:
        return list(dict.fromkeys(names))
    parsed = _parse(target.metadata, limit=None)
    return _profiles_of(parsed, registry) if isinstance(parsed, Envelope) else []
