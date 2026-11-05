"""The parts of runner contract v1 this package carries: its constants, the envelope schema and the capability list.

The build copies ``schemas/envelope.v1.schema.json`` and ``capabilities.json`` from the repository root
into the package (see hatch_build.py), so this release of the library implements the standard as it
stood at its tag. A registry release carries its own capability list, which ``Registry`` prefers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

RUNNER_CONTRACT = 1
"""The ``runner_contract`` version this library reads and the tool writes."""

REPOSITORY = "https://github.com/NE-Build-Track/live-runner-standard"
MAX_METADATA_BYTES = 1024
MAX_VERSION_CHARS = 64
PROFILE_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+(-[a-z0-9]+)*)+\.v[1-9][0-9]*$")
ENDPOINT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

_PACKAGED = Path(__file__).resolve().parent / "data"


class Mode(StrEnum):
    SINGLE_SHOT = "single-shot"
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class Capability:
    """One entry of the capability list."""

    name: str
    group: str
    mode: Mode
    summary: str


def _standard_file(name: str, checkout_path: str) -> Path:
    packaged = _PACKAGED / name
    if packaged.is_file():
        return packaged
    # An editable install or a source tree carries no copy; read the checkout's own root.
    parents = Path(__file__).resolve().parents
    if len(parents) > 4 and (parents[4] / checkout_path).is_file():
        return parents[4] / checkout_path
    raise FileNotFoundError(
        f"{name} is missing from this install of livepeer-runner-contract; reinstall it from a tag of {REPOSITORY}"
    )


@cache
def envelope_schema() -> Mapping[str, Any]:
    """The envelope's JSON Schema, ``schemas/envelope.v1.schema.json``."""
    path = _standard_file("envelope.v1.schema.json", "schemas/envelope.v1.schema.json")
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


@cache
def envelope_validator() -> Draft202012Validator:
    return Draft202012Validator(envelope_schema())


@cache
def conformance_report_validator() -> Draft202012Validator:
    """A validator for ``schemas/conformance-report.v1.schema.json``, which ``conform`` checks its reports with."""
    path = _standard_file("conformance-report.v1.schema.json", "schemas/conformance-report.v1.schema.json")
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def parse_capabilities(doc: Any) -> dict[str, Capability]:
    """The capabilities in a ``capabilities.json`` document, by name; malformed items are skipped."""
    out: dict[str, Capability] = {}
    items = doc.get("capabilities") if isinstance(doc, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name, group, mode, summary = (item.get(k) for k in ("name", "group", "mode", "summary"))
        if isinstance(name, str) and isinstance(group, str) and mode in set(Mode) and isinstance(summary, str):
            out[name] = Capability(name, group, Mode(mode), summary)
    return out


@cache
def capabilities() -> Mapping[str, Capability]:
    """The capability list as of this library's release."""
    path = _standard_file("capabilities.json", "capabilities.json")
    return parse_capabilities(json.loads(path.read_text(encoding="utf-8")))
