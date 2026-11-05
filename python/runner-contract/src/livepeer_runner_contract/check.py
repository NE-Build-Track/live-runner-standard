"""``runner-contract check``: everything that can be wrong with a runner's registration, one line each.

It reads a static ``[[Runners]]`` block (TOML), the JSON a runner registers dynamically, or a bare
envelope, and checks the registration fields as the node reads them (orchestrator/registry.go) and
the envelope against the standard and a registry release.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .envelope import Envelope, IneligibleReason, _parse, envelope_problems
from .manifest import PRICE_UNITS, ROUTE_ID, Manifest, _http_url, revision_of
from .registry import EntryKind, Registry
from .standard import MAX_METADATA_BYTES, MAX_VERSION_CHARS, Capability, Mode

__all__ = ["Registration", "check_registrations", "read_registrations"]

# livepeer/node's StaticRunner and its nested tables. BurntSushi/toml matches keys to these fields
# case-insensitively, and the node refuses any key that matches none.
STATIC_KEYS = (
    "ID",
    "Proxy",
    "RunnerURL",
    "Version",
    "Metadata",
    "GPU",
    "App",
    "Mode",
    "Status",
    "Capacity",
    "HealthURL",
    "HealthCode",
    "PriceInfo",
)
PRICE_KEYS = ("Price", "Currency", "Unit")
GPU_KEYS = ("ID", "Name", "VRAMMB")


@dataclass
class Registration:
    """One runner's registration fields, from either format, with its problems found while reading."""

    where: str
    runner_id: str | None
    app: Any
    mode: Any
    capacity: Any
    url: Any
    version: Any
    metadata: Any
    price: Any
    problems: list[str]
    static: bool = False
    health_url: Any = None
    bare: bool = False
    """A bare envelope, with no registration fields around it."""


def _fold(table: Mapping[str, Any], names: tuple[str, ...], where: str, problems: list[str]) -> dict[str, Any]:
    """Keys folded to the node's field names, as BurntSushi/toml matches them."""
    out: dict[str, Any] = {}
    by_lower = {n.lower(): n for n in names}
    for key, value in table.items():
        name = by_lower.get(key.lower())
        if name is None:
            problems.append(f"{where}: {key} isn't a key the node knows; it refuses the whole file")
        else:
            out[name] = value
    return out


def read_registrations(text: str, *, name: str) -> list[Registration]:
    """The registrations in a file: each ``[[Runners]]`` of a TOML block, or one JSON registration or envelope."""
    stripped = text.strip()
    if name.endswith(".json") or stripped.startswith("{"):
        return [_read_json(stripped, name)]
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [
            Registration(name, None, None, None, None, None, None, None, None, [f"{name}: isn't TOML or JSON: {exc}"])
        ]
    runners = doc.get("Runners")
    problems = [
        f"{name}: {key} isn't a key the node knows; it refuses the whole file" for key in doc if key != "Runners"
    ]
    if not isinstance(runners, list) or not runners:
        problems.append(f"{name}: has no [[Runners]] table")
        return [Registration(name, None, None, None, None, None, None, None, None, problems)]
    out = []
    for i, raw in enumerate(runners):
        where = f"{name}: [[Runners]] #{i + 1}"
        reg_problems = list(problems) if i == 0 else []
        fields = _fold(raw, STATIC_KEYS, where, reg_problems)
        runner_id = fields.get("ID") if isinstance(fields.get("ID"), str) else None
        if runner_id:
            where = f"{name}: {runner_id}"
        price = fields.get("PriceInfo")
        if isinstance(price, dict):
            price = _fold(price, PRICE_KEYS, f"{where}: PriceInfo", reg_problems)
            if "Price" in price and (
                isinstance(price["Price"], str | bool) or not isinstance(price["Price"], int | float)
            ):
                reg_problems.append(f"{where}: PriceInfo.Price must be a TOML number; the node can't read a string")
                price["Price"] = None
            price = {k.lower(): v for k, v in price.items()}
        gpu = fields.get("GPU")
        if isinstance(gpu, dict):
            _fold(gpu, GPU_KEYS, f"{where}: GPU", reg_problems)
        out.append(
            Registration(
                where,
                runner_id,
                fields.get("App"),
                fields.get("Mode"),
                fields.get("Capacity"),
                fields.get("RunnerURL"),
                fields.get("Version"),
                fields.get("Metadata"),
                price,
                reg_problems,
                static=True,
                health_url=fields.get("HealthURL"),
            )
        )
    return out


def _read_json(text: str, name: str) -> Registration:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return Registration(name, None, None, None, None, None, None, None, None, [f"{name}: isn't JSON: {exc}"])
    if isinstance(doc, dict) and "runner_contract" in doc:  # a bare envelope: the file is the Metadata string
        return Registration(name, None, None, None, None, None, None, text, None, [], bare=True)
    if not isinstance(doc, dict) or "metadata" not in doc:
        problem = f"{name}: is neither a registration (with metadata) nor an envelope (with runner_contract)"
        return Registration(name, None, None, None, None, None, None, None, None, [problem])
    runner_id = doc.get("runner_id") if isinstance(doc.get("runner_id"), str) else None
    where = f"{name}: {runner_id}" if runner_id else name
    price = doc.get("price_info")
    return Registration(
        where,
        runner_id,
        doc.get("app"),
        doc.get("mode"),
        doc.get("capacity"),
        doc.get("runner_url"),
        doc.get("version"),
        doc.get("metadata"),
        dict(price) if isinstance(price, dict) else price,
        [],
    )


def _price_problems(price: Any) -> list[str]:
    if not isinstance(price, dict):
        return ["PriceInfo: is missing; a runner states its wholesale price"]
    problems = []
    amount = price.get("price")
    try:
        value = Decimal(str(amount)) if isinstance(amount, int | float | str) and not isinstance(amount, bool) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 0:
        problems.append("PriceInfo.Price: must be a positive number")
    currency = price.get("currency")
    if currency not in (None, "") and (not isinstance(currency, str) or currency.strip().lower() != "usd"):
        problems.append("PriceInfo.Currency: must be usd")
    unit = price.get("unit")
    if unit not in (None, "") and (not isinstance(unit, str) or unit.strip().lower() not in PRICE_UNITS):
        problems.append("PriceInfo.Unit: must be hour or fixed, as the node charges")
    return problems


def check_registrations(
    registrations: list[Registration],
    *,
    registry: Registry,
    capabilities: Mapping[str, Capability],
    manifest: Manifest | None = None,
) -> list[str]:
    """Every problem with the registrations, one line each, prefixed with where it is."""
    lines: list[str] = []
    for reg in registrations:
        lines.extend(reg.problems)
        lines.extend(f"{reg.where}: {p}" for p in _check_one(reg, registry, capabilities, manifest))
    return lines


def _check_one(
    reg: Registration, registry: Registry, capabilities: Mapping[str, Capability], manifest: Manifest | None
) -> list[str]:
    problems: list[str] = []
    app, mode = (manifest.app, manifest.mode.value) if reg.bare and manifest is not None else (reg.app, reg.mode)
    if not reg.bare:
        problems.extend(_registration_problems(reg, capabilities))

    metadata = reg.metadata
    if not isinstance(metadata, str) or not metadata:
        return [*problems, "Metadata: is missing; it holds the envelope"]
    size = len(metadata.encode("utf-8", "replace"))
    if size > MAX_METADATA_BYTES:
        problems.append(f"Metadata: is {size:,} bytes; the limit is 1,024")
    parsed = _parse(metadata, limit=None)  # the size is reported above; check the rest regardless
    if not isinstance(parsed, Envelope):
        if parsed.reason is IneligibleReason.INVALID:
            try:
                doc = json.loads(metadata)
            except ValueError:
                doc = None
            schema = envelope_problems(doc) if doc is not None else []
            problems.extend(f"envelope: {p}" for p in schema or [parsed.detail])
        else:
            problems.append(f"envelope: {parsed.detail}")
        return problems

    problems.extend(_endpoint_problems(parsed, app, mode, registry))
    if manifest is not None:
        expected = revision_of(manifest)
        if parsed.deployment_revision != expected:
            problems.append(
                f"deployment_revision: is {parsed.deployment_revision}, but the manifest makes {expected}; "
                "rebuild the registration with `runner-contract block`"
            )
    return problems


def _registration_problems(reg: Registration, capabilities: Mapping[str, Capability]) -> list[str]:
    problems = []
    if reg.runner_id is not None and not ROUTE_ID.match(reg.runner_id):
        problems.append("ID: must be 1 to 128 ASCII letters, digits, '-' or '_'")
    capability = capabilities.get(reg.app) if isinstance(reg.app, str) else None
    if not isinstance(reg.app, str) or not reg.app:
        problems.append("App: is missing; it is the runner's capability")
    elif capability is None:
        problems.append(f"App: {reg.app!r} isn't on the capability list; the App ID is the capability and nothing else")
    mode = reg.mode if reg.mode not in (None, "") else None
    if mode is not None and mode not in set(Mode):
        problems.append("Mode: must be single-shot or persistent")
    elif capability is not None:
        effective = mode or Mode.PERSISTENT.value  # the node's default
        if effective != capability.mode.value:
            said = f"is {effective}" if mode else "is missing, so the node makes it persistent"
            problems.append(f"Mode: {said}; {capability.name} is {capability.mode.value}")
    if reg.capacity is not None and (
        not isinstance(reg.capacity, int) or isinstance(reg.capacity, bool) or not 1 <= reg.capacity <= 10000
    ):
        problems.append("Capacity: must be an integer from 1 to 10,000")
    if not isinstance(reg.url, str) or not _http_url(reg.url):
        problems.append("RunnerURL: must be an absolute http or https URL")
    if reg.version is not None and (not isinstance(reg.version, str) or len(reg.version) > MAX_VERSION_CHARS):
        problems.append("Version: must be a string of at most 64 characters")
    if reg.static:
        if reg.health_url in (None, ""):
            problems.append("HealthURL: is missing; without it the node treats a static runner as always healthy")
        elif not isinstance(reg.health_url, str) or not _http_url(reg.health_url):
            problems.append("HealthURL: must be an absolute http or https URL")
    problems.extend(_price_problems(reg.price))
    return problems


def _endpoint_problems(envelope: Envelope, app: Any, mode: Any, registry: Registry) -> list[str]:
    problems = []
    for endpoint in envelope.endpoints.values():
        where = f"endpoints.{endpoint.name}"
        entry = registry.get(endpoint.profile)
        if entry is None:
            refused = registry.refused.get(endpoint.profile)
            why = f"is refused by the registry: {refused}" if refused else "isn't in the registry release"
            problems.append(f"{where}: profile {endpoint.profile} {why}")
            continue
        if entry.kind is not EntryKind.PROFILE:
            problems.append(f"{where}: {entry.id} is a shared entry, not a wire profile")
            continue
        if endpoint.name not in entry.operations:
            names = ", ".join(sorted(entry.operations)) or "none"
            problems.append(f"{where}: {endpoint.name} isn't an operationId in {entry.id} (it has {names})")
        if isinstance(app, str) and app and app not in entry.capabilities:
            problems.append(f"{where}: {entry.id} doesn't serve {app}; it serves {', '.join(entry.capabilities)}")
        effective_mode = mode or Mode.PERSISTENT.value
        if isinstance(app, str) and app and entry.mode is not None and entry.mode.value != effective_mode:
            problems.append(f"{where}: {entry.id} is {entry.mode.value}; the runner is {effective_mode}")
    return problems
