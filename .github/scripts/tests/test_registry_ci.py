"""Tests for the CI checks, on small repositories built in a temporary directory."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import registry_ci as ci
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
DIGEST = "sha256:" + "ab" * 32

ECHO: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "zio.echo.v1", "version": "1"},
    "paths": {
        "/v1/echo": {
            "post": {
                "operationId": "echo",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/EchoRequest"},
                            "examples": {"hello": {"value": {"text": "hi"}}},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The echo",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/EchoResponse"},
                                "examples": {"hello": {"value": {"text": "hi", "characters": 2}}},
                            }
                        },
                    }
                },
                "x-livepeer": {
                    "framing": "json",
                    "success": {"status": [200]},
                    "errors": [{"status": 400, "kind": "input_invalid"}, {"status": 503, "kind": "unavailable"}],
                    "work_reports": [{"measure": "characters", "in": "body", "pointer": "/characters"}],
                    "inputs": [{"field": "/text", "kind": "inline", "max_bytes": 1000}],
                },
            }
        }
    },
    "components": {
        "schemas": {
            "EchoRequest": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string", "maxLength": 1000, "x-media": {"by": "inline"}}},
                "additionalProperties": False,
            },
            "EchoResponse": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "characters": {"type": "integer"}},
            },
        }
    },
    "x-livepeer": {
        "registry_entry": 1,
        "profile": "zio.echo.v1",
        "kind": "profile",
        "mode": "single-shot",
        "capabilities": ["chat"],
        "admission": {"default": "candidate"},
        "attempts": "gateway",
    },
}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> ci.Repo:
    for name in ("schemas", "capabilities.json", "CODEOWNERS", "registry"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, tmp_path / name)
        else:
            shutil.copy(source, tmp_path / name)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    git(tmp_path, "config", "user.name", "CI")
    return ci.Repo(tmp_path)


def write(repo: ci.Repo, rel: str, doc: Any) -> None:
    path = repo.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def with_echo(repo: ci.Repo, doc: dict[str, Any] | None = None) -> None:
    write(repo, "registry/zio/echo.v1.json", doc if doc is not None else ECHO)
    (repo.root / ci.INDEX).write_text(ci.render_index(ci.build_index(repo)), encoding="utf-8")


def release(repo: ci.Repo, tag: str = "2026.11.1") -> None:
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "release")
    git(repo.root, "tag", tag)


def all_failures(repo: ci.Repo) -> list[str]:
    return [f for check in ci.CHECKS.values() for f in check(repo)]


def test_this_repository_passes() -> None:
    repo = ci.Repo(ROOT)
    for name in ("schemas", "index", "names", "capabilities"):
        assert ci.CHECKS[name](repo) == [], name


def test_a_good_entry_passes(repo: ci.Repo) -> None:
    with_echo(repo)
    assert all_failures(repo) == []
    index = json.loads((repo.root / ci.INDEX).read_text())
    assert index["entries"]["zio.echo.v1"]["steward"] == ["@NE-Build-Track/cloud-spe"]
    assert index["entries"]["zio.echo.v1"]["status"] == "active"


def test_an_example_must_match_its_schema(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["paths"]["/v1/echo"]["post"]["requestBody"]["content"]["application/json"]["examples"]["hello"]["value"] = {
        "text": 5
    }
    with_echo(repo, doc)
    assert any("example hello" in f for f in ci.check_schemas(repo))


def test_operations_need_x_livepeer_and_an_operation_id(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    op = doc["paths"]["/v1/echo"]["post"]
    del op["x-livepeer"]
    op["operationId"] = "Echo"
    with_echo(repo, doc)
    failures = ci.check_schemas(repo)
    assert any("operationId" in f for f in failures)
    assert any("has no x-livepeer" in f for f in failures)


def test_unknown_error_kinds_and_bad_x_media_fail(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["paths"]["/v1/echo"]["post"]["x-livepeer"]["errors"].append({"status": 500, "kind": "oops"})
    doc["components"]["schemas"]["EchoRequest"]["properties"]["text"]["x-media"] = {"by": "carrier-pigeon"}
    with_echo(repo, doc)
    failures = ci.check_schemas(repo)
    assert any("x-livepeer" in f and "oops" in f for f in failures)
    assert any("x-media" in f for f in failures)


def test_a_shared_entry_has_no_paths(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["x-livepeer"] = {"registry_entry": 1, "profile": "zio.echo.v1", "kind": "shared"}
    with_echo(repo, doc)
    assert any("shared entry" in f for f in ci.check_schemas(repo))


def test_the_id_must_match_the_path(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["x-livepeer"]["profile"] = "zio.other.v1"
    with_echo(repo, doc)
    assert any("its path makes it zio.echo.v1" in f for f in ci.check_names(repo))


def test_bad_file_names_fail(repo: ci.Repo) -> None:
    with_echo(repo)
    write(repo, "registry/zio/Echo.json", ECHO)
    write(repo, "registry/zio/deep/echo.v1.json", ECHO)
    failures = ci.check_names(repo)
    assert any("Echo isn't a valid ID" in f for f in failures)
    assert any("deep" in f for f in failures)


def test_a_namespace_needs_a_codeowners_line(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["x-livepeer"]["profile"] = "acme.echo.v1"
    write(repo, "registry/acme/echo.v1.json", doc)
    assert any("registry/acme/: has no CODEOWNERS line" in f for f in ci.check_names(repo))


def test_capabilities_and_extends_must_exist(repo: ci.Repo) -> None:
    doc = copy.deepcopy(ECHO)
    doc["x-livepeer"]["capabilities"] = ["teleport", "live-transcode"]
    doc["x-livepeer"]["extends"] = "zio.base.v1"
    with_echo(repo, doc)
    failures = ci.check_names(repo)
    assert any("teleport isn't on the capability list" in f for f in failures)
    assert any("live-transcode is persistent" in f for f in failures)
    assert any("extends zio.base.v1" in f for f in failures)


def test_the_index_must_be_regenerated(repo: ci.Repo) -> None:
    with_echo(repo)
    doc = copy.deepcopy(ECHO)
    doc["info"]["version"] = "2"
    write(repo, "registry/zio/echo.v1.json", doc)
    assert ci.check_index(repo) != []


def test_aliases_and_deprecations_come_from_the_namespace_file(repo: ci.Repo) -> None:
    write(
        repo, "registry/zio/namespace.json", {"deprecated": ["zio.echo.v1"], "aliases": {"zio.say.v1": "zio.echo.v1"}}
    )
    with_echo(repo)
    assert all_failures(repo) == []
    entry = json.loads((repo.root / ci.INDEX).read_text())["entries"]["zio.echo.v1"]
    assert entry["status"] == "deprecated"
    assert entry["aliases"] == ["zio.say.v1"]


def test_bad_aliases_fail(repo: ci.Repo) -> None:
    write(
        repo,
        "registry/zio/namespace.json",
        {"aliases": {"zio.echo.v1": "zio.echo.v1", "fal.x.v1": "zio.echo.v1", "zio.y.v1": "zio.none.v1"}},
    )
    with_echo(repo)
    failures = ci.check_names(repo)
    assert any("already an entry's ID" in f for f in failures)
    assert any("fal.x.v1 must be a valid ID in this namespace" in f for f in failures)
    assert any("zio.none.v1, which isn't in the registry" in f for f in failures)


def test_released_entries_never_change(repo: ci.Repo) -> None:
    with_echo(repo)
    release(repo)
    assert ci.check_immutability(repo) == []
    doc = copy.deepcopy(ECHO)
    doc["info"]["version"] = "2"
    with_echo(repo, doc)
    failures = ci.check_immutability(repo)
    assert any("changed after release 2026.11.1" in f for f in failures)
    assert any("zio.echo.v1.sha256 changed" in f for f in failures)


def test_released_entries_are_never_removed(repo: ci.Repo) -> None:
    with_echo(repo)
    release(repo)
    (repo.root / "registry/zio/echo.v1.json").unlink()
    (repo.root / ci.INDEX).write_text(ci.render_index(ci.build_index(repo)), encoding="utf-8")
    failures = ci.check_immutability(repo)
    assert any("is gone" in f for f in failures)


def test_status_and_aliases_may_change_after_release(repo: ci.Repo) -> None:
    with_echo(repo)
    release(repo)
    write(
        repo, "registry/zio/namespace.json", {"deprecated": ["zio.echo.v1"], "aliases": {"zio.say.v1": "zio.echo.v1"}}
    )
    with_echo(repo)
    assert all_failures(repo) == []


def test_the_latest_release_is_compared(repo: ci.Repo) -> None:
    with_echo(repo)
    release(repo, "2026.9.3")
    doc = copy.deepcopy(ECHO)
    doc["info"]["version"] = "2"
    write(repo, "registry/zio/echo.v1.json", doc)
    release(repo, "2026.11.1")  # releases never do this; it moves the baseline for the test
    assert ci.latest_release(repo) == "2026.11.1"
    assert not any("echo.v1.json: changed" in f for f in ci.check_immutability(repo))


def test_capability_list_rules(repo: ci.Repo) -> None:
    data = json.loads((repo.root / "capabilities.json").read_text())
    data["capabilities"] += [
        {"name": "chat", "group": "text", "mode": "single-shot", "summary": "Again"},
        {"name": "Big-Chat", "group": "text", "mode": "single-shot", "summary": "Shouting"},
        {"name": "live-chat", "group": "live", "mode": "single-shot", "summary": "Live but not"},
        {"name": "poetry", "group": "verse", "mode": "single-shot", "summary": ""},
    ]
    write(repo, "capabilities.json", data)
    failures = ci.check_capabilities(repo)
    assert any("chat is listed twice" in f for f in failures)
    assert any("'Big-Chat' must be lowercase" in f for f in failures)
    assert any("live-* capabilities, and only they" in f for f in failures)
    assert any("group must be one of" in f for f in failures)
    assert any("summary must be" in f for f in failures)


def test_the_envelope_schema() -> None:
    schema = json.loads((ROOT / "schemas/envelope.v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    envelope = {
        "runner_contract": 1,
        "deployment_revision": DIGEST,
        "model": {"name": "qwen3.8-27b", "source": "hf:Qwen/Qwen3.8-27B-AWQ", "revision": "0" * 40},
        "endpoints": {
            "chat_completions": {"profile": "openai.chat.v1"},
            "messages": {"profile": "anthropic.messages.v1"},
        },
        "limits": {"context_tokens": 131072},
        "a_later_optional_key": {"ignored": True},
    }
    assert list(validator.iter_errors(envelope)) == []
    assert not validator.is_valid({**envelope, "runner_contract": 2})
    assert not validator.is_valid({**envelope, "endpoints": {}})
    assert not validator.is_valid({**envelope, "deployment_revision": "sha256:XYZ"})
    assert not validator.is_valid({**envelope, "endpoints": {"chat": {"profile": "OpenAI.Chat"}}})
    assert validator.is_valid({**envelope, "admission": "something-new"})  # readers treat it as private


def test_the_conformance_report_schema() -> None:
    schema = json.loads((ROOT / "schemas/conformance-report.v1.schema.json").read_text())
    report = {
        "conformance_report": 1,
        "runner_id": "qwen38-rig2",
        "deployment_revision": DIGEST,
        "profile": "openai.chat.v1",
        "registry_release": "2026.11.1",
        "suite": {"id": "openai.chat.v1", "version": "1"},
        "tool": {"name": "runner-contract", "version": "0.2.0"},
        "started_at": "2026-11-05T12:00:00Z",
        "finished_at": "2026-11-05T12:01:30Z",
        "checks": [{"id": "health", "result": "pass"}, {"id": "cancellation", "result": "skip"}],
        "pass": True,
    }
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(report)) == []
    assert not validator.is_valid({**report, "signature": "nope"})


def test_the_spec_example_profile_ids_are_valid() -> None:
    for profile in (
        "openai.chat.v1",
        "openai.responses.stateless.v1",
        "anthropic.messages.v1",
        "typesafe.systemone.v1",
        "zio.vod-transcode.v1",
        "fal.ideogram-v4.v1",
        "fal.queue.v1",
        "livepeer.conformance-report.v1",
    ):
        assert ci.PROFILE_ID.match(profile), profile
    for bad in ("openai.chat", "openai.v1", "OpenAI.chat.v1", "openai.chat.v0", "open-ai.chat.v1", "openai..chat.v1"):
        assert not ci.PROFILE_ID.match(bad), bad
