# fal importer

Generates one registry entry per fal endpoint, `registry/fal/<name>.v<major>.json`, from fal's public
queue OpenAPI. Every entry extends [`fal.queue.v1`](../../registry/fal/queue.v1.json), which describes
how the Livepeer fal route adapter behaves for every endpoint. Each endpoint entry adds that endpoint's
own request and output schemas. [A daily workflow](#the-daily-workflow) runs it and proposes any new
majors; it also runs by hand.

## Running it

From the repository root, with [uv](https://docs.astral.sh/uv/):

```sh
RUN="uv run --python 3.12 --with-requirements .github/scripts/requirements.txt python -I"

$RUN importers/fal/import_fal.py --fetch    # fetch fal's OpenAPI for every endpoint, then generate
$RUN importers/fal/import_fal.py            # generate again from the cache, offline
$RUN importers/fal/import_fal.py --check    # write nothing; exit 1 if any entry would be written
$RUN importers/fal/import_fal.py --report r.json   # also record what was written, as JSON

# after any entry is written, regenerate the index
uv run --python 3.12 --with-requirements .github/scripts/requirements.txt \
  python .github/scripts/registry_ci.py index --write
```

`--fetch` is the only network access: one GET per endpoint to
`https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<endpoint ID>`, in turn, a second apart
(`--delay`). It needs no fal key. Documents are cached in `--cache` (default
`~/.cache/live-runner-standard/fal-openapi`). An endpoint whose fetch fails is reported and skipped,
and isn't regenerated from an older cached copy. `--endpoint <ID>` limits a run to some endpoints.

The importer is deterministic: the same endpoint list and the same fal documents give byte-identical
entries, whatever order fal writes its keys in.

`--report` records each entry written (its ID, path, endpoint ID, fal schema digest, and the entry and
digest it follows) and the endpoints not imported. [`report.py`](report.py) turns that record into the
workflow's commit message and issue, and refuses any value that isn't an ID, a path or a digest.

## The daily workflow

[`.github/workflows/fal-import.yml`](../../.github/workflows/fal-import.yml) runs every day at 05:23 UTC,
and by hand from the Actions tab (`workflow_dispatch`). From `main`, it:

1. runs the importer with `--fetch`, with the CI scripts' pinned requirements. If no fal schema
   changed, it stops there. It fails if the import changed anything but new files under
   `registry/fal/`, since released entries never change;
2. if an earlier `fal-import/*` branch already holds the same new entries byte for byte, skips to
   step 6 for that branch, so a rerun pushes nothing new;
3. regenerates `registry/index.json` and runs checks 1-5;
4. commits as `github-actions[bot]`, with no attribution lines, to `fal-import/<UTC date>` (or
   `fal-import/<UTC date>-<run number>` if that branch already exists), and pushes it;
5. dispatches [`validate`](../../.github/workflows/validate.yml) on the branch, whose checks attach to
   its commit;
6. opens an issue labelled `fal-import` (creating the label if needed) and assigned to eliteprox,
   listing each new entry with its fal endpoint ID and old and new fal schema digests, with the link
   that opens the pull request. An open issue for the same branch is updated instead, and a closed
   one is left closed.

The organization doesn't let GitHub Actions open pull requests, so someone other than eliteprox opens
it from the issue's link: he approves it as code owner, and GitHub doesn't let an author approve their
own pull request. The merge makes a registry release. A run still fails, after proposing what it
could, when any endpoint wasn't imported; its log says which and why.

When a branch's entries are superseded (a later run proposes them again with more changes, on a new
branch), close the older issue and delete its branch. To have a closed proposal made again, delete
its branch.

## What it reads

- [`endpoints.json`](endpoints.json): the endpoints to import, each with the capabilities it serves,
  chosen by hand from [`capabilities.json`](../../capabilities.json). `base` names the entry they
  extend. Adding an endpoint is a pull request here; its capabilities must be in the base entry's list.
- fal's OpenAPI for each endpoint, treated as untrusted data (see "fal's text").
- [`registry/fal/queue.v1.json`](../../registry/fal/queue.v1.json): the adapter's behavior, copied
  into each entry.

## What it writes

The name is the endpoint ID with a leading `fal-ai/` dropped, lowercased, and `/`, `_` and `.` turned
into `-`, with repeats collapsed: `ideogram/v4` is `fal.ideogram-v4.v1`, and `fal-ai/flux/schnell` is
`fal.flux-schnell.v1`. Two endpoints that would get the same name are refused.

Each entry holds:

- **The request:** fal's input schema for the endpoint and every component it reaches, with one
  example built from fal's own example and default values. Placeholders (`https://example.com/...`,
  minimum values) fill required fields where fal gives none, and the example says so. Every example
  validates against the entry's schema.
- **The answer:** the adapter's result body, with fal's output schema under `output`, or the adapter's
  error body.
- **Behavior:** `fal.queue.v1`'s operation `x-livepeer` (framing, success rule, ordered error
  mappings, cost hint, recovery), copied unchanged except for three keys. `inputs` and `outputs` name
  this endpoint's own URLs: top-level input fields fal fetches (`*_url`, `*_urls`, `format: uri`), and
  the URL of each output file object or URL field, down one level, with `*` for every item of a list
  (`/output/images/*/url`). Media types are guessed from field and component names. Deeper URL fields
  aren't listed; the schemas show them. For `text-to-image` and `image-edit` endpoints,
  `work_reports` counts the image URLs returned (`images`, `aggregate: count`).
- **`x-livepeer`:** `extends: fal.queue.v1`, its mode, admission, attempts and readiness, and the
  capabilities from `endpoints.json`.
- **`info.x-source`:** the endpoint ID, the OpenAPI URL, and the fal schema digest.

CI requires every operation to carry its own `x-livepeer`, so each entry repeats the adapter's instead
of inheriting it through `extends`.

## The fal schema digest and versions

`info.x-source.sha256` is the SHA-256 of the canonical JSON (as in SPEC.md) of
`{"components": {"schemas": ...}, "endpoint_id": ..., "input_schema": ..., "output_schema": ...}`:
fal's input root, its output root, and every component they reach, exactly as fal publishes them,
before any text is cleaned. The rest of fal's document (its OpenAPI version, `info`, servers and
queue status routes) isn't included, so relabeling the document makes no new version.

The digest is not the adapter's `schema_sha256`. The adapter's bundle also holds
`source.openapi_version`, which fal changed from `3.0.4` to `3.1.0` after the adapter's snapshot of
1 September 2026, so no adapter digest matches a fresh fetch.

For each endpoint, the importer:

- writes `v1` when the endpoint has no entry;
- leaves the latest entry alone when its digest matches (with `--rewrite`, before a release only, it
  rewrites one whose bytes differ because the importer changed);
- writes the next major (`v2`, `v3`…) when fal's schema changed, and never touches an existing
  entry. The steward deprecates the old one in `registry/fal/namespace.json` if needed.

Any change to fal's schema counts, including a description edit.

## fal's text

fal's schemas are untrusted. The importer copies what validates (types, enums, bounds, required
fields, defaults, `$ref`s) and `x-fal-order-properties`, for form order. It drops `examples`, fal's UI
hints and other `x-` extensions, collapses whitespace in titles and descriptions, and shortens
descriptions to 200 characters, ending at the first sentence where it can. It leaves out any
description that reads like an instruction to a model. It refuses an endpoint whose defaults or enum
values do. Only schema field titles and descriptions come from fal; the entry's own descriptions are
the importer's.

## Capability choices to review

Most endpoints map straight from fal's own category. These are judgement calls:

| Endpoint | Chosen | Why |
|---|---|---|
| `fal-ai/marlin` | `media-tools` | Captions a video (text out); no video-understanding capability |
| `fal-ai/mmaudio-v2` | `sound-effects`, `video-to-video` | Adds generated sound to a video |
| `fal-ai/whisper` | `transcription`, `translation` | `task: translate` translates speech into English |
| `*/reference-to-video` | `image-to-video` | Reference images, some also take reference video or audio |
