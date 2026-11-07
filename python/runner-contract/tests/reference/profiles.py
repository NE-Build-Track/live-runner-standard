"""Each profile's reference answers, written from its registry entry (schemas, error mappings, work reports).

A steward who adds a suite for a new profile adds its ``Behaviour`` here, so CI check 6 can run it.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

from reference.server import Answer, Behaviour, count_tokens

CREATED = 1793952000
WORDS = ["Livepeer", " runs", " AI", " jobs."]
TEXT = "".join(WORDS)
COUNT = "Count from 1 to 200"
THOUGHT = "Thinking it over."


def _output(request: dict[str, Any], fields: tuple[str, ...], thinks: int) -> tuple[list[str], bool, int]:
    """The words of an answer, whether its budget cut it short, and the output tokens it spent.

    A word is a token. A prompt asking to count to 200 wants 200 of them; anything else wants WORDS.
    Thinking spends ``thinks`` tokens of the budget (the first of ``fields`` the request sets) first.
    """
    wanted = [f" {i}" if i > 1 else "1" for i in range(1, 201)] if COUNT in json.dumps(request) else WORDS
    budget = next((request[f] for f in fields if isinstance(request.get(f), int)), None)
    room = len(wanted) if budget is None else max(0, budget - thinks)
    words = wanted[:room]
    spent = thinks + len(words) if budget is None else min(budget, thinks + len(words))
    return words, len(words) < len(wanted), spent


class OpenAIChat(Behaviour):
    profile = "openai.chat.v1"

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        words, capped, spent = _output(request, ("max_completion_tokens", "max_tokens"), thinks)
        finish = "length" if capped else "stop"
        usage = {"prompt_tokens": tokens, "completion_tokens": spent, "total_tokens": tokens + spent}
        if request.get("stream"):
            base = {"id": "chatcmpl-ref", "object": "chat.completion.chunk", "created": CREATED, "model": model}

            def chunk(delta: dict[str, Any], finish: str | None = None) -> tuple[None, dict[str, Any]]:
                return None, {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            events: list[tuple[str | None, Any]] = [chunk({"role": "assistant", "content": ""})]
            events += [chunk({"content": w}) for w in words]
            events.append(chunk({}, finish))
            if (request.get("stream_options") or {}).get("include_usage"):
                events.append((None, {**base, "choices": [], "usage": usage}))
            events.append((None, "[DONE]"))
            return Answer(200, events=events)
        message = {"role": "assistant", "content": "".join(words)}
        return Answer(
            200,
            {
                "id": "chatcmpl-ref",
                "object": "chat.completion",
                "created": CREATED,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
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

    def _response(
        self,
        model: str,
        status: str,
        usage: dict[str, int] | None,
        words: list[str] | None = None,
        thinks: int = 0,
    ) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        if thinks and status != "in_progress":
            # Reasoning as vLLM returns it: an item of its own, whose content also has text.
            thought = [{"type": "reasoning_text", "text": THOUGHT}]
            output.append({"type": "reasoning", "id": "rs_ref", "summary": [], "content": thought})
        if words and status != "in_progress":
            content = [{"type": "output_text", "text": "".join(words)}]
            item = "completed" if status == "completed" else "incomplete"
            output.append({"type": "message", "id": "msg_ref", "role": "assistant", "status": item, "content": content})
        response: dict[str, Any] = {
            "id": "resp_ref",
            "object": "response",
            "created_at": CREATED,
            "status": status,
            "model": model,
            "output": output,
            "usage": usage,
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return response

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        words, capped, spent = _output(request, ("max_output_tokens",), thinks)
        usage = {"input_tokens": tokens, "output_tokens": spent, "total_tokens": tokens + spent}
        status = "incomplete" if capped else "completed"
        final = self._response(model, status, usage, words, thinks)
        if not request.get("stream"):
            return Answer(200, final)
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
        deltas = []
        if thinks:
            deltas.append(("response.reasoning_text.delta", {"item_id": "rs_ref", "output_index": 0, "delta": THOUGHT}))
        index = 1 if thinks else 0
        deltas += [
            ("response.output_text.delta", {"item_id": "msg_ref", "output_index": index, "delta": w}) for w in words
        ]
        for i, (kind, delta) in enumerate(deltas, 1):
            events.append((kind, {"type": kind, "sequence_number": i, "content_index": 0, **delta}))
        kind = f"response.{status}"
        events.append((kind, {"type": kind, "sequence_number": len(deltas) + 1, "response": final}))
        return Answer(200, events=events)

    def error_event(self) -> tuple[str | None, Any]:
        return "error", {"type": "error", "message": "the engine failed mid-stream", "code": None, "param": None}


class AnthropicMessages(Behaviour):
    profile = "anthropic.messages.v1"

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
        model = request.get("model", "")
        tokens = count_tokens(request)
        words, capped, spent = _output(request, ("max_tokens",), thinks)
        stop = "max_tokens" if capped else "end_turn"
        content: list[dict[str, Any]] = [{"type": "thinking", "thinking": THOUGHT}] if thinks else []
        if words:
            content.append({"type": "text", "text": "".join(words)})
        message = {
            "id": "msg_ref",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": tokens, "output_tokens": spent},
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
        for word in words:
            delta = {"type": "text_delta", "text": word}
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}))
        events += [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": spent},
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

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
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

    def answer(self, endpoint: str, request: dict[str, Any], thinks: int = 0) -> Answer:
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


class OpenAIResponsesV2(OpenAIResponses):
    """The same answers; v2's success rule counts the incomplete one a budget cuts short as success."""

    profile = "openai.responses.stateless.v2"


class OpenAIChatV2(OpenAIChat):
    """The same answers; v2 differs only in the requests a gateway lets through."""

    profile = "openai.chat.v2"


class AnthropicMessagesV2(AnthropicMessages):
    """The same answers; v2 differs only in the requests a gateway lets through."""

    profile = "anthropic.messages.v2"


BEHAVIOURS: dict[str, Behaviour] = {
    b.profile: b
    for b in (
        OpenAIChat(),
        OpenAIChatV2(),
        OpenAIResponses(),
        OpenAIResponsesV2(),
        AnthropicMessages(),
        AnthropicMessagesV2(),
        TypeSafeSystemOne(),
        OpenAIEmbeddings(),
    )
}


def manifest_for(
    profiles: list[str], url: str, registry: Any, *, runner_id: str = "reference-runner", reasoning: str | None = None
) -> str:
    """A manifest for a reference runner serving ``profiles`` at ``url``, with limits for the suites' limit cases.

    ``reasoning`` is the ``[reasoning] default`` it declares, or None to declare none.
    """
    declared = "" if reasoning is None else f'\n[reasoning]\ndefault = "{reasoning}"\n'
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
{declared}
[revision]
images = ["reference/runner@sha256:{"0" * 64}"]
settings = {{ context_tokens = 4096 }}
"""
