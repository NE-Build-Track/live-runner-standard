"""The runner manifest (``runner.toml``): what a builder writes once, and what the tool builds from it.

From one manifest the tool builds the envelope (``deployment_revision`` included), the node's static
``[[Runners]]`` block and the JSON a runner sends to register dynamically. The registration fields
match livepeer/node's ``StaticRunner`` (static, ``RunnerConfig``) and ``heartbeatRequest`` (dynamic,
``POST /runners/heartbeat``) in orchestrator/registry.go. README.md ("The manifest") documents every key.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .envelope import CapacityGroup, Channels, Endpoint, Model, deployment_revision, envelope_problems
from .standard import (
    DIGEST,
    ENDPOINT_NAME,
    MAX_METADATA_BYTES,
    MAX_VERSION_CHARS,
    PROFILE_ID,
    RUNNER_CONTRACT,
    Capability,
    Mode,
)

__all__ = [
    "Manifest",
    "ManifestError",
    "build_envelope",
    "load_manifest",
    "parse_manifest",
    "render_block_json",
    "render_block_toml",
    "render_envelope",
    "revision_of",
]

ROUTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
"""The node's rule for runner IDs (validRouteID)."""
IMAGE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
PRICE_UNITS = ("hour", "fixed")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SESSION_KEYS = ("stop", "channels", "idle_timeout_s", "drain_timeout_s")

_KEYS: Mapping[str, tuple[str, ...]] = {
    "runner": ("id", "app", "mode", "capacity", "url", "health_url", "version", "admission"),
    "price": ("amount", "currency", "unit"),
    "model": ("name", "source", "revision"),
    "endpoints": ("name", "profile", "path", "method", *SESSION_KEYS),
    "revision": ("images", "settings", "presets"),
    "runtime": ("name", "image", "versions", "encoder"),
    "capacity_group": ("id", "slots"),
}
_TABLES = ("runner", "price", "model", "endpoints", "limits", "revision", "runtime", "capacity_group", "attributes")


class ManifestError(ValueError):
    """The manifest can't build a valid registration. ``problems`` holds one line per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class Manifest:
    id: str
    app: str
    mode: Mode
    capacity: int
    url: str
    health_url: str | None
    version: str | None
    admission: str | None
    price_amount: Decimal | int
    price_currency: str
    price_unit: str
    model: Model | None
    endpoints: tuple[Endpoint, ...]
    images: tuple[str, ...]
    settings: Mapping[str, Any]
    presets: tuple[str, ...] = ()
    limits: Mapping[str, int | float] = field(default_factory=lambda: MappingProxyType({}))
    runtime: Mapping[str, Any] | None = None
    capacity_group: CapacityGroup | None = None
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    warnings: tuple[str, ...] = ()


def _plain(value: Any) -> Any:
    """TOML values as JSON values: Decimal floats become floats; dates have no JSON form."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class _Reader:
    """Reads a manifest's tables, collecting one problem per bad key."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def table(self, doc: Mapping[str, Any], name: str, *, required: bool) -> dict[str, Any]:
        value = doc.get(name)
        if value is None:
            if required:
                self.fail(f"[{name}]", "is missing")
            return {}
        if not isinstance(value, dict):
            self.fail(name, "must be a table")
            return {}
        self.known(value, f"[{name}]", _KEYS.get(name))
        return value

    def known(self, table: Mapping[str, Any], where: str, keys: tuple[str, ...] | None) -> None:
        if keys is None:
            return
        for key in table:
            if key not in keys:
                self.fail(f"{where} {key}", f"isn't a manifest key (expected one of {', '.join(keys)})")

    def string(self, table: Mapping[str, Any], key: str, where: str, *, required: bool = True) -> str | None:
        value = table.get(key)
        if value is None:
            if required:
                self.fail(f"{where} {key}", "is missing")
            return None
        if not isinstance(value, str) or not value:
            self.fail(f"{where} {key}", "must be a non-empty string")
            return None
        return value

    def integer(
        self, table: Mapping[str, Any], key: str, where: str, *, minimum: int, required: bool = True
    ) -> int | None:
        value = table.get(key)
        if value is None:
            if required:
                self.fail(f"{where} {key}", "is missing")
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            self.fail(f"{where} {key}", f"must be an integer of at least {minimum}")
            return None
        return value

    def strings(self, table: Mapping[str, Any], key: str, where: str) -> list[str]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            self.fail(f"{where} {key}", "must be a list of non-empty strings")
            return []
        return value

    def json_value(self, value: Any, where: str) -> bool:
        """Whether a value has one JSON form. Floats have one, but languages print them differently."""
        if isinstance(value, datetime | date | time):
            self.fail(where, "is a TOML date or time, which has no JSON form; write it as a string")
            return False
        if isinstance(value, Decimal):
            if not value.is_finite():
                self.fail(where, "must be a finite number")
                return False
            return True
        if isinstance(value, dict):
            return all(self.json_value(v, f"{where}.{k}") for k, v in value.items())
        if isinstance(value, list):
            return all(self.json_value(v, f"{where}[{i}]") for i, v in enumerate(value))
        return True


def _todos(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        return [f"{where}: is still a TODO"] if value.strip().upper().startswith("TODO") else []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _todos(v, f"{where}.{k}" if where else k)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _todos(v, f"{where}[{i}]")]
    return []


def _has_float(value: Any) -> bool:
    if isinstance(value, Decimal | float):
        return True
    if isinstance(value, dict):
        return any(_has_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_float(v) for v in value)
    return False


def parse_manifest(text: str, *, capabilities: Mapping[str, Capability]) -> Manifest:
    """Read and check a manifest. ``capabilities`` is the capability list to check ``app`` and ``mode`` against."""
    try:
        doc = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"the manifest isn't valid TOML: {exc}"]) from exc
    r = _Reader()
    r.problems.extend(_todos(doc, ""))
    for key in doc:
        if key not in _TABLES:
            r.fail(key, f"isn't a manifest table (expected one of {', '.join(_TABLES)})")

    runner = r.table(doc, "runner", required=True)
    runner_id = r.string(runner, "id", "[runner]")
    if runner_id is not None and not ROUTE_ID.match(runner_id):
        r.fail("[runner] id", "must be 1 to 128 ASCII letters, digits, '-' or '_' (the node's rule)")
    app = r.string(runner, "app", "[runner]")
    mode_text = r.string(runner, "mode", "[runner]")
    mode = Mode(mode_text) if mode_text in set(Mode) else None
    if mode_text is not None and mode is None:
        r.fail("[runner] mode", "must be single-shot or persistent")
    capability = capabilities.get(app) if app is not None else None
    if app is not None and capability is None:
        r.fail("[runner] app", f"{app!r} isn't on the capability list; the App ID is the capability and nothing else")
    elif capability is not None and mode is not None and capability.mode is not mode:
        r.fail("[runner] mode", f"{app} is a {capability.mode.value} capability")
    capacity = r.integer(runner, "capacity", "[runner]", minimum=1)
    if capacity is not None and capacity > 10000:
        r.fail("[runner] capacity", "must be at most 10,000 (the node's limit)")
    url = r.string(runner, "url", "[runner]")
    if url is not None and not _http_url(url):
        r.fail("[runner] url", "must be an absolute http or https URL")
    health_url = r.string(runner, "health_url", "[runner]", required=False)
    if health_url is not None and not _http_url(health_url):
        r.fail("[runner] health_url", "must be an absolute http or https URL")
    version = r.string(runner, "version", "[runner]", required=False)
    if version is not None and len(version) > MAX_VERSION_CHARS:
        r.fail("[runner] version", f"is {len(version)} characters; the limit is 64")
    admission = r.string(runner, "admission", "[runner]", required=False)
    if admission is not None and admission not in ("candidate", "private"):
        r.fail("[runner] admission", "must be candidate or private")

    price = r.table(doc, "price", required=True)
    amount = price.get("amount")
    if amount is None:
        r.fail("[price] amount", "is missing")
    elif (
        isinstance(amount, bool)
        or not isinstance(amount, int | Decimal)
        or not (amount > 0)
        or (isinstance(amount, Decimal) and not amount.is_finite())
    ):
        r.fail("[price] amount", "must be a positive number")
        amount = None
    currency = r.string(price, "currency", "[price]")
    if currency is not None and currency.lower() != "usd":
        r.fail("[price] currency", "must be usd (the node prices runners in USD)")
    unit = r.string(price, "unit", "[price]")
    if unit is not None and unit.lower() not in PRICE_UNITS:
        r.fail("[price] unit", "must be hour (runner time) or fixed (per call), as the node charges")

    model = None
    model_table = r.table(doc, "model", required=False)
    if model_table:
        name = r.string(model_table, "name", "[model]")
        source = r.string(model_table, "source", "[model]")
        revision = r.string(model_table, "revision", "[model]", required=False)
        if source is not None and not re.match(r"^[a-z][a-z0-9-]*:.+$", source):
            r.fail("[model] source", "must be <origin>:<id>, such as hf:Qwen/Qwen3.8-27B-AWQ")
        if source is not None and source.startswith("hf:") and revision is None:
            r.warnings.append("[model] revision: missing; a Hugging Face model has an immutable commit to pin")
        if name is not None and source is not None:
            model = Model(name, source, revision)

    endpoints = _endpoints(doc.get("endpoints"), mode, r)

    limits_table = r.table(doc, "limits", required=False)
    limits: dict[str, int | float] = {}
    for key, value in limits_table.items():
        if not re.match(r"^[a-z][a-z0-9_]*$", key):
            r.fail(f"[limits] {key}", "must be lowercase letters, digits and underscores")
        elif isinstance(value, bool) or not isinstance(value, int | Decimal) or value < 0:
            r.fail(f"[limits] {key}", "must be a number of at least 0")
        elif r.json_value(value, f"[limits] {key}"):
            limits[key] = _plain(value)

    revision_table = r.table(doc, "revision", required=True)
    images = r.strings(revision_table, "images", "[revision]")
    if revision_table and "images" not in revision_table:
        r.fail("[revision] images", "is missing")
    elif revision_table and not images:
        r.fail("[revision] images", "needs at least one image digest, so a change of code voids approvals")
    for image in images:
        if not IMAGE.match(image):
            r.fail("[revision] images", f"{image!r} must be pinned by digest: name@sha256:<64 hex>")
    settings = revision_table.get("settings", {})
    if not isinstance(settings, dict):
        r.fail("[revision] settings", "must be a table")
        settings = {}
    r.json_value(settings, "[revision] settings")
    if _has_float(settings):
        r.warnings.append(
            "[revision] settings: holds a float; other languages may print it differently and get another revision"
        )
    presets = r.strings(revision_table, "presets", "[revision]")

    runtime = _runtime(r.table(doc, "runtime", required=False), r)
    group_table = r.table(doc, "capacity_group", required=False)
    capacity_group = None
    if group_table:
        group_id = r.string(group_table, "id", "[capacity_group]")
        slots = r.integer(group_table, "slots", "[capacity_group]", minimum=1)
        if group_id is not None and slots is not None:
            capacity_group = CapacityGroup(group_id, slots)

    attributes_table = r.table(doc, "attributes", required=False)
    attributes: dict[str, Any] = {}
    for key, value in attributes_table.items():
        scalar = (str, int, Decimal, bool)
        ok = isinstance(value, scalar) or (isinstance(value, list) and all(isinstance(v, scalar) for v in value))
        if not ok:
            r.fail(f"[attributes] {key}", "must be a string, number or boolean, or a list of them")
        elif r.json_value(value, f"[attributes] {key}"):
            attributes[key] = _plain(value)

    if r.problems:
        raise ManifestError(r.problems)
    assert runner_id and app and mode and capacity and url and amount is not None and currency and unit  # noqa: PT018
    return Manifest(
        id=runner_id,
        app=app,
        mode=mode,
        capacity=capacity,
        url=url,
        health_url=health_url,
        version=version,
        admission=admission,
        price_amount=amount,
        price_currency=currency.lower(),
        price_unit=unit.lower(),
        model=model,
        endpoints=endpoints,
        images=tuple(images),
        settings=MappingProxyType(_plain(settings)),
        presets=tuple(presets),
        limits=MappingProxyType(limits),
        runtime=runtime,
        capacity_group=capacity_group,
        attributes=MappingProxyType(attributes),
        warnings=tuple(r.warnings),
    )


def _endpoints(value: Any, mode: Mode | None, r: _Reader) -> tuple[Endpoint, ...]:
    if value is None or (isinstance(value, list) and not value):
        r.fail("[[endpoints]]", "needs at least one endpoint")
        return ()
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        r.fail("endpoints", "must be an array of tables, [[endpoints]]")
        return ()
    out: list[Endpoint] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        where = f"[[endpoints]] #{i + 1}"
        r.known(item, where, _KEYS["endpoints"])
        name = r.string(item, "name", where)
        profile = r.string(item, "profile", where)
        if name is not None and not ENDPOINT_NAME.match(name):
            r.fail(f"{where} name", "must be the operationId of the endpoint's operation: ^[a-z][a-z0-9_]*$")
        elif name is not None and name in seen:
            r.fail(f"{where} name", f"{name} is used twice")
        if profile is not None and not PROFILE_ID.match(profile):
            r.fail(f"{where} profile", "must be a registry ID, <namespace>.<name>.v<major>")
        path = r.string(item, "path", where, required=False)
        stop = r.string(item, "stop", where, required=False)
        for key, given in (("path", path), ("stop", stop)):
            if given is not None and not given.startswith("/"):
                r.fail(f"{where} {key}", "must start with /")
        method = r.string(item, "method", where, required=False)
        if method is not None and method.upper() not in METHODS:
            r.fail(f"{where} method", f"must be one of {', '.join(METHODS)}")
        session = [k for k in SESSION_KEYS if k in item]
        if session and mode is Mode.SINGLE_SHOT:
            r.fail(where, f"{', '.join(session)} are for persistent runners only")
        channels = None
        if "channels" in item:
            table = item["channels"]
            if not isinstance(table, dict) or set(table) - {"in", "out"}:
                r.fail(f"{where} channels", "must be a table of in and out lists")
            else:
                channels = Channels(tuple(r.strings(table, "in", where)), tuple(r.strings(table, "out", where)))
        idle = r.integer(item, "idle_timeout_s", where, minimum=1, required=False)
        drain = r.integer(item, "drain_timeout_s", where, minimum=0, required=False)
        if name is not None and profile is not None:
            seen.add(name)
            out.append(
                Endpoint(
                    name,
                    profile,
                    path=path,
                    method=None if method is None else method.upper(),
                    stop=stop,
                    channels=channels,
                    idle_timeout_s=idle,
                    drain_timeout_s=drain,
                )
            )
    return tuple(out)


def _runtime(table: dict[str, Any], r: _Reader) -> Mapping[str, Any] | None:
    if not table:
        return None
    out: dict[str, Any] = {}
    for key in ("name", "encoder"):
        value = r.string(table, key, "[runtime]", required=False)
        if value is not None:
            out[key] = value
    image = r.string(table, "image", "[runtime]", required=False)
    if image is not None:
        if DIGEST.match(image):
            out["image"] = image
        else:
            r.fail("[runtime] image", "must be the image's digest, sha256:<64 hex>")
    if "versions" in table:
        versions = table["versions"]
        if not isinstance(versions, dict) or not all(isinstance(v, str) for v in versions.values()):
            r.fail("[runtime] versions", "must be a table of strings")
        else:
            out["versions"] = dict(versions)
    return MappingProxyType(out)


def load_manifest(path: str, *, capabilities: Mapping[str, Capability]) -> Manifest:
    with open(path, encoding="utf-8") as handle:
        return parse_manifest(handle.read(), capabilities=capabilities)


def revision_of(manifest: Manifest) -> str:
    """The manifest's ``deployment_revision``: each wire profile once, with the presets, by the SPEC's recipe."""
    profiles = sorted({e.profile for e in manifest.endpoints} | set(manifest.presets))
    return deployment_revision(manifest.images, profiles, manifest.settings, manifest.model)


def build_envelope(manifest: Manifest) -> dict[str, Any]:
    """The envelope as a JSON object, in the SPEC example's key order."""
    envelope: dict[str, Any] = {"runner_contract": RUNNER_CONTRACT, "deployment_revision": revision_of(manifest)}
    if manifest.model is not None:
        model = {"name": manifest.model.name, "source": manifest.model.source}
        if manifest.model.revision is not None:
            model["revision"] = manifest.model.revision
        envelope["model"] = model
    endpoints: dict[str, Any] = {}
    for e in manifest.endpoints:
        item: dict[str, Any] = {"profile": e.profile}
        for key in ("path", "method", "stop"):
            if getattr(e, key) is not None:
                item[key] = getattr(e, key)
        if e.channels is not None:
            item["channels"] = {"in": list(e.channels.inputs), "out": list(e.channels.outputs)}
        for key in ("idle_timeout_s", "drain_timeout_s"):
            if getattr(e, key) is not None:
                item[key] = getattr(e, key)
        endpoints[e.name] = item
    envelope["endpoints"] = endpoints
    if manifest.limits:
        envelope["limits"] = dict(manifest.limits)
    if manifest.runtime:
        envelope["runtime"] = dict(manifest.runtime)
    if manifest.admission is not None:
        envelope["admission"] = manifest.admission
    if manifest.capacity_group is not None:
        envelope["capacity_group"] = {"id": manifest.capacity_group.id, "slots": manifest.capacity_group.slots}
    if manifest.attributes:
        envelope["attributes"] = dict(manifest.attributes)
    return envelope


def render_envelope(manifest: Manifest) -> str:
    """The envelope as the compact JSON a runner registers in ``Metadata``: at most 1,024 bytes, or ManifestError."""
    envelope = build_envelope(manifest)
    problems = envelope_problems(envelope)
    if problems:
        raise ManifestError([f"envelope: {p}" for p in problems])
    text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    size = len(text.encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        raise ManifestError([f"envelope: is {size:,} bytes; the limit is 1,024 (trim attributes, limits or runtime)"])
    return text


def _toml_basic(text: str) -> str:
    escapes = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = []
    for ch in text:
        if ch in escapes:
            out.append(escapes[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_string(text: str, *, literal: bool = False) -> str:
    """A TOML string: a literal string (no escapes, as for JSON) where it can be one, else a basic string."""
    if literal and "'" not in text and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return f"'{text}'"
    return _toml_basic(text)


def _number(value: Decimal | int) -> str:
    """The price as a TOML or JSON number: the node reads Price as a json.Number, which a TOML string can't fill."""
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return text if any(c in text for c in ".eE") else text + ".0"


def render_block_toml(manifest: Manifest) -> str:
    """The node's static ``[[Runners]]`` block, for its ``RunnerConfig`` file."""
    if manifest.health_url is None:
        raise ManifestError(
            ["[runner] health_url: is missing; without HealthURL the node treats a static runner as always healthy"]
        )
    metadata = render_envelope(manifest)
    lines = [
        "[[Runners]]",
        f"ID = {_toml_string(manifest.id)}",
        f"App = {_toml_string(manifest.app)}",
        f"Mode = {_toml_string(manifest.mode.value)}",
        f"Capacity = {manifest.capacity}",
        f"RunnerURL = {_toml_string(manifest.url)}",
        f"HealthURL = {_toml_string(manifest.health_url)}",
        "HealthCode = 200",
        'Status = "ready"',
    ]
    if manifest.version is not None:
        lines.append(f"Version = {_toml_string(manifest.version)}")
    lines += [
        f"Metadata = {_toml_string(metadata, literal=True)}",
        "[Runners.PriceInfo]",
        f"Price = {_number(manifest.price_amount)}",
        f"Currency = {_toml_string(manifest.price_currency)}",
        f"Unit = {_toml_string(manifest.price_unit)}",
    ]
    block = "\n".join(lines) + "\n"
    parsed = tomllib.loads(block)["Runners"][0]  # the block must read back exactly
    if parsed["Metadata"] != metadata or not math.isclose(
        float(parsed["PriceInfo"]["Price"]), float(manifest.price_amount)
    ):
        raise AssertionError("the [[Runners]] block doesn't read back as written")
    return block


def render_block_json(manifest: Manifest) -> str:
    """The registration a runner sends to the node's ``POST /runners/heartbeat`` (with its credential)."""
    body: dict[str, Any] = {
        "runner_id": manifest.id,
        "app": manifest.app,
        "mode": manifest.mode.value,
        "capacity": manifest.capacity,
        "runner_url": manifest.url,
        "status": "ready",
    }
    if manifest.version is not None:
        body["version"] = manifest.version
    body["metadata"] = render_envelope(manifest)
    body["price_info"] = {"price": "\0price\0", "currency": manifest.price_currency, "unit": manifest.price_unit}
    text = json.dumps(body, indent=2, ensure_ascii=False)
    return text.replace('"\\u0000price\\u0000"', _number(manifest.price_amount)) + "\n"
