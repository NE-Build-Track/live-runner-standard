"""Turn the importer's --report file into what the fal import workflow publishes.

    python importers/fal/report.py paths REPORT     # the files the import wrote, one per line
    python importers/fal/report.py commit REPORT    # the commit message
    python importers/fal/report.py issue REPORT --branch B --repository OWNER/NAME --server URL --run-url URL

Every value is checked against a strict pattern before it's printed, since the issue is public; the
report holds only endpoint IDs from endpoints.json, registry IDs, paths and hex digests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$")
PROFILE_ID = re.compile(r"^fal\.[a-z0-9]+(-[a-z0-9]+)*\.v[1-9][0-9]*$")
PATH = re.compile(r"^registry/fal/[a-z0-9]+(-[a-z0-9]+)*\.v[1-9][0-9]*\.json$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
BRANCH = re.compile(r"^fal-import/[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9]+)?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
URL = re.compile(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/%-]*)?$")


def checked(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise SystemExit(f"report.py: refusing {what} {value!r}")
    return value


def load(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("fal_import_report") != 1:
        raise SystemExit("report.py: not a fal import report")
    for entry in report["entries"]:
        checked(entry["endpoint_id"], ENDPOINT_ID, "endpoint ID")
        checked(entry["profile"], PROFILE_ID, "registry ID")
        checked(entry["path"], PATH, "path")
        checked(entry["sha256"], DIGEST, "digest")
        if entry["replaces"] is not None:
            checked(entry["replaces"]["profile"], PROFILE_ID, "registry ID")
            checked(entry["replaces"]["sha256"], DIGEST, "digest")
    for endpoint_id in report["not_imported"]:
        checked(endpoint_id, ENDPOINT_ID, "endpoint ID")
    return report


def commit_message(report: dict[str, Any]) -> str:
    entries = report["entries"]
    changed = sum(1 for e in entries if e["replaces"] is not None)
    added = len(entries) - changed
    parts = [f"next majors for {changed} changed fal schema{'s' * (changed != 1)}"] if changed else []
    if added:
        parts.append(f"{added} new endpoint{'s' * (added != 1)}")
    lines = [f"fal import: {' and '.join(parts)}", ""]
    for e in entries:
        tail = f", after {e['replaces']['profile']}" if e["replaces"] else ""
        lines.append(f"- {e['profile']} ({e['endpoint_id']}){tail}")
    lines += ["", "Written by importers/fal/import_fal.py from fal's public queue OpenAPI."]
    return "\n".join(lines) + "\n"


def issue_body(report: dict[str, Any], branch: str, repository: str, server: str, run_url: str) -> str:
    branch = checked(branch, BRANCH, "branch")
    repository = checked(repository, REPOSITORY, "repository")
    base = f"{checked(server, URL, 'server URL')}/{repository}"
    run_url = checked(run_url, URL, "run URL")
    compare = f"{base}/compare/main...{branch}?expand=1"
    validate = f"{base}/actions/workflows/validate.yml?query=branch%3A{branch.replace('/', '%2F')}"
    lines = [
        (
            f"The daily fal import found changed fal schemas and pushed branch `{branch}` with the next "
            "major of each entry below. Released entries are unchanged."
        ),
        "",
        "| New entry | fal endpoint | Replaces | fal schema digest, old → new |",
        "|---|---|---|---|",
    ]
    for e in report["entries"]:
        old = e["replaces"]
        replaces = f"`{old['profile']}`" if old else "(new endpoint)"
        digests = f"`{old['sha256']}` → `{e['sha256']}`" if old else f"`{e['sha256']}`"
        lines.append(f"| `{e['profile']}` | `{e['endpoint_id']}` | {replaces} | {digests} |")
    lines += [
        "",
        f"**Open the pull request:** {compare}",
        "",
        (
            "GitHub Actions can't open pull requests in this organization, so this issue stands in for one. "
            f"[validate]({validate}) runs on the branch, and runs again on the pull request. The fal "
            "steward approves it as code owner of `registry/fal/`, and GitHub doesn't let a pull request's "
            "author approve it."
        ),
        "",
        (
            "Before merging, check each new entry's capabilities and text against its previous major, and "
            "deprecate a previous major in `registry/fal/namespace.json` once runners should move off it. "
            "The merge makes a registry release."
        ),
    ]
    if report["not_imported"]:
        skipped = ", ".join(f"`{e}`" for e in report["not_imported"])
        lines += ["", f"Not imported in this run (see the run log): {skipped}."]
    lines += ["", f"Run: {run_url}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["paths", "commit", "issue"])
    parser.add_argument("report", type=Path)
    parser.add_argument("--branch", default="")
    parser.add_argument("--repository", default="")
    parser.add_argument("--server", default="https://github.com")
    parser.add_argument("--run-url", default="")
    args = parser.parse_args(argv)
    report = load(args.report)
    if args.what == "paths":
        sys.stdout.write("".join(e["path"] + "\n" for e in report["entries"]))
    elif args.what == "commit":
        sys.stdout.write(commit_message(report))
    else:
        sys.stdout.write(issue_body(report, args.branch, args.repository, args.server, args.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
