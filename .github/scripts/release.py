"""Helpers for the release workflow: the next registry release tag, and its notes.

    python .github/scripts/release.py next-tag    # prints tag=<next tag>, or nothing if no release is due
    python .github/scripts/release.py notes TAG   # prints the release notes for TAG

A registry release is due when registry/, schemas/, capabilities.json or conformance/ differ from
the latest release. Tags are by date: <year>.<month>.<n>, with n counting from 1 within each month (UTC).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from registry_ci import RELEASE_TAG, Repo, latest_release

RELEASED_PATHS = ("registry", "schemas", "capabilities.json", "conformance")
ROOT = Path(__file__).resolve().parents[2]
REPO_GIT = "git+https://github.com/NE-Build-Track/live-runner-standard.git"


def next_tag(tags: list[str], now: datetime) -> str:
    """The next date tag after the given tags, for the month of now."""
    prefix = f"{now.year}.{now.month}."
    numbers = [int(m.group(3)) for t in tags if (m := RELEASE_TAG.match(t)) and t.startswith(prefix)]
    return f"{prefix}{max(numbers, default=0) + 1}"


def release_due(repo: Repo) -> bool:
    previous = latest_release(repo)
    if previous is None:
        return True
    changed = subprocess.run(
        ["git", "diff", "--quiet", previous, "HEAD", "--", *RELEASED_PATHS], cwd=repo.root, check=False
    )
    return changed.returncode != 0


def entries_at(repo: Repo, ref: str | None) -> dict[str, dict[str, object]]:
    if ref is None:
        return {}
    data: dict[str, dict[str, object]] = json.loads(repo.git("show", f"{ref}:registry/index.json")).get("entries", {})
    return data


def notes(repo: Repo, tag: str, previous: str | None) -> str:
    before = entries_at(repo, previous)
    after = entries_at(repo, tag)
    added = sorted(set(after) - set(before))
    deprecated = sorted(
        e
        for e, v in after.items()
        if v.get("status") == "deprecated" and before.get(e, {}).get("status") != "deprecated"
    )
    lines = [f"Registry release {tag} of runner contract v1: {len(after)} entries."]
    if previous:
        lines.append(f"Changes since {previous}:")
    lines.append("")
    if added:
        lines.append(f"Added ({len(added)}):")
        lines += [f"- `{e}`" for e in added]
    if deprecated:
        lines.append(f"Deprecated ({len(deprecated)}):")
        lines += [f"- `{e}`" for e in deprecated]
    if not added and not deprecated:
        lines.append("No entries added or deprecated; schemas, aliases or the capability list changed.")
    lines += [
        "",
        "Pin it in a gateway:",
        "",
        "```",
        f"livepeer-runner-registry @ {REPO_GIT}@{tag}#subdirectory=python/runner-registry",
        "```",
        "",
        "Attached: the index (`index.json`) and the registry, schemas and capability list as a tarball.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    repo = Repo(ROOT)
    if argv[:1] == ["next-tag"]:
        if release_due(repo):
            tags = repo.git("tag", "--list").decode().split()
            print(f"tag={next_tag(tags, datetime.now(UTC))}")
        return 0
    if len(argv) == 2 and argv[0] == "notes":
        tag = argv[1]
        tags = [t for t in repo.git("tag", "--list").decode().split() if RELEASE_TAG.match(t) and t != tag]
        older = [t for t in tags if _key(t) < _key(tag)]
        print(notes(repo, tag, max(older, key=_key) if older else None), end="")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


def _key(tag: str) -> tuple[int, ...]:
    match = RELEASE_TAG.match(tag)
    assert match is not None
    return tuple(int(g) for g in match.groups())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
