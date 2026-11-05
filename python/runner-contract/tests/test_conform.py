"""conform: the suite format's checks, and the command as a builder runs it."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from conftest import write_release
from livepeer_runner_contract import Registry
from livepeer_runner_contract.cli import main
from livepeer_runner_contract.conform import SuiteError, available_suites, load_common, load_suite
from reference.profiles import BEHAVIOURS, manifest_for
from reference.server import ReferenceRunner, Settings

ROOT = Path(__file__).resolve().parents[3]
TEXT = ["openai.chat.v1", "openai.responses.stateless.v1", "anthropic.messages.v1"]

ECHO_SUITE: dict[str, Any] = {
    "conformance_suite": 1,
    "profile": "zio.echo.v1",
    "version": "1",
    "cancel": "echo.json",
    "cases": [
        {
            "id": "echo.json",
            "endpoint": "echo",
            "request": {"text": "hi"},
            "expect": {"framing": "json"},
            "content": [{"in": "body", "pointer": "/text"}],
        },
        {
            "id": "echo.invalid",
            "endpoint": "echo",
            "request": {},
            "valid": False,
            "expect": {"kinds": ["input_invalid"]},
        },
    ],
}


def release_with_suite(root: Path, suite: Any, profile: str = "zio.echo.v1") -> Registry:
    write_release(root)
    namespace, _, name = profile.partition(".")
    (root / "conformance" / namespace / name).mkdir(parents=True)
    (root / "conformance" / namespace / name / "suite.json").write_text(json.dumps(suite))
    shutil.copytree(ROOT / "conformance" / "common", root / "conformance" / "common")
    return Registry.load(root)


def test_a_suite_loads(tmp_path: Path) -> None:
    suite = load_suite(release_with_suite(tmp_path, ECHO_SUITE), "zio.echo.v1")
    assert [c.id for c in suite.cases] == ["echo.json", "echo.invalid"]
    assert suite.cases[1].valid is False and suite.cancel == "echo.json"
    assert available_suites(tmp_path) == ["zio.echo.v1"]


def broken(change: Any) -> dict[str, Any]:
    suite = copy.deepcopy(ECHO_SUITE)
    change(suite)
    return suite


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: s["cases"][0].update(endpoint="shout"), "endpoint 'shout' isn't an operation of zio.echo.v1"),
        (lambda s: s["cases"][0].update(expect={"framing": "sse"}), "echo doesn't answer with framing 'sse'"),
        (lambda s: s["cases"][1].update(expect={"kinds": ["oops"]}), "kinds must be a list of base kinds"),
        (lambda s: s["cases"][1].update(expect={"kinds": ["busy"], "framing": "json"}), "expect must be"),
        (lambda s: s["cases"][0].update(limit={"name": "context_tokens"}), 'needs a "$fill" string'),
        (lambda s: s["cases"][0].update(limit={"input": "/nope"}, request={"text": "$fill"}), "has no input at /nope"),
        (lambda s: s["cases"][0].update(limit={"name": "tokens"}), "limit must be"),
        (lambda s: s["cases"][0].update(golden=True), "golden isn't a case key"),
        (lambda s: s["cases"][1].update(id="echo.json"), "case id echo.json is used twice"),
        (lambda s: s.update(cancel="echo.nope"), "cancel names 'echo.nope'"),
        (lambda s: s.update(profile="zio.other.v1"), "profile must be zio.echo.v1"),
        (lambda s: s.update(version=""), "version must be"),
        (lambda s: s.update(cases=[]), "cases must be a non-empty list"),
        (lambda s: s.update(extra=1), "extra isn't a suite key"),
        (lambda s: s["cases"][0].update(content=[{"in": "header", "pointer": "/x"}]), "each content item needs"),
        (lambda s: s["cases"][0].update(work_reports="yes"), "work_reports must be true or false"),
    ],
)
def test_a_broken_suite_names_each_problem(tmp_path: Path, change: Any, expected: str) -> None:
    registry = release_with_suite(tmp_path, broken(change))
    with pytest.raises(SuiteError) as caught:
        load_suite(registry, "zio.echo.v1")
    assert any(expected in p for p in caught.value.problems), caught.value.problems


def test_only_wire_profiles_have_suites(tmp_path: Path) -> None:
    registry = release_with_suite(tmp_path, {**ECHO_SUITE, "profile": "livepeer.errors.v1"}, "livepeer.errors.v1")
    with pytest.raises(SuiteError, match="shared entry"):
        load_suite(registry, "livepeer.errors.v1")


def test_a_broken_common_suite(tmp_path: Path) -> None:
    registry = release_with_suite(tmp_path, ECHO_SUITE)
    path = tmp_path / "conformance/common/suite.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "keepalive_s": 0}))
    with pytest.raises(SuiteError, match="keepalive_s must be a positive number"):
        load_common(registry)
    path.write_text("[]")
    with pytest.raises(SuiteError, match="conformance_common"):
        load_common(registry)


def test_a_missing_suite(tmp_path: Path) -> None:
    write_release(tmp_path)
    with pytest.raises(SuiteError, match="doesn't exist"):
        load_suite(Registry.load(tmp_path), "zio.echo.v1")


# The command


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(ROOT)


@pytest.fixture
def text_runner(registry: Registry) -> Iterator[ReferenceRunner]:
    with ReferenceRunner([BEHAVIOURS[p] for p in TEXT], registry) as runner:
        yield runner


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    status = main(["conform", "--registry", str(ROOT), "--cancel-settle", "0.1", *argv])
    return status, capsys.readouterr().err


def read(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(path.read_text())
    schema = json.loads((ROOT / "schemas/conformance-report.v1.schema.json").read_text())
    assert not list(Draft202012Validator(schema).iter_errors(report))
    return report


def test_conform_writes_one_report_per_profile(
    tmp_path: Path, registry: Registry, text_runner: ReferenceRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(TEXT, text_runner.url, registry))
    status, err = run(capsys, "--manifest", str(manifest), "--out", str(tmp_path / "report.json"))
    assert status == 0, err
    for profile in TEXT:
        report = read(tmp_path / f"report.{profile}.json")
        assert report["pass"] and report["profile"] == profile and report["runner_id"] == "reference-runner"
        assert report["suite"] == {"id": profile, "version": "1+common.1"}
        assert report["tool"]["name"] == "runner-contract" and "registry_release" not in report
        assert f"{profile}: pass" in err


def test_conform_one_profile_from_a_block_checks_the_revision(
    tmp_path: Path, registry: Registry, text_runner: ReferenceRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(TEXT, text_runner.url, registry))
    assert main(["block", str(manifest)]) == 0
    block = tmp_path / "block.toml"
    block.write_text(capsys.readouterr().out)
    out = tmp_path / "chat.json"
    status, err = run(
        capsys, "--block", str(block), "--manifest", str(manifest), "--profile", "openai.chat.v1", "--out", str(out)
    )
    assert status == 0, err
    checks = {c["id"]: c for c in read(out)["checks"]}
    assert checks["common.revision"]["detail"] == "the manifest recomputes the envelope's deployment_revision"
    assert checks["common.registration"]["result"] == "pass"

    other = tmp_path / "other.toml"
    other.write_text(manifest.read_text().replace("context_tokens = 4096 }", "context_tokens = 8192 }"))
    status, err = run(
        capsys, "--block", str(block), "--manifest", str(other), "--profile", "openai.chat.v1", "--out", str(out)
    )
    assert status == 1 and "fail  common.revision" in err


def test_conform_from_an_envelope_with_overrides(
    tmp_path: Path, registry: Registry, text_runner: ReferenceRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(TEXT, "http://runner.invalid:9", registry))
    assert main(["envelope", str(manifest)]) == 0
    envelope = tmp_path / "envelope.json"
    envelope.write_text(capsys.readouterr().out)
    argv = ["--envelope", str(envelope), "--url", text_runner.url, "--health-url", text_runner.health_url]
    status, err = run(capsys, *argv, "--profile", "anthropic.messages.v1", "--out", str(tmp_path / "r.json"))
    assert status == 2 and "no runner ID" in err
    argv += ["--runner-id", "rig-7", "--model", "some-model"]
    status, err = run(capsys, *argv, "--profile", "anthropic.messages.v1", "--out", str(tmp_path / "r.json"))
    assert status == 0, err
    report = read(tmp_path / "r.json")
    assert report["runner_id"] == "rig-7"
    assert {c["id"]: c["result"] for c in report["checks"]}["common.revision"] == "skip"


def test_conform_fails_a_broken_runner(tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(faults=frozenset({"no_final"}))
    with ReferenceRunner([BEHAVIOURS["openai.chat.v1"]], registry, settings) as runner:
        manifest = tmp_path / "runner.toml"
        manifest.write_text(manifest_for(["openai.chat.v1"], runner.url, registry))
        status, err = run(capsys, "--manifest", str(manifest), "--out", str(tmp_path / "r.json"))
    assert status == 1
    assert "fail  chat_completions.sse.answer" in err and "openai.chat.v1: FAIL" in err
    assert read(tmp_path / "r.json")["pass"] is False


def test_conform_needs_a_suite_and_a_runner(
    tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(["openai.chat.v1"], "http://127.0.0.1:9", registry))
    status, err = run(capsys, "--manifest", str(manifest), "--profile", "openai.speech.v1")
    assert status == 2 and "has no conformance suite for openai.speech.v1" in err
    status, err = run(capsys)
    assert status == 2 and "pass --manifest, --block or --envelope" in err


def test_an_unreachable_runner_fails_every_check_it_reaches(
    tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(["openai.embeddings.v1"], "http://127.0.0.1:9", registry))
    status, _ = run(capsys, "--manifest", str(manifest), "--out", str(tmp_path / "r.json"), "--timeout", "2")
    assert status == 1
    checks = {c["id"]: c for c in read(tmp_path / "r.json")["checks"]}
    assert checks["common.health.ready"]["result"] == "fail"
    assert checks["embeddings.json.answer"]["detail"].startswith("no answer: ConnectionRefusedError")


def test_a_release_directory_names_the_release(
    tmp_path: Path, registry: Registry, text_runner: ReferenceRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    release = tmp_path / "registry-2026.11.7"
    for name in ("registry", "schemas", "conformance"):
        shutil.copytree(ROOT / name, release / name)
    shutil.copy(ROOT / "capabilities.json", release / "capabilities.json")
    manifest = tmp_path / "runner.toml"
    manifest.write_text(manifest_for(["openai.chat.v1"], text_runner.url, registry))
    status = main(
        [
            "conform",
            "--registry",
            str(release),
            "--cancel-settle",
            "0.1",
            "--manifest",
            str(manifest),
            "--out",
            str(tmp_path / "r.json"),
        ]
    )
    assert status == 0, capsys.readouterr().err
    assert read(tmp_path / "r.json")["registry_release"] == "2026.11.7"
    assert Registry.load(release / "registry").release == "2026.11.7"
