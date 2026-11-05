# livepeer-runner-registry

A registry release of the [live runner standard](https://github.com/NE-Build-Track/live-runner-standard),
as a Python package: the wire-profile entries and `registry/index.json`, the three schemas,
`capabilities.json` and the conformance suites (`conformance/`), exactly as they are at the
release's tag. The repository root is the only copy in git; the build copies the files in (see
`hatch_build.py`).

A gateway pins a release by installing its tag, and reads it with the gateway library,
[`livepeer-runner-contract`](../runner-contract/README.md):

```sh
pip install "livepeer-runner-registry @ git+https://github.com/NE-Build-Track/live-runner-standard.git@2026.11.1#subdirectory=python/runner-registry"
```

```python
from livepeer_runner_contract import Registry

registry = Registry.load()  # this package's data; checks every entry's sha256 against the index
```

It is never published to PyPI. Install it only by a direct git reference, as above, so no package
of the same name from another index can stand in for it.

## API

| Name | What it is |
|---|---|
| `data_dir() -> Path` | The release's root: `registry/`, `schemas/`, `capabilities.json` and `conformance/` |
| `release() -> str \| None` | The release tag this package holds, such as `2026.11.1`; `None` for a build of an untagged commit |
| `__version__` | The package version |

An editable install, or a source tree, carries no copy: `data_dir()` is then the checkout's root.

## Versions

The package version is the registry release's date tag: `2026.11.1`, `2026.11.2`…, so
`release()` names exactly what a gateway pinned. A build of any other commit reports the
placeholder in `pyproject.toml`, `2026.11.1.dev0`, which pip and uv sort before the first release
and `release()` reports as `None`.

The release workflow (`.github/workflows/release.yml`) sets the version. For each merge to
`main` that changes `registry/`, `schemas/`, `capabilities.json` or `conformance/`, it picks the next tag, makes one
commit on top of the merge that sets `version` in `python/runner-registry/pyproject.toml` to that
tag, and tags that commit. `main` keeps the placeholder, so no build of an untagged commit can claim
a release's version.
