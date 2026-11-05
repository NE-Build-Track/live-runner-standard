# Runner contract v1

**Version 1, locked on 5 November 2026. Maintained by NE-SPE.**

Runner contract v1 is the live runner standard. It is a clean break: every runner and gateway on the
network moves to it, and earlier metadata formats are ignored. Runners and gateways meet at two shared
contracts: a small envelope in each runner's registration, and a registry entry for each wire profile
it speaks. HTTP gateways, MCP servers, SDKs and consoles all read those two and build their own
surfaces, so no gateway's format becomes something a runner must speak.

This repository holds the machine-readable half of the standard:

| File | What it defines |
|---|---|
| [`schemas/envelope.v1.schema.json`](schemas/envelope.v1.schema.json) | The envelope |
| [`schemas/registry-entry.v1.schema.json`](schemas/registry-entry.v1.schema.json) | The `x-livepeer` extension of a registry entry |
| [`schemas/conformance-report.v1.schema.json`](schemas/conformance-report.v1.schema.json) | The conformance report |
| [`capabilities.json`](capabilities.json) | The capability list |
| [`registry/`](registry/) | The registry entries and their index |

Where this document and a schema disagree, the disagreement is a bug: report it, and until it is fixed
the stricter reading applies.

## The principle

Runners and gateways meet at two shared contracts and nowhere else: the envelope a runner publishes in
its registration, and the registry entry for each wire profile it speaks. A gateway reads only those
two, and each surface (an HTTP API, MCP tools, an SDK, a console) translates what its gateway knows. So
a new runner needs no gateway change, and a new gateway needs no runner change.

| Layer | Reads | Rule it follows |
|---|---|---|
| Runner | Its own code and settings | Publishes the envelope and behaves as its profiles' entries say |
| Node and orchestrator | The runner's registration | Carries the envelope unchanged in discovery, and meters wholesale usage |
| Registry | Nothing at run time | Holds one immutable entry per wire profile, reviewed by its steward |
| Gateway | Envelopes from discovery, and a pinned registry release | Selects runners and calls them as the entries say; keeps approvals in its own policy |
| Surfaces | What their gateway knows | HTTP routes, MCP tools, SDK methods and console forms come from registry entries |

The test for any field or rule: if it would mean nothing without one particular gateway, it belongs in
that gateway, not in the envelope or the registry.

## What every runner publishes

A runner publishes its registration fields, which the node defines, and an envelope in `Metadata` that
says what it serves and how to reach it. Everything that follows from a wire profile, such as framing,
errors and work reports, lives in the profile's registry entry.

### Registration fields

| Field | Rule |
|---|---|
| `App` | The capability, from the [capability list](capabilities.json): `chat`, `transcription`, `video-transcode`… |
| `Mode` | `single-shot` or `persistent` |
| `Capacity` | The requests or sessions the runner truly serves at once; the one place capacity is stated |
| `PriceInfo` | The wholesale price. Its unit must match how the node actually charges |
| `HealthURL` | Answers 200 only once the runner is proven ready |
| `Status` | `ready` only while health answers 200 |
| `Version` | The runner's build label, such as its image tag; at most 64 characters, for information only |
| `Metadata` | The envelope as compact JSON, at most 1,024 bytes exactly as registered |

`Version` carries no behavior: no gateway selects, approves or refuses a runner by it. The machine
identity of a deployment is `deployment_revision`.

### The envelope

| Key | Required | Holds |
|---|---|---|
| `runner_contract` | Yes | `1` |
| `deployment_revision` | Yes | `sha256:` and 64 lowercase hex digits, made by the recipe below; approvals key on it |
| `endpoints.{name}.profile` | Yes, one or more | The wire profile this endpoint speaks, as a registry ID |
| `endpoints.{name}.path`, `.method` | No | Only where they differ from the entry's defaults |
| `endpoints.{name}` session keys | Persistent runners | `stop`, `channels`, `idle_timeout_s`, `drain_timeout_s` |
| `model` | When the runner serves a model | `name`: what requests send. `source`: where it comes from, as `<origin>:<id>` (`hf:Qwen/Qwen3.8-27B-AWQ`, `fal:ideogram/v4`, `vendor:<vendor>/<model>`). `revision`: immutable, wherever the source has one |
| `runner_id` | No | The node's runner ID, when the node doesn't report it |
| `runtime` | No | `name`, `image` digest, `versions`, `encoder`: true values, or absent |
| `limits` | No | Only limits the runner enforces, such as `context_tokens` or `max_input_bytes` |
| `admission` | No | `candidate` (the default): any app may select it. `private`: only apps holding an approval. Any other value means `private` |
| `capacity_group` | No | `{id, slots}` for registrations that share one GPU |
| `attributes` | No | App-specific selection values, such as languages, presets or encoding profiles, passed through uninterpreted |

An endpoint's `{name}` is the `operationId` of that endpoint's operation in its profile's registry
entry, and matches `^[a-z][a-z0-9_]*$`.

A runner is `private` when its envelope or its profile's entry says so; an entry's default
`private` means every runner of that profile needs an approval.

Never in the envelope: prices, URLs, secrets, prose, capacity, approvals, any gateway's trust marks, or
usage and framing fields (the entry has those).

**Size.** The `Metadata` string, exactly as registered, is at most 1,024 bytes of UTF-8. The node and
every gateway carry each runner's metadata in every heartbeat and every discovery answer, and a
discovery answer over 1 MiB drops the whole orchestrator, so the limit is per runner and strict.

A two-endpoint chat runner's whole envelope, 381 bytes once the digest and revision are filled in:

```json
{"runner_contract":1,"deployment_revision":"sha256:…",
 "model":{"name":"qwen3.8-27b","source":"hf:Qwen/Qwen3.8-27B-AWQ","revision":"…"},
 "endpoints":{"chat_completions":{"profile":"openai.chat.v1"},"messages":{"profile":"anthropic.messages.v1"}},
 "limits":{"context_tokens":131072}}
```

### The `deployment_revision` recipe

`deployment_revision` is `sha256:` followed by the lowercase hex SHA-256 of one canonical JSON object
holding:

- **`images`:** every image, as its reference pinned by digest (`vllm/vllm-openai@sha256:…`), sorted;
- **`model`:** the model's `source` and `revision`, when there is a model;
- **`profiles`:** the wire profiles served, each once, and any encoding profiles or presets, sorted;
- **`settings`:** the settings that change behavior, such as context length, encoder, input-origin
  policy or parsers.

Canonical means UTF-8, keys sorted, no whitespace between tokens, and non-ASCII characters written as
themselves. In Python:

```python
import hashlib, json

def deployment_revision(images, profiles, settings, model=None):
    obj = {"images": sorted(images), "profiles": sorted(profiles), "settings": settings}
    if model is not None:
        obj["model"] = {"source": model["source"], "revision": model.get("revision")}
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
```

It leaves out what can change without changing behavior: the runner's URL, its capacity, its price,
timestamps, and any gateway's or tool's code. Moving a tunnel or raising capacity keeps the revision,
and so keeps approvals; changing the model or an image voids them. The same inputs give the same
revision on any machine.

## Capabilities and App IDs

A runner's App ID is its capability and nothing else: what it does, never who runs it, who defined it
or which gateway uses it. Which model it serves, which wire profiles it speaks and who may call it all
live in the envelope, and gateways select on those.

**The capability list** is [`capabilities.json`](capabilities.json), stewarded by NE-SPE and extended
by pull request:

| Kind | Capabilities |
|---|---|
| Text | `chat`, `embeddings`, `translation`, `decision` |
| Images | `text-to-image`, `image-edit`, `image-to-3d`, `text-to-3d` |
| Video | `text-to-video`, `image-to-video`, `audio-to-video`, `video-to-video`, `video-transcode` |
| Audio | `text-to-speech`, `transcription`, `music`, `sound-effects` |
| Media | `media-tools` |
| Live sessions | `live-video-to-video`, `live-transcode`, `live-transcription` |

**The rules:**

- **No names of a gateway, operator or team,** no model, and no contract version in an App ID.
- **A live form is its own capability** (`transcription` and `live-transcription`), so one capability
  never mixes sessions with single requests. Every `live-*` capability is `persistent`; every other
  capability is `single-shot`.
- **An App ID promises only the capability.** Whether two runners can stand in for each other is for
  each gateway's selection to decide.

**How a gateway finds what it can rely on:**

| Question | Answered by |
|---|---|
| What does a capability mean, and how is it called? | The capability list and the registry's wire profiles |
| What is running now, and where? | Discovery: each runner's App ID, price, capacity and envelope |
| What is on offer across the network? | A catalog that combines discovery from every orchestrator and groups it by capability, then model |
| What does my app rely on? | The gateway's own policy: capability, model and wire profile, with approvals for specific deployments |

A gateway depends on a capability, never on one runner. A policy reads, for example, "`chat`, model
`hf:Qwen/Qwen3.8-27B-AWQ`, profile `openai.chat.v1`, from these approved deployments". If one runner
disappears, any other approved runner that matches serves instead. The network doesn't promise
availability; an app that needs guaranteed capacity arranges it with operators, through `private`
admission and approvals.

**Examples of moving to v1:**

| Before v1 | App ID in v1 | The rest, in the envelope |
|---|---|---|
| `blueclaw/chat/qwen3.8-27b` | `chat` | Model `qwen3.8-27b`; profiles `openai.chat.v1` and others |
| `fal-ideogram-v4` | `text-to-image` | Model `fal:ideogram/v4`; profile `fal.ideogram-v4.v1` |
| `video/vod-transcode-v1` | `video-transcode` | Profile `zio.vod-transcode.v1`; `admission: private`; encoding profiles in `attributes` |
| `whisper` | `transcription` | Model `whisper-large-v3`; profile `openai.transcriptions.v1` |
| `live/flux-klein` | `live-video-to-video` | Model `flux-klein`; presets in `attributes` |

## Wire profiles and the registry

A wire profile names one way of talking to a runner, such as `openai.chat.v1`, and its contract lives
in one registry entry that every gateway reads. A gateway lists only endpoints whose profile is in the
registry release it pins.

**Naming.** `<namespace>.<name>.v<major>`, lowercase, with dots between parts. The namespace is
letters and digits; the name is one or more dot-separated parts of letters, digits and hyphens; the
major is a positive integer. As a pattern:
`^[a-z][a-z0-9]*(\.[a-z0-9]+(-[a-z0-9]+)*)+\.v[1-9][0-9]*$`. An entry's path follows its ID:
`openai.chat.v1` is `registry/openai/chat.v1.json`.

| Namespace | Steward | Review rule | Holds |
|---|---|---|---|
| `openai.*`, `anthropic.*`, `typesafe.*` | NE-SPE | A steward, plus one approver from a different gateway team | Livepeer copies of those public APIs, with the rules Livepeer adds |
| `livepeer.*` | NE-SPE | The same | Shapes everyone reuses: error kinds, input and output kinds, the conformance report, shared session and job shapes |
| `zio.*`, `blueclaw.*` | Cloud SPE | One steward | Ziply.io's and BlueClaw's own profiles |
| `fal.*` | eliteprox | One steward | `fal.queue.v1` and the endpoint entries generated from fal's OpenAPI |
| `comfystream.*` (provisional) | eliteprox | One steward | comfystream pipelines and eliteprox's other runners' own profiles |

A new namespace is a pull request that adds its directory and its `CODEOWNERS` line, approved by NE-SPE.

### What an entry holds

An entry is an OpenAPI 3.1.0 document with an `x-livepeer` extension at its root, defined by
[`registry-entry.v1.schema.json`](schemas/registry-entry.v1.schema.json). An entry is one of two kinds:

- **`profile`:** a wire profile runners speak. Each operation is one endpoint, its `operationId` is the
  endpoint's name, and its path and method are the endpoint's defaults. Each operation carries its own
  `x-livepeer` object.
- **`shared`:** a shape other entries reuse, such as the conformance report. It has `components` and no
  `paths`.

A profile entry holds:

- **Calls:** each endpoint's default path and method, and its request schema.
- **Answers:** the response framing (`json`, `sse`, `ndjson`, `binary`, or several, such as `json`
  and `sse` for an endpoint that streams on request, told apart by the answer's content type), the
  success rule, and the schema of the body, or of each line or event.
- **Errors:** every error the runner returns, mapped to a base kind.
- **Work reports:** what the runner reports about the work, such as tokens or audio seconds, where it
  appears, and how to ask for it.
- **Cost hint:** optional, such as typical duration or encode speed, so an app can estimate wholesale
  cost before calling.
- **Attempts:** whether the caller or the gateway owns retries.
- **Recovery,** for an adapter in front of a queued backend: where the answer carries the backend's
  request ID, and which operation collects a late result.
- **Admission and inputs:** the default admission, and the input and output kinds a caller must bring.
- **Media hints** (`x-media`) on schema fields that carry media, for forms and players.
- **Sessions,** for persistent profiles: channels, events, control calls and timeouts.
- **`extends`:** a base entry whose behavior this one shares, as each fal endpoint extends `fal.queue.v1`.
- **The capabilities** it serves, from the capability list.
- **Examples,** which validate against the entry's own schemas, **and a conformance suite.**

For `ndjson` and `sse` framing, the response schema and its examples describe one line or event.

Entries point into requests and answers with JSON pointers (RFC 6901), in which a `*` token stands for
every item of an array or every member of an object: `/data/*/url` names each image's URL. A work
report reads a number, counts the values a pointer names, sums them, or adds up the lengths of the
strings it names, and may read the request, such as
the characters of text to speak. Reports of the same measure add up. A condition on a `*` pointer, such as
`equals` or `matches`, holds when any value the pointer names satisfies it, and `present: false`
holds when it names none.

### The rules

- **Immutable entries.** Bytes included in a registry release never change; a change is a new major.
  The index lists each entry's sha256, a gateway refuses an entry whose bytes don't match, and an ID is
  never reused.
- **Aliases, not duplicates.** Where two names already mean the same thing, the index points both at
  one entry.
- **Derive to draft, review to publish, test to enforce.** A builder may generate a draft entry from a
  runner's code; the steward reviews it into the registry; conformance checks every runner against the
  published entry. Gateways never build tools or descriptions from a runner's own description.

### The index

[`registry/index.json`](registry/index.json) lists every entry by ID with its path, sha256, steward,
status (`active` or `deprecated`) and aliases. It is generated from the tree and never edited by
hand. A namespace's deprecations and aliases live in its own `registry/<namespace>/namespace.json`,
which its steward reviews:

```json
{"deprecated": ["openai.chat.v1"], "aliases": {"openai.audio.transcriptions.v1": "openai.transcriptions.v1"}}
```

An alias is an ID in the same namespace that no entry has. After a release, an entry's status and
aliases may change; nothing else about it may. Each registry release is a git tag named by date
(`2026.11.1`, `2026.11.2`…), and a gateway pins one.

## Errors

Every error a runner returns maps to one base kind, and each base kind carries a fixed retry rule, so
any gateway can decide whether to retry without knowing the profile. Runners keep their profile's own
error format: OpenAI-compatible runners still return OpenAI-shaped errors, and the entry maps them.

| Retry rule | Meaning | Base kinds |
|---|---|---|
| `never` | The request itself is wrong; a retry fails the same way | `input_invalid`, `input_unsupported` |
| `elsewhere` | No work started; the same request may go to another runner at once | `busy`, `unavailable` |
| `owner` | Work may have happened; only whoever owns the attempt may retry | `deadline_exceeded`, `output_failed`, `upstream_failed`, `internal` |
| `after_fix` | The caller must fix something first, such as access to an input | `input_unreadable` |

**How a gateway reads an answer.** It checks the entry's error mappings in order, and the first
that matches decides the kind, whatever the status. If none matches and the success rule holds, the
answer succeeded. Otherwise it failed, as `internal`. So an error a runner sends inside a stream, or in
the body of a 200, is still an error. A mapping can look at the status, the content type, the body,
the last or any NDJSON line, or the last or any SSE event, and test a value for equality, a regular
expression, or presence.

An error the entry doesn't map is `internal`.

Some mappings: Ziply.io's `encoder_unavailable` is `unavailable`, OpenAI's 429 is `busy`, and the fal
adapter's 504 after its deadline is `deadline_exceeded`, retried only by its owner.

These kinds cover failures inside the runner. A gateway classifies its own failures (access, payment,
reporting) separately.

## Inputs and outputs

Media and data move in and out of a runner in one of six kinds, defined once in `livepeer.*`. Each
profile declares which kinds it uses, per field (`x-media`) and per endpoint, and a gateway lists only
profiles whose kinds it can supply. This area is marked for a security review.

| Kind | Direction | What travels | Rules |
|---|---|---|---|
| `inline` | In and out | A value inside the JSON: text, or base64 with a media type | The profile states a size cap |
| `body` | In and out | The whole body is the media: multipart in, binary out | The content type is declared; within the gateway's body cap |
| `url` (in) | In | An HTTPS URL the runner fetches | Origin policy `public` or `deployment` (`deployment` means private); the runner refuses internal addresses; it checks sha256 where one is given |
| `url` (out) | Out | A URL the runner returns | It states when it expires, and it is fetchable without paying the node |
| `storage-grant` | In or out | Scoped access to the caller's storage: an S3 POST policy or a presigned PUT | Limited to one prefix; expires shortly after the deadline; a completion record is written last |
| `trickle` | In and out | A Trickle channel of segments, for sessions only | The segment media type is declared; segments are at most 10 MB (10,000,000 bytes); the reader keeps up |

Each rule closes a gap seen on the network before v1: outputs that answered 402 to anyone who hadn't
paid, output URLs that expired without saying so, and a runner that read only its own deployment's
storage without declaring it.

## Wholesale and retail

The standard covers wholesale usage only. Retail usage belongs to each enterprise app, and the two
don't need to align.

|  | Retail usage | Wholesale usage |
|---|---|---|
| Who pays whom | The app's customers pay the enterprise app (the gateway) | The gateway pays the network for runner time |
| Units | The app's own: tokens, work units, seconds, credits | The runner's `PriceInfo`: per second or hour of runner time, or a fixed price per call |
| Who measures it | The app | The node's payment protocol |
| Who defines it | Each app | The runner operator's price and the node |

What the standard asks of runners:

- **An honest price unit.** The unit in `PriceInfo` matches how the node actually charges.
- **An optional cost hint** in the registry entry, so an app can estimate wholesale cost before a call.
- **Work reports as wire facts.** Whatever the runner reports about the work is described in its entry,
  tagged from one vocabulary so generic tools can show it: `input_tokens`, `output_tokens`, `images`,
  `audio_seconds`, `media_seconds` and `characters`. Apps may use those reports to price retail, but
  they aren't billing units.

What stays with each app: its retail units and prices, whether it refuses a call with no work report,
how it meters work a runner doesn't report, and watching its margin between retail and wholesale.

## How every runner behaves

These rules hold for every profile, so a gateway can rely on them without knowing the runner. Where a
rule differs by profile, the entry says how.

| Concern | Rule |
|---|---|
| Readiness | `HealthURL` answers 200 only after a self-test of every advertised profile passes, and 503 with `{"status": "starting"}` or `{"status": "failed"}` otherwise. A runner that loses its model, GPU or upstream vendor returns to 503. A health check that always answers 200 doesn't conform |
| Headers | Response headers within 1 s, since payment starts when they arrive |
| Keepalive | A streamed answer sends a line, event or byte at least every 15 s |
| Success | Exactly as the entry's success rule says; a 200 alone is not success where the entry says otherwise |
| Errors | In the profile's own format, every one listed in the entry and mapped to a base kind |
| Work reports | Where the entry says, when the caller asks for them as the entry describes |
| Limits | Enforced as advertised, refused with a declared error before heavy work starts |
| Cancellation | A closed connection or stopped session ends the work within 10 s: processes killed, GPU memory freed, uploads aborted |
| Attempts | No deduplication unless the entry says so; when the caller owns attempts, the entry says `attempts: caller` |
| Lost answers | A runner that writes to the caller's storage writes a completion record last; an adapter for a queued backend returns the backend's request ID, so a late result can still be collected |
| Inputs and outputs | Only the kinds the entry declares, by their rules |
| Proxies | A runner that forwards to a vendor checks the vendor in its self-test and maps the vendor's errors to base kinds |
| Price | The `PriceInfo` unit matches how the node charges for the work |

## What every gateway does, and must not do

A gateway reads only v1 envelopes, takes behavior only from the registry, and keeps its own trust
decisions to itself.

**A gateway must:**

1. **Read only `runner_contract: 1` envelopes.** Ignore unknown keys within the version. An unknown
   version, a broken required key or a record over 1,024 bytes makes that runner alone ineligible, and
   never fails a discovery refresh.
2. **Log each runner it ignores** once, with the reason and a link to this repository.
3. **Select by capability, model and wire profile,** then by attributes, limits and admission, and fail
   over only among runners that match.
4. **Take behavior from the registry entry:** paths, framing, the success rule, errors, work reports,
   attempts and input and output kinds.
5. **Pin a registry release** and check each entry's digest against its index.
6. **Keep approvals in its own policy,** keyed on orchestrator, runner, `deployment_revision` and
   profile, and reloadable without a restart.
7. **Honor `admission: private`,** and treat any value it doesn't know as `private`.
8. **Check a request against the entry's schema** before paying.
9. **Retry by the base kind's rule,** only where the app opts in, and never when the entry says
   `attempts: caller`.
10. **Build its surfaces from the registry:** HTTP routes, MCP tools, SDK methods and console forms come
    from entries, not from code per runner.

**A gateway must not:**

- **Base behavior on an App ID** beyond the capability it names, or on any operator-chosen name.
- **Read keys the envelope doesn't define.**
- **Put its own trust, prices or routes** into the envelope or the registry.
- **Show metadata strings to people or models as text.** Only reviewed registry text becomes
  descriptions; metadata values are data.
- **Fetch anything a runner's metadata points to.** Callers can't verify runner-supplied URLs, and each
  fetch through the node costs money and a capacity slot.

## Sessions and live media

A persistent runner serves sessions instead of requests, under a `live-*` capability. It declares each
session endpoint in its envelope, and its registry entry describes what flows through it.

A live transcoder's session endpoint, in its envelope (App ID `live-transcode`):

```json
"endpoints": {"session": {"profile": "zio.live-transcode.v1", "path": "/v1/live/start", "stop": "/v1/live/stop",
  "channels": {"in": ["source"], "out": ["renditions-*", "events"]},
  "idle_timeout_s": 30, "drain_timeout_s": 10}}
```

The registry entry adds:

- **Each channel's** direction, media type, segment size cap and timing.
- **The event schema,** with a counter such as `epoch` so a reader can drop stale events.
- **Control calls** beyond start and stop, such as update, pause and resume.
- **Presets,** where the profile takes a named preset instead of parameters; `attributes` lists the
  ones this runner offers.

Rules for sessions:

- **An idle timeout is required** before a persistent runner is `candidate`, so another caller's silent
  source can't hold the only slot.
- **`Capacity` counts sessions,** and a runner refuses one it can't serve with a `busy` error.
- **A gateway that can't carry the channels skips the profile;** one that speaks HTTP but not Trickle
  doesn't list a Trickle profile.

## Conformance, interim

A runner proves each profile it advertises with that profile's conformance suite before it registers
as `ready`, and gateways approve on the evidence instead of the operator's word. This is the interim
approach, marked to revisit: continuous checks against real traffic, a shared probe service or attested
evidence may serve better in the long run.

- **Each profile has a suite** in `conformance/<namespace>/<name>.v<major>/`. Every suite checks the
  envelope (valid, at most 1,024 bytes, revision recomputes), health semantics, golden requests against
  the schemas and success rule, work reports, declared errors, limits and cancellation; session suites
  add idle and drain.
- **Builders run it** with the builder tool's `conform` command on the runner's own host, which needs no
  payment.
- **It writes a report** in the format of
  [`conformance-report.v1.schema.json`](schemas/conformance-report.v1.schema.json): runner ID,
  `deployment_revision`, profile, suite and tool versions, time, each check's result and an overall
  pass. Reports aren't signed.
- **The builder attaches the report to an approval request,** and the gateway stores it with the
  approval.
- **Gateways decide what to require:** a report, a recent report, their own paid probe, or nothing.

## Versioning

Within v1 the envelope gains only optional keys; a breaking change to the envelope or the gateway rules
is `runner_contract: 2`. Profiles never change once released. The standard is versioned with a
[changelog](CHANGELOG.md), and changes by review among the namespace stewards.

| What | Version | Changes when |
|---|---|---|
| The standard | `runner_contract: 1`, stated here | A breaking change to the envelope or the gateway rules makes `runner_contract: 2` |
| Registry releases | By date: `2026.11.1`, `2026.11.2`… | Every merge that changes `registry/`, `schemas/` or `capabilities.json`, tagged automatically |
| Each entry | The major in its ID, as in `openai.chat.v1` | Never; a breaking change is a new entry with the next major |
| The builder tool and gateway library | Semver tags (`v0.1.0`…), `0.x` until the first projects have switched | Ordinary code changes; each release names the standard version it implements |

## Open questions

- **Conformance, long-term:** continuous checks, a shared probe service or attested evidence, in place
  of the interim report.
- **Input and output kinds:** a security review, starting with runner-side URL fetching, output URLs and
  storage-grant scoping.
- **Outside operators:** onboarding beyond this repository and the ignored-runner log line.
- **eliteprox's namespaces:** `comfystream.*` is provisional, and music, meshes and image
  transformations may become shared profiles instead. Names can change at no cost until the first
  registry release.
- **Model `source` origins:** settle the list (`hf`, `ollama`, `fal`, `vendor`) and each one's
  identifier format.
- **The capability list:** names may shift once the first real entries are written.
- **Admission and capacity:** how a one-slot runner is shared before it becomes `candidate`.
