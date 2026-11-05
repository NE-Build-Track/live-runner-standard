# Live runner standard

This repository holds runner contract v1, the live runner standard: how a live runner tells every
gateway on the Livepeer network what it does and how to call it.

A runner publishes a small **envelope** in its registration metadata, and speaks one or more **wire
profiles**, each described by an entry in this repository's **registry**. Gateways of every kind
(HTTP APIs, MCP servers, SDKs, consoles) read those two and nothing else, so a new runner needs no
gateway change and a new gateway needs no runner change. [`SPEC.md`](SPEC.md) is the standard.

v1 is a clean break. Gateways ignore runners in any earlier metadata format and log a link here.

## What's here

| Path | Holds |
|---|---|
| [`SPEC.md`](SPEC.md) | The standard: the envelope, capabilities, wire profiles, errors, inputs and outputs, and how runners and gateways behave |
| [`capabilities.json`](capabilities.json) | The capability list, which is the only thing a runner's App ID may be |
| [`schemas/`](schemas/) | JSON Schemas for the envelope, a registry entry's `x-livepeer` extension and the conformance report |
| [`registry/`](registry/) | One entry per wire profile, by namespace, and the generated `index.json` |
| [`conformance/`](conformance/) | Each profile's conformance suite, which `runner-contract conform` runs against a runner |
| [`CODEOWNERS`](CODEOWNERS) | Each namespace's steward |
| [`CHANGELOG.md`](CHANGELOG.md) | Changes to the standard |
| [`python/`](python/) | The `runner-contract` builder tool and the Python gateway library ([`livepeer-runner-contract`](python/runner-contract/README.md)), and the registry as a package ([`livepeer-runner-registry`](python/runner-registry/README.md)) |
| [`importers/fal/`](importers/fal/) | The importer that generates the `fal.*` endpoint entries from fal's public OpenAPI |

Still to come: steward review of the first entries (a change to a released entry is a new major);
`comfystream.*` entries; and conformance suites for the other profiles.

## Building a runner

1. **Pick its capability** from [`capabilities.json`](capabilities.json), such as `chat` or
   `video-transcode`. That is its App ID, with nothing else in it: no model, team or version.
2. **Pick its wire profiles** from [`registry/`](registry/), such as `openai.chat.v1`. If none fits,
   propose an entry in your namespace by pull request (see [Contributing](#contributing)).
3. **Write its envelope** as [`SPEC.md`](SPEC.md) describes: `runner_contract: 1`, the
   `deployment_revision` from the recipe, one endpoint per profile, and the model it serves. Keep it
   under 1,024 bytes. `runner-contract envelope` builds it from a short manifest.
4. **Make it behave** as "How every runner behaves" in [`SPEC.md`](SPEC.md) says: honest readiness,
   errors mapped by its entry, work reports, cancellation within 10 s, and an honest price unit.
5. **Register it** with `App` set to the capability and `Metadata` set to the envelope.
   `runner-contract block` prints the node's `[[Runners]]` block and `runner-contract check`
   validates one.

The [`runner-contract` tool](python/runner-contract/README.md) installs from this repository's tags:

```sh
uvx --from "git+https://github.com/NE-Build-Track/live-runner-standard.git@v0.2.0#subdirectory=python/runner-contract" \
  runner-contract init --capability chat --profile openai.chat.v1
```

## Building a gateway

A gateway reads only `runner_contract: 1` envelopes, selects runners by capability, model and wire
profile, takes every behavior from the registry entry, and pins a registry release. "What every
gateway does, and must not do" in [`SPEC.md`](SPEC.md) lists the rules. The Python gateway library,
[`livepeer-runner-contract`](python/runner-contract/README.md), implements reading envelopes, selection,
request checks and the answer rules; a gateway pins a registry release by installing
[`livepeer-runner-registry`](python/runner-registry/README.md) from its tag.

## Contributing

Anyone may propose a change by pull request: a new entry, a new major of an entry, an alias, a
deprecation, or a change to the spec, the schemas or the capability list.

1. Open a pull request.
2. CI runs the checks below.
3. The path's steward, named in [`CODEOWNERS`](CODEOWNERS), reviews it.
4. It merges, and ships in the next registry release.

| Path | Steward | Approvals |
|---|---|---|
| `SPEC.md`, `schemas/`, `capabilities.json`, `CODEOWNERS`, `python/`, `.github/` | NE-SPE (`@NE-Build-Track/ne-spe`) | Two: an NE-SPE steward, plus one from Cloud SPE or eliteprox |
| `registry/livepeer/`, `openai/`, `anthropic/`, `typesafe/`, and their suites | NE-SPE | The same two |
| `registry/zio/`, `registry/blueclaw/`, and their suites | Cloud SPE (`@NE-Build-Track/cloud-spe`) | One |
| `registry/fal/`, `importers/fal/` | eliteprox | One |
| `registry/comfystream/` (provisional) | eliteprox | One |

GitHub enforces one code-owner approval on every path. The second approval on NE-SPE's paths is the
reviewer's rule.

A new namespace is a pull request that adds its directory and its `CODEOWNERS` line, approved by
NE-SPE. A namespace's deprecations and aliases go in its `registry/<namespace>/namespace.json`.

New majors of `fal.*` entries come from [the daily fal import](.github/workflows/fal-import.yml). GitHub
Actions can't open pull requests here, so it pushes a `fal-import/<date>` branch and opens an issue
for eliteprox with the link that opens the pull request
([`importers/fal/`](importers/fal/README.md#the-daily-workflow)).

**What a reviewer checks in an entry:**

- It declares a success rule, maps every error to a base kind, and states its input and output kinds,
  attempts owner and work reports.
- Its examples validate against its own schemas, and it ships a conformance suite.
- Its ID follows the naming rule and its capabilities are on the list.
- It carries no gateway-specific meaning, and no text written to steer a model.

Commit messages and pull requests carry no co-author or other attribution lines.

## CI

Every pull request runs six checks before a steward sees it:

1. **Schemas.** The three schemas are valid JSON Schema 2020-12. Every entry is a valid OpenAPI 3.1.0
   document whose `x-livepeer` objects match
   [`registry-entry.v1.schema.json`](schemas/registry-entry.v1.schema.json), and every example
   validates against the schema beside it.
2. **Immutability.** An entry included in a registry release can't change or disappear; CI compares the
   tree with the latest release tag. After a release, only an entry's status and aliases change.
3. **The index.** `registry/index.json` must equal what CI generates from the tree.
4. **Names and links.** Each ID matches its path, each namespace has a `CODEOWNERS` line, every
   `extends` target and alias target exists, and every capability an entry names is on the list.
5. **The capability list.** Names are unique, lowercase and hyphenated, and only `live-*` capabilities
   are persistent.
6. **Conformance suites.** Every suite in [`conformance/`](conformance/) passes against a reference
   runner for its profile, and fails against deliberately broken ones (in the `python` job).

Run them locally with [uv](https://docs.astral.sh/uv/):

```sh
uv run --python 3.12 --with-requirements .github/scripts/requirements.txt \
  python .github/scripts/registry_ci.py all

# after adding or changing an entry, regenerate the index
uv run --python 3.12 --with-requirements .github/scripts/requirements.txt \
  python .github/scripts/registry_ci.py index --write
```

## Releases

Each merge that changes `registry/`, `schemas/`, `capabilities.json` or `conformance/` becomes a
registry release, tagged by date (`2026.11.1`, `2026.11.2`…) by
[the release workflow](.github/workflows/release.yml). A gateway pins one. The builder tool and the gateway library are tagged separately, by semver
(`v0.1.0`…). Neither is published to PyPI; both install from this repository's tags. The latest
are `2026.11.2` and `v0.2.0`; see [Releases](https://github.com/NE-Build-Track/live-runner-standard/releases).

## License

[MIT](LICENSE).
