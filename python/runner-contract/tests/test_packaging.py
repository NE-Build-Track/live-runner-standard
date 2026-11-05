"""The packages ship the root's files: one build hook for both, and the standard's bytes unchanged."""

from __future__ import annotations

import json
from pathlib import Path

from livepeer_runner_contract import capabilities, standard

PYTHON = Path(__file__).resolve().parents[2]
ROOT = PYTHON.parent


def test_both_packages_share_one_build_hook() -> None:
    hooks = [(PYTHON / name / "hatch_build.py").read_bytes() for name in ("runner-contract", "runner-registry")]
    assert hooks[0] == hooks[1], "python/*/hatch_build.py must stay the same file"


def test_the_embedded_standard_is_the_roots() -> None:
    root_schema = json.loads((ROOT / "schemas/envelope.v1.schema.json").read_text(encoding="utf-8"))
    assert standard.envelope_schema() == root_schema
    root_caps = json.loads((ROOT / "capabilities.json").read_text(encoding="utf-8"))["capabilities"]
    assert [c.name for c in capabilities().values()] == [c["name"] for c in root_caps]
