"""Copies files from the repository root into this package's build.

The repository root is the single source of truth for the standard's files (registry/, schemas/,
capabilities.json, LICENSE), so no package commits a copy. The ``copy`` table under
``[tool.hatch.build.hooks.custom]`` in pyproject.toml maps each root path to its place in the wheel.

Two build paths must ship the same bytes:

- A wheel built from a checkout (pip or uv installing from a git subdirectory, ``pip wheel .``)
  reads the files from ``../../``.
- An sdist carries the files under ``_repo/``, so a wheel built from that sdist (``uv build``,
  ``python -m build``) reads them there, where ``../../`` no longer exists.

An editable install copies nothing: the package then reads the checkout itself.

python/runner-contract/hatch_build.py and python/runner-registry/hatch_build.py are the same file, and
a test keeps them so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

STAGED = "_repo"  # where an sdist carries the copied files


class CopyFromRepository(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            return
        copies: dict[str, str] = self.config.get("copy", {})
        source_root = self._source_root(copies)
        for source, target in copies.items():
            destination = f"{STAGED}/{source}" if self.target_name == "sdist" else target
            build_data["force_include"][str(source_root / source)] = destination

    def _source_root(self, copies: dict[str, str]) -> Path:
        project = Path(self.root)
        candidates = (project / STAGED, project.parent.parent)
        for candidate in candidates:
            if all((candidate / source).exists() for source in copies):
                return candidate
        missing = ", ".join(sorted(copies))
        raise FileNotFoundError(
            f"can't find {missing} in {candidates[0]} (an sdist) or {candidates[1]} (the repository root); "
            "build from a checkout of live-runner-standard or from its sdist"
        )
