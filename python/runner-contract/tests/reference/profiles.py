"""Each profile's reference answers, written from its registry entry (schemas, error mappings, work reports).

A steward who adds a suite for a new profile adds its ``Behaviour`` here, so CI check 6 can run it.
"""

from __future__ import annotations

import base64
import struct
from typing import Any

from reference.server import Answer, Behaviour, count_tokens

CREATED = 1793952000
WORDS = ["Livepeer", " runs", " AI", " jobs."]
TEXT = "".join(WORDS)


class OpenAIChat(Behaviour):
    profile = "openai.chat.v1"

    def answer(self, endpoint: str, request: dict[str, Any]) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        usage = {"prompt_tokens": tokens, "completion_tokens": len(WORDS), "total_tokens": tokens + len(WORDS)}
        if request.get("stream"):
            base = {"id": "chatcmpl-ref", "object": "chat.completion.chunk", "created": CREATED, "model": model}

            def chunk(delta: dict[str, Any], finish: str | None = None) -> tuple[None, dict[str, Any]]:
                return None, {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            events: list[tuple[str | None, Any]] = [chunk({"role": "assistant", "content": ""})]
            events += [chunk({"content": w}) for w in WORDS]
            events.append(chunk({}, "stop"))
            if (request.get("stream_options") or {}).get("include_usage"):
                events.append((None, {**base, "choices": [], "usage": usage}))
            events.append((None, "[DONE]"))
            return Answer(200, events=events)
        message = {"role": "assistant", "content": TEXT}
        return Answer(
            200,
            {
                "id": "chatcmpl-ref",
                "object": "chat.completion",
                "created": CREATED,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    def invalid(self, endpoint: str, problem: str) -> Answer:
        return Answer(
            400,
            {
                "error": {
                    "message": f"invalid request: {problem}",
                    "type": "BadRequestError",
                    "param": None,
                    "code": 400,
                }
            },
        )

    def too_long(self, endpoint: str, limit: int) -> Answer:
        message = f"This model's maximum context length is {limit} tokens. However, your request has more input tokens."
        return Answer(400, {"error": {"message": message, "type": "BadRequestError", "param": None, "code": 400}})

    def too_large(self, endpoint: str) -> Answer:
        return Answer(
            413, {"error": {"message": "request too large", "type": "BadRequestError", "param": None, "code": 413}}
        )

    def error_event(self) -> tuple[str | None, Any]:
        return None, {"error": {"message": "the engine failed mid-stream", "type": "internal_error", "code": None}}


class OpenAIResponses(OpenAIChat):
    profile = "openai.responses.stateless.v1"

    def _response(self, model: str, status: str, usage: dict[str, int] | None) -> dict[str, Any]:
        output = []
        if status == "completed":
            content = [{"type": "output_text", "text": TEXT}]
            output = [
                {"type": "message", "id": "msg_ref", "role": "assistant", "status": "completed", "content": content}
            ]
        return {
            "id": "resp_ref",
            "object": "response",
            "created_at": CREATED,
            "status": status,
            "model": model,
            "output": output,
            "usage": usage,
        }

    def answer(self, endpoint: str, request: dict[str, Any]) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        usage = {"input_tokens": tokens, "output_tokens": len(WORDS), "total_tokens": tokens + len(WORDS)}
        completed = self._response(model, "completed", usage)
        if not request.get("stream"):
            return Answer(200, completed)
        events: list[tuple[str | None, Any]] = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": self._response(model, "in_progress", None),
                },
            )
        ]
        for i, word in enumerate(WORDS, 1):
            delta = {"item_id": "msg_ref", "output_index": 0, "content_index": 0, "delta": word}
            events.append(
                ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": i, **delta})
            )
        events.append(
            ("response.completed", {"type": "response.completed", "sequence_number": 9, "response": completed})
        )
        return Answer(200, events=events)

    def error_event(self) -> tuple[str | None, Any]:
        return "error", {"type": "error", "message": "the engine failed mid-stream", "code": None, "param": None}


class AnthropicMessages(Behaviour):
    profile = "anthropic.messages.v1"

    def answer(self, endpoint: str, request: dict[str, Any]) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        message = {
            "id": "msg_ref",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": tokens, "output_tokens": len(WORDS)},
        }
        if not request.get("stream"):
            return Answer(200, message)
        start = {**message, "content": [], "stop_reason": None, "usage": {"input_tokens": tokens, "output_tokens": 1}}
        events: list[tuple[str | None, Any]] = [
            ("message_start", {"type": "message_start", "message": start}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            ("ping", {"type": "ping"}),
        ]
        for word in WORDS:
            delta = {"type": "text_delta", "text": word}
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}))
        events += [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": len(WORDS)},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        return Answer(200, events=events)

    def _error(self, status: int, kind: str, message: str) -> Answer:
        return Answer(status, {"type": "error", "error": {"type": kind, "message": message}})

    def invalid(self, endpoint: str, problem: str) -> Answer:
        return self._error(400, "invalid_request_error", f"invalid request: {problem}")

    def too_long(self, endpoint: str, limit: int) -> Answer:
        return self._error(
            400, "invalid_request_error", f"prompt is too long: it exceeds the context length of {limit} tokens"
        )

    def too_large(self, endpoint: str) -> Answer:
        return self._error(413, "request_too_large", "request too large")

    def error_event(self) -> tuple[str | None, Any]:
        return "error", {"type": "error", "error": {"type": "overloaded_error", "message": "the engine is overloaded"}}


class TypeSafeSystemOne(Behaviour):
    profile = "typesafe.systemone.v1"

    def _answer(self, question: Any) -> dict[str, Any]:
        kind = question.get("type") if isinstance(question, dict) else None
        criteria = question.get("criteria") if isinstance(question, dict) else None
        if kind == "choice" and isinstance(criteria, dict) and criteria:
            options = list(criteria)
            return {
                "type": "choice",
                "choice": options[0],
                "confidence": 0.6,
                "probabilities": {o: 1 / len(options) for o in options},
            }
        if kind == "score" and isinstance(criteria, list) and criteria:
            return {
                "type": "score",
                "score": 0.0,
                "confidence": 0.5,
                "legend": {str(i): level for i, level in enumerate(criteria)},
                "probabilities": {str(i): 1 / len(criteria) for i in range(len(criteria))},
            }
        if kind == "box":
            return {"type": "box", "box": {"top": 0.1, "bottom": 0.9, "left": 0.1, "right": 0.9}, "confidence": 0.5}
        return {"type": "noul", "noul": 0.5}

    def answer(self, endpoint: str, request: dict[str, Any]) -> Answer:
        questions = request.get("questions") or {}
        answers = {name: self._answer(q) for name, q in questions.items()} if isinstance(questions, dict) else {}
        usage = {"input_tokens": count_tokens(request), "output_tokens": 0}
        return Answer(
            200, {"model": request.get("model", ""), "answers": answers or {"x": self._answer({})}, "usage": usage}
        )

    def invalid(self, endpoint: str, problem: str) -> Answer:
        return Answer(422, {"detail": [{"loc": ["body"], "msg": problem, "type": "value_error"}]})

    def too_long(self, endpoint: str, limit: int) -> Answer:
        message = f"the state exceeds the context length of {limit} tokens"
        return Answer(400, {"error": {"message": message, "type": "invalid_request_error"}})

    def too_large(self, endpoint: str) -> Answer:
        return Answer(413, {"detail": "request too large"})

    def error_event(self) -> tuple[str | None, Any]:
        raise NotImplementedError("systemone doesn't stream")


class OpenAIEmbeddings(Behaviour):
    profile = "openai.embeddings.v1"
    VECTOR = (0.0353, -0.0168, 0.0427)

    def answer(self, endpoint: str, request: dict[str, Any]) -> Answer:
        given = request.get("input", "")
        inputs = given if isinstance(given, list) else [given]
        if request.get("encoding_format") == "base64":
            vector: Any = base64.b64encode(struct.pack("<3f", *self.VECTOR)).decode()
        else:
            vector = list(self.VECTOR)
        data = [{"object": "embedding", "index": i, "embedding": vector} for i in range(len(inputs))]
        tokens = count_tokens(request)
        usage = {"prompt_tokens": tokens, "total_tokens": tokens}
        return Answer(200, {"object": "list", "data": data, "model": request.get("model", ""), "usage": usage})

    def _error(self, status: int, message: str) -> Answer:
        return Answer(
            status, {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": None}}
        )

    def invalid(self, endpoint: str, problem: str) -> Answer:
        return self._error(400, f"invalid request: {problem}")

    def too_long(self, endpoint: str, limit: int) -> Answer:
        return self._error(400, f"the input exceeds {limit} tokens")

    def too_large(self, endpoint: str) -> Answer:
        return self._error(413, "request too large")

    def error_event(self) -> tuple[str | None, Any]:
        raise NotImplementedError("embeddings don't stream")


BEHAVIOURS: dict[str, Behaviour] = {
    b.profile: b
    for b in (OpenAIChat(), OpenAIResponses(), AnthropicMessages(), TypeSafeSystemOne(), OpenAIEmbeddings())
}


def manifest_for(profiles: list[str], url: str, registry: Any, *, runner_id: str = "reference-runner") -> str:
    """A manifest for a reference runner serving ``profiles`` at ``url``, with limits for the suites' limit cases."""
    entries = [registry.entry(p) for p in profiles]
    endpoints = "".join(
        f'\n[[endpoints]]\nname = "{name}"\nprofile = "{entry.id}"\n'
        for entry in entries
        for name in sorted(entry.operations)
    )
    return f"""
[runner]
id = "{runner_id}"
app = "{entries[0].capabilities[0]}"
mode = "single-shot"
capacity = 4
url = "{url}"
health_url = "{url}/health"
version = "reference"

[price]
amount = 1.00
currency = "usd"
unit = "hour"

[model]
name = "reference-model"
source = "hf:reference/model"
revision = "{"0" * 40}"
{endpoints}
[limits]
context_tokens = 4096
max_input_bytes = 9437184

[revision]
images = ["reference/runner@sha256:{"0" * 64}"]
settings = {{ context_tokens = 4096 }}
"""
