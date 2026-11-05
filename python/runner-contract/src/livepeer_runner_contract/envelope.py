"""The envelope a runner publishes in ``Metadata``: reading it, and the ``deployment_revision`` recipe.

``parse_envelope`` is what a gateway runs on every discovery answer. It never raises on bad input: a
runner whose metadata is too large, isn't JSON, isn't ``runner_contract: 1`` or breaks the schema
gets an ``Ineligible`` with the reason, and every other runner is unaffected (SPEC.md, "What every
gateway does", rule 1). Reasons never quote a runner's values, only the schema's, so they are safe
to log.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from jsonschema.exceptions import ValidationError

from ._json import StrictJSONError, loads_strict, safe
from .standard import MAX_METADATA_BYTES, RUNNER_CONTRACT, envelope_validator

__all__ = [
    "Admission",
    "AttributeValue",
    "CapacityGroup",
    "Channels",
    "Endpoint",
    "Envelope",
    "Ineligible",
    "IneligibleReason",
    "Model",
    "Runtime",
    "canonical_json",
    "deployment_revision",
    "envelope_problems",
    "parse_envelope",
]


class IneligibleReason(StrEnum):
    NO_METADATA = "no_metadata"
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    NOT_V1 = "not_v1"
    """No ``runner_contract`` key: metadata in a format from before v1."""
    UNKNOWN_VERSION = "unknown_version"
    INVALID = "invalid"
    """A v1 envelope that breaks the envelope schema."""


@dataclass(frozen=True)
class Ineligible:
    """Why a runner's metadata makes that runner, and only it, ineligible."""

    reason: IneligibleReason
    detail: str

    def __str__(self) -> str:
        return self.detail


class Admission(StrEnum):
    CANDIDATE = "candidate"
    PRIVATE = "private"

    @classmethod
    def read(cls, value: object) -> Admission:
        """``candidate`` when absent or ``candidate``; any other value means ``private``."""
        return cls.CANDIDATE if value is None or value == "candidate" else cls.PRIVATE


@dataclass(frozen=True)
class Model:
    name: str
    source: str
    revision: str | None = None


@dataclass(frozen=True)
class Channels:
    """A persistent endpoint's channel names, in and out."""

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Endpoint:
    """One ``endpoints.{name}``. ``path`` and ``method`` are set only where they differ from the entry."""

    name: str
    profile: str
    path: str | None = None
    method: str | None = None
    stop: str | None = None
    channels: Channels | None = None
    idle_timeout_s: int | None = None
    drain_timeout_s: int | None = None


@dataclass(frozen=True)
class Runtime:
    name: str | None = None
    image: str | None = None
    versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    encoder: str | None = None


@dataclass(frozen=True)
class CapacityGroup:
    id: str
    slots: int


AttributeValue = str | int | float | bool | tuple[str | int | float | bool, ...]


@dataclass(frozen=True)
class Envelope:
    """A valid ``runner_contract: 1`` envelope, holding only the keys the standard defines."""

    deployment_revision: str
    endpoints: Mapping[str, Endpoint]
    model: Model | None = None
    runner_id: str | None = None
    runtime: Runtime | None = None
    limits: Mapping[str, int | float] = field(default_factory=lambda: MappingProxyType({}))
    admission: Admission = Admission.CANDIDATE
    """What the envelope says. The admission that applies to an endpoint also depends on its entry: see ``select``."""
    capacity_group: CapacityGroup | None = None
    attributes: Mapping[str, AttributeValue] = field(default_factory=lambda: MappingProxyType({}))
    size: int = 0
    """The ``Metadata`` string's size in bytes."""

    @property
    def profiles(self) -> tuple[str, ...]:
        """Each profile the endpoints speak, once, in endpoint order."""
        return tuple(dict.fromkeys(e.profile for e in self.endpoints.values()))


def _path_text(path: Iterable[Any]) -> str:
    parts = [str(p) if isinstance(p, int) else safe(p, 40) for p in path]
    return ".".join(parts) if parts else "the envelope"


def _describe(error: ValidationError) -> str:
    """A schema error in the schema's words, never quoting the runner's value."""
    if "propertyNames" in error.absolute_schema_path:
        keyword, expected = error.validator, error.validator_value
        return "has a key that " + (f"doesn't match {expected}" if keyword == "pattern" else "isn't allowed")
    keyword, expected = error.validator, cast(Any, error.validator_value)
    if keyword == "required" and isinstance(error.instance, dict):
        missing = [k for k in expected if k not in error.instance]
        return f"is missing {', '.join(missing)}"
    if keyword == "type":
        return f"must be of type {expected if isinstance(expected, str) else ' or '.join(expected)}"
    if keyword == "const":
        return f"must be {json.dumps(expected)}"
    if keyword == "enum":
        return f"must be one of {', '.join(json.dumps(v) for v in expected)}"
    if keyword == "pattern":
        return f"doesn't match {expected}"
    if keyword in ("minimum", "minLength", "minItems", "minProperties"):
        return f"is below the {keyword} of {expected}"
    if keyword in ("maximum", "maxLength", "maxItems", "maxProperties"):
        return f"is above the {keyword} of {expected}"
    if keyword == "anyOf":
        return "must be a string, number or boolean, or a list of them"
    return f"fails the schema's {keyword} rule"


def envelope_problems(value: object) -> list[str]:
    """Every way ``value`` breaks the envelope schema, one line each; empty when it is valid."""
    errors = sorted(envelope_validator().iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{_path_text(e.absolute_path)}: {_describe(e)}" for e in errors]


def parse_envelope(raw: object) -> Envelope | Ineligible:
    """Read a runner's ``Metadata`` string. Never raises on bad input.

    Checks, in order: the size of the string exactly as registered (at most 1,024 bytes of UTF-8),
    that it is one JSON object with no duplicate keys, that ``runner_contract`` is 1, and the envelope
    schema. Keys the standard doesn't define are ignored and not kept.
    """
    try:
        return _parse(raw)
    except Exception:  # an envelope must never fail a discovery refresh
        return Ineligible(IneligibleReason.INVALID, "the metadata couldn't be read")


def _parse(raw: object, *, limit: int | None = MAX_METADATA_BYTES) -> Envelope | Ineligible:
    """parse_envelope's rules; ``runner-contract check`` passes no limit to report everything else too."""
    if not isinstance(raw, str) or not raw:
        return Ineligible(IneligibleReason.NO_METADATA, "no metadata: not a runner contract v1 runner")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        return Ineligible(IneligibleReason.NOT_JSON, "the metadata isn't valid UTF-8")
    if limit is not None and size > limit:
        return Ineligible(IneligibleReason.TOO_LARGE, f"the metadata is {size:,} bytes; the limit is 1,024")
    try:
        doc = loads_strict(raw)
    except StrictJSONError as exc:
        return Ineligible(IneligibleReason.NOT_JSON, f"the metadata isn't one JSON object: {exc}")
    if not isinstance(doc, dict):
        return Ineligible(IneligibleReason.NOT_JSON, "the metadata isn't a JSON object")
    if "runner_contract" not in doc:
        return Ineligible(IneligibleReason.NOT_V1, "no runner_contract key: metadata from before runner contract v1")
    version = doc["runner_contract"]
    if isinstance(version, bool) or version != RUNNER_CONTRACT:
        shown = version if isinstance(version, int) and not isinstance(version, bool) else "not 1"
        return Ineligible(
            IneligibleReason.UNKNOWN_VERSION, f"runner_contract is {shown}; this gateway reads only runner_contract 1"
        )
    problems = envelope_problems(doc)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        return Ineligible(IneligibleReason.INVALID, f"the envelope breaks the schema: {problems[0]}{more}")
    return _build(doc, size)


def _build(doc: dict[str, Any], size: int) -> Envelope:
    """An Envelope from a schema-valid document."""
    endpoints = {}
    for name, item in doc["endpoints"].items():
        channels = item.get("channels")
        endpoints[name] = Endpoint(
            name=name,
            profile=item["profile"],
            path=item.get("path"),
            method=item.get("method"),
            stop=item.get("stop"),
            channels=None
            if channels is None
            else Channels(tuple(channels.get("in", ())), tuple(channels.get("out", ()))),
            idle_timeout_s=item.get("idle_timeout_s"),
            drain_timeout_s=item.get("drain_timeout_s"),
        )
    model = doc.get("model")
    runtime = doc.get("runtime")
    group = doc.get("capacity_group")
    return Envelope(
        deployment_revision=doc["deployment_revision"],
        endpoints=MappingProxyType(endpoints),
        model=None if model is None else Model(model["name"], model["source"], model.get("revision")),
        runner_id=doc.get("runner_id"),
        runtime=None
        if runtime is None
        else Runtime(
            name=runtime.get("name"),
            image=runtime.get("image"),
            versions=MappingProxyType(dict(runtime.get("versions", {}))),
            encoder=runtime.get("encoder"),
        ),
        limits=MappingProxyType(dict(doc.get("limits", {}))),
        admission=Admission.read(doc.get("admission")),
        capacity_group=None if group is None else CapacityGroup(group["id"], group["slots"]),
        attributes=MappingProxyType(
            {k: tuple(v) if isinstance(v, list) else v for k, v in doc.get("attributes", {}).items()}
        ),
        size=size,
    )


def canonical_json(value: Any) -> str:
    """The recipe's canonical JSON: keys sorted, no whitespace between tokens, non-ASCII written as itself."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def deployment_revision(
    images: Iterable[str],
    profiles: Iterable[str],
    settings: Mapping[str, Any],
    model: Mapping[str, Any] | Model | None = None,
) -> str:
    """``sha256:`` and the SHA-256 of the canonical JSON of the deployment's inputs (SPEC.md, the recipe).

    This is the SPEC's function: it sorts ``images`` and ``profiles`` as given, so a caller that serves
    one profile on several endpoints passes it once (the tool does).
    """
    obj: dict[str, Any] = {"images": sorted(images), "profiles": sorted(profiles), "settings": dict(settings)}
    if model is not None:
        if isinstance(model, Model):
            obj["model"] = {"source": model.source, "revision": model.revision}
        else:
            obj["model"] = {"source": model["source"], "revision": model.get("revision")}
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
