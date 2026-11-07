# Conformance suites

Each wire profile's conformance suite, which `runner-contract conform` runs against a runner on the
runner's own host (SPEC.md, "Conformance, interim"). Suites ship in every registry release with the
entries, so a builder's run, and the report it attaches to an approval request, is pinned to one.

| Path | Holds |
|---|---|
| `common/suite.json` | The thresholds of the checks every suite runs: streamed headers within 1 s, keepalive within 15 s, cancellation, and the health body's shape (`livepeer.health.v1`) |
| `<namespace>/<name>.v<major>/suite.json` | One profile's suite: golden requests per endpoint, and what each answer must be |

A suite is data: one success case per framing the endpoint answers with, at least one declared
error (such as an invalid body, answered `input_invalid`), a case per limit the entry or the
runner's envelope declares, and work reports where the entry needs them asked for. Requests keep
model output loose: a case asks only for non-empty text where it looks. A success case's output
budget leaves a reasoning model room to think and still answer (512 tokens in the chat suites). A
case with `when` runs only for a runner whose envelope declares those values, as the chat suites'
`reasoning_off` cases do for `reasoning.default: off`. Content is the answer's text, not reasoning in
the same shape: the Responses suites count only `output_text` parts and
`response.output_text.delta` events, since a reasoning item carries text too. A case whose request
needs values minted for each run, such as an input URL on the deployment's storage, its sha256 or
an output grant, names them as `"$fixture.<name>"`, which the builder passes with `--fixtures`; its
`fetch` names the URLs to read the outputs back from. The format, every check,
and the report are in [`python/runner-contract/README.md`](../python/runner-contract/README.md#conformance).

## Adding a suite

1. Write `conformance/<namespace>/<name>.v<major>/suite.json` for an entry in your namespace, with
   `"conformance_suite": 1`, the entry's ID as `profile`, and `"version": "1"`. Raise `version` with
   every change to the cases.
2. Add a reference runner for the profile in
   [`python/runner-contract/tests/reference/profiles.py`](../python/runner-contract/tests/reference/profiles.py):
   a `Behaviour` that answers as the entry says, its errors included.
3. Run CI check 6 locally; it runs every suite against its reference runner, and against broken
   ones that must fail:

   ```sh
   uv run --python 3.12 --no-project --with-requirements .github/scripts/requirements.txt \
     --with-requirements .github/scripts/requirements-dev.txt \
     --with ./python/runner-registry --with ./python/runner-contract \
     pytest -q python/runner-contract/tests/test_suites.py
   ```

The suites here today: `openai.chat.v2`, `openai.responses.stateless.v2` and `anthropic.messages.v2` (and the
v1 of each, deprecated), `typesafe.systemone.v1` and `openai.embeddings.v1`. Session suites (idle and drain) come with the
first persistent profile's suite.
