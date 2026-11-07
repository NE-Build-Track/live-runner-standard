"""The manifest: what it builds (envelope, static block, dynamic registration) and what it refuses."""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

from livepeer_runner_contract import Envelope, capabilities, parse_envelope
from livepeer_runner_contract.manifest import (
    Manifest,
    ManifestError,
    build_envelope,
    parse_manifest,
    render_block_json,
    render_block_toml,
    render_envelope,
    revision_of,
)

IMAGE = "vllm/vllm-openai@sha256:" + "1" * 64
EXAMPLE = f"""
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

[limits]
context_tokens = 131072

[revision]
images = ["{IMAGE}"]
settings = {{ max_model_len = 131072 }}
"""


def manifest(text: str = EXAMPLE) -> Manifest:
    return parse_manifest(text, capabilities=capabilities())


def problems(text: str) -> list[str]:
    with pytest.raises(ManifestError) as caught:
        manifest(text)
    return list(caught.value.problems)


def test_the_example_builds_its_envelope() -> None:
    m = manifest()
    text = render_envelope(m)
    assert text.startswith('{"runner_contract":1,"deployment_revision":"sha256:')
    assert " " not in text
    envelope = parse_envelope(text)
    assert isinstance(envelope, Envelope)
    assert envelope.deployment_revision == revision_of(m)
    assert json.loads(text)["endpoints"] == {"chat_completions": {"profile": "openai.chat.v1"}}
    assert list(json.loads(text)) == [
        "runner_contract",
        "deployment_revision",
        "model",
        "endpoints",
        "runner_id",
        "limits",
    ]
    assert json.loads(text)["runner_id"] == "qwen38-rig2"  # the node reports no runner ID in discovery
    assert not m.warnings


def test_the_static_block_is_what_the_node_reads() -> None:
    m = manifest()
    block = render_block_toml(m)
    assert block.splitlines()[:10] == [
        "[[Runners]]",
        'ID = "qwen38-rig2"',
        'App = "chat"',
        'Mode = "single-shot"',
        "Capacity = 8",
        'RunnerURL = "http://vllm:8000"',
        'HealthURL = "http://vllm:8000/health"',
        "HealthCode = 200",
        'Status = "ready"',
        'Version = "vllm-0.32.0-qwen38"',
    ]
    assert block.splitlines()[10].startswith('Metadata = \'{"runner_contract":1,')
    assert block.splitlines()[11:] == ["[Runners.PriceInfo]", "Price = 1.00", 'Currency = "usd"', 'Unit = "hour"']
    runner = tomllib.loads(block)["Runners"][0]
    assert runner["Metadata"] == render_envelope(m)
    assert isinstance(runner["PriceInfo"]["Price"], float)  # a number: the node's json.Number can't take a string


def test_the_dynamic_registration_is_the_heartbeat_body() -> None:
    body = json.loads(render_block_json(manifest()))
    assert body == {
        "runner_id": "qwen38-rig2",
        "app": "chat",
        "mode": "single-shot",
        "capacity": 8,
        "runner_url": "http://vllm:8000",
        "status": "ready",
        "version": "vllm-0.32.0-qwen38",
        "metadata": render_envelope(manifest()),
        "price_info": {"price": 1.0, "currency": "usd", "unit": "hour"},
    }
    assert '"price": 1.00,' in render_block_json(manifest())


def test_a_quote_in_the_envelope_falls_back_to_a_basic_string() -> None:
    text = EXAMPLE + '\n[attributes]\nnote = "it\'s fine"\nweird = "tab\\there\\u007f"\n'
    m = manifest(text)
    block = render_block_toml(m)
    assert 'Metadata = "' in block
    assert tomllib.loads(block)["Runners"][0]["Metadata"] == render_envelope(m)


def test_every_extension_reaches_the_envelope() -> None:
    text = f"""
[runner]
id = "live-1"
app = "live-transcode"
mode = "persistent"
capacity = 2
url = "https://runner.example"
health_url = "https://runner.example/health"
admission = "private"

[price]
amount = 3
currency = "USD"
unit = "fixed"

[[endpoints]]
name = "session"
profile = "zio.live-transcode.v1"
path = "/v1/live/start"
method = "post"
stop = "/v1/live/stop"
channels = {{ in = ["source"], out = ["renditions-*", "events"] }}
idle_timeout_s = 30
drain_timeout_s = 10

[revision]
images = ["{IMAGE}"]
presets = ["h264-1080p", "av1-720p"]
settings = {{ encoder = "nvenc" }}

[runtime]
name = "ffmpeg"
image = "sha256:{"2" * 64}"
versions = {{ ffmpeg = "7.1" }}
encoder = "nvenc"

[capacity_group]
id = "gpu0"
slots = 2

[attributes]
presets = ["h264-1080p", "av1-720p"]
low_latency = true
"""
    m = manifest(text)
    envelope = build_envelope(m)
    assert envelope["endpoints"]["session"] == {
        "profile": "zio.live-transcode.v1",
        "path": "/v1/live/start",
        "method": "POST",
        "stop": "/v1/live/stop",
        "channels": {"in": ["source"], "out": ["renditions-*", "events"]},
        "idle_timeout_s": 30,
        "drain_timeout_s": 10,
    }
    assert envelope["admission"] == "private"
    assert envelope["runtime"] == {
        "name": "ffmpeg",
        "encoder": "nvenc",
        "image": "sha256:" + "2" * 64,
        "versions": {"ffmpeg": "7.1"},
    }
    assert envelope["capacity_group"] == {"id": "gpu0", "slots": 2}
    assert envelope["attributes"] == {"presets": ["h264-1080p", "av1-720p"], "low_latency": True}
    assert "model" not in envelope
    assert isinstance(parse_envelope(render_envelope(m)), Envelope)
    assert m.price_currency == "usd"
    assert "Price = 3\n" in render_block_toml(m)


def test_the_revision_follows_behavior_not_placement() -> None:
    base = revision_of(manifest())
    moved = EXAMPLE.replace('url = "http://vllm:8000"', 'url = "https://tunnel.example"').replace(
        "capacity = 8", "capacity = 16"
    )
    assert revision_of(manifest(moved.replace("amount = 1.00", "amount = 2.50"))) == base
    assert revision_of(manifest(EXAMPLE.replace("1" * 64, "9" * 64))) != base
    assert revision_of(manifest(EXAMPLE.replace("max_model_len = 131072", "max_model_len = 65536"))) != base
    assert revision_of(manifest(EXAMPLE.replace("a" * 40, "b" * 40))) != base
    twice = EXAMPLE + '\n[[endpoints]]\nname = "chat_completions_v2"\nprofile = "openai.chat.v1"\n'
    assert revision_of(manifest(twice)) == base  # a profile counts once


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda t: t.replace('app = "chat"', 'app = "blueclaw/chat/qwen"'),
            "[runner] app: 'blueclaw/chat/qwen' isn't on the capability list",
        ),
        (
            lambda t: t.replace('mode = "single-shot"', 'mode = "persistent"'),
            "[runner] mode: chat is a single-shot capability",
        ),
        (lambda t: t.replace('id = "qwen38-rig2"', 'id = "qwen 38"'), "[runner] id: must be 1 to 128"),
        (lambda t: t.replace("capacity = 8", "capacity = 0"), "[runner] capacity: must be an integer of at least 1"),
        (
            lambda t: t.replace('url = "http://vllm:8000"', 'url = "vllm:8000"'),
            "[runner] url: must be an absolute http or https URL",
        ),
        (
            lambda t: t.replace('version = "vllm-0.32.0-qwen38"', f'version = "{"v" * 65}"'),
            "[runner] version: is 65 characters",
        ),
        (lambda t: t.replace('unit = "hour"', 'unit = "second"'), "[price] unit: must be hour"),
        (lambda t: t.replace('currency = "usd"', 'currency = "eur"'), "[price] currency: must be usd"),
        (lambda t: t.replace("amount = 1.00", "amount = 0"), "[price] amount: must be a positive number"),
        (lambda t: t.replace("amount = 1.00", 'amount = "1.00"'), "[price] amount: must be a positive number"),
        (lambda t: t.replace(IMAGE, "vllm/vllm-openai:latest"), "must be pinned by digest"),
        (
            lambda t: t.replace(f'images = ["{IMAGE}"]', "images = []"),
            "[revision] images: needs at least one image digest",
        ),
        (
            lambda t: t.replace('name = "chat_completions"', 'name = "chat-completions"'),
            "name: must be the operationId",
        ),
        (
            lambda t: t.replace('profile = "openai.chat.v1"', 'profile = "openai/chat"'),
            "profile: must be a registry ID",
        ),
        (
            lambda t: t.replace('profile = "openai.chat.v1"', 'profile = "openai.chat.v1"\nidle_timeout_s = 30'),
            "idle_timeout_s are for persistent runners only",
        ),
        (lambda t: t + "\n[gpu]\nid = 0\n", "gpu: isn't a manifest table"),
        (lambda t: t.replace("[price]", "[price]\nprice_usd = 2"), "[price] price_usd: isn't a manifest key"),
        (lambda t: t.replace('id = "qwen38-rig2"', 'id = "TODO"'), "runner.id: is still a TODO"),
        (
            lambda t: t.replace("settings = { max_model_len = 131072 }", "settings = { built = 2026-11-05 }"),
            "has no JSON form",
        ),
        (
            lambda t: t.replace("context_tokens = 131072", "context_tokens = true"),
            "[limits] context_tokens: must be a number",
        ),
        (lambda t: "[runner\n" + t, "isn't valid TOML"),
    ],
)
def test_each_problem_is_one_line(change: Any, expected: str) -> None:
    lines = problems(change(EXAMPLE))
    assert any(expected in line for line in lines), lines


def test_every_missing_key_is_reported_at_once() -> None:
    lines = problems("[runner]\n")
    for expected in (
        "[runner] id: is missing",
        "[runner] app: is missing",
        "[price]: is missing",
        "[[endpoints]]: needs at least one",
        "[revision]: is missing",
    ):
        assert any(expected in line for line in lines), (expected, lines)


def test_warnings_for_an_unpinned_model_and_float_settings() -> None:
    text = EXAMPLE.replace(f'revision = "{"a" * 40}"\n', "").replace("max_model_len = 131072", "temperature = 0.7")
    warnings = manifest(text).warnings
    assert any("model] revision" in w for w in warnings) and any("float" in w for w in warnings)


def test_a_static_block_needs_a_health_url() -> None:
    m = manifest(EXAMPLE.replace('health_url = "http://vllm:8000/health"\n', ""))
    with pytest.raises(ManifestError, match="HealthURL"):
        render_block_toml(m)
    assert json.loads(render_block_json(m))["runner_id"] == "qwen38-rig2"  # a dynamic runner heartbeats instead


def test_an_envelope_over_1024_bytes_is_refused() -> None:
    with pytest.raises(ManifestError, match="the limit is 1,024"):
        render_envelope(manifest(EXAMPLE + f'\n[attributes]\nnote = "{"x" * 900}"\n'))


def test_the_reasoning_default_reaches_the_envelope() -> None:
    envelope = parse_envelope(render_envelope(manifest(EXAMPLE + '\n[reasoning]\ndefault = "off"\n')))
    assert isinstance(envelope, Envelope) and envelope.reasoning == "off"
    assert "reasoning" not in build_envelope(manifest())


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ('default = "sometimes"', "[reasoning] default: must be on"),
        ("", "[reasoning] default: is required"),
        ('default = "on"\nswitch = true', "[reasoning] switch: isn't a manifest key"),
    ],
)
def test_a_bad_reasoning_table(table: str, expected: str) -> None:
    assert any(p.startswith(expected) for p in problems(EXAMPLE + "\n[reasoning]\n" + table + "\n"))


def test_a_reasoning_default_without_settings_warns() -> None:
    text = EXAMPLE.replace("settings = { max_model_len = 131072 }", "") + '\n[reasoning]\ndefault = "on"\n'
    assert any("[reasoning] default" in w for w in manifest(text).warnings)
