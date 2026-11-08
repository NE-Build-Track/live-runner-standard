# Changelog

The standard's changes, newest first. Registry releases are git tags named by date
(`2026.11.1`, `2026.11.2`…) and list their entries in the release notes.

## 2026.11.3 and v0.3.0, 7 November 2026

From BlueClaw's first `conform` run: stock vLLM 0.32.0 serving Qwen3.8-27B with a reasoning parser,
on `openai.chat.v1`, `anthropic.messages.v1` and `openai.responses.stateless.v1`. From BlueClaw's
second note: what Claude Code and Codex send. And from Ziply.io's request.

### Reasoning

- SPEC.md, "Reasoning": a deployment chooses whether its model reasons by default, and both choices
  conform. The envelope gains an optional `reasoning` key, `{"default": "on" | "off"}`; any other
  default reads as undeclared, so a later value never makes a runner ineligible. Gateway rule 3
  selects by it. The setting that makes the default belongs in the revision's `settings`.
- The success cases of the chat, Messages and Responses suites send 512 output tokens, not 16, so a
  reasoning model can think and still answer. Suite versions: `openai.chat.v1` 2,
  `anthropic.messages.v1` 2, `openai.responses.stateless.v1` 2.
- The Responses suites counted reasoning as content: in BlueClaw's run, `responses.sse.content`
  passed on reasoning deltas alone (all 16 output tokens were thinking), because `any-event /delta`
  also names `response.reasoning_text.delta`, and `/output/*/content/*/text` also names a reasoning
  item's text. A suite's content item may now add `where` and `value`, and the Responses suites look
  only at `output_text` parts and `response.output_text.delta` events.
- A suite case can carry `when`, envelope pointers and values: it runs only for a runner that
  declares them. Each chat suite has a `reasoning_off` case for `reasoning.default: off`, which must
  answer within 16 tokens.
- Open question: a per-request reasoning switch for every chat profile, once it's measured which
  ones stock engines honor.

### `openai.responses.stateless.v2`

- New entry. v1 mapped every `incomplete` answer to `output_failed`, so an answer the caller's own
  `max_output_tokens` cut short was an error, while `openai.chat.v1` counts `finish_reason: "length"`
  as success. v2's success rule accepts `completed` or `incomplete`; `failed` and `error` events stay
  `internal`. v1 is deprecated (`registry/openai/namespace.json`). Its suite adds `responses.capped.*`,
  answers cut short that must count as success.
- `registry-entry.v1.schema.json`: a success rule's final condition may use `matches`, as error
  mappings already could.

### Open request schemas: `openai.chat.v2` and `anthropic.messages.v2`

- The chat entries' request schemas listed every type they allowed, so a gateway applying rule 8
  refused coding agents. Claude Code sends reminders as `system` messages between turns, server tools
  such as `web_search_20250305`, and blocks such as `document`, `redacted_thinking` and
  `tool_reference`. Codex sends `custom` and `web_search` tools, then `custom_tool_call` and
  `custom_tool_call_output` items. BlueClaw had turned the check off for all three chat profiles.
- SPEC.md, "What an entry holds": request schemas are open where a vendor's API grows. A list keyed by
  `type` (tools, tool calls, input items, content parts and blocks) ends with a branch for other types,
  which pass to the runner. The types an entry describes are still checked in full, so an image to fetch
  by URL is still refused. Gateway rule 8 says so, and a new runner rule says a runner serves, ignores
  or refuses a type its entry doesn't describe, never fails on it as `internal`, and never fetches for
  it.
- New entries `openai.chat.v2` and `anthropic.messages.v2`, open in that way, with suites at version 1.
  `anthropic.messages.v2` also takes `system` messages inside `messages`, as the Messages API now does:
  a runner that can't serve one refuses with 400 and `Unexpected role "system"`, as Anthropic does, and
  Claude Code then resends without them. `openai.responses.stateless.v2`, not yet released, is opened
  the same way, and a function call's output may also be a list of content parts. `openai.chat.v1`
  and `anthropic.messages.v1` are deprecated.
- Open: a runner can't declare the extra types it serves, and a suite case can't yet send an
  undescribed type and accept either a success or a refusal.

### Readiness and reports

- `runner-contract ready`: the readiness rule for a stock engine whose own health check runs no
  self-test. It self-tests each advertised profile with its suite's success cases, answers
  `livepeer.health.v1`, and then follows the engine's health check. SPEC.md's readiness rule names it,
  so operators don't each write one. `--front` also relays every other request to the engine, for a
  runner the node reaches at one address (BlueClaw's tunnels have one target each, which is why
  BlueClaw wrote its own front).
- Report groups: `Report.groups` and `group_of` sort checks into registration, health, cancellation
  and calls, and `conform` prints each group's result. The report format is unchanged.
- `common.cancel` no longer fails when health was never ready; `common.health.ready` already says so.

### From Ziply.io's request (`zio-ty1.6`)

- `runner-contract envelope` and `block` write `runner_id` (the manifest's `id`) into the envelope:
  Livepeer Node at `bbe126e` reports no runner ID in discovery, so a gateway had only the URL to key
  approvals on. `check` refuses a block whose envelope names another ID. Not part of the revision.
- SPEC.md, "Inputs and outputs": under `origin: deployment` the runner fetches only from its
  deployment's own origins, which may be internal, and refuses every other address. Only
  `origin: public` refuses internal addresses outright. The old wording contradicted
  `zio.vod-transcode.v1` and `zio.transcribe.v1`.
- Fixtures: `conform --fixtures FILE` fills a case's `"$fixture.<name>"` strings with values the
  builder minted (input URLs, sha256s, storage grants), and a case's `fetch` names URLs that must
  answer 2xx with a body after the answer, such as outputs and their completion record. A case
  missing a fixture fails. Values never reach a report, since no schema problem quotes the value it
  refuses (see the library's changes below).
- Open: session cases for persistent profiles (start, Trickle channels, stop, idle and drain).

### Library (`livepeer-runner-contract` 0.3.0)

- `Envelope.reasoning` and `Reasoning`; `select(..., reasoning=...)`; the manifest's
  `[reasoning] default`.
- `select(..., model_revision=...)`, for a gateway that approves one model revision.
- `livepeer_runner_contract.ready.Readiness` for runners that aren't stock engines, with `fail()`
  for a call that shows the runner broken.
- Success conditions read `matches`. An entry whose conditions hold a key this reader doesn't know is
  refused rather than read loosely.
- `validate_request` and `validate_response` never quote the value they refuse: a problem says where
  and what the schema wants, such as `at /messages/0/content/0/source/type: must be "base64"`, and in
  a union of shapes it names the cause in the shape the value's type picks. A refused value could be a
  caller's whole conversation, so BlueClaw clipped each problem before showing it. `schema_problems`
  does the same for any schema's errors, such as a conformance report's. 0.2.0 ignores `matches` in a success condition, so it misreads
  `openai.responses.stateless.v2`: pin 0.3.0 with any release that has it.

## 2026.11.2 and v0.2.0, 5 November 2026

### Conformance

- `conformance/`: the first conformance suites, for `openai.chat.v1`,
  `openai.responses.stateless.v1`, `anthropic.messages.v1`, `typesafe.systemone.v1` and
  `openai.embeddings.v1`, and `conformance/common/` with the thresholds of the checks every suite
  runs. Each suite holds a success case per framing, a declared-error case, limit cases and
  work-report cases. Suites ship in registry releases and in `livepeer-runner-registry`.
- `runner-contract conform` (`livepeer-runner-contract` 0.2.0): runs each profile's suite
  against a running runner on its own host and writes a `conformance-report.v1` report per
  profile. It checks the envelope and revision, health, streamed headers within 1 s, keepalive, every
  case's answer, schema, content and work reports, and survival of a closed request.
- CI check 6, in the `python` job: every suite passes against a reference runner (a small stdlib
  server per profile, in `python/runner-contract/tests/reference/`), and deliberately broken
  runners fail it.
- The library reads work reports (`Result.work`, `Operation.work_reports`, with `count`, `sum`,
  `length`, header and request reports), checks answers against response schemas
  (`validate_response`), treats `*` on an object as every member, and takes a registry release's
  tag from a `registry-<tag>/` directory's name.

### The daily fal import

- `.github/workflows/fal-import.yml` re-imports fal's schemas daily; a changed schema becomes a
  new major on a `fal-import/<date>` branch with an issue for eliteprox to open the pull request.
- SPEC.md: only a streamed answer must send its headers within 1 s.

## 2026.11.1 and v0.1.0, 5 November 2026

### Runner contract v1

- `SPEC.md`: runner contract v1, from the decisions locked on 5 November 2026.
- The envelope, registry-entry and conformance-report schemas.
- The capability list, with 21 capabilities.
- `CODEOWNERS`, naming each namespace's steward, and `registry/<namespace>/namespace.json` for a
  namespace's deprecations and aliases.
- CI checks 1–5: schemas and examples, immutability, the index, names and links, and the capability
  list.
- The first registry entries, 92 in all: `livepeer.*` (error kinds, input and output kinds, health,
  the conformance report); `openai.*`, `anthropic.*` and `typesafe.*` text profiles drafted from
  inference-gateway; `openai.*` media profiles drafted from BlueClaw; Ziply.io's five `zio.*`
  profiles; and `fal.queue.v1` with 73 fal endpoint entries generated by `importers/fal/`.
- The registry-entry schema gained what the drafts needed: several framings per endpoint, ordered
  error mappings that can match any line or event, `present`, `matches` and `content_type`
  conditions, work reports from the request with `count`, `sum` and `length`, `*` pointers,
  `recovery`, and `session.stop`. Two capabilities were added: `text-to-3d` and `audio-to-video`.
- `python/runner-contract` (`livepeer-runner-contract` 0.1.0): the `runner-contract` tool (`init`,
  `envelope`, `block`, `check`, `draft`) and the gateway library (`parse_envelope`, `Registry.load`,
  `parse_discovery`, `select`, `validate_request`, `read_result`, `classify`, `log_ignored`,
  `deployment_revision`). `python/runner-registry` (`livepeer-runner-registry`) packages the
  registry, schemas and capability list from the repository root at build. CI job `python` tests,
  lints, type-checks and builds both.
- Not yet: `comfystream.*` (eliteprox's own runners' source isn't public) and conformance suites.
