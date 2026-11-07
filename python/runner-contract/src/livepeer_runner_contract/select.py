"""Runners from discovery, and selecting the endpoints that match what an app needs.

Selection is filtering, not policy: which runners an app trusts lives in the gateway's approvals,
which it passes in, and ranking, routes and retail rules stay in the gateway.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .envelope import (
    Admission,
    AttributeValue,
    Endpoint,
    Envelope,
    Ineligible,
    IneligibleReason,
    Reasoning,
    parse_envelope,
)
from .registry import EntryKind, Operation, Registry

__all__ = [
    "ApprovalKey",
    "Approvals",
    "Discovery",
    "IgnoredRunner",
    "Price",
    "Runner",
    "RunnerKey",
    "Selection",
    "parse_discovery",
    "select",
]


@dataclass(frozen=True)
class RunnerKey:
    """A runner's identity in discovery: its orchestrator's address and its runner ID."""

    orchestrator: str
    runner_id: str

    def __str__(self) -> str:
        return f"{self.runner_id} at {self.orchestrator}"


@dataclass(frozen=True)
class Price:
    """A discovery answer's ``price_info``, as the node reports it (wei per second, or fixed)."""

    price: str
    currency: str
    unit: str
    price_usd: str | None = None


@dataclass(frozen=True)
class Runner:
    """One runner from discovery, with its envelope read."""

    orchestrator: str
    runner_id: str
    app: str
    envelope: Envelope
    mode: str = ""
    url: str = ""
    capacity: int | None = None
    capacity_available: int | None = None
    version: str = ""
    """For information only: nothing selects on it."""
    price: Price | None = None

    @property
    def key(self) -> RunnerKey:
        return RunnerKey(self.orchestrator, self.runner_id)


@dataclass(frozen=True)
class IgnoredRunner:
    key: RunnerKey
    reason: Ineligible


@dataclass(frozen=True)
class Discovery:
    runners: tuple[Runner, ...]
    ignored: tuple[IgnoredRunner, ...]


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _price(value: Any) -> Price | None:
    if not isinstance(value, dict) or not value:
        return None

    def number(v: Any) -> str:
        return (
            v if isinstance(v, str) else json.dumps(v) if isinstance(v, int | float) and not isinstance(v, bool) else ""
        )

    usd = value.get("price_usd")
    return Price(number(value.get("price")), _str(value.get("currency")), _str(value.get("unit")), number(usd) or None)


def parse_discovery(answer: Any) -> Discovery:
    """Read a discovery answer: the parsed JSON, or its text.

    Each runner whose metadata isn't a valid v1 envelope is in ``ignored`` with the reason, for
    ``log_ignored``. Never raises: a malformed answer, entry or record is skipped.
    """
    if isinstance(answer, str | bytes):
        try:
            answer = json.loads(answer)
        except ValueError:
            return Discovery((), ())
    runners: list[Runner] = []
    ignored: list[IgnoredRunner] = []
    for entry in answer if isinstance(answer, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("address"), str) or not entry["address"]:
            continue  # a discovery service's own "nodes" entry, or junk
        orchestrator = entry["address"]
        records = entry.get("runners")
        for position, record in enumerate(records if isinstance(records, list) else []):
            if not isinstance(record, dict):
                continue
            parsed = parse_envelope(record.get("metadata"))
            runner_id = _str(record.get("runner_id"))
            if not runner_id and isinstance(parsed, Envelope) and parsed.runner_id:
                runner_id = parsed.runner_id  # the node didn't report one
            runner_id = runner_id or _str(record.get("url")) or f"#{position}"
            if isinstance(parsed, Ineligible):
                ignored.append(IgnoredRunner(RunnerKey(orchestrator, runner_id), parsed))
                continue
            app = _str(record.get("app"))
            if not app:
                reason = Ineligible(IneligibleReason.INVALID, "the registration has no app")
                ignored.append(IgnoredRunner(RunnerKey(orchestrator, runner_id), reason))
                continue
            runners.append(
                Runner(
                    orchestrator=orchestrator,
                    runner_id=runner_id,
                    app=app,
                    envelope=parsed,
                    mode=_str(record.get("mode")),
                    url=_str(record.get("url")),
                    capacity=_int(record.get("capacity")),
                    capacity_available=_int(record.get("capacity_available")),
                    version=_str(record.get("version")),
                    price=_price(record.get("price_info")),
                )
            )
    return Discovery(tuple(runners), tuple(ignored))


@dataclass(frozen=True)
class ApprovalKey:
    """What a gateway's approval names: one deployment of one runner, for one profile (its entry ID)."""

    orchestrator: str
    runner_id: str
    deployment_revision: str
    profile: str


Approvals = Container[ApprovalKey] | Callable[[ApprovalKey], bool]
"""A set of approval keys, or a function that answers for one; the gateway's policy, reloadable at will."""


@dataclass(frozen=True)
class Selection:
    """One endpoint of one runner that matches."""

    runner: Runner
    endpoint: Endpoint
    profile: str
    """The entry ID the endpoint speaks (an alias in the envelope is resolved)."""
    operation: Operation
    admission: Admission
    """``private`` when the envelope or the profile's entry says so (or the envelope says anything unknown)."""
    approval: ApprovalKey
    approved: bool

    @property
    def name(self) -> str:
        return self.endpoint.name

    @property
    def path(self) -> str:
        """The path to call: the envelope's, where it differs, or the entry's default."""
        return self.endpoint.path or self.operation.path

    @property
    def method(self) -> str:
        return self.endpoint.method or self.operation.method


def _approved(approvals: Approvals, key: ApprovalKey) -> bool:
    if callable(approvals):
        return bool(approvals(key))
    return key in approvals


def _attribute_matches(have: AttributeValue | None, want: AttributeValue) -> bool:
    if have is None:
        return False
    have_set = have if isinstance(have, tuple) else (have,)
    want_set = want if isinstance(want, tuple) else (want,)
    return all(any(_same(w, h) for h in have_set) for w in want_set)


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def select(
    runners: Iterable[Runner],
    capability: str,
    model: str | None = None,
    profile: str | None = None,
    attributes: Mapping[str, AttributeValue | list[str | int | float | bool]] | None = None,
    approvals: Approvals = frozenset(),
    *,
    registry: Registry,
    limits: Mapping[str, int | float] | None = None,
    approved_only: bool = False,
    reasoning: Reasoning | str | None = None,
    model_revision: str | None = None,
) -> list[Selection]:
    """The endpoints that match, one ``Selection`` each, in the order the runners came.

    A runner matches when its App ID is ``capability`` and, where given, its model's ``name`` or
    ``source`` is ``model``. Each of its endpoints then matches when:

    - its profile is in ``registry`` (directly or by alias), is a ``profile`` entry, serves
      ``capability``, and has an operation named like the endpoint;
    - it is ``profile``, where given (aliases resolve on both sides);
    - each of ``attributes`` is among the runner's values for that attribute;
    - each of ``limits`` is at most the runner's advertised limit (a runner that advertises no such
      limit doesn't match);
    - its ``reasoning.default`` is ``reasoning``, where given (a runner that declares none doesn't match);
    - its model's ``revision`` is ``model_revision``, where given;
    - it is approved, where the endpoint is ``private`` (by the envelope, or by the entry's default
      admission), or wherever ``approved_only`` is set. Approvals key on orchestrator, runner,
      ``deployment_revision`` and the profile's entry ID.
    """
    wanted_profile = None if profile is None else registry.canonical(profile)
    wanted_attributes: dict[str, AttributeValue] = {
        k: tuple(v) if isinstance(v, list | tuple) else v for k, v in (attributes or {}).items()
    }
    if profile is not None and wanted_profile is None:
        return []
    out: list[Selection] = []
    for runner in runners:
        envelope = runner.envelope
        if runner.app != capability:
            continue
        if model is not None and (envelope.model is None or model not in (envelope.model.name, envelope.model.source)):
            continue
        if not all(_attribute_matches(envelope.attributes.get(k), v) for k, v in wanted_attributes.items()):
            continue
        if reasoning is not None and envelope.reasoning != reasoning:
            continue
        if model_revision is not None and (envelope.model is None or envelope.model.revision != model_revision):
            continue
        if limits and not all(
            isinstance(envelope.limits.get(k), int | float) and envelope.limits[k] >= v for k, v in limits.items()
        ):
            continue
        for endpoint in envelope.endpoints.values():
            entry = registry.get(endpoint.profile)
            if entry is None or entry.kind is not EntryKind.PROFILE or capability not in entry.capabilities:
                continue
            if wanted_profile is not None and entry.id != wanted_profile:
                continue
            operation = entry.operations.get(endpoint.name)
            if operation is None:
                continue
            private = envelope.admission is Admission.PRIVATE or entry.admission is Admission.PRIVATE
            admission = Admission.PRIVATE if private else Admission.CANDIDATE
            key = ApprovalKey(runner.orchestrator, runner.runner_id, envelope.deployment_revision, entry.id)
            approved = _approved(approvals, key)
            if (private or approved_only) and not approved:
                continue
            out.append(Selection(runner, endpoint, entry.id, operation, admission, key, approved))
    return out
