# livepeer-runner-contract

Runner contract v1, the [live runner standard](https://github.com/NE-Build-Track/live-runner-standard),
for Python: the `runner-contract` builder tool, and the gateway library that reads envelopes and
registry entries. Version 0.2 implements `runner_contract: 1` as
[`SPEC.md`](../../SPEC.md) states it, and adds `conform`; 0.1.0 is the latest tag.

The library makes no network calls and holds no policy. Approvals, routes and retail rules stay in
each gateway; the library reads what runners publish and what the registry says.

## Install

Neither package is published to PyPI. Install them from this repository's tags:

```sh
# the tool and the library
pip install "livepeer-runner-contract @ git+https://github.com/NE-Build-Track/live-runner-standard.git@v0.1.0#subdirectory=python/runner-contract"

# a registry release, which a gateway pins (or pass Registry.load a directory instead)
pip install "livepeer-runner-registry @ git+https://github.com/NE-Build-Track/live-runner-standard.git@2026.11.1#subdirectory=python/runner-registry"

# or run the tool without installing it
uvx --from "git+https://github.com/NE-Build-Track/live-runner-standard.git@v0.1.0#subdirectory=python/runner-contract" \
  runner-contract block runner.toml
```

`runner-contract check` needs a registry release: pass `--registry DIR` (a checkout of this
repository works), or add the registry package to the same run with
`uvx --with "livepeer-runner-registry @ git+…@2026.11.1#subdirectory=python/runner-registry" --from … runner-contract check …`.

The library doesn't depend on the registry package, so it never chooses a release for a gateway: a
gateway pins one itself (SPEC.md, gateway rule 5). There is no extra for it either, since an extra
could only name the package, and a bare name would resolve against PyPI, where anyone could publish
it. Python 3.12 or later; the only dependencies are `jsonschema` and `referencing`.

## The tool

| Command | What it does |
|---|---|
| `runner-contract init --capability chat --profile openai.chat.v2 [--out runner.toml]` | Writes a starter manifest. With a registry at hand, one `[[endpoints]]` per operation of each profile. Every value to fill is a `TODO`, which the other commands refuse |
| `runner-contract envelope runner.toml` | Prints the envelope as compact JSON, with `deployment_revision` computed by the SPEC's recipe. Fails over 1,024 bytes |
| `runner-contract block runner.toml [--format toml\|json]` | Prints the node's static `[[Runners]]` block, or the JSON a runner sends to `POST /runners/heartbeat` |
| `runner-contract check FILE [--manifest runner.toml] [--registry DIR]` | Checks a `[[Runners]]` block, a registration JSON or a bare envelope: one line per problem, exit 1 if any |
| `runner-contract conform --manifest runner.toml [--block FILE \| --envelope FILE] [--url URL] [--profile ID] [--out report.json]` | Runs each profile's conformance suite against the running runner, on its own host, and writes a report per profile. See [Conformance](#conformance) |
| `runner-contract ready --manifest runner.toml --engine-health URL [--listen HOST:PORT] [--front]` | Answers a stock engine's `HealthURL` as the SPEC's readiness rule says. See [Readiness](#readiness) |
| `runner-contract draft --openapi FILE [--profile ID]` | Drafts a registry entry from a runner's OpenAPI document (JSON), for a steward to review |
| `runner-contract draft --pydantic module:Request,module:Response [--profile ID]` | Drafts one from pydantic models (pydantic must be installed alongside) |

Exit status: 0 clean, 1 problems, 2 a usage error (a missing file, no registry). Warnings go to
standard error and don't fail.

`check` reads the registration fields as livepeer/node reads them (`orchestrator/registry.go`), and
reports:

- the envelope against the schema; `Metadata` at most 1,024 bytes as registered; `Version` at most 64
  characters;
- `App` on the capability list, and `Mode` the capability's mode (a missing `Mode` is `persistent` to
  the node);
- each endpoint's profile in the registry release (or an alias of one), a wire profile that serves
  the App, and the endpoint's name an `operationId` of that entry;
- `PriceInfo` present: a positive number (a TOML number: the node reads `Price` as a `json.Number`,
  which a TOML string can't fill), currency `usd`, unit `hour` or `fixed`;
- for a static block, keys the node doesn't know (it refuses the whole file), and a missing
  `HealthURL` (without one the node treats the runner as always healthy);
- with `--manifest`, a `deployment_revision` the manifest doesn't recompute to.

## Conformance

`runner-contract conform` proves a runner speaks its profiles (SPEC.md, "Conformance, interim"). It
runs on the runner's own host and calls the runner directly at `--url`: no node, no payment. The
builder attaches each report it writes to an approval request. It is new in 0.2: until `v0.2.0` is
tagged, install from `main` (`@main` in place of `@v0.1.0` above).

```sh
runner-contract conform --manifest runner.toml --registry path/to/registry-2026.11.2 --out report.json
```

| Option | What it does |
|---|---|
| `--manifest FILE` | The runner's manifest: the envelope (if no block or envelope is given), and the inputs that recompute `deployment_revision` |
| `--block FILE` or `--envelope FILE` | The registration as registered: a `[[Runners]]` block or registration JSON (whose fields `check` also checks), or a bare envelope |
| `--url URL`, `--health-url URL` | Where to reach the runner and its health check (default: the manifest's or block's) |
| `--profile ID` | A profile to conform, repeatable (default: every profile the envelope advertises) |
| `--registry DIR` | The registry release whose entries and `conformance/` suites to use (default: the installed `livepeer-runner-registry`). A directory named `registry-<tag>`, as the release tarball unpacks, names the release in the report |
| `--out FILE` | The report (default `conformance-report.json`); with several profiles, `conformance-report.<profile>.json` each |
| `--runner-id ID`, `--model NAME` | The report's runner ID and the model name requests send, where the manifest, block or envelope doesn't say |
| `--timeout S`, `--health-timeout S`, `--cancel-settle S` | Seconds to wait on each request (120), on health (10), and after closing a request mid-answer before probing (the common suite's 2) |
| `--fixtures FILE` | A JSON object of values the builder minted for this run, by the names the suite's cases use as `"$fixture.<name>"`: input URLs on the deployment's own storage, their sha256, storage grants, and URLs to read outputs back from. Values never appear in a report |
| `--verbose` | Print passing checks too; failures and skips always print |

Exit status: 0 when every report passes, 1 when a check failed, 2 for a usage error (no runner, no
suite for a profile, a suite that can't run). Each report follows
[`conformance-report.v1.schema.json`](../../schemas/conformance-report.v1.schema.json): runner ID,
`deployment_revision`, profile, `registry_release` (when known), `suite` (`id` is the profile,
`version` is `<suite version>+common.<common version>`), `tool`, start and finish times, every
check, and `pass`, true only when no check failed.

**The checks.** Every suite runs the common checks, with the thresholds in `conformance/common/`:

| Check | Passes when |
|---|---|
| `common.envelope` | The envelope is a valid `runner_contract: 1` envelope of at most 1,024 bytes |
| `common.revision` | The manifest recomputes the envelope's `deployment_revision` (skipped without a manifest) |
| `common.profile` | The envelope advertises an endpoint of the profile |
| `common.registration` | A given block's fields read as the node reads them, as `check` says (skipped without a block) |
| `common.health.ready`, `common.health.after` | Health answers 200 with a `livepeer.health.v1` body whose status is `ready`, before the cases and after them |
| `common.health.starting` | Always skipped: a black-box tool can't make the runner unready |
| `common.cancel` | After the suite's `cancel` case is closed mid-answer (a stream after its first bytes; a JSON request before its answer), health stays ready and the next request succeeds. When health wasn't ready before the cases, only the next request counts, so one health fault fails health alone. That the work stopped within 10 s can't be seen from outside |

Then each case of the profile's suite runs, giving `<case>.request` (the request is valid against
the entry, or invalid where the case says so), `<case>.headers` (a streamed answer's headers within 1 s; skipped for one that isn't streamed),
`<case>.answer` (`read_result`: success with the expected framing, or an error of an expected base
kind), `<case>.schema` (the body, or every SSE event or NDJSON line, matches the entry's response
schema), `<case>.content` (non-empty values where the case looks, so model output stays loose),
`<case>.work_reports` (every measure the entry reports for that framing, where the request asks
for it as the entry says), and for streams `<case>.keepalive` (no wait over 15 s between bytes). A
limit case the runner's envelope doesn't advertise the limit for is skipped, and so is a case whose
`when` the envelope doesn't declare.

**Groups.** `Report.groups` (and the summary line `conform` prints) gives one result for each of
four groups: `registration` (`common.envelope`, `common.revision`, `common.profile`,
`common.registration`), `health` (`common.health.*`), `cancellation` (`common.cancel`) and `calls`
(every case's checks). A group fails when any of its checks does. `group_of(check_id)` names a
check's group.

**The suite format.** A suite is `conformance/<namespace>/<name>.v<major>/suite.json`:

```json
{
  "conformance_suite": 1,
  "profile": "openai.chat.v2",
  "version": "1",
  "cancel": "chat_completions.sse",
  "cases": [
    {"id": "chat_completions.json", "endpoint": "chat_completions",
     "request": {"model": "$model", "messages": [{"role": "user", "content": "Reply with one short sentence."}], "max_tokens": 16},
     "expect": {"framing": "json"},
     "content": [{"in": "body", "pointer": "/choices/0/message/content"}],
     "work_reports": true},
    {"id": "chat_completions.invalid", "endpoint": "chat_completions",
     "request": {"model": "$model", "messages": [], "max_tokens": 16}, "valid": false,
     "expect": {"kinds": ["input_invalid"]}},
    {"id": "chat_completions.context_tokens", "endpoint": "chat_completions", "limit": {"name": "context_tokens"},
     "request": {"model": "$model", "messages": [{"role": "user", "content": "$fill"}], "max_tokens": 1},
     "expect": {"kinds": ["input_unsupported"]}}
  ]
}
```

| Key | Holds |
|---|---|
| `conformance_suite`, `profile`, `version` | `1`; the entry ID its directory names; the suite's version, raised whenever its cases change |
| `cancel` | The case the cancellation check starts and abandons; optional |
| `cases[].id`, `endpoint` | A unique ID (it prefixes the case's checks), and the operationId to call |
| `cases[].request` | The JSON body. The string `"$model"` becomes the runner's model name, and `"$fill"` the text that pushes a limit case past its limit |
| `cases[].expect` | `{"framing": "json" \| "sse" \| "ndjson"}`: success with that framing. `{"kinds": [...]}`: an error of one of these base kinds |
| `cases[].valid` | `false` for a request that must break the entry's request schema (the check confirms it does) |
| `cases[].content` | `[{"in": "body" \| "last-line" \| "any-line" \| "last-event" \| "any-event", "pointer": "…"}]`: each must name a non-empty value. `where` keeps only what `pointer` names whose relative pointers name the given values, and `value` is a relative pointer to the content in each: `{"in": "any-event", "pointer": "", "where": {"/type": "response.output_text.delta"}, "value": "/delta"}` counts a Responses stream's text and not its reasoning |
| `cases[].work_reports` | `true` to require every measure the entry reports for the answer's framing |
| `cases[].limit` | `{"name": "context_tokens"}` or `{"name": "max_input_bytes"}`: an envelope limit, skipped when the runner doesn't advertise it. `{"input": "/messages"}`: the entry's `max_bytes` for that input |
| `cases[].request` fixtures | A string `"$fixture.<name>"` (lowercase letters, digits, `_`) anywhere in the request becomes that fixture's value. A case whose fixtures weren't given fails, so a report can't pass without them. Where a request holding fixtures breaks the schema, `<case>.request` names only the paths |
| `cases[].fetch` | `["$fixture.descriptor_url", …]`: URLs that must answer 2xx with a body once the answer succeeds, such as uploaded outputs and their completion record, checked as `<case>.fetch` |
| `cases[].when` | `{"/reasoning/default": "off"}`: envelope pointers and the values they must name; the case is skipped for a runner whose envelope doesn't declare each one |
| `note` | Prose for the steward, on the suite or a case |

**Adding a suite.** A steward adds `conformance/<namespace>/<name>.v<major>/suite.json` for an entry
in their namespace, and a reference runner for it in `tests/reference/profiles.py`: a `Behaviour`
that answers as the entry says, including its error formats. CI check 6
(`tests/test_suites.py`) runs every suite against its reference runner and fails a suite that has
none, one the reference runner fails, and one a deliberately broken runner passes; add a broken
case to `BROKEN` there for anything new the suite checks. Run one by hand with
`python tests/reference/server.py openai.chat.v2 --port 8765` and point `conform --url` at it.
Suites ship in registry releases, so a builder's run is pinned to a release like the entries.

## Readiness

`runner-contract ready` is the SPEC's readiness rule for an engine whose own health check runs no
self-test, such as vLLM's and SGLang's `/health` or Ollama's `/`. Run it beside the engine and
register its address as the node's `HealthURL`. It never carries the node's traffic. It is new in
0.3: until `v0.3.0` is tagged, install from `main`.

```sh
runner-contract ready --manifest runner.toml --engine-health http://vllm:8000/health --listen 0.0.0.0:8080
```

It answers every `GET` with a `livepeer.health.v1` body: 503 `starting` until a self-test passes,
then 200 `ready`. The self-test is each advertised profile's suite, its success cases only, for
the reasoning mode the envelope declares: the requests `conform` sends, judged by the entry's success
rule, with content where the suite looks. Timing, schemas and work reports are `conform`'s to judge.
After a pass it polls `--engine-health` every `--interval` seconds (10): when that stops answering
2xx it answers 503 `failed`, and when the engine is back it self-tests again before `ready`.
`--retest S` repeats the self-test while ready, for an engine whose health stays 2xx after it loses
its model.

**`--front`**, for a runner the node reaches at one address, such as a tunnel with a single
target: the service answers health at `--health-path` (`/healthz`) and relays every other request
to the engine at `--url` unchanged, streams included, so `RunnerURL` and `HealthURL` both name it.
Until it is ready it refuses calls with 503, which every entry maps to `unavailable`. A caller that
closes its connection closes the engine's, so cancellation reaches the engine, and an engine answer
of 500 or above (other than 503) runs the self-test again. Put the engine on a network only the
front shares. It takes `--manifest`, `--block`, `--envelope`, `--url`, `--model`, `--runner-id` and
`--registry` as `conform` does, `--timeout` (120) for each self-test request, and `--fixtures`: a
success case that needs fixtures it wasn't given is left out of the self-test, since minted inputs
expire.

**A runner you write** doesn't need the command: `livepeer_runner_contract.ready.Readiness` is the
same state machine, with your self-test as `test` and your own check (GPU, encoder, model) as
`engine`. Call `step()` on a timer, `fail(detail)` when a call shows the runner broken (it answers
`failed` at once and self-tests again on the next step), and serve `answer(*readiness.snapshot())`
as the health check's status and body.

## The manifest

`runner.toml` describes one registration. The tool builds the envelope and both registration formats
from it, so `Metadata` is never written by hand.

```toml
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
revision = "<40-hex commit>"

[[endpoints]]
name = "chat_completions"
profile = "openai.chat.v2"

[limits]
context_tokens = 131072

[revision]
images = ["vllm/vllm-openai@sha256:<digest>"]
settings = { max_model_len = 131072 }
```

`runner-contract block runner.toml` prints:

```toml
[[Runners]]
ID = "qwen38-rig2"
App = "chat"
Mode = "single-shot"
Capacity = 8
RunnerURL = "http://vllm:8000"
HealthURL = "http://vllm:8000/health"
HealthCode = 200
Status = "ready"
Version = "vllm-0.32.0-qwen38"
Metadata = '{"runner_contract":1,"deployment_revision":"sha256:…",…}'
[Runners.PriceInfo]
Price = 1.00
Currency = "usd"
Unit = "hour"
```

and `--format json` the dynamic registration, with the node's snake_case fields (`runner_id`,
`app`, `mode`, `capacity`, `runner_url`, `status`, `version`, `metadata`, `price_info`).

Every key:

| Key | Required | Goes to | Rule |
|---|---|---|---|
| `[runner] id` | Yes | `ID` / `runner_id` | 1 to 128 ASCII letters, digits, `-` or `_` (the node's rule) |
| `[runner] app` | Yes | `App` | A capability from the capability list, and nothing else |
| `[runner] mode` | Yes | `Mode` | `single-shot` or `persistent`: the capability's mode |
| `[runner] capacity` | Yes | `Capacity` | Requests (or sessions) it truly serves at once, 1 to 10,000 |
| `[runner] url` | Yes | `RunnerURL` | Absolute http or https |
| `[runner] health_url` | For a static block | `HealthURL` | Answers 200 only once a self-test of every profile passes. A dynamic runner heartbeats instead |
| `[runner] version` | No | `Version` | A build label, at most 64 characters, for information only |
| `[runner] admission` | No | envelope `admission` | `candidate` or `private` |
| `[price] amount` | Yes | `PriceInfo.Price` | A positive number, in USD |
| `[price] currency` | Yes | `PriceInfo.Currency` | `usd` |
| `[price] unit` | Yes | `PriceInfo.Unit` | `hour` (runner time) or `fixed` (per call): how the node charges |
| `[model] name`, `source`, `revision` | When it serves a model | envelope `model` | `source` is `<origin>:<id>`; `revision` immutable, where the source has one |
| `[[endpoints]] name`, `profile` | One or more | envelope `endpoints` | `name` is the operationId in the profile's entry |
| `[[endpoints]] path`, `method` | No | the endpoint | Only where they differ from the entry's defaults |
| `[[endpoints]] stop`, `channels = { in = […], out = […] }`, `idle_timeout_s`, `drain_timeout_s` | Persistent runners | the endpoint | Session keys; refused on a single-shot runner |
| `[limits]` | No | envelope `limits` | Name = number, only limits the runner enforces |
| `[revision] images` | Yes, one or more | the revision | Every image, pinned as `name@sha256:<64 hex>` |
| `[revision] settings` | No | the revision | The settings that change behavior. Floats warn: other languages print them differently |
| `[revision] presets` | No | the revision | Encoding profiles or presets offered; they join the wire profiles in the recipe's `profiles` |
| `[runtime] name`, `image`, `versions`, `encoder` | No | envelope `runtime` | True values only; `image` is a `sha256:` digest |
| `[capacity_group] id`, `slots` | No | envelope `capacity_group` | For registrations that share one GPU |
| `[attributes]` | No | envelope `attributes` | Name = string, number, boolean, or a list of them |
| `[reasoning] default` | No | envelope `reasoning` | `on` or `off`: whether the model reasons when a request doesn't say. Put the runtime setting that makes it in `[revision] settings` |

`deployment_revision` hashes `images`, the model's `source` and `revision`, the profiles (each
endpoint's profile once, plus `presets`) and `settings`, by the SPEC's recipe. The URL, capacity,
price, version and attributes stay out of it, so moving a tunnel or raising capacity keeps approvals.
The envelope carries `runner_id`, the manifest's `id`: Livepeer Node (at `bbe126e`) reports no runner ID
in discovery, and SPEC.md allows the key for exactly that. `check` refuses a block whose envelope names
another ID than its `ID`, since a gateway excludes such a runner. It isn't part of the revision.

## The library

```python
from livepeer_runner_contract import (
    Registry,
    Retry,
    log_ignored,
    parse_discovery,
    read_result,
    select,
    validate_request,
)

registry = Registry.load()  # the installed release; or Registry.load("path/to/release")
found = parse_discovery(discovery_json)  # one orchestrator's or a discovery service's answer
for ignored in found.ignored:
    log_ignored(logger, ignored.key, ignored.reason)

choices = select(
    found.runners,
    "chat",
    model="hf:Qwen/Qwen3.8-27B-AWQ",
    profile="openai.chat.v2",
    approvals=my_policy.approved,
    registry=registry,
)
choice = choices[0]
check = validate_request(registry, choice.profile, choice.name, body)  # before paying
if check.ok:
    ...  # call choice.runner.url + choice.path with choice.method, then:
result = read_result(registry, choice.profile, choice.name, status=200, content_type=ctype, body=answer)
if not result.success and result.error and result.error.retry is Retry.ELSEWHERE:
    ...  # the same request may go to another matching runner
```

### Envelopes

| Name | What it is |
|---|---|
| `parse_envelope(raw) -> Envelope \| Ineligible` | Reads a `Metadata` string. Checks the size of the raw string (1,024 bytes of UTF-8), one JSON object with no duplicate keys, `runner_contract: 1`, and the schema. Ignores unknown keys and never raises |
| `Envelope` | `deployment_revision`, `endpoints` (name to `Endpoint`), `model`, `runner_id`, `runtime`, `limits`, `admission`, `capacity_group`, `attributes`, `reasoning` (`Reasoning.ON`, `Reasoning.OFF`, or None when undeclared), `size`, and `profiles` |
| `Endpoint` | `name`, `profile`, `path`, `method`, and the session keys `stop`, `channels`, `idle_timeout_s`, `drain_timeout_s` |
| `Ineligible` | `reason` (an `IneligibleReason`: `no_metadata`, `too_large`, `not_json`, `not_v1`, `unknown_version`, `invalid`) and `detail`, which quotes only the schema, never the runner's values |
| `Admission` | `candidate` or `private`; `Admission.read` makes any other value `private` |
| `envelope_problems(value) -> list[str]` | Every schema problem, one line each |
| `deployment_revision(images, profiles, settings, model=None) -> str` | The SPEC's recipe, exactly; `canonical_json` is its JSON |

### The registry

| Name | What it is |
|---|---|
| `Registry.load(path=None, *, strict=True)` | Loads a release directory (holding `registry/index.json`) or a `registry/` directory; with no path, the installed `livepeer-runner-registry`. Checks each entry's sha256 against the index, that its path follows its ID, and resolves aliases and `extends`. A refused entry raises `RegistryError`, or with `strict=False` is left out and listed in `refused` |
| `registry.entry(id)`, `get(id)`, `canonical(id)`, `operation(id, endpoint)` | Lookups; every ID may be an alias. `UnknownProfile` and `UnknownEndpoint` are `LookupError`s |
| `registry.capabilities`, `release`, `refused` | The release's capability list, its tag (from the package), and refused entries |
| `Entry` | `id`, `kind`, `mode`, `capabilities`, `admission` (the default), `attempts`, `status`, `aliases`, `operations` (after `extends`), `x` (the root `x-livepeer`), `document` |
| `Operation` | `name`, `method`, `path`, `framings`, `success` (`SuccessRule`: `status`, `final`), `errors` (`ErrorMapping`s), `attempts`, `defined_in`, and the raw `x` and `spec` |
| `ErrorKind`, `Retry` | The base kinds, and each kind's fixed rule as `kind.retry`: `never`, `elsewhere`, `owner`, `after_fix` |

An entry that `extends` another serves the base's operations, with its own replacing any of the same
`operationId`, and takes any of the base's root keys it doesn't state (`readiness`, `presets`,
`cost_hint`…).

### Discovery and selection

| Name | What it is |
|---|---|
| `parse_discovery(answer) -> Discovery` | Reads a discovery answer (parsed or text): `runners` (each a `Runner` with its `Envelope`) and `ignored` (each with its `RunnerKey` and `Ineligible`). Never raises |
| `select(runners, capability, model=None, profile=None, attributes=None, approvals=frozenset(), *, registry, limits=None, approved_only=False, reasoning=None, model_revision=None) -> list[Selection]` | The matching endpoints, in the order the runners came. See below |
| `ApprovalKey(orchestrator, runner_id, deployment_revision, profile)` | What an approval names. `approvals` is a set of them, or a function answering for one, so the gateway can reload its policy at will |
| `Selection` | `runner`, `endpoint`, `profile` (the entry ID), `operation`, `admission`, `approval`, `approved`, and `name`, `path`, `method` to call |

`select` keeps a runner whose App ID is the capability and whose model's `name` or `source` is
`model`. Of its endpoints it keeps each whose profile is in the registry (directly or by alias), is a
wire profile serving the capability, has an operation of the endpoint's name, and is `profile` where
given; whose runner has each wanted attribute value; and whose runner advertises each wanted limit at
least as high (a limit it doesn't advertise doesn't match); and, where `reasoning` is given, whose
runner declares that `reasoning.default` (one that declares none doesn't match), and where
`model_revision` is given, whose model has that revision. An endpoint is `private` when the envelope
says so (or says anything unknown) or its entry's default admission is `private`; a private endpoint
needs an approval, and with `approved_only` every endpoint does. The registry is a required keyword:
without it the library couldn't tell which profiles the pinned release has, or which are private.

### Requests and answers

| Name | What it is |
|---|---|
| `validate_request(registry, profile, endpoint, body, *, content_type="application/json") -> RequestCheck` | Checks a JSON body (or bytes of one) against the operation's request schema; for other media types, only that the operation accepts the content type. `check.ok`, `check.problems` |
| `read_result(registry, profile, endpoint, *, status, content_type, body, request=…, headers=None) -> Result` | Applies SPEC.md's "How a gateway reads an answer": the error mappings in order, whatever the status, then the success rule. `success`, `framing`, `value`, `failure`, `error` (a `Classification`), and `work`: the work reports found, by measure, added up |
| `classify(registry, profile, endpoint, *, status, content_type=None, body=b"") -> Classification` | The first mapping that matches, else `internal`: `kind`, `retry`, `mapping` |
| `ResultReader(operation, *, status, content_type, max_buffer=16 MiB, request=…, headers=None)` | The same, fed chunk by chunk with `feed` as a stream passes through, then `finish()` or `classify()`. It keeps only the last NDJSON line or SSE event (testing `any-line` and `any-event` mappings as each passes) and a bounded JSON or error body |
| `framing_of(content_type, declared=())` | `text/event-stream` is `sse`, `application/x-ndjson` `ndjson`, JSON types `json`, anything else `binary` |
| `validate_response(registry, profile, endpoint, value, *, status, content_type) -> SchemaCheck \| None` | Checks an answer's decoded body, or one SSE event's data or NDJSON line, against the entry's response schema for that status (else its range, else `default`) and content type; None where the entry declares none |
| `schema_problems(errors, subject="the body") -> tuple[str, ...]` | Any schema's errors as the lines above, such as a conformance report's against `conformance_report_validator()` |

A problem says where and what the schema wants, never the value it refuses, so a gateway can return
it to the caller and log it: `at /messages/0/content/0/source/type: must be "base64"`, or
`the body is missing model`. Where a value matches none of a union's shapes, as a block of an unknown
shape in a list keyed by `type`, the line names the cause in the shape the value's type picks. The
chat entries pass types they don't describe, such as Codex's custom tools (SPEC.md, "What an entry
holds"), so their checks don't refuse coding agents.

The check is pure Python and costs about 15 to 45 ms for a 0.6 MB coding agent's request, about 25
times what parsing it costs, and grows with its size. An asyncio gateway runs it off the event loop
(`await asyncio.to_thread(validate_request, ...)`) so other calls' streams don't wait on it.

A success rule's final conditions, like error mappings, test `equals`, `matches` or `present`, so
`openai.responses.stateless.v2` can accept `completed` or `incomplete`. From 0.3 the library refuses
an entry whose conditions hold a key it doesn't know, rather than read the entry more loosely than
it says: 0.2 ignores `matches` in a final condition, so don't pair it with a release that has v2.
| `split_stream(framing, body)` | A whole SSE or NDJSON answer as its events or lines: `(event type, text)` pairs |
| `Operation.work_reports` | The entry's work reports, as `WorkReport`s: `measure`, `location`, `framing`, `event`, `pointer`, `header`, `aggregate` (`value`, `count`, `sum`, `length`), and `asked(request)` |

Conditions follow the schema: `in` is `body`, `last-line`, `any-line`, `last-event` or `any-event`;
`event`, `data`, `pointer` with `equals`, `matches` or `present`, and `status`, `framing` and
`content_type` on mappings. A `*` in a pointer stands for every item of an array or every member of
an object; a test passes when any value the pointer names passes, and `present: false` when it
names none. A work report reads the body, a header, the request, or the last or any line or event
(for any, the last match counts); the request it needs asking for is in `asked`. SSE follows the WHATWG
parsing rules, so an event the stream never finishes with a blank line doesn't count. `matches`
patterns run with Python's `re`; an entry whose pattern `re` can't compile is refused on load.

### Logging ignored runners

| Name | What it is |
|---|---|
| `log_ignored(logger, runner_key, reason, *, level=WARNING) -> bool` | Logs `ignoring runner <id> at <orchestrator>: <reason>; see https://github.com/NE-Build-Track/live-runner-standard` once per runner, and returns whether it logged. IDs and addresses that aren't plain tokens are quoted, so no runner can forge a log line |
| `forget_ignored(runner_key=None)` | Forgets one runner, or all, so it is logged again |

## Development

From the repository root, with [uv](https://docs.astral.sh/uv/) and the pins CI uses:

```sh
RUN="uv run --python 3.12 --no-project --with-requirements .github/scripts/requirements.txt \
  --with-requirements .github/scripts/requirements-dev.txt --with pydantic==2.14.3 \
  --with ./python/runner-registry --with ./python/runner-contract"
$RUN pytest -q python    # includes CI check 6, tests/test_suites.py
$RUN ruff check --line-length 120 python && $RUN ruff format --check --line-length 120 python
$RUN mypy --strict python/runner-contract/src python/runner-contract/tests
$RUN mypy --strict python/runner-registry/src python/runner-registry/tests python/check_dist.py
uv build --out-dir dist python/runner-registry && uv build --out-dir dist python/runner-contract
$RUN python python/check_dist.py dist
```

The library's tests build their own small registry, so they don't depend on the entries in
`registry/`; the conformance tests (check 6) read the repository's registry and suites, since they
check what will ship.
`python/runner-contract/hatch_build.py` and `python/runner-registry/hatch_build.py` are one file in
two places (a test keeps them equal); it copies the repository root's files into each build.

## License

[MIT](../../LICENSE).
