"""Generate fal.<name>.v<major> registry entries from fal's public queue OpenAPI.

    python importers/fal/import_fal.py --fetch   # refresh the cache from fal.ai, then generate
    python importers/fal/import_fal.py           # generate from the cache only
    python importers/fal/import_fal.py --check   # write nothing; exit 1 if anything would change

README.md says what it reads, what it writes and how it versions entries.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REGISTRY = ROOT / "registry" / "fal"
ENDPOINTS = HERE / "endpoints.json"
OPENAPI_URL = "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={}"
USER_AGENT = "live-runner-standard-fal-importer"
PROFILE_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+(-[a-z0-9]+)*)+\.v[1-9][0-9]*$")
COMPONENT = "#/components/schemas/"
ADAPTER_COMPONENTS = ("AdapterResult", "AdapterError")

# fal's schemas are untrusted text. Annotations that don't validate anything are dropped, long
# descriptions are shortened, and any text that reads like an instruction to a model is left out.
DROPPED_KEYWORDS = {"examples", "example", "ui", "_fal_ui_field"}
KEPT_EXTENSIONS = {"x-fal-order-properties"}
SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "dependentSchemas"}
SCHEMA_LISTS = {"anyOf", "oneOf", "allOf", "prefixItems"}
SCHEMA_ONE = {"items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames"}
INSTRUCTION_LIKE = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier)|disregard (all|any|the|previous)"
    r"|system prompt|you are (now )?(an? |the )?(ai|assistant|language model|chatbot)|as an ai\b"
    r"|\b(assistant|claude|chatgpt|anthropic|jailbreak)\b|do not (tell|reveal|mention)"
    r"|</?\s*(system|instructions?)\s*>",
    re.IGNORECASE,
)
DESCRIPTION_LIMIT = 200
TITLE_LIMIT = 80


class NotImported(Exception):
    """An endpoint that can't be imported; the run reports it and goes on."""


def canonical(value: Any) -> bytes:
    """SPEC.md's canonical JSON: UTF-8, keys sorted, no whitespace, non-ASCII as itself."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sorted_json(value: Any) -> Any:
    return json.loads(canonical(value))


def entry_name(endpoint_id: str) -> str:
    name = endpoint_id.removeprefix("fal-ai/").lower()
    name = re.sub(r"[/_.]", "-", name)
    return re.sub(r"-+", "-", name).strip("-")


def cache_file(cache: Path, endpoint_id: str) -> Path:
    return cache / (quote(endpoint_id, safe="") + ".json")


# Fetching (the only network access)


def fetch(endpoint_ids: list[str], cache: Path, delay: float) -> dict[str, str]:
    """Fetch each endpoint's OpenAPI in turn into the cache; return failures by endpoint."""
    cache.mkdir(parents=True, exist_ok=True)
    failures = {}
    for i, endpoint_id in enumerate(endpoint_ids):
        if i:
            time.sleep(delay)
        request = urllib.request.Request(
            OPENAPI_URL.format(endpoint_id), headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            if not isinstance(json.loads(raw), dict):
                raise TypeError("not a JSON object")
        except (urllib.error.URLError, TimeoutError, TypeError, ValueError) as exc:
            failures[endpoint_id] = f"fetch failed: {exc}"
            continue
        target = cache_file(cache, endpoint_id)
        partial = target.with_suffix(".partial")
        partial.write_bytes(raw)
        partial.replace(target)
    return failures


# Reading fal's document


def at(doc: Any, *path: str) -> Any:
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise NotImported(f"fal's OpenAPI has no {'/'.join(path)}")
        node = node[key]
    return node


def refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                found.add(child)
            else:
                found |= refs(child)
    elif isinstance(value, list):
        for child in value:
            found |= refs(child)
    return found


def extract(doc: Any, endpoint_id: str) -> dict[str, Any]:
    """fal's input root, output root and every component they reach, as the adapter projects them."""
    if at(doc, "info", "x-fal-metadata", "endpointId") != endpoint_id:
        raise NotImported("fal's OpenAPI is for another endpoint")
    path = f"/{endpoint_id}"
    for route, method in ((path, "post"), (f"{path}/requests/{{request_id}}", "get")):
        at(doc, "paths", route, method)
    for route, method in (
        (f"{path}/requests/{{request_id}}/status", "get"),
        (f"{path}/requests/{{request_id}}/cancel", "put"),
    ):
        at(doc, "paths", route, method)
    input_root = at(doc, "paths", path, "post", "requestBody", "content", "application/json", "schema")
    output_root = at(
        doc,
        "paths",
        f"{path}/requests/{{request_id}}",
        "get",
        "responses",
        "200",
        "content",
        "application/json",
        "schema",
    )
    every = at(doc, "components", "schemas")
    selected: dict[str, Any] = {}
    pending = sorted(refs(input_root) | refs(output_root))
    while pending:
        ref = pending.pop(0)
        if not ref.startswith(COMPONENT):
            raise NotImported(f"unsupported reference {ref}")
        name = ref[len(COMPONENT) :]
        if name in selected:
            continue
        if not isinstance(every.get(name), dict):
            raise NotImported(f"reference {ref} points nowhere")
        selected[name] = every[name]
        pending.extend(sorted(refs(every[name])))
    for name in ADAPTER_COMPONENTS:
        if name in selected:
            raise NotImported(f"fal's component {name} collides with the adapter's")
    fal: dict[str, Any] = sorted_json(
        {"components": selected, "endpoint_id": endpoint_id, "input": input_root, "output": output_root}
    )
    return fal


def fal_digest(fal: dict[str, Any]) -> str:
    """The sha256 of exactly the fal schema an entry uses; fal's document labels don't count."""
    shape = {
        "components": {"schemas": fal["components"]},
        "endpoint_id": fal["endpoint_id"],
        "input_schema": fal["input"],
        "output_schema": fal["output"],
    }
    return hashlib.sha256(canonical(shape)).hexdigest()


# Cleaning fal's text


def short_text(text: str, limit: int) -> str | None:
    text = " ".join(text.split())
    if not text or INSTRUCTION_LIKE.search(text):
        return None
    if len(text) <= limit:
        return text
    sentence = text.split(". ", 1)[0].rstrip(".") + "."
    if len(sentence) <= limit:
        return sentence
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def check_data(value: Any, where: str) -> None:
    """Values that validate or fill requests are copied unchanged, so they must be clean already."""
    if isinstance(value, str) and INSTRUCTION_LIKE.search(value):
        raise NotImported(f"{where} holds text that reads like an instruction; review it by hand")
    if isinstance(value, dict):
        for child in value.values():
            check_data(child, where)
    elif isinstance(value, list):
        for child in value:
            check_data(child, where)


def clean(schema: Any, where: str) -> Any:
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in DROPPED_KEYWORDS or (key.startswith("x-") and key not in KEPT_EXTENSIONS):
            continue
        if key in SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: clean(child, where) for name, child in value.items()}
        elif key in SCHEMA_LISTS and isinstance(value, list):
            out[key] = [clean(child, where) for child in value]
        elif key in SCHEMA_ONE:
            out[key] = clean(value, where)
        elif key in ("description", "title") and isinstance(value, str):
            text = short_text(value, DESCRIPTION_LIMIT if key == "description" else TITLE_LIMIT)
            if text:
                out[key] = text
        else:
            check_data(value, f"{where}: {key}")
            out[key] = value
    return out


# Deriving inputs, outputs and an example


def resolve(schema: Any, components: dict[str, Any]) -> Any:
    """Follow $ref, and unwrap an anyOf or oneOf with a single non-null branch."""
    for _ in range(8):
        if not isinstance(schema, dict):
            return schema
        if isinstance(schema.get("$ref"), str) and schema["$ref"].startswith(COMPONENT):
            schema = components.get(schema["$ref"][len(COMPONENT) :], {})
            continue
        options = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(options, list):
            real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(real) == 1:
                schema = real[0]
                continue
        return schema
    return schema


MEDIA_WORDS = (
    ("video/*", ("video",), ()),
    ("image/*", ("image", "mask", "frame", "picture", "thumbnail"), ()),
    ("audio/*", ("audio", "music", "voice", "speech"), ()),
    ("model/*", ("mesh", "model", "blend", "usdz"), ("glb", "fbx", "obj", "stl", "3d")),
)


def media_type(*hints: str) -> list[str]:
    """A media type guessed from field and component names: words inside names, short tokens whole."""
    text = " ".join(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", hint) for hint in hints).lower()
    tokens = set(re.split(r"[^a-z0-9]+", text))
    for kind, words, whole in MEDIA_WORDS:
        if any(word in text for word in words) or tokens & set(whole):
            return [kind]
    return []


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def url_inputs(fal: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level input fields that carry a URL fal fetches; nested ones aren't listed."""
    root = resolve(fal["input"], fal["components"])
    found = []
    for name, prop in (root.get("properties") or {}).items():
        target = resolve(prop, fal["components"])
        items = resolve(target.get("items"), fal["components"]) if target.get("type") == "array" else None
        value = items if isinstance(items, dict) else target
        hint = str(value.get("_fal_ui_field") or (value.get("ui") or {}).get("field") or "")
        named = name in ("url", "urls") or name.endswith(("_url", "_urls"))
        if value.get("type") != "string" or not (named or value.get("format") == "uri" or hint):
            continue
        field = "/" + escape(name) + ("/*" if isinstance(items, dict) else "")
        entry: dict[str, Any] = {"field": field, "kind": "url", "origin": "public"}
        types = media_type(hint or name)
        if types:
            entry["media_types"] = types
        found.append(entry)
    return found


def is_file(schema: Any) -> bool:
    return isinstance(schema, dict) and "url" in (schema.get("properties") or {})


def url_outputs(fal: dict[str, Any], expires: dict[str, Any]) -> list[dict[str, Any]]:
    """Output URLs: fal file objects' `url`, or URL strings, up to one level down; * for list items."""
    components = fal["components"]

    def file_url(prop: Any) -> tuple[str, str] | None:
        """(component name, pointer from the field to the URL) for a fal file object or a list of them."""
        node, suffix, name = prop, "", ""
        for _ in range(8):
            if not isinstance(node, dict):
                return None
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(COMPONENT):
                name = ref[len(COMPONENT) :]
                node = components.get(name)
                continue
            if is_file(node):
                return name, suffix + "/url"
            options = node.get("anyOf") or node.get("oneOf")
            if isinstance(options, list):
                real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
                if len(real) > 1:
                    found = [file_url(o) for o in real]
                    suffixes = {f[1] for f in found if f is not None}
                    return ("", suffix + suffixes.pop()) if None not in found and len(suffixes) == 1 else None
                if not real:
                    return None
                node = real[0]
                continue
            if node.get("type") != "array":
                return None
            node, suffix = node.get("items"), suffix + "/*"
        return None

    def url_string(prop_name: str, prop: Any) -> str | None:
        """The pointer suffix for a field that is a URL string, or a list of them."""
        target = resolve(prop, components)
        suffix = ""
        if isinstance(target, dict) and target.get("type") == "array":
            target, suffix = resolve(target.get("items"), components), "/*"
        named = prop_name in ("url", "urls") or prop_name.endswith(("_url", "_urls"))
        return suffix if named and isinstance(target, dict) and target.get("type") == "string" else None

    found = []

    def add(pointer: str, prop_name: str, prop: Any) -> bool:
        located = file_url(prop)
        if located is None:
            suffix = url_string(prop_name, prop)
            located = None if suffix is None else ("", suffix)
        if located is None:
            return False
        entry: dict[str, Any] = {"field": pointer + located[1], "kind": "url"}
        types = media_type(located[0]) or media_type(prop_name)
        if types:
            entry["media_types"] = types
        entry["expires"] = copy.deepcopy(expires)
        found.append(entry)
        return True

    root = resolve(fal["output"], components)
    for name, prop in (root.get("properties") or {}).items():
        pointer = "/output/" + escape(name)
        if add(pointer, name, prop):
            continue
        inner = resolve(prop, components)
        for sub, sub_prop in (inner.get("properties") or {}).items() if isinstance(inner, dict) else ():
            add(f"{pointer}/{escape(sub)}", sub, sub_prop)
    return found


IMAGE_CAPABILITIES = {"text-to-image", "image-edit"}


def image_reports(outputs: list[dict[str, Any]], capabilities: list[str]) -> list[dict[str, Any]]:
    """For image generators and editors, count the images returned: one per URL an image output names."""
    if not IMAGE_CAPABILITIES & set(capabilities):
        return []
    return [
        {"measure": "images", "in": "body", "pointer": o["field"], "aggregate": "count"}
        for o in outputs
        if o.get("media_types") == ["image/*"]
    ]


MISSING = object()


def sample(prop: Any, components: dict[str, Any], where: str) -> Any:
    """A value for one field from fal's own examples, else its default, else its first enum value."""
    for schema in (prop, resolve(prop, components)):
        if isinstance(schema, dict):
            for example in schema.get("examples") or []:
                try:
                    check_data(example, where)
                except NotImported:
                    continue
                return example
    for schema in (prop, resolve(prop, components)):
        if isinstance(schema, dict):
            if "default" in schema and schema["default"] is not None:
                return schema["default"]
            if schema.get("enum"):
                return schema["enum"][0]
    return MISSING


PLACEHOLDER_FILES = {"image/*": "image.png", "video/*": "video.mp4", "audio/*": "audio.mp3", "model/*": "model.glb"}


def placeholder(schema: Any, name: str, components: dict[str, Any], depth: int = 0) -> Any:
    """A schema-valid stand-in for a required field fal gives no value for."""
    schema = resolve(schema, components)
    if not isinstance(schema, dict) or depth > 6:
        return MISSING
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    options = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(options, list):
        for option in options:
            value = placeholder(option, name, components, depth + 1)
            if value is not MISSING and value is not None:
                return value
        return MISSING
    kind = schema.get("type")
    if kind == "string" and "pattern" not in schema:
        if name in ("url", "urls") or name.endswith(("_url", "_urls")) or schema.get("format") == "uri":
            kinds = media_type(name)
            return "https://example.com/" + (PLACEHOLDER_FILES[kinds[0]] if kinds else "file")
        return "example" if schema.get("maxLength", 7) >= 7 and schema.get("minLength", 0) <= 7 else MISSING
    if kind in ("integer", "number"):
        if "minimum" in schema:
            return schema["minimum"]
        if "exclusiveMinimum" in schema:
            return schema["exclusiveMinimum"] + 1
        return min(0, schema.get("maximum", 0))
    if kind == "boolean":
        return False
    if kind == "array":
        item = placeholder(schema.get("items"), name, components, depth + 1)
        return MISSING if item is MISSING else [item] * max(1, schema.get("minItems", 1))
    if kind == "object" or "properties" in schema:
        value = {}
        for field in schema.get("required") or []:
            inner = placeholder((schema.get("properties") or {}).get(field), field, components, depth + 1)
            if inner is MISSING:
                return MISSING
            value[field] = inner
        return value
    return MISSING


def request_example(fal: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """A request from fal's own example and default values; True when placeholders filled gaps."""
    components = fal["components"]
    root = resolve(fal["input"], components)
    props = root.get("properties") or {}
    value = {}
    filled = False
    for name in root.get("required") or []:
        picked = sample(props.get(name), components, f"example {name}")
        if picked is MISSING:
            picked = placeholder(props.get(name), name, components)
            filled = True
        if picked is MISSING:
            raise NotImported(f"no example, default or placeholder for required field {name}")
        value[name] = picked
    if not value:
        for name in root.get("x-fal-order-properties") or sorted(props):
            schema = props.get(name)
            if isinstance(schema, dict) and (schema.get("examples") or resolve(schema, components).get("examples")):
                picked = sample(schema, components, f"example {name}")
                if picked is not MISSING:
                    value[name] = picked
                    break
    return value, filled


# Building an entry


def build(base: dict[str, Any], endpoint: dict[str, Any], fal: dict[str, Any], major: int) -> dict[str, Any]:
    endpoint_id = endpoint["endpoint_id"]
    profile = f"fal.{entry_name(endpoint_id)}.v{major}"
    if not PROFILE_ID.match(profile):
        raise NotImported(f"{profile} isn't a valid registry ID")
    digest = fal_digest(fal)
    example, filled = request_example(fal)
    summary = "Built from fal's own example and default values"
    if filled:
        summary += ", with placeholders where fal gives none"
    components = {name: clean(schema, name) for name, schema in fal["components"].items()}
    input_root = clean(fal["input"], "input")
    output_root = clean(fal["output"], "output")

    base_op = base["paths"]["/"]["post"]
    base_x = base_op["x-livepeer"]
    op_x = copy.deepcopy(base_x)
    whole_body = [i for i in base_x["inputs"] if "field" not in i]
    op_x["inputs"] = whole_body + url_inputs(fal)
    outputs = url_outputs(fal, next(o["expires"] for o in base_x["outputs"] if o["kind"] == "url"))
    if outputs:
        op_x["outputs"] = outputs
    else:
        op_x.pop("outputs", None)
    reports = image_reports(outputs, endpoint["capabilities"])
    if reports:
        op_x["work_reports"] = reports

    adapter = base["components"]["schemas"]
    result = copy.deepcopy(adapter["AdapterResult"])
    result["properties"]["output"] = output_root
    result["properties"]["endpoint_id"] = {"const": endpoint_id, "type": "string"}
    error = copy.deepcopy(adapter["AdapterError"])
    error["properties"]["endpoint_id"] = {"const": endpoint_id, "type": "string"}
    components["AdapterResult"] = result
    components["AdapterError"] = error

    def error_response(code: str) -> dict[str, Any]:
        response = base_op["responses"][code]
        return {
            "description": response["description"],
            "content": {"application/json": {"schema": {"$ref": f"{COMPONENT}AdapterError"}}},
        }

    root_x = base["x-livepeer"]
    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": profile,
            "version": str(major),
            "description": (
                f"fal endpoint {endpoint_id}, model fal:{endpoint_id}, through the Livepeer fal route adapter. "
                "The request and output schemas are fal's own, from its public queue OpenAPI (x-source), "
                f"with long descriptions shortened and examples and fal's UI hints left out. Behavior is {root_x['profile']}'s."
            ),
            "x-source": {"endpoint_id": endpoint_id, "openapi_url": OPENAPI_URL.format(endpoint_id), "sha256": digest},
        },
        "paths": {
            "/": {
                "post": {
                    "operationId": base_op["operationId"],
                    "summary": f"Run fal endpoint {endpoint_id}",
                    "description": (
                        f"The body is fal's input for {endpoint_id}, forwarded to fal unchanged; a success answer "
                        f"carries fal's output under `output`. Behaves as {root_x['profile']} describes. Each URL output "
                        "pointer names the URL itself, and a `*` stands for every item of a list. URL fields nested "
                        "deeper aren't listed in inputs or outputs; the schemas show them."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": input_root,
                                "examples": {"fal": {"summary": summary, "value": example}},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": base_op["responses"]["200"]["description"],
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "oneOf": [
                                            {"$ref": f"{COMPONENT}AdapterResult"},
                                            {"$ref": f"{COMPONENT}AdapterError"},
                                        ]
                                    }
                                }
                            },
                        },
                        "4XX": error_response("4XX"),
                        "5XX": error_response("5XX"),
                    },
                    "x-livepeer": op_x,
                }
            }
        },
        "components": {"schemas": components},
        "x-livepeer": {
            "registry_entry": 1,
            "profile": profile,
            "kind": "profile",
            "extends": root_x["profile"],
            "mode": root_x["mode"],
            "capabilities": endpoint["capabilities"],
            "admission": copy.deepcopy(root_x["admission"]),
            "attempts": root_x["attempts"],
            "readiness": root_x["readiness"],
        },
    }
    validator = Draft202012Validator({"$ref": "#/paths/~1/post/requestBody/content/application~1json/schema", **doc})
    problems = [e.message for e in validator.iter_errors(example)]
    if problems:
        raise NotImported(f"the built example doesn't validate: {problems[0]}")
    return doc


def render(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def majors(name: str) -> dict[int, Path]:
    found = {}
    for path in REGISTRY.glob(f"{name}.v*.json"):
        match = re.fullmatch(rf"{re.escape(name)}\.v([1-9][0-9]*)\.json", path.name)
        if match:
            found[int(match.group(1))] = path
    return found


def plan(
    base: dict[str, Any], endpoint: dict[str, Any], fal: dict[str, Any], rewrite: bool
) -> tuple[str, Path | None, bytes]:
    """What to do for one endpoint: (outcome, path to write or None, bytes)."""
    name = entry_name(endpoint["endpoint_id"])
    existing = majors(name)
    if not existing:
        return "new", REGISTRY / f"{name}.v1.json", render(build(base, endpoint, fal, 1))
    latest = max(existing)
    current = existing[latest].read_bytes()
    try:
        recorded = json.loads(current)["info"]["x-source"]["sha256"]
    except (ValueError, KeyError, TypeError):
        raise NotImported(f"{existing[latest].name} has no info.x-source.sha256; fix it by hand") from None
    if recorded != fal_digest(fal):
        major = latest + 1
        return (
            f"fal schema changed: new major v{major}",
            REGISTRY / f"{name}.v{major}.json",
            render(build(base, endpoint, fal, major)),
        )
    rendered = render(build(base, endpoint, fal, latest))
    if rendered == current:
        return "unchanged", None, rendered
    if rewrite:
        return f"rewritten (same fal schema) v{latest}", existing[latest], rendered
    return f"differs from v{latest} with the same fal schema; left as is (--rewrite before a release)", None, rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_cache = (
        Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "live-runner-standard" / "fal-openapi"
    )
    parser.add_argument(
        "--cache", type=Path, default=default_cache, help=f"where fal's documents are kept (default {default_cache})"
    )
    parser.add_argument("--fetch", action="store_true", help="fetch every endpoint's OpenAPI from fal.ai first")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between fetches (default 1)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if any entry would be written")
    parser.add_argument(
        "--rewrite",
        action="store_true",
        help="rewrite an entry whose fal schema is unchanged but whose bytes differ (only before a release)",
    )
    parser.add_argument("--endpoint", action="append", default=[], help="import only this endpoint ID (repeatable)")
    args = parser.parse_args(argv)

    listing = json.loads(ENDPOINTS.read_text(encoding="utf-8"))
    base_id = listing["base"]
    namespace, rest = base_id.split(".", 1)
    base = json.loads((ROOT / "registry" / namespace / f"{rest}.json").read_text(encoding="utf-8"))
    endpoints = [e for e in listing["endpoints"] if not args.endpoint or e["endpoint_id"] in args.endpoint]
    unknown = set(args.endpoint) - {e["endpoint_id"] for e in endpoints}
    if unknown:
        parser.error(f"not in {ENDPOINTS.name}: {', '.join(sorted(unknown))}")
    base_capabilities = set(base["x-livepeer"]["capabilities"])

    failures: dict[str, str] = {}
    if args.fetch:
        failures |= fetch([e["endpoint_id"] for e in endpoints], args.cache, args.delay)

    names: dict[str, str] = {}
    pending = False
    for endpoint in endpoints:
        endpoint_id = endpoint["endpoint_id"]
        name = entry_name(endpoint_id)
        if name in names:
            failures[endpoint_id] = f"its name {name} collides with {names[name]}"
            continue
        names[name] = endpoint_id
        if endpoint_id in failures:
            continue
        extra = set(endpoint["capabilities"]) - base_capabilities
        if extra:
            failures[endpoint_id] = f"capabilities {sorted(extra)} aren't in {base_id}"
            continue
        try:
            source = cache_file(args.cache, endpoint_id)
            if not source.is_file():
                raise NotImported(f"not in the cache ({source}); run with --fetch")
            fal = extract(json.loads(source.read_bytes()), endpoint_id)
            outcome, target, data = plan(base, endpoint, fal, args.rewrite)
        except (NotImported, ValueError) as exc:
            failures[endpoint_id] = str(exc)
            continue
        if target is not None:
            pending = True
            if not args.check:
                target.write_bytes(data)
        elif not outcome.startswith("unchanged"):
            pending = True
        print(f"{endpoint_id}: {outcome}")

    for endpoint_id, reason in sorted(failures.items()):
        print(f"{endpoint_id}: NOT IMPORTED: {reason}", file=sys.stderr)
    return 1 if failures or (args.check and pending) else 0


if __name__ == "__main__":
    sys.exit(main())
