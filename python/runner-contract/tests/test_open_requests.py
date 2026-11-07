"""The chat entries' request schemas, against what coding agents send (SPEC.md, "What an entry holds").

Each list keyed by ``type`` passes the types its entry doesn't describe, so a gateway's check (rule 8)
lets them through and the runner decides; the types it does describe are still checked in full.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from livepeer_runner_contract import Registry, validate_request

ROOT = Path(__file__).resolve().parents[3]
CHAT, MESSAGES, RESPONSES = "openai.chat.v2", "anthropic.messages.v2", "openai.responses.stateless.v2"
ENDPOINT = {CHAT: "chat_completions", MESSAGES: "messages", RESPONSES: "responses"}
PNG = "iVBORw0KGgo="

# What Claude Code sends: reminders as system messages between turns, blocks the entry doesn't
# describe, and server tools beside its own.
CLAUDE_CODE = {
    "model": "m",
    "max_tokens": 32000,
    "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Fix the test."}]},
        {"role": "system", "content": "<system-reminder>The file changed.</system-reminder>"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Read it first.", "signature": "s"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "x = 1"}, {"type": "tool_reference", "tool_name": "Grep"}],
                },
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}},
            ],
        },
    ],
    "tools": [
        {"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}},
        {"type": "custom", "name": "Edit", "input_schema": {"type": "object"}, "defer_loading": True},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    ],
}

# What Codex sends: a freeform tool and a built-in one beside its functions, and on the next turn the
# calls the freeform tool made.
CODEX = {
    "model": "m",
    "store": False,
    "stream": True,
    "instructions": "You are Codex.",
    "reasoning": {"effort": "medium", "summary": "auto"},
    "include": ["reasoning.encrypted_content"],
    "prompt_cache_key": "k",
    "tool_choice": "auto",
    "tools": [
        {"type": "function", "name": "shell", "parameters": {"type": "object"}, "strict": False},
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "Edit files.",
            "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"},
        },
        {"type": "web_search"},
    ],
    "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Sandbox: none."}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix the test."}]},
        {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": "*** Begin Patch"},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done"},
        {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": '{"cmd": "ls"}'},
        {"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "a.py"}]},
    ],
}

# The same in Chat Completions: a custom tool, the call it made, and a part the entry doesn't describe.
CHAT_AGENT = {
    "model": "m",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "input_audio", "input_audio": {}}]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "custom", "custom": {"name": "apply_patch", "input": "x"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "Done"},
    ],
    "tools": [
        {"type": "function", "function": {"name": "shell", "parameters": {"type": "object"}}},
        {"type": "custom", "custom": {"name": "apply_patch"}},
    ],
    "tool_choice": {"type": "allowed_tools", "allowed_tools": {"mode": "auto", "tools": []}},
}


# Requests real coding agents sent, one per agent and wire, captured by BlueClaw's agent tests from
# each agent's first turn against a local server. Paths, device and session IDs are replaced, and the
# prose (prompts, messages, tool descriptions) is elided: the entries check shape, and this repo is
# public, so it doesn't republish the agents' own prompts.
AGENTS = Path(__file__).parent / "fixtures" / "agents"
CAPTURED = {
    "claude-code-2.1.319.anthropic-messages.json": MESSAGES,
    "codex-0.165.0.openai-responses.json": RESPONSES,
    "opencode-1.18.34.openai-chat.json": CHAT,
    "opencode-1.18.34.openai-chat.title.json": CHAT,
}


def captured(name: str) -> Any:
    return json.loads((AGENTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def real() -> Registry:
    return Registry.load(ROOT)


@pytest.mark.parametrize(("name", "profile"), sorted(CAPTURED.items()))
def test_what_coding_agents_really_sent_passes(real: Registry, name: str, profile: str) -> None:
    check = validate_request(real, profile, ENDPOINT[profile], captured(name))
    assert check.ok, check.problems


def test_every_captured_request_is_checked() -> None:
    assert sorted(path.name for path in AGENTS.glob("*.json")) == sorted(CAPTURED)


@pytest.mark.parametrize(
    ("name", "profile", "where"),
    [
        # Claude Code's reminder, a system message among the turns.
        ("claude-code-2.1.319.anthropic-messages.json", "anthropic.messages.v1", ["at /messages/1/role"]),
        # Codex's namespace of tools, and its built-in web search.
        ("codex-0.165.0.openai-responses.json", "openai.responses.stateless.v1", ["at /tools/4", "at /tools/8"]),
    ],
)
def test_the_v1_entries_refused_what_they_really_sent(
    real: Registry, name: str, profile: str, where: list[str]
) -> None:
    problems = validate_request(real, profile, ENDPOINT[profile.replace(".v1", ".v2")], captured(name)).problems
    for place in where:
        assert any(p.startswith(place) for p in problems), (place, problems)


@pytest.mark.parametrize(("profile", "body"), [(MESSAGES, CLAUDE_CODE), (RESPONSES, CODEX), (CHAT, CHAT_AGENT)])
def test_what_coding_agents_send_passes(real: Registry, profile: str, body: dict[str, Any]) -> None:
    check = validate_request(real, profile, ENDPOINT[profile], body)
    assert check.ok, check.problems


@pytest.mark.parametrize(
    ("profile", "body", "where"),
    [
        (
            "anthropic.messages.v1",
            CLAUDE_CODE,
            ["at /messages/1/role", "at /messages/2/content/1", "at /messages/3/content/0/content/1", "at /tools/2"],
        ),
        ("openai.responses.stateless.v1", CODEX, ["at /input/3", "at /tools/1", "at /tools/2"]),
        ("openai.chat.v1", CHAT_AGENT, ["at /messages/0/content/1", "at /messages/1/tool_calls/0", "at /tools/1"]),
    ],
)
def test_the_v1_entries_refused_it(real: Registry, profile: str, body: dict[str, Any], where: list[str]) -> None:
    """Why the new majors exist: a gateway applying rule 8 to a v1 entry refused both agents. One line
    names one misfit under a union, the first and deepest, so a list's later misfits aren't listed."""
    problems = validate_request(real, profile, ENDPOINT[profile.replace(".v1", ".v2")], body).problems
    for place in where:
        assert any(p.startswith(place) for p in problems), (place, problems)


def message_with(*blocks: dict[str, Any], role: str = "user") -> dict[str, Any]:
    return {"model": "m", "max_tokens": 16, "messages": [{"role": role, "content": list(blocks)}]}


def responses_with(*items: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"model": "m", "store": False, "input": list(items), **fields}


def chat_with(*parts: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "user", "content": list(parts)}], **fields}


URL_IMAGE = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}


@pytest.mark.parametrize(
    ("profile", "body", "problem"),
    [
        # A described type is checked in full, so a URL to fetch is refused wherever it appears.
        (MESSAGES, message_with(URL_IMAGE), 'at /messages/0/content/0/source/type: must be "base64"'),
        (
            MESSAGES,
            message_with({"type": "tool_result", "tool_use_id": "t", "content": [URL_IMAGE]}),
            'at /messages/0/content/0/content/0/source/type: must be "base64"',
        ),
        (
            RESPONSES,
            responses_with(
                {"role": "user", "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}
            ),
            "at /input/0/content/0/image_url: doesn't match",
        ),
        (
            CHAT,
            chat_with({"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}),
            "at /messages/0/content/0/image_url/url: doesn't match",
        ),
        # A described type missing what it needs.
        (MESSAGES, {**message_with({"type": "text", "text": "hi"}), "tools": [{"name": "x"}]}, "at /tools/0"),
        (MESSAGES, message_with({"type": "tool_use", "id": "t", "name": "x"}), "at /messages/0/content/0"),
        (RESPONSES, responses_with({"type": "function_call", "call_id": "c"}), "at /input/0"),
        (RESPONSES, responses_with(tools=[{"type": "function"}]), "at /tools/0"),
        (CHAT, chat_with({"type": "text", "text": "hi"}, tools=[{"type": "function"}]), "at /tools/0"),
        # What the profile excludes, and what isn't a type at all.
        (RESPONSES, responses_with(previous_response_id="r"), "at /previous_response_id: isn't allowed. Not part"),
        (RESPONSES, responses_with({"content": "no role"}), "at /input/0"),
        (MESSAGES, message_with({"type": 3}), "at /messages/0/content/0"),
        (MESSAGES, message_with({"type": "text", "text": "hi"}, role="robot"), "at /messages/0/role: must be one of"),
    ],
)
def test_the_types_an_entry_describes_are_still_checked(
    real: Registry, profile: str, body: dict[str, Any], problem: str
) -> None:
    problems = validate_request(real, profile, ENDPOINT[profile], body).problems
    assert any(p.startswith(problem) for p in problems), problems
