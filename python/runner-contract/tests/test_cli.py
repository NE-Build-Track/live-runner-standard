"""The runner-contract tool: init, envelope, block, check and draft, run as a builder would."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import write_release
from livepeer_runner_contract.cli import main

IMAGE = "vllm/vllm-openai@sha256:" + "1" * 64
MANIFEST = f"""
[runner]
id = "qwen38-rig2"
app = "chat"
mode = "single-shot"
capacity = 8
url = "http://vllm:8000"
health_url = "http://vllm:8000/health"
version = "vllm-0.32.0-qwen38"

[price]
amount = 1.00
currency = "usd"
unit = "hour"

[model]
name = "qwen3.8-27b"
source = "hf:Qwen/Qwen3.8-27B-AWQ"
revision = "{"a" * 40}"

[[endpoints]]
name = "chat_completions"
profile = "openai.chat.v1"

[revision]
images = ["{IMAGE}"]
settings = {{ max_model_len = 131072 }}
"""


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


@pytest.fixture
def work(tmp_path: Path, release_dir: Path) -> Path:
    (tmp_path / "runner.toml").write_text(MANIFEST)
    return tmp_path


def block(work: Path, capsys: pytest.CaptureFixture[str], manifest: str = MANIFEST, fmt: str = "toml") -> Path:
    (work / "m.toml").write_text(manifest)
    status, out, err = run(capsys, "block", str(work / "m.toml"), "--format", fmt)
    assert status == 0, err
    path = work / f"block.{fmt}"
    path.write_text(out)
    return path


def check(capsys: pytest.CaptureFixture[str], path: Path, release_dir: Path, *extra: str) -> tuple[int, list[str]]:
    status, out, _ = run(capsys, "check", str(path), "--registry", str(release_dir), *extra)
    return status, out.splitlines()


def test_envelope_prints_compact_json(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status, out, err = run(capsys, "envelope", str(work / "runner.toml"))
    assert status == 0
    assert json.loads(out)["runner_contract"] == 1 and out.count("\n") == 1
    assert "of 1,024 bytes" in err


def test_envelope_lists_every_problem(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (work / "bad.toml").write_text(
        MANIFEST.replace('app = "chat"', 'app = "whisper"').replace('unit = "hour"', 'unit = "minute"')
    )
    status, out, err = run(capsys, "envelope", str(work / "bad.toml"))
    assert status == 1 and out == ""
    assert len(err.splitlines()) == 2 and all(line.startswith(f"{work / 'bad.toml'}: ") for line in err.splitlines())


def test_a_missing_manifest_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status, _, err = run(capsys, "envelope", str(tmp_path / "nope.toml"))
    assert status == 2 and "No such file" in err


def test_the_generated_block_checks_clean(work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = block(work, capsys)
    status, lines = check(capsys, path, release_dir, "--manifest", str(work / "runner.toml"))
    assert status == 0, lines
    assert lines == [f"{path}: ok, against the registry at {release_dir}"]


def test_the_generated_registration_json_checks_clean(
    work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = block(work, capsys, fmt="json")
    assert check(capsys, path, release_dir, "--manifest", str(work / "runner.toml"))[0] == 0


def test_a_bare_envelope_checks_with_or_without_a_manifest(
    work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = run(capsys, "envelope", str(work / "runner.toml"))
    path = work / "envelope.json"
    path.write_text(out)
    assert check(capsys, path, release_dir)[0] == 0
    assert check(capsys, path, release_dir, "--manifest", str(work / "runner.toml"))[0] == 0
    other = work / "other.toml"
    other.write_text(MANIFEST.replace("131072", "65536"))
    status, lines = check(capsys, path, release_dir, "--manifest", str(other))
    assert status == 1 and len(lines) == 1 and "deployment_revision: is sha256:" in lines[0]


def edit_block(path: Path, old: str, new: str) -> Path:
    path.write_text(path.read_text().replace(old, new, 1))
    return path


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ('Mode = "single-shot"\n', "", "Mode: is missing, so the node makes it persistent; chat is single-shot"),
        ('App = "chat"', 'App = "blueclaw/chat/qwen"', "App: 'blueclaw/chat/qwen' isn't on the capability list"),
        (
            'Version = "vllm-0.32.0-qwen38"',
            f'Version = "{"v" * 65}"',
            "Version: must be a string of at most 64 characters",
        ),
        ("Price = 1.00", 'Price = "1.00"', "PriceInfo.Price must be a TOML number"),
        ("Price = 1.00", "Price = 0", "PriceInfo.Price: must be a positive number"),
        ('Unit = "hour"', 'Unit = "minute"', "PriceInfo.Unit: must be hour or fixed"),
        ('[Runners.PriceInfo]\nPrice = 1.00\nCurrency = "usd"\nUnit = "hour"\n', "", "PriceInfo: is missing"),
        ('HealthURL = "http://vllm:8000/health"\n', "", "HealthURL: is missing"),
        ("HealthCode = 200", "HealthCode = 200\nGpuCount = 2", "GpuCount isn't a key the node knows"),
        (
            '"chat_completions":',
            '"completions":',
            "endpoints.completions: completions isn't an operationId in openai.chat.v1",
        ),
        ('"openai.chat.v1"', '"openai.unknown.v1"', "profile openai.unknown.v1 isn't in the registry release"),
        ('"openai.chat.v1"', '"openai.speech.v1"', "openai.speech.v1 doesn't serve chat"),
        ('"runner_contract":1', '"runner_contract":2', "envelope: runner_contract is 2"),
        (
            '"deployment_revision":"sha256:',
            '"deployment_revision":"sha1:',
            "envelope: deployment_revision: doesn't match",
        ),
    ],
)
def test_check_finds_each_problem(
    work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, expected: str
) -> None:
    path = edit_block(block(work, capsys), old, new)
    status, lines = check(capsys, path, release_dir)
    assert status == 1
    assert any(expected in line for line in lines), lines
    assert all(line.startswith(f"{path}: ") for line in lines)


def test_check_case_folds_keys_as_the_node_does(
    work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = edit_block(block(work, capsys), "RunnerURL", "runnerurl")
    assert check(capsys, path, release_dir)[0] == 0


def test_check_finds_an_oversized_envelope(work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = edit_block(block(work, capsys), '"endpoints":', '"note":"' + "x" * 1000 + '","endpoints":')
    path = edit_block(path, '"openai.chat.v1"', '"openai.unknown.v1"')
    status, lines = check(capsys, path, release_dir)
    assert status == 1 and any("Metadata: is 1," in line and "the limit is 1,024" in line for line in lines), lines
    assert any("openai.unknown.v1 isn't in the registry release" in line for line in lines), lines  # still checked


def test_check_reads_each_runner_of_a_file(work: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = block(work, capsys)
    text = path.read_text()
    path.write_text(text + text.replace('ID = "qwen38-rig2"', 'ID = "second"').replace("Capacity = 8", "Capacity = 0"))
    status, lines = check(capsys, path, release_dir)
    assert status == 1 and lines == [f"{path}: second: Capacity: must be an integer from 1 to 10,000"]


def test_check_needs_a_registry(work: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = block(work, capsys)
    status, _, err = run(capsys, "check", str(path), "--registry", str(tmp_path / "none"))
    assert status == 2 and "holds neither" in err


def test_check_reports_a_refused_entry(work: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    release = write_release(tmp_path / "release")
    chat = release / "registry/openai/chat.v1.json"
    chat.write_text(chat.read_text() + " ")
    status, lines = check(capsys, block(work, capsys), release)
    assert status == 1 and any("is refused by the registry: its bytes don't match" in line for line in lines), lines


def test_check_explains_what_isnt_a_registration(
    tmp_path: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, text in (("a.json", '{"hello": 1}'), ("b.toml", "[Other]\nx = 1\n"), ("c.toml", "[[Runners")):
        (tmp_path / name).write_text(text)
        status, lines = check(capsys, tmp_path / name, release_dir)
        assert status == 1 and lines, name


def test_init_writes_a_starter_that_names_its_todos(
    tmp_path: Path, release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "runner.toml"
    status, _, err = run(
        capsys,
        "init",
        "--capability",
        "chat",
        "--profile",
        "openai.chat.v1",
        "--profile",
        "openai.models.v1",
        "--out",
        str(out),
        "--registry",
        str(release_dir),
    )
    assert status == 0 and "wrote" in err
    text = out.read_text()
    assert 'name = "chat_completions"' in text and 'name = "list_models"' in text
    assert 'mode = "single-shot"' in text
    status, _, err = run(capsys, "envelope", str(out))
    assert status == 1
    assert "runner.id: is still a TODO" in err and "[price] amount: must be a positive number" in err
    assert run(capsys, "init", "--capability", "chat", "--profile", "openai.chat.v1", "--out", str(out))[0] == 2


def test_init_leaves_session_controls_to_the_session_endpoint(
    release_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["init", "--capability", "live-transcode", "--profile", "zio.live.v1", "--out", "-"]
    status, out, _ = run(capsys, *argv, "--registry", str(release_dir))
    assert status == 0 and 'name = "session"' in out and 'name = "stop"' not in out


def test_init_for_a_persistent_capability(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "init", "--capability", "live-transcode", "--profile", "zio.live.v1", "--out", "-")
    assert status == 0
    assert 'mode = "persistent"' in out and "idle_timeout_s = 30" in out and 'name = "TODO"' in out


@pytest.mark.parametrize(("capability", "profile"), [("whisper", "openai.transcriptions.v1"), ("chat", "openai/chat")])
def test_init_refuses_what_isnt_in_the_standard(
    capsys: pytest.CaptureFixture[str], capability: str, profile: str
) -> None:
    assert run(capsys, "init", "--capability", capability, "--profile", profile, "--out", "-")[0] == 2


OPENAPI: dict[str, Any] = {
    "openapi": "3.0.2",
    "info": {"title": "Whisper server", "version": "0.1.0"},
    "paths": {
        "/v1/audio/transcriptions": {
            "post": {
                "operationId": "transcribeAudio",
                "summary": "Transcribe",
                "requestBody": {"content": {"multipart/form-data": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "object"}}, "text/event-stream": {}}},
                    "422": {"description": "Validation Error"},
                    "418": {"description": "Teapot"},
                },
                "security": [{"key": []}],
            }
        },
        "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {"schemas": {"Body": {"type": "object"}}, "securitySchemes": {"key": {"type": "http"}}},
}


def test_draft_from_openapi(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "openapi.json"
    source.write_text(json.dumps(OPENAPI))
    status, out, _ = run(capsys, "draft", "--openapi", str(source), "--profile", "zio.transcribe.v1")
    assert status == 0
    draft = json.loads(out)
    assert draft["openapi"] == "3.1.0" and "converted from OpenAPI 3.0.2" in draft["info"]["description"]
    assert draft["x-livepeer"]["profile"] == "zio.transcribe.v1" and draft["x-livepeer"]["mode"].startswith("TODO")
    op = draft["paths"]["/v1/audio/transcriptions"]["post"]
    assert op["operationId"] == "transcribe_audio" and "security" not in op
    x = op["x-livepeer"]
    assert x["framing"] == ["json", "sse"] and x["success"]["status"] == [200]
    assert x["errors"][0] == {"status": 422, "kind": "input_invalid", "note": "TODO: confirm this kind"}
    assert x["errors"][1]["kind"].startswith("TODO")
    assert draft["paths"]["/health"]["get"]["operationId"] == "get_health"
    assert "securitySchemes" not in draft["components"]


def test_draft_from_pydantic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("pydantic")
    (tmp_path / "runner_models.py").write_text(
        "from pydantic import BaseModel\n"
        "class Voice(BaseModel):\n    name: str\n"
        "class SpeakRequest(BaseModel):\n    text: str\n    voice: Voice\n"
        "class SpeakResponse(BaseModel):\n    url: str\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status, out, err = run(capsys, "draft", "--pydantic", "runner_models:SpeakRequest,runner_models:SpeakResponse")
    assert status == 0, err
    draft = json.loads(out)
    op = draft["paths"]["/TODO"]["post"]
    assert op["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SpeakRequest"}
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SpeakResponse"
    }
    assert set(draft["components"]["schemas"]) == {"SpeakRequest", "SpeakResponse", "Voice"}
    assert draft["x-livepeer"]["profile"].startswith("TODO")
    sys.modules.pop("runner_models", None)


def test_draft_problems_are_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "x.yaml").write_text("openapi: 3.1.0")
    assert run(capsys, "draft", "--openapi", str(tmp_path / "x.yaml"))[0] == 2
    assert run(capsys, "draft", "--pydantic", "no_such_module_here:Model")[0] == 2
    assert run(capsys, "draft", "--pydantic", "json")[0] == 2


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == "runner-contract 0.2.0"
