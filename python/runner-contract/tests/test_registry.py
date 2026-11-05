"""Registry.load: digests, aliases, extends, refusals, and the fixtures themselves."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from conftest import ENTRIES, ROOT, write_release
from livepeer_runner_contract import (
    Admission,
    Attempts,
    EntryKind,
    ErrorKind,
    Framing,
    Location,
    Mode,
    Registry,
    RegistryError,
    Retry,
    UnknownEndpoint,
    UnknownProfile,
)


def test_fixtures_follow_the_schema() -> None:
    schema = json.loads((ROOT / "schemas/registry-entry.v1.schema.json").read_text(encoding="utf-8"))
    root = Draft202012Validator(schema)
    operation = Draft202012Validator({"$ref": "#/$defs/operation", "$defs": schema["$defs"]})
    for rel, doc in ENTRIES.items():
        assert not [e.message for e in root.iter_errors(doc["x-livepeer"])], rel
        for item in doc.get("paths", {}).values():
            for method, op in item.items():
                if method != "parameters":
                    assert not [e.message for e in operation.iter_errors(op["x-livepeer"])], (rel, op["operationId"])


def test_fixtures_are_openapi_31() -> None:
    validator = pytest.importorskip("openapi_spec_validator")
    for doc in ENTRIES.values():
        validator.validate(doc)


def test_loads_every_entry(registry: Registry) -> None:
    assert sorted(e.id for e in registry) == sorted(d["x-livepeer"]["profile"] for d in ENTRIES.values())
    assert not registry.refused
    assert registry.capabilities is not None and registry.capabilities["chat"].mode is Mode.SINGLE_SHOT
    chat = registry.entry("openai.chat.v1")
    assert chat.kind is EntryKind.PROFILE and chat.mode is Mode.SINGLE_SHOT
    assert (
        chat.capabilities == ("chat",) and chat.admission is Admission.CANDIDATE and chat.attempts is Attempts.GATEWAY
    )
    op = registry.operation("openai.chat.v1", "chat_completions")
    assert (op.method, op.path, op.framings) == ("POST", "/v1/chat/completions", (Framing.JSON, Framing.SSE))
    assert op.success.status == frozenset({200})
    assert op.success.final[0].location is Location.LAST_EVENT and op.success.final[0].framing is Framing.SSE
    assert [m.kind for m in op.errors][:3] == [ErrorKind.INPUT_UNSUPPORTED, ErrorKind.INPUT_INVALID, ErrorKind.BUSY]
    assert registry.entry("livepeer.errors.v1").kind is EntryKind.SHARED


def test_aliases_resolve(registry: Registry) -> None:
    assert registry.canonical("openai.chatcompletions.v1") == "openai.chat.v1"
    assert registry.entry("openai.chatcompletions.v1").id == "openai.chat.v1"
    assert "openai.chatcompletions.v1" in registry
    assert registry.canonical("openai.nothing.v1") is None
    with pytest.raises(UnknownProfile):
        registry.entry("openai.nothing.v1")
    with pytest.raises(UnknownEndpoint):
        registry.operation("openai.chat.v1", "completions")


def test_extends_inherits_operations_and_root_keys(registry: Registry) -> None:
    flux = registry.entry("fal.flux.v1")
    assert flux.extends == "fal.queue.v1"
    assert sorted(flux.operations) == ["result", "run"]
    assert flux.operations["run"].defined_in == "fal.flux.v1"  # its own run overrides the base's
    assert flux.operations["result"].defined_in == "fal.queue.v1"
    assert flux.operations["run"].attempts is Attempts.CALLER
    assert flux.operations["result"].attempts is Attempts.GATEWAY  # the operation's own override
    assert flux.x["readiness"] == "The adapter holds the operator's fal key"
    assert flux.capabilities == ("text-to-image",)


def test_kinds_carry_the_fixed_retry_rules() -> None:
    assert {k: k.retry for k in ErrorKind} == {
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


def test_loads_a_registry_directory_itself(release_dir: Path) -> None:
    registry = Registry.load(release_dir / "registry")
    assert "openai.chat.v1" in registry
    assert registry.capabilities is not None  # beside registry/


def test_a_release_without_a_capability_list(tmp_path: Path) -> None:
    assert Registry.load(write_release(tmp_path, capabilities=False)).capabilities is None


def test_a_changed_entry_is_refused(tmp_path: Path) -> None:
    write_release(tmp_path)
    path = tmp_path / "registry/openai/chat.v1.json"
    path.write_text(path.read_text().replace('"busy"', '"internal"'))
    with pytest.raises(RegistryError, match=r"openai.chat.v1: its bytes don't match the sha256"):
        Registry.load(tmp_path)
    registry = Registry.load(tmp_path, strict=False)
    assert "openai.chat.v1" not in registry
    assert "openai.chatcompletions.v1" not in registry  # its alias goes with it
    assert "zio.echo.v1" in registry
    assert set(registry.refused) == {"openai.chat.v1"}


def test_an_entry_extending_a_refused_entry_is_refused(tmp_path: Path) -> None:
    write_release(tmp_path)
    (tmp_path / "registry/fal/queue.v1.json").write_text("{}")
    registry = Registry.load(tmp_path, strict=False)
    assert set(registry.refused) == {"fal.queue.v1", "fal.flux.v1"}


def _with(entries: dict[str, dict[str, Any]], rel: str, **x: Any) -> dict[str, dict[str, Any]]:
    entries[rel]["x-livepeer"].update(x)
    return entries


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda e: _with(e, "fal/queue.v1.json", extends="fal.flux.v1"), "loops"),
        (lambda e: _with(e, "fal/flux.v1.json", extends="fal.gone.v1"), "isn't in the release"),
        (lambda e: _with(e, "zio/echo.v1.json", profile="zio.other.v1"), "doesn't follow its ID"),
        (lambda e: _with(e, "zio/echo.v1.json", mode="sometimes"), "unknown mode"),
    ],
)
def test_broken_entries_are_refused(
    tmp_path: Path, entries: dict[str, dict[str, Any]], change: Any, reason: str
) -> None:
    write_release(tmp_path, change(entries))
    registry = Registry.load(tmp_path, strict=False)
    assert any(reason in why for why in registry.refused.values()), registry.refused


def test_a_pattern_python_cant_run_is_refused(tmp_path: Path, entries: dict[str, dict[str, Any]]) -> None:
    op = entries["openai/speech.v1.json"]["paths"]["/v1/audio/speech"]["post"]["x-livepeer"]
    op["errors"][0]["matches"] = "(unclosed"
    write_release(tmp_path, entries)
    assert "matches" in Registry.load(tmp_path, strict=False).refused["openai.speech.v1"]


def test_an_index_path_outside_registry_is_refused(tmp_path: Path) -> None:
    write_release(tmp_path)
    index_path = tmp_path / "registry/index.json"
    index = json.loads(index_path.read_text())
    index["entries"]["zio.echo.v1"]["path"] = "registry/../capabilities.json"
    index_path.write_text(json.dumps(index))
    assert "isn't under registry/" in Registry.load(tmp_path, strict=False).refused["zio.echo.v1"]


@pytest.mark.parametrize("index", ["not json", '{"registry_index": 2, "entries": {}}', "[]"])
def test_a_broken_index_raises(tmp_path: Path, index: str) -> None:
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry/index.json").write_text(index)
    with pytest.raises(RegistryError):
        Registry.load(tmp_path)


def test_a_directory_without_an_index_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="holds neither"):
        Registry.load(tmp_path)


def test_load_without_a_path_reads_the_installed_package() -> None:
    package = importlib.import_module("livepeer_runner_registry")
    registry = Registry.load(strict=False)
    assert registry.root == Path(package.data_dir())
    assert registry.release == package.release()


def test_load_without_a_path_or_the_package_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.import_module

    def missing(name: str, package: str | None = None) -> Any:
        if name == "livepeer_runner_registry":
            raise ImportError(name)
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(RegistryError, match=r"subdirectory=python/runner-registry"):
        Registry.load()
