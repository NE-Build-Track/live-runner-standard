"""deployment_revision: the SPEC's own Python snippet and this implementation agree."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from livepeer_runner_contract import Model, canonical_json, deployment_revision

SPEC = Path(__file__).resolve().parents[3] / "SPEC.md"


def spec_snippet() -> Callable[..., str]:
    """The ```python block under "The deployment_revision recipe" in SPEC.md, run as written."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### The `deployment_revision` recipe", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, "SPEC.md has no Python snippet for the recipe"
    namespace: dict[str, Any] = {}
    exec(match.group(1), namespace)  # noqa: S102  # the SPEC's own reference code
    function: Callable[..., str] = namespace["deployment_revision"]
    return function


CASES: list[tuple[list[str], list[str], dict[str, Any], dict[str, Any] | None]] = [
    (["vllm/vllm-openai@sha256:" + "1" * 64], ["openai.chat.v1"], {"max_model_len": 131072}, None),
    (
        ["b@sha256:" + "2" * 64, "a@sha256:" + "3" * 64],
        ["openai.chat.v1", "anthropic.messages.v1"],
        {"max_model_len": 131072, "parsers": {"tool": "hermes", "reasoning": "qwen3"}, "enabled": True},
        {"name": "qwen3.8-27b", "source": "hf:Qwen/Qwen3.8-27B-AWQ", "revision": "c" * 40},
    ),
    ([], ["zio.vod-transcode.v1", "h264-1080p", "av1-720p"], {"encoder": "nvenc", "origins": ["s3:bucket"]}, None),
    (["x@sha256:" + "4" * 64], ["fal.queue.v1"], {}, {"name": "ideogram-v4", "source": "fal:ideogram/v4"}),
    (
        ["x@sha256:" + "5" * 64],
        ["openai.transcriptions.v1"],
        {"langue": "français", "emoji": "🎙", "ключ": "значение"},
        None,
    ),
    (["x@sha256:" + "6" * 64], ["openai.chat.v1"], {"temperature": 0.7, "nothing": None, "list": [3, 1, 2]}, None),
]


@pytest.mark.parametrize(("images", "profiles", "settings", "model"), CASES)
def test_the_spec_snippet_and_this_implementation_agree(
    images: list[str], profiles: list[str], settings: dict[str, Any], model: dict[str, Any] | None
) -> None:
    expected = spec_snippet()(images, profiles, settings, model)
    assert deployment_revision(images, profiles, settings, model) == expected
    if model is not None:
        as_model = Model(model["name"], model["source"], model.get("revision"))
        assert deployment_revision(images, profiles, settings, as_model) == expected
    assert re.match(r"^sha256:[0-9a-f]{64}$", expected)


def test_order_of_inputs_doesnt_matter() -> None:
    a = deployment_revision(["y", "x"], ["p.b.v1", "p.a.v1"], {"b": 1, "a": {"d": 2, "c": 3}})
    b = deployment_revision(["x", "y"], ["p.a.v1", "p.b.v1"], {"a": {"c": 3, "d": 2}, "b": 1})
    assert a == b


def test_the_model_name_is_left_out_but_source_and_revision_count() -> None:
    base = {"name": "a", "source": "hf:org/model", "revision": "1" * 40}
    same = deployment_revision([], ["p.a.v1"], {}, base)
    assert deployment_revision([], ["p.a.v1"], {}, {**base, "name": "renamed"}) == same
    assert deployment_revision([], ["p.a.v1"], {}, {**base, "revision": "2" * 40}) != same
    assert deployment_revision([], ["p.a.v1"], {}, {**base, "source": "hf:org/other"}) != same
    assert deployment_revision([], ["p.a.v1"], {}) != same


def test_canonical_json_writes_non_ascii_as_itself() -> None:
    assert canonical_json({"b": "é", "a": [1, {"d": None, "c": True}]}) == '{"a":[1,{"c":true,"d":null}],"b":"é"}'
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"x": float("nan")})
