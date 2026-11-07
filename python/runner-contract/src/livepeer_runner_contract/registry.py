"""A pinned registry release: its entries, checked against the index, with aliases and ``extends`` resolved.

A release directory looks like the repository root at a release tag: ``registry/index.json``, the
entries under ``registry/<namespace>/``, and usually ``capabilities.json`` and ``schemas/`` beside
``registry/``. The ``livepeer-runner-registry`` package installs exactly that.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from ._json import ABSENT, json_equal, lookup
from .envelope import Admission
from .standard import PROFILE_ID, Capability, Mode, parse_capabilities

__all__ = [
    "Aggregate",
    "Attempts",
    "Entry",
    "EntryKind",
    "ErrorKind",
    "ErrorMapping",
    "FinalCondition",
    "Framing",
    "Location",
    "Operation",
    "Registry",
    "RegistryError",
    "Retry",
    "SuccessRule",
    "UnknownEndpoint",
    "UnknownProfile",
    "WorkReport",
]

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_RELEASE_DIR = re.compile(r"^registry-([0-9]{4}\.[0-9]{1,2}\.[1-9][0-9]*)$")
REGISTRY_INSTALL = (
    "livepeer-runner-registry @ git+https://github.com/NE-Build-Track/live-runner-standard.git"
    "@<release>#subdirectory=python/runner-registry"
)


class RegistryError(Exception):
    """The registry release can't be used as given."""


class UnknownProfile(LookupError):
    """No entry, or alias, has this ID in the registry release."""


class UnknownEndpoint(LookupError):
    """The entry has no operation with this operationId."""


class EntryKind(StrEnum):
    PROFILE = "profile"
    SHARED = "shared"


class Framing(StrEnum):
    JSON = "json"
    SSE = "sse"
    NDJSON = "ndjson"
    BINARY = "binary"


class Location(StrEnum):
    """Where in an answer a condition looks (the schema's ``in``)."""

    BODY = "body"
    LAST_LINE = "last-line"
    ANY_LINE = "any-line"
    """Any NDJSON line of the answer (error mappings only)."""
    LAST_EVENT = "last-event"
    ANY_EVENT = "any-event"
    """Any SSE event of the answer (error mappings only)."""
    HEADER = "header"
    REQUEST = "request"
    """The request the caller sent (work reports only)."""


class Attempts(StrEnum):
    CALLER = "caller"
    """The caller owns retries: the gateway must not retry."""
    GATEWAY = "gateway"


class Retry(StrEnum):
    NEVER = "never"
    """The request itself is wrong; a retry fails the same way."""
    ELSEWHERE = "elsewhere"
    """No work started; the same request may go to another runner at once."""
    OWNER = "owner"
    """Work may have happened; only whoever owns the attempt may retry."""
    AFTER_FIX = "after_fix"
    """The caller must fix something first, such as access to an input."""


class ErrorKind(StrEnum):
    INPUT_INVALID = "input_invalid"
    INPUT_UNSUPPORTED = "input_unsupported"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    OUTPUT_FAILED = "output_failed"
    UPSTREAM_FAILED = "upstream_failed"
    INTERNAL = "internal"
    INPUT_UNREADABLE = "input_unreadable"

    @property
    def retry(self) -> Retry:
        """The base kind's fixed retry rule (SPEC.md, "Errors")."""
        return _RETRY[self]


_RETRY: Mapping[ErrorKind, Retry] = MappingProxyType(
    {
        ErrorKind.INPUT_INVALID: Retry.NEVER,
        ErrorKind.INPUT_UNSUPPORTED: Retry.NEVER,
        ErrorKind.BUSY: Retry.ELSEWHERE,
        ErrorKind.UNAVAILABLE: Retry.ELSEWHERE,
        ErrorKind.DEADLINE_EXCEEDED: Retry.OWNER,
        ErrorKind.OUTPUT_FAILED: Retry.OWNER,
        ErrorKind.UPSTREAM_FAILED: Retry.OWNER,
        ErrorKind.INTERNAL: Retry.OWNER,
        ErrorKind.INPUT_UNREADABLE: Retry.AFTER_FIX,
    }
)


@dataclass(frozen=True)
class FinalCondition:
    """What must also hold at the end of a successful answer. ``framing`` scopes it to answers with that framing.

    ``pointer`` may hold ``*`` tokens, each standing for every item of an array; the test then holds
    when any value it names passes (``present: false`` holds when it names none).
    """

    location: Location
    framing: Framing | None = None
    event: str | None = None
    data: str | None = None
    pointer: str | None = None
    equals: Any = ABSENT
    present: bool | None = None
    matches: re.Pattern[str] | None = None
    """A regular expression the value at ``pointer``, as a string, must contain a match for."""


@dataclass(frozen=True)
class SuccessRule:
    status: frozenset[int]
    final: tuple[FinalCondition, ...] = ()


@dataclass(frozen=True)
class ErrorMapping:
    """One error the runner returns, mapped to a base kind. Every condition given must hold.

    ``content_type`` is a prefix of the answer's content type; ``matches`` a regular expression the
    value at ``pointer``, as a string, must contain a match for. Pointers test as in ``FinalCondition``.
    """

    kind: ErrorKind
    status: int | None = None
    framing: Framing | None = None
    content_type: str | None = None
    location: Location | None = None
    event: str | None = None
    pointer: str | None = None
    equals: Any = ABSENT
    matches: re.Pattern[str] | None = None
    present: bool | None = None
    note: str | None = None
    data: str | None = None
    """Never set on an error mapping; here so both kinds of condition test alike."""


class Aggregate(StrEnum):
    """How a work report turns what its pointer names into a number."""

    VALUE = "value"
    """The number at the pointer (several numbers, under a ``*`` pointer, add up)."""
    COUNT = "count"
    """How many values the pointer names."""
    SUM = "sum"
    """The numbers the pointer names, added."""
    LENGTH = "length"
    """The total length of the strings the pointer names."""


@dataclass(frozen=True)
class WorkReport:
    """One thing a runner reports about the work, where it appears, and how the caller asks for it."""

    measure: str
    location: Location
    framing: Framing | None = None
    event: str | None = None
    pointer: str | None = None
    header: str | None = None
    request_pointer: str | None = None
    """Where the caller asks for the report, when it must."""
    request_value: Any = ABSENT
    """The value it sets there."""
    aggregate: Aggregate = Aggregate.VALUE

    def asked(self, request: Any) -> bool:
        """Whether ``request`` asks for this report, or the report needs no asking."""
        if self.request_pointer is None:
            return True
        found = lookup(request, self.request_pointer)
        return found is not ABSENT and json_equal(found, self.request_value)


@dataclass(frozen=True)
class Operation:
    """One endpoint of a profile: an OpenAPI operation and its ``x-livepeer`` object.

    ``x`` and ``spec`` are the entry's own objects, for the parts this library doesn't model (work
    reports, inputs and outputs, sessions, recovery, cost hints); treat them as read-only.
    """

    name: str
    method: str
    path: str
    defined_in: str
    """The entry that defines it: the profile itself, or a base it extends."""
    framings: tuple[Framing, ...]
    success: SuccessRule
    errors: tuple[ErrorMapping, ...]
    attempts: Attempts
    x: Mapping[str, Any]
    spec: Mapping[str, Any]
    document: Mapping[str, Any] = field(repr=False)
    """The entry document ``spec`` comes from, which its ``$ref``s resolve against."""
    pointer: str = ""
    """The JSON pointer of ``spec`` within ``document``."""
    work_reports: tuple[WorkReport, ...] = ()


@dataclass(frozen=True)
class Entry:
    """One registry entry, with the operations it serves after ``extends`` is resolved."""

    id: str
    path: str
    sha256: str
    status: str
    steward: tuple[str, ...]
    aliases: tuple[str, ...]
    kind: EntryKind
    extends: str | None
    mode: Mode | None
    capabilities: tuple[str, ...]
    admission: Admission | None
    """The profile's default admission."""
    attempts: Attempts | None
    operations: Mapping[str, Operation]
    x: Mapping[str, Any]
    """The root ``x-livepeer`` object, with any keys it lacks taken from the entry it extends."""
    document: Mapping[str, Any] = field(repr=False)

    @property
    def deprecated(self) -> bool:
        return self.status == "deprecated"


class _Malformed(ValueError):
    pass


def _framing(value: Any) -> Framing:
    if value not in set(Framing):
        raise _Malformed(f"unknown framing {value!r}")
    return Framing(value)


def _framings(value: Any) -> tuple[Framing, ...]:
    items = value if isinstance(value, list) else [value]
    if not items:
        raise _Malformed("framing is empty")
    return tuple(_framing(v) for v in items)


def _location(value: Any) -> Location:
    if value not in set(Location):
        raise _Malformed(f"unknown location {value!r}")
    return Location(value)


def _optional_str(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise _Malformed(f"{key} must be a string")
    return value


def _present(item: Mapping[str, Any]) -> bool | None:
    value = item.get("present")
    if value is not None and not isinstance(value, bool):
        raise _Malformed("present must be true or false")
    return value


def _pattern(item: Mapping[str, Any]) -> re.Pattern[str] | None:
    source = _optional_str(item, "matches")
    if source is None:
        return None
    try:
        return re.compile(source)
    except re.error as exc:
        # ECMA-262 and Python's re agree on ordinary patterns; this library runs patterns with re.
        raise _Malformed(f"matches {source!r} isn't a pattern Python's re can run: {exc}") from exc


_FINAL_KEYS = frozenset({"framing", "in", "event", "data", "pointer", "equals", "matches", "present"})
_ERROR_KEYS = frozenset(
    {"kind", "status", "framing", "content_type", "in", "event", "pointer", "equals", "matches", "present", "note"}
)


def _known(item: Mapping[str, Any], keys: frozenset[str], what: str) -> None:
    """Refuse a condition with a key this reader doesn't know, rather than read it more loosely than it says."""
    unknown = sorted(set(item) - keys)
    if unknown:
        raise _Malformed(f"{what} has {', '.join(unknown)}, which this reader doesn't know; upgrade the library")


def _final(item: Any) -> FinalCondition:
    if not isinstance(item, dict) or "in" not in item:
        raise _Malformed("a final condition needs `in`")
    _known(item, _FINAL_KEYS, "a final condition")
    return FinalCondition(
        location=_location(item["in"]),
        framing=None if item.get("framing") is None else _framing(item["framing"]),
        event=_optional_str(item, "event"),
        data=_optional_str(item, "data"),
        pointer=_optional_str(item, "pointer"),
        equals=item.get("equals", ABSENT),
        matches=_pattern(item),
        present=_present(item),
    )


def _success(value: Any) -> SuccessRule:
    if not isinstance(value, dict) or not isinstance(value.get("status"), list) or not value["status"]:
        raise _Malformed("success needs a list of statuses")
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in value["status"]):
        raise _Malformed("success statuses must be integers")
    final = value.get("final", [])
    # The schema makes `final` a list of conditions; an entry written before that holds one object.
    items = final if isinstance(final, list) else [final]
    return SuccessRule(frozenset(value["status"]), tuple(_final(i) for i in items))


def _error(item: Any) -> ErrorMapping:
    if not isinstance(item, dict) or item.get("kind") not in set(ErrorKind):
        raise _Malformed("each error needs a base kind")
    _known(item, _ERROR_KEYS, "an error mapping")
    status = item.get("status")
    if status is not None and (not isinstance(status, int) or isinstance(status, bool)):
        raise _Malformed("an error's status must be an integer")
    return ErrorMapping(
        kind=ErrorKind(item["kind"]),
        status=status,
        framing=None if item.get("framing") is None else _framing(item["framing"]),
        content_type=_optional_str(item, "content_type"),
        location=None if item.get("in") is None else _location(item["in"]),
        event=_optional_str(item, "event"),
        pointer=_optional_str(item, "pointer"),
        equals=item.get("equals", ABSENT),
        matches=_pattern(item),
        present=_present(item),
        note=_optional_str(item, "note"),
    )


_MEASURES = ("input_tokens", "output_tokens", "images", "audio_seconds", "media_seconds", "characters")


def _work_report(item: Any) -> WorkReport:
    if not isinstance(item, dict) or item.get("measure") not in _MEASURES or "in" not in item:
        raise _Malformed("each work report needs a measure and `in`")
    asked = item.get("request")
    if asked is not None and (not isinstance(asked, dict) or not isinstance(asked.get("pointer"), str)):
        raise _Malformed("a work report's request needs a pointer and a value")
    aggregate = item.get("aggregate", "value")
    if aggregate not in set(Aggregate):
        raise _Malformed(f"unknown aggregate {aggregate!r}")
    return WorkReport(
        measure=item["measure"],
        location=_location(item["in"]),
        framing=None if item.get("framing") is None else _framing(item["framing"]),
        event=_optional_str(item, "event"),
        pointer=_optional_str(item, "pointer"),
        header=_optional_str(item, "header"),
        request_pointer=None if asked is None else asked["pointer"],
        request_value=ABSENT if asked is None else asked.get("value"),
        aggregate=Aggregate(aggregate),
    )


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _own_operations(entry_id: str, doc: Mapping[str, Any], attempts: Attempts | None) -> dict[str, Operation]:
    out: dict[str, Operation] = {}
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        raise _Malformed("paths must be an object")
    for route, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            name = op.get("operationId")
            x = op.get("x-livepeer")
            if not isinstance(name, str) or not isinstance(x, dict):
                raise _Malformed(f"{method.upper()} {route} needs an operationId and an x-livepeer object")
            if name in out:
                raise _Malformed(f"operationId {name} is used twice")
            if "framing" not in x:
                raise _Malformed(f"{name}: x-livepeer needs framing")
            own_attempts = x.get("attempts")
            if own_attempts is not None and own_attempts not in set(Attempts):
                raise _Malformed(f"{name}: unknown attempts {own_attempts!r}")
            effective = Attempts(own_attempts) if own_attempts is not None else attempts
            out[name] = Operation(
                name=name,
                method=method.upper(),
                path=route,
                defined_in=entry_id,
                framings=_framings(x["framing"]),
                success=_success(x.get("success")),
                errors=tuple(_error(e) for e in x.get("errors", [])),
                # A shared entry has no attempts; an operation it defines takes its profile's.
                attempts=effective if effective is not None else Attempts.GATEWAY,
                x=x,
                spec=op,
                document=doc,
                pointer=f"/paths/{_escape(route)}/{method}",
                work_reports=tuple(_work_report(w) for w in x.get("work_reports", [])),
            )
    return out


@dataclass
class _Raw:
    """An entry as read, before extends is resolved."""

    id: str
    fields: Mapping[str, Any]
    doc: dict[str, Any]
    x: dict[str, Any]


_INHERITED = ("mode", "capabilities", "admission", "attempts", "readiness", "presets", "cost_hint")


class Registry:
    """A registry release: entries by ID, aliases, and the release's capability list.

    Build one with ``Registry.load``. Every lookup takes an entry ID or an alias.
    """

    def __init__(
        self,
        entries: Mapping[str, Entry],
        aliases: Mapping[str, str],
        *,
        capabilities: Mapping[str, Capability] | None = None,
        release: str | None = None,
        refused: Mapping[str, str] | None = None,
        root: Path | None = None,
    ) -> None:
        self.entries: Mapping[str, Entry] = MappingProxyType(dict(entries))
        self.aliases: Mapping[str, str] = MappingProxyType(dict(aliases))
        self.capabilities: Mapping[str, Capability] | None = (
            None if capabilities is None else MappingProxyType(dict(capabilities))
        )
        """The release's ``capabilities.json``, when the release directory has one."""
        self.release = release
        """The release tag, such as ``2026.11.1``: the package's, or a ``registry-<tag>/`` directory's name."""
        self.refused: Mapping[str, str] = MappingProxyType(dict(refused or {}))
        """Entries refused on load, by ID, with the reason (only when loaded with ``strict=False``)."""
        self.root = root
        self._validators: dict[Any, Any] = {}  # compiled request validators, by operation

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, *, strict: bool = True) -> Registry:
        """Load a registry release and check every entry against the index.

        ``path`` is a release directory (holding ``registry/index.json``) or a ``registry/`` directory
        itself (holding ``index.json``). With no path, the release installed as the
        ``livepeer-runner-registry`` package is loaded.

        An entry whose bytes don't match its sha256 in the index, that can't be read, or whose
        ``extends`` can't be resolved is refused. With ``strict`` (the default) any refusal raises
        ``RegistryError``; without it, the entry is left out and listed in ``refused``.
        """
        release = None
        if path is None:
            try:
                package = importlib.import_module("livepeer_runner_registry")
            except ImportError as exc:
                raise RegistryError(
                    f"no registry path given, and livepeer-runner-registry isn't installed; pass a path, or "
                    f'install a release: "{REGISTRY_INSTALL}"'
                ) from exc
            given = Path(package.data_dir())
            release = package.release()
        else:
            given = Path(path)
        if (given / "registry" / "index.json").is_file():
            registry_dir = given / "registry"
        elif (given / "index.json").is_file():
            registry_dir = given
        else:
            raise RegistryError(f"{given}: holds neither registry/index.json nor index.json")
        if release is None:
            # A release tarball unpacks to registry-<tag>/, so its directory names the release.
            named = _RELEASE_DIR.match(registry_dir.resolve().parent.name)
            release = named.group(1) if named else None
        return cls._load_dir(registry_dir, release=release, strict=strict)

    @classmethod
    def _load_dir(cls, registry_dir: Path, *, release: str | None, strict: bool) -> Registry:
        try:
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"{registry_dir / 'index.json'}: can't be read: {exc}") from exc
        if (
            not isinstance(index, dict)
            or index.get("registry_index") != 1
            or not isinstance(index.get("entries"), dict)
        ):
            raise RegistryError(f"{registry_dir / 'index.json'}: isn't a registry_index 1 document")

        refused: dict[str, str] = {}
        raws: dict[str, _Raw] = {}
        aliases: dict[str, str] = {}
        for entry_id, fields in sorted(index["entries"].items()):
            try:
                raws[entry_id] = cls._read(registry_dir, entry_id, fields)
            except _Malformed as exc:
                refused[entry_id] = str(exc)
                continue
            for alias in fields.get("aliases") or []:
                if not isinstance(alias, str) or alias in index["entries"] or alias in aliases:
                    refused[entry_id] = f"alias {alias!r} clashes with another ID"
                else:
                    aliases[alias] = entry_id

        entries: dict[str, Entry] = {}
        for entry_id in sorted(raws):
            if entry_id in refused:
                continue
            try:
                entries[entry_id] = cls._resolve(entry_id, raws, refused)
            except _Malformed as exc:
                refused[entry_id] = str(exc)
        if strict and refused:
            lines = "; ".join(f"{k}: {v}" for k, v in sorted(refused.items()))
            raise RegistryError(
                f"{registry_dir}: refused {len(refused)} entr{'y' if len(refused) == 1 else 'ies'}: {lines}"
            )
        aliases = {a: t for a, t in aliases.items() if t in entries}

        capabilities = None
        capabilities_file = registry_dir.parent / "capabilities.json"
        if capabilities_file.is_file():
            try:
                capabilities = parse_capabilities(json.loads(capabilities_file.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise RegistryError(f"{capabilities_file}: can't be read: {exc}") from exc
        return cls(
            entries, aliases, capabilities=capabilities, release=release, refused=refused, root=registry_dir.parent
        )

    @staticmethod
    def _read(registry_dir: Path, entry_id: str, fields: Any) -> _Raw:
        if (
            not isinstance(fields, dict)
            or not isinstance(fields.get("path"), str)
            or not isinstance(fields.get("sha256"), str)
        ):
            raise _Malformed("its index line needs a path and a sha256")
        rel = PurePosixPath(fields["path"])
        if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("registry",):
            raise _Malformed(f"its path {fields['path']!r} isn't under registry/")
        namespace, _, name = entry_id.partition(".")
        if rel.parts[1:] != (namespace, f"{name}.json"):
            raise _Malformed(f"its path {fields['path']!r} doesn't follow its ID")
        try:
            raw = (registry_dir / PurePosixPath(*rel.parts[1:])).read_bytes()
        except OSError as exc:
            raise _Malformed(f"can't be read: {exc.strerror or exc}") from exc
        if hashlib.sha256(raw).hexdigest() != fields["sha256"]:
            raise _Malformed("its bytes don't match the sha256 in index.json")
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise _Malformed("isn't JSON") from exc
        x = doc.get("x-livepeer") if isinstance(doc, dict) else None
        if not isinstance(x, dict):
            raise _Malformed("has no x-livepeer object")
        if x.get("profile") != entry_id:
            raise _Malformed("its x-livepeer.profile isn't its ID")
        if x.get("kind") not in set(EntryKind):
            raise _Malformed("its kind is neither profile nor shared")
        return _Raw(entry_id, fields, doc, x)

    @classmethod
    def _resolve(cls, entry_id: str, raws: Mapping[str, _Raw], refused: Mapping[str, str]) -> Entry:
        chain = [raws[entry_id]]
        while (base := chain[-1].x.get("extends")) is not None:
            if not isinstance(base, str) or not PROFILE_ID.match(base):
                raise _Malformed("extends isn't an entry ID")
            if base in refused:
                raise _Malformed(f"extends {base}, which is refused")
            if base not in raws:
                raise _Malformed(f"extends {base}, which isn't in the release")
            if any(r.id == base for r in chain):
                raise _Malformed("its extends chain loops")
            chain.append(raws[base])

        x: dict[str, Any] = {}
        for raw in reversed(chain):  # the base first, so each entry overrides what it extends
            x.update({k: v for k, v in raw.x.items() if k in _INHERITED or raw is chain[0]})
        own = chain[0]

        mode = x.get("mode")
        attempts = x.get("attempts")
        admission = x.get("admission")
        capabilities = x.get("capabilities") or []
        if mode is not None and mode not in set(Mode):
            raise _Malformed(f"unknown mode {mode!r}")
        if attempts is not None and attempts not in set(Attempts):
            raise _Malformed(f"unknown attempts {attempts!r}")
        default_admission = None
        if admission is not None:
            if not isinstance(admission, dict) or admission.get("default") not in set(Admission):
                raise _Malformed("admission needs a default of candidate or private")
            default_admission = Admission(admission["default"])
        effective_attempts = None if attempts is None else Attempts(attempts)

        operations: dict[str, Operation] = {}
        for raw in reversed(chain):
            operations.update(_own_operations(raw.id, raw.doc, effective_attempts))
        fields = own.fields
        return Entry(
            id=entry_id,
            path=fields["path"],
            sha256=fields["sha256"],
            status=str(fields.get("status", "active")),
            steward=tuple(fields.get("steward") or ()),
            aliases=tuple(fields.get("aliases") or ()),
            kind=EntryKind(own.x["kind"]),
            extends=own.x.get("extends"),
            mode=None if mode is None else Mode(mode),
            capabilities=tuple(c for c in capabilities if isinstance(c, str)),
            admission=default_admission,
            attempts=effective_attempts,
            operations=MappingProxyType(operations),
            x=MappingProxyType(x),
            document=own.doc,
        )

    def canonical(self, profile: str) -> str | None:
        """The entry ID that ``profile`` names, itself or through an alias; None when it names none."""
        if profile in self.entries:
            return profile
        return self.aliases.get(profile)

    def get(self, profile: str) -> Entry | None:
        canonical = self.canonical(profile)
        return None if canonical is None else self.entries[canonical]

    def entry(self, profile: str) -> Entry:
        """The entry ``profile`` names, or ``UnknownProfile``."""
        found = self.get(profile)
        if found is None:
            raise UnknownProfile(profile)
        return found

    def operation(self, profile: str, endpoint: str) -> Operation:
        """The operation whose operationId is ``endpoint`` in ``profile``'s entry."""
        found = self.entry(profile).operations.get(endpoint)
        if found is None:
            raise UnknownEndpoint(f"{profile} has no operation {endpoint}")
        return found

    def __contains__(self, profile: object) -> bool:
        return isinstance(profile, str) and self.canonical(profile) is not None

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries.values())

    def __len__(self) -> int:
        return len(self.entries)
