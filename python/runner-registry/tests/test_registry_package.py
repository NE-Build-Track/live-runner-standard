"""The registry package finds its release's data, and reports its version honestly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import livepeer_runner_registry as package

ROOT = Path(__file__).resolve().parents[3]


def test_data_dir_holds_a_whole_release() -> None:
    data = package.data_dir()
    assert (data / "registry" / "index.json").is_file()
    assert (data / "capabilities.json").is_file()
    for name in ("envelope.v1.schema.json", "registry-entry.v1.schema.json", "conformance-report.v1.schema.json"):
        assert (data / "schemas" / name).is_file()


def test_data_matches_the_repository_root() -> None:
    """Whether installed from a wheel or read from the checkout, the bytes are the root's."""
    data = package.data_dir()
    for rel in ("capabilities.json", "schemas/envelope.v1.schema.json", "schemas/registry-entry.v1.schema.json"):
        assert (data / rel).read_bytes() == (ROOT / rel).read_bytes(), rel


def test_every_indexed_entry_is_present() -> None:
    """The packaged entries are the tree's bytes; whether the index's digests match is CI check 3's job."""
    data = package.data_dir()
    index = json.loads((data / "registry" / "index.json").read_text(encoding="utf-8"))
    for entry_id, fields in index["entries"].items():
        assert (data / fields["path"]).read_bytes() == (ROOT / fields["path"]).read_bytes(), entry_id


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2026.11.1", "2026.11.1"), ("2026.12.14", "2026.12.14"), ("2026.11.1.dev0", None), ("0+unknown", None)],
)
def test_release_is_only_a_release_tag(monkeypatch: pytest.MonkeyPatch, version: str, expected: str | None) -> None:
    monkeypatch.setattr(package, "__version__", version)
    assert package.release() == expected
