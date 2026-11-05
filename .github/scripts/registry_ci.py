"""CI checks 1-5 for the live runner standard.

    python .github/scripts/registry_ci.py all              # every check
    python .github/scripts/registry_ci.py index --write    # regenerate registry/index.json

Each check prints one line per failure and exits 1 if there was any. README.md ("CI") says what
each check enforces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from openapi_spec_validator import OpenAPIV31SpecValidator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

PROFILE_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+(-[a-z0-9]+)*)+\.v[1-9][0-9]*$")
OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
RELEASE_TAG = re.compile(r"^([0-9]{4})\.([0-9]{1,2})\.([1-9][0-9]*)$")
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
CAPABILITY_GROUPS = {"text", "images", "video", "audio", "media", "live"}
SCHEMA_FILES = (
    "envelope.v1.schema.json",
    "registry-entry.v1.schema.json",
    "conformance-report.v1.schema.json",
)
INDEX = "registry/index.json"
NAMESPACE_FILE = "namespace.json"


@dataclass(frozen=True)
class Entry:
    id: str
    path: str  # relative to the repository root
    raw: bytes
    doc: Any  # None when the file isn't valid JSON


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read_json(self, rel: str) -> Any:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def registry_files(self) -> list[str]:
        base = self.root / "registry"
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())

    def entries(self) -> list[Entry]:
        """Every file at registry/<namespace>/<name>.v<major>.json."""
        found = []
        for rel in self.registry_files():
            parts = PurePosixPath(rel).parts
            if len(parts) != 3 or not parts[2].endswith(".json") or parts[2] == NAMESPACE_FILE:
                continue
            raw = (self.root / rel).read_bytes()
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
            found.append(Entry(f"{parts[1]}.{parts[2][: -len('.json')]}", rel, raw, doc))
        return found

    def namespace_files(self) -> dict[str, Any]:
        """registry/<namespace>/namespace.json, by namespace: each one's deprecations and aliases."""
        out = {}
        for rel in self.registry_files():
            parts = PurePosixPath(rel).parts
            if len(parts) == 3 and parts[2] == NAMESPACE_FILE:
                try:
                    out[parts[1]] = self.read_json(rel)
                except ValueError:
                    out[parts[1]] = None
        return out

    def git(self, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True).stdout


class CodeOwners:
    """The subset of CODEOWNERS syntax this repository uses: *, /dir/ and /file."""

    def __init__(self, text: str) -> None:
        self.rules: list[tuple[str, list[str]]] = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                pattern, *owners = line.split()
                self.rules.append((pattern, owners))

    @staticmethod
    def _matches(pattern: str, rel: str) -> bool:
        if pattern == "*":
            return True
        target = pattern.lstrip("/")
        if pattern.endswith("/"):
            return rel.startswith(target)
        return rel == target or rel.startswith(target + "/")

    def owners_for(self, rel: str) -> list[str]:
        owners: list[str] = []
        for pattern, rule_owners in self.rules:
            if self._matches(pattern, rel):
                owners = rule_owners
        return owners

    def has_line(self, pattern: str) -> bool:
        return any(p == pattern and owners for p, owners in self.rules)


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def resolve(doc: Any, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node = doc
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def operations(doc: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for route, item in (doc.get("paths") or {}).items():
        for method in HTTP_METHODS:
            if isinstance(item, dict) and isinstance(item.get(method), dict):
                yield route, method, item[method]


def walk_x_media(node: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "x-media":
                yield f"{pointer}/x-media", value
            else:
                yield from walk_x_media(value, f"{pointer}/{escape(key)}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk_x_media(value, f"{pointer}/{i}")


def media_types(doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every request and response media type object of every operation, with its JSON pointer."""
    for route, method, op in operations(doc):
        base = f"/paths/{escape(route)}/{method}"
        body = op.get("requestBody")
        if isinstance(body, dict) and "$ref" not in body:
            for name, mt in (body.get("content") or {}).items():
                yield f"{base}/requestBody/content/{escape(name)}", mt
        for code, response in (op.get("responses") or {}).items():
            if isinstance(response, dict) and "$ref" not in response:
                for name, mt in (response.get("content") or {}).items():
                    yield f"{base}/responses/{escape(str(code))}/content/{escape(name)}", mt


def openapi_errors(doc: dict[str, Any]) -> list[str]:
    messages = []
    try:
        for error in OpenAPIV31SpecValidator(cast(Any, doc)).iter_errors():
            messages.append(error.message)
    except Exception as exc:  # noqa: BLE001  # the validator can raise on a malformed document after reporting it
        if not messages:
            messages.append(f"{type(exc).__name__}: {exc}")
    return messages


def error_text(error: Any) -> str:
    where = "/".join(str(p) for p in error.absolute_path)
    return f"{error.message} (at /{where})" if where else error.message


# Check 1: schemas and examples


def check_schemas(repo: Repo) -> list[str]:
    failures = []
    for name in SCHEMA_FILES:
        rel = f"schemas/{name}"
        try:
            Draft202012Validator.check_schema(repo.read_json(rel))
        except (OSError, ValueError) as exc:
            failures.append(f"{rel}: can't be read as JSON: {exc}")
        except SchemaError as exc:
            failures.append(f"{rel}: not a valid JSON Schema 2020-12: {exc.message}")
    if failures:
        return failures

    entry_schema = repo.read_json("schemas/registry-entry.v1.schema.json")
    root_validator = Draft202012Validator(entry_schema)
    defs = entry_schema["$defs"]
    operation_validator = Draft202012Validator({"$ref": "#/$defs/operation", "$defs": defs})
    media_validator = Draft202012Validator({"$ref": "#/$defs/media", "$defs": defs})

    for entry in repo.entries():
        where = entry.path
        doc = entry.doc
        if not isinstance(doc, dict):
            failures.append(f"{where}: not a JSON object")
            continue
        if doc.get("openapi") != "3.1.0":
            failures.append(f'{where}: "openapi" must be "3.1.0"')
            continue
        openapi_failures = openapi_errors(doc)
        failures.extend(f"{where}: not valid OpenAPI 3.1.0: {message}" for message in openapi_failures)
        if openapi_failures:
            continue
        x = doc.get("x-livepeer")
        if not isinstance(x, dict):
            failures.append(f"{where}: has no x-livepeer object at its root")
            continue
        for error in root_validator.iter_errors(x):
            failures.append(f"{where}: x-livepeer: {error_text(error)}")

        kind = x.get("kind")
        ops = list(operations(doc))
        if kind == "shared" and doc.get("paths"):
            failures.append(f"{where}: a shared entry has components and no paths")
        if kind == "profile" and not ops:
            failures.append(f"{where}: a profile entry needs at least one operation")
        seen: set[str] = set()
        for route, method, op in ops:
            at = f"{where}: {method.upper()} {route}"
            op_id = op.get("operationId")
            if not isinstance(op_id, str) or not OPERATION_ID.match(op_id):
                failures.append(f"{at}: operationId must match {OPERATION_ID.pattern}")
            elif op_id in seen:
                failures.append(f"{at}: operationId {op_id} is used twice")
            else:
                seen.add(op_id)
            op_x = op.get("x-livepeer")
            if op_x is None:
                failures.append(f"{at}: has no x-livepeer object")
                continue
            for error in operation_validator.iter_errors(op_x):
                failures.append(f"{at}: x-livepeer: {error_text(error)}")
            session = op_x.get("session") if isinstance(op_x, dict) else None
            if isinstance(session, dict):
                op_ids = {o.get("operationId") for _, _, o in ops}
                for control in [*session.get("controls", []), *([session["stop"]] if "stop" in session else [])]:
                    if control not in op_ids:
                        failures.append(f"{at}: session control {control} is not an operation")
                admission: Any = x.get("admission")
                if (
                    isinstance(admission, dict)
                    and admission.get("default") == "candidate"
                    and "idle_timeout_s" not in session
                ):
                    failures.append(f"{at}: a candidate persistent profile needs session.idle_timeout_s")
                events = session.get("events")
                if isinstance(events, dict) and isinstance(events.get("schema"), str):
                    try:
                        resolve(doc, events["schema"])
                    except (KeyError, IndexError, ValueError):
                        failures.append(f"{at}: session event schema {events['schema']} doesn't exist")

        for pointer, media in walk_x_media(doc):
            for error in media_validator.iter_errors(media):
                failures.append(f"{where}: {pointer}: {error_text(error)}")

        failures.extend(check_examples(entry))
    return failures


def check_examples(entry: Entry) -> list[str]:
    """Every example validates against the schema beside it, resolved within the entry."""
    failures = []
    doc = entry.doc
    registry: Registry[Any] = Registry().with_resource(
        "urn:entry", Resource.from_contents(doc, default_specification=DRAFT202012)
    )
    for pointer, mt in media_types(doc):
        if not isinstance(mt, dict) or "schema" not in mt:
            continue
        examples: list[tuple[str, Any]] = []
        if "example" in mt:
            examples.append(("example", mt["example"]))
        for name, example in (mt.get("examples") or {}).items():
            try:
                if isinstance(example, dict) and "$ref" in example:
                    example = resolve(doc, example["$ref"])
            except (KeyError, IndexError, ValueError):
                failures.append(f"{entry.path}: {pointer}: example {name} points nowhere")
                continue
            if isinstance(example, dict) and "value" in example:
                examples.append((name, example["value"]))
        if not examples:
            continue
        validator = Draft202012Validator({"$ref": f"urn:entry#{pointer}/schema"}, registry=registry)
        for name, value in examples:
            for error in validator.iter_errors(value):
                failures.append(f"{entry.path}: {pointer}: example {name}: {error_text(error)}")
    return failures


# Check 2: immutability


def latest_release(repo: Repo) -> str | None:
    tags = repo.git("tag", "--list").decode().split()
    releases = [t for t in tags if RELEASE_TAG.match(t)]
    if not releases:
        return None
    return max(releases, key=lambda t: tuple(int(g) for g in RELEASE_TAG.match(t).groups()))  # type: ignore[union-attr]


def check_immutability(repo: Repo) -> list[str]:
    release = latest_release(repo)
    if release is None:
        return []
    failures = []
    released = repo.git("ls-tree", "-r", "--name-only", release, "--", "registry/").decode().split()
    for rel in released:
        name = PurePosixPath(rel).name
        if rel == INDEX or name == NAMESPACE_FILE:
            continue
        current = repo.root / rel
        if not current.is_file():
            failures.append(f"{rel}: was in release {release} and is gone; entries are never removed")
        elif current.read_bytes() != repo.git("show", f"{release}:{rel}"):
            failures.append(f"{rel}: changed after release {release}; a change is a new entry with the next major")

    if INDEX in released:
        old = json.loads(repo.git("show", f"{release}:{INDEX}")).get("entries", {})
        try:
            new = repo.read_json(INDEX).get("entries", {})
        except (OSError, ValueError):
            return [*failures, f"{INDEX}: can't be read"]
        for entry_id, fields in old.items():
            if entry_id not in new:
                failures.append(f"{INDEX}: {entry_id} was in release {release} and is gone; IDs are never removed")
                continue
            for key, value in fields.items():
                if key not in ("status", "aliases") and new[entry_id].get(key) != value:
                    failures.append(f"{INDEX}: {entry_id}.{key} changed after release {release}")
    return failures


# Check 3: the index


def build_index(repo: Repo) -> dict[str, Any]:
    owners = CodeOwners((repo.root / "CODEOWNERS").read_text(encoding="utf-8"))
    deprecated: set[str] = set()
    aliases: dict[str, list[str]] = {}
    for data in repo.namespace_files().values():
        if isinstance(data, dict):
            deprecated.update(x for x in data.get("deprecated", []) if isinstance(x, str))
            for alias, target in (data.get("aliases") or {}).items():
                aliases.setdefault(str(target), []).append(alias)
    entries = {}
    for entry in repo.entries():
        entries[entry.id] = {
            "aliases": sorted(aliases.get(entry.id, [])),
            "path": entry.path,
            "sha256": hashlib.sha256(entry.raw).hexdigest(),
            "status": "deprecated" if entry.id in deprecated else "active",
            "steward": owners.owners_for(entry.path),
        }
    return {"entries": entries, "registry_index": 1}


def render_index(index: dict[str, Any]) -> str:
    return json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_index(repo: Repo) -> list[str]:
    expected = render_index(build_index(repo))
    path = repo.root / INDEX
    if not path.is_file() or path.read_text(encoding="utf-8") != expected:
        hint = "run `python .github/scripts/registry_ci.py index --write` and commit the result"
        return [f"{INDEX}: doesn't match the tree; {hint}"]
    return []


# Check 4: names and links


def check_names(repo: Repo) -> list[str]:
    failures = []
    owners = CodeOwners((repo.root / "CODEOWNERS").read_text(encoding="utf-8"))
    for rel in repo.registry_files():
        parts = PurePosixPath(rel).parts
        if rel != INDEX and not (len(parts) == 3 and parts[2].endswith(".json")):
            failures.append(f"{rel}: the registry holds only registry/<namespace>/<name>.v<major>.json")

    entries = repo.entries()
    ids = {e.id for e in entries}
    docs = {e.id: e.doc for e in entries if isinstance(e.doc, dict)}
    try:
        capability_modes = {c["name"]: c.get("mode") for c in repo.read_json("capabilities.json")["capabilities"]}
    except (OSError, ValueError, KeyError, TypeError):
        capability_modes = {}

    for entry in entries:
        if not PROFILE_ID.match(entry.id):
            failures.append(f"{entry.path}: {entry.id} isn't a valid ID (<namespace>.<name>.v<major>)")
        x = docs.get(entry.id, {}).get("x-livepeer")
        if not isinstance(x, dict):
            continue
        if x.get("profile") != entry.id:
            failures.append(f"{entry.path}: x-livepeer.profile is {x.get('profile')!r}; its path makes it {entry.id}")
        target = x.get("extends")
        if target is not None and target not in ids:
            failures.append(f"{entry.path}: extends {target}, which isn't in the registry")
        for capability in x.get("capabilities", []):
            if capability not in capability_modes:
                failures.append(f"{entry.path}: capability {capability} isn't on the capability list")
            elif x.get("mode") and capability_modes[capability] != x["mode"]:
                failures.append(
                    f"{entry.path}: capability {capability} is {capability_modes[capability]}; the entry is {x['mode']}"
                )

    for entry_id in ids:  # extends must not loop
        seen: set[str] = set()
        current: Any = entry_id
        while current in docs and current not in seen:
            seen.add(current)
            current = (docs[current].get("x-livepeer") or {}).get("extends")
        if current in seen:
            failures.append(f"{entry_id}: its extends chain loops")

    namespaces = {PurePosixPath(rel).parts[1] for rel in repo.registry_files() if rel != INDEX}
    for ns in sorted(namespaces):
        if not owners.has_line(f"/registry/{ns}/"):
            failures.append(f"registry/{ns}/: has no CODEOWNERS line naming its steward")

    alias_owner: dict[str, str] = {}
    for ns, data in sorted(repo.namespace_files().items()):
        rel = f"registry/{ns}/{NAMESPACE_FILE}"
        if not isinstance(data, dict) or set(data) - {"deprecated", "aliases"}:
            failures.append(f'{rel}: holds only "deprecated" (a list of IDs) and "aliases" (alias to ID)')
            continue
        for deprecated in data.get("deprecated", []):
            if deprecated not in ids or not str(deprecated).startswith(f"{ns}."):
                failures.append(f"{rel}: deprecates {deprecated}, which isn't an entry in this namespace")
        for alias, target in (data.get("aliases") or {}).items():
            if not PROFILE_ID.match(alias) or not alias.startswith(f"{ns}."):
                failures.append(f"{rel}: alias {alias} must be a valid ID in this namespace")
            elif alias in ids:
                failures.append(f"{rel}: alias {alias} is already an entry's ID")
            elif alias in alias_owner:
                failures.append(f"{rel}: alias {alias} is defined twice")
            if target not in ids:
                failures.append(f"{rel}: alias {alias} points at {target}, which isn't in the registry")
            alias_owner[alias] = ns
    return failures


# Check 5: the capability list


def check_capabilities(repo: Repo) -> list[str]:
    rel = "capabilities.json"
    try:
        data = repo.read_json(rel)
    except (OSError, ValueError) as exc:
        return [f"{rel}: can't be read as JSON: {exc}"]
    if not isinstance(data, dict) or data.get("capability_list") != 1 or not isinstance(data.get("capabilities"), list):
        return [f'{rel}: must be {{"capability_list": 1, "capabilities": [...]}}']
    failures = []
    seen: set[str] = set()
    for i, cap in enumerate(data["capabilities"]):
        at = f"{rel}: capabilities[{i}]"
        if not isinstance(cap, dict) or set(cap) != {"name", "group", "mode", "summary"}:
            failures.append(f"{at}: needs exactly name, group, mode and summary")
            continue
        name = cap["name"]
        if not isinstance(name, str) or not CAPABILITY.match(name):
            failures.append(f"{at}: {name!r} must be lowercase words joined by hyphens")
        elif name in seen:
            failures.append(f"{at}: {name} is listed twice")
        else:
            seen.add(name)
        if cap["group"] not in CAPABILITY_GROUPS:
            failures.append(f"{at}: group must be one of {', '.join(sorted(CAPABILITY_GROUPS))}")
        if cap["mode"] not in ("single-shot", "persistent"):
            failures.append(f"{at}: mode must be single-shot or persistent")
        elif isinstance(name, str) and name.startswith("live-") != (cap["mode"] == "persistent"):
            failures.append(f"{at}: live-* capabilities, and only they, are persistent")
        if not isinstance(cap["summary"], str) or not cap["summary"].strip():
            failures.append(f"{at}: summary must be a short sentence")
    return failures


CHECKS: dict[str, Callable[[Repo], list[str]]] = {
    "schemas": check_schemas,
    "immutability": check_immutability,
    "index": check_index,
    "names": check_names,
    "capabilities": check_capabilities,
}


def report(name: str, failures: list[str]) -> None:
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for failure in failures:
        print(f"::error title={name}::{failure}" if annotate else f"{name}: {failure}")
    print(f"{name}: {'ok' if not failures else f'{len(failures)} failure(s)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("check", choices=[*CHECKS, "all"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--write", action="store_true", help="index only: regenerate registry/index.json")
    args = parser.parse_args(argv)
    repo = Repo(args.root)

    if args.write:
        if args.check != "index":
            parser.error("--write works only with the index check")
        (repo.root / INDEX).write_text(render_index(build_index(repo)), encoding="utf-8")
        print(f"wrote {INDEX}")
        return 0

    failed = False
    for name in CHECKS if args.check == "all" else [args.check]:
        failures = CHECKS[name](repo)
        report(name, failures)
        failed = failed or bool(failures)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
