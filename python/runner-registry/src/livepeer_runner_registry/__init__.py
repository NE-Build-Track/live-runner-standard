"""A registry release of the live runner standard, as package data.

The data directory mirrors the repository root at the release's tag::

    registry/index.json         every entry by ID: path, sha256, steward, status and aliases
    registry/<namespace>/...    the entries
    schemas/*.json              the envelope, registry-entry and conformance-report schemas
    capabilities.json           the capability list

``livepeer_runner_contract.Registry.load()`` reads it and checks every entry's digest. This package
holds no code beyond finding the data.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

__all__ = ["__version__", "data_dir", "release"]

_RELEASE = re.compile(r"^[0-9]{4}\.[0-9]{1,2}\.[1-9][0-9]*$")

try:
    __version__ = metadata.version("livepeer-runner-registry")
except metadata.PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"


def data_dir() -> Path:
    """The release's root directory, holding ``registry/``, ``schemas/`` and ``capabilities.json``.

    An installed wheel carries the data in the package. An editable install or a source tree has no
    copy, so the data is the checkout's own repository root.
    """
    packaged = Path(__file__).resolve().parent / "data"
    if (packaged / "registry" / "index.json").is_file():
        return packaged
    parents = Path(__file__).resolve().parents
    if len(parents) > 4 and (parents[4] / "registry" / "index.json").is_file():
        return parents[4]
    raise FileNotFoundError(
        "this install of livepeer-runner-registry carries no registry data; reinstall it from a "
        "release tag of https://github.com/NE-Build-Track/live-runner-standard"
    )


def release() -> str | None:
    """The registry release this package holds, such as ``2026.11.1``, or None for a build of an untagged commit."""
    return __version__ if _RELEASE.match(__version__) else None
