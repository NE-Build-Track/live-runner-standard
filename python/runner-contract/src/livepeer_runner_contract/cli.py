"""The ``runner-contract`` builder tool.

    runner-contract init --capability chat --profile openai.chat.v1 [--out runner.toml]
    runner-contract envelope runner.toml
    runner-contract block runner.toml [--format toml|json]
    runner-contract check block.toml|registration.json|envelope.json [--manifest runner.toml] [--registry DIR]
    runner-contract conform --manifest runner.toml [--block block.toml] [--url URL] [--profile ID] [--out report.json]
    runner-contract draft --openapi openapi.json [--profile ID]
    runner-contract draft --pydantic module:Request,module:Response [--profile ID]

Problems print one per line; the exit status is 1 when there are any and 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .check import check_registrations, read_registrations
from .conform import Options, SuiteError, Target, available_suites, profiles_to_run, run_suite
from .draft import draft_from_models, draft_from_openapi
from .envelope import Envelope, _parse
from .manifest import Manifest, ManifestError, load_manifest, render_block_json, render_block_toml, render_envelope
from .registry import EntryKind, Registry, RegistryError
from .standard import PROFILE_ID, RUNNER_CONTRACT, Capability, Mode, capabilities

USAGE_ERROR = 2


class _Usage(Exception):
    pass


def _registry(path: str | None, *, required: bool) -> Registry | None:
    """The registry given, or the installed release; None when there is none and it isn't required."""
    try:
        return Registry.load(path, strict=False)
    except RegistryError as exc:
        if required or path is not None:
            raise _Usage(str(exc)) from exc
        return None


def _capabilities(registry: Registry | None) -> Mapping[str, Capability]:
    if registry is not None and registry.capabilities:
        return registry.capabilities
    return capabilities()


def _manifest(path: str, registry: Registry | None) -> Manifest:
    try:
        manifest = load_manifest(path, capabilities=_capabilities(registry))
    except OSError as exc:
        raise _Usage(f"{path}: {exc.strerror or exc}") from exc
    for warning in manifest.warnings:
        print(f"{path}: warning: {warning}", file=sys.stderr)
    return manifest


STARTER = """\
# The runner-contract manifest for one runner registration (runner contract v{contract}).
#   runner-contract envelope {out}   builds the envelope that goes in Metadata
#   runner-contract block {out}      prints the node's [[Runners]] block (--format json for dynamic registration)
# Every key: https://github.com/NE-Build-Track/live-runner-standard/tree/main/python/runner-contract#the-manifest
# Fill each TODO; the tool refuses a manifest that still has one.

[runner]
id = "TODO"                          # the node's runner ID: letters, digits, - and _
app = "{capability}"{app_pad}# the capability, and nothing else
mode = "{mode}"{mode_pad}# {capability} is {mode}
capacity = 1                         # TODO: requests{sessions} it truly serves at once
url = "TODO"                         # http(s) URL the node forwards to
health_url = "TODO"                  # answers 200 only once a self-test of every profile passes
version = "TODO"                     # build label, such as the image tag; at most 64 characters
# admission = "private"              # only apps holding an approval may select it

[price]
amount = 0                           # TODO: the wholesale price, in USD
currency = "usd"
unit = "hour"                        # hour (runner time) or fixed (per call): how the node charges

# [model]                            # when the runner serves a model
# name = "TODO"                      # what requests send
# source = "hf:TODO"                 # <origin>:<id>
# revision = "TODO"                  # immutable, wherever the source has one
{endpoints}
# [limits]                           # only limits the runner enforces
# context_tokens = 131072

[revision]                           # the deployment_revision recipe's inputs
images = ["TODO"]                    # every image, pinned: name@sha256:<64 hex>
settings = {{}}                        # the settings that change behavior, such as context length
"""


def _starter_endpoints(profiles: list[str], registry: Registry | None, mode: Mode) -> str:
    blocks = []
    for profile in profiles:
        entry = registry.get(profile) if registry is not None else None
        names = sorted(entry.operations) if entry is not None and entry.kind is EntryKind.PROFILE else []
        if entry is not None:
            # A session's stop and control calls are the session endpoint's, not endpoints of their own.
            controls = set()
            for op in entry.operations.values():
                session = op.x.get("session")
                if isinstance(session, dict):
                    controls.update([session.get("stop"), *session.get("controls", [])])
            names = [n for n in names if n not in controls]
        note = "" if names else "    # TODO: the operationId of this endpoint in the profile's registry entry"
        for name in names or ["TODO"]:
            lines = ["", "[[endpoints]]", f'name = "{name}"{note}', f'profile = "{profile}"']
            lines.append('# path = "/..."                      # only where it differs from the entry\'s default')
            if mode is Mode.PERSISTENT:
                lines += [
                    '# stop = "/..."                      # the path that stops a session',
                    '# channels = { in = ["TODO"], out = ["TODO"] }',
                    "idle_timeout_s = 30                  # required before a persistent runner is candidate",
                    "# drain_timeout_s = 10",
                ]
            blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


def cmd_init(args: argparse.Namespace) -> int:
    registry = _registry(args.registry, required=False)
    capability = _capabilities(registry).get(args.capability)
    if capability is None:
        raise _Usage(f"{args.capability!r} isn't on the capability list")
    for profile in args.profile:
        if not PROFILE_ID.match(profile):
            raise _Usage(f"{profile!r} isn't a registry ID, <namespace>.<name>.v<major>")
        if registry is not None and profile not in registry:
            print(f"warning: {profile} isn't in the registry release", file=sys.stderr)
    text = STARTER.format(
        contract=RUNNER_CONTRACT,
        out=args.out if args.out != "-" else "runner.toml",
        capability=capability.name,
        app_pad=" " * max(1, 29 - len(capability.name)),
        mode=capability.mode.value,
        mode_pad=" " * max(1, 28 - len(capability.mode.value)),
        sessions=" (sessions)" if capability.mode is Mode.PERSISTENT else "",
        endpoints=_starter_endpoints(args.profile, registry, capability.mode),
    )
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    if out.exists() and not args.force:
        raise _Usage(f"{out} exists; pass --force to replace it")
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    return 0


def cmd_envelope(args: argparse.Namespace) -> int:
    manifest = _manifest(args.manifest, _registry(args.registry, required=False))
    text = render_envelope(manifest)
    print(text)
    print(f"{args.manifest}: envelope is {len(text.encode('utf-8')):,} of 1,024 bytes", file=sys.stderr)
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    manifest = _manifest(args.manifest, _registry(args.registry, required=False))
    render = render_block_json if args.format == "json" else render_block_toml
    sys.stdout.write(render(manifest))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    registry = _registry(args.registry, required=True)
    assert registry is not None
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        raise _Usage(f"{args.file}: {exc.strerror or exc}") from exc
    manifest = _manifest(args.manifest, registry) if args.manifest else None
    problems = check_registrations(
        read_registrations(text, name=args.file),
        registry=registry,
        capabilities=_capabilities(registry),
        manifest=manifest,
    )
    for problem in problems:
        print(problem)
    if not problems:
        release = f"registry release {registry.release}" if registry.release else f"the registry at {registry.root}"
        print(f"{args.file}: ok, against {release}")
    return 1 if problems else 0


def _conform_target(args: argparse.Namespace, registry: Registry) -> Target:
    """The runner under test, from a manifest and/or a block or envelope, with the command line's overrides."""
    manifest = _manifest(args.manifest, registry) if args.manifest else None
    metadata, url, health_url, runner_id, source = None, None, None, None, "manifest"
    registration_problems: tuple[str, ...] | None = None
    if args.block:
        try:
            text = Path(args.block).read_text(encoding="utf-8")
        except OSError as exc:
            raise _Usage(f"{args.block}: {exc.strerror or exc}") from exc
        registrations = [r for r in read_registrations(text, name=args.block) if not r.bare]
        if args.runner_id:
            registrations = [r for r in registrations if r.runner_id == args.runner_id]
        if len(registrations) != 1:
            raise _Usage(f"{args.block}: holds {len(registrations)} matching registrations; pick one with --runner-id")
        (reg,) = registrations
        registration_problems = tuple(
            check_registrations([reg], registry=registry, capabilities=_capabilities(registry), manifest=manifest)
        )
        metadata = reg.metadata if isinstance(reg.metadata, str) else None
        url = reg.url if isinstance(reg.url, str) else None
        health_url = reg.health_url if isinstance(reg.health_url, str) else None
        runner_id, source = reg.runner_id, "block"
    elif args.envelope:
        try:
            metadata = Path(args.envelope).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _Usage(f"{args.envelope}: {exc.strerror or exc}") from exc
        source = "envelope"
    elif manifest is not None:
        metadata = render_envelope(manifest)
    if not metadata:
        raise _Usage("conform needs a runner: pass --manifest, --block or --envelope")
    parsed = _parse(metadata, limit=None)
    envelope = parsed if isinstance(parsed, Envelope) else None
    if envelope is None and manifest is None:
        raise _Usage(f"the envelope doesn't parse ({parsed}), and no --manifest gives a deployment_revision")
    model = args.model or (manifest.model.name if manifest and manifest.model else None)
    if model is None and envelope is not None and envelope.model is not None:
        model = envelope.model.name
    runner_id = args.runner_id or runner_id or (manifest.id if manifest else None)
    if runner_id is None and envelope is not None:
        runner_id = envelope.runner_id
    url = args.url or url or (manifest.url if manifest else None)
    if not runner_id:
        raise _Usage("no runner ID: pass --runner-id, or give a manifest or block that has one")
    if not url:
        raise _Usage("no runner URL: pass --url")
    return Target(
        runner_id=runner_id,
        url=url,
        metadata=metadata,
        health_url=args.health_url or health_url or (manifest.health_url if manifest else None),
        model=model,
        manifest=manifest,
        metadata_from=source,
        registration_problems=registration_problems,
    )


def _report_path(out: str, profile: str, several: bool) -> Path:
    path = Path(out)
    return path.with_name(f"{path.stem}.{profile}{path.suffix}") if several else path


def cmd_conform(args: argparse.Namespace) -> int:
    registry = _registry(args.registry, required=True)
    assert registry is not None
    target = _conform_target(args, registry)
    profiles = profiles_to_run(target, registry, args.profile or ())
    if not profiles:
        raise _Usage("no profile to conform: the envelope advertises none; pass --profile")
    missing = [p for p in profiles if p not in available_suites(registry.root or Path("."))]
    if missing:
        where = f"registry release {registry.release}" if registry.release else f"the registry at {registry.root}"
        raise _Usage(f"{where} has no conformance suite for {', '.join(missing)}")
    options = Options(timeout=args.timeout, health_timeout=args.health_timeout, cancel_settle_s=args.cancel_settle)
    failed = False
    for profile in profiles:
        try:
            report = run_suite(registry, target, profile, options)
        except SuiteError as exc:
            raise _Usage(f"{profile}'s suite can't run: " + "; ".join(exc.problems)) from exc
        path = _report_path(args.out, profile, len(profiles) > 1)
        path.write_text(json.dumps(report.document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        counts = {r: sum(1 for c in report.checks if c.result == r) for r in ("pass", "fail", "skip")}
        for check in report.checks:
            if check.result != "pass" or args.verbose:
                print(f"{check.result:4}  {check.id}: {check.detail}", file=sys.stderr)
        verdict = "pass" if report.passed else "FAIL"
        print(
            f"{profile}: {verdict} ({counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped); "
            f"wrote {path}",
            file=sys.stderr,
        )
        failed = failed or not report.passed
    return 1 if failed else 0


def cmd_draft(args: argparse.Namespace) -> int:
    if args.openapi:
        try:
            source = json.loads(Path(args.openapi).read_text(encoding="utf-8"))
        except OSError as exc:
            raise _Usage(f"{args.openapi}: {exc.strerror or exc}") from exc
        except ValueError as exc:
            raise _Usage(f"{args.openapi}: isn't JSON ({exc}); export the OpenAPI document as JSON") from exc
        if not isinstance(source, dict):
            raise _Usage(f"{args.openapi}: isn't an OpenAPI document")
        draft = draft_from_openapi(source, args.profile)
    else:
        try:
            draft = draft_from_models([s.strip() for s in args.pydantic.split(",") if s.strip()], args.profile)
        except (ImportError, AttributeError, ValueError, RuntimeError) as exc:
            raise _Usage(str(exc)) from exc
    text = json.dumps(draft, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}; a steward reviews it into the registry", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="runner-contract",
        description=f"Build and check live runner registrations (runner contract v{RUNNER_CONTRACT}).",
    )
    top.add_argument("--version", action="version", version=f"runner-contract {__version__}")
    sub = top.add_subparsers(dest="command", required=True, metavar="command")
    registry_help = "a registry release directory (default: the installed livepeer-runner-registry)"

    p = sub.add_parser("init", help="write a starter manifest")
    p.add_argument("--capability", required=True, help="the runner's capability, such as chat")
    p.add_argument("--profile", required=True, action="append", help="a wire profile it speaks (repeatable)")
    p.add_argument("--out", default="runner.toml", help="where to write it (- for standard output)")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    p.add_argument("--registry", help=registry_help)
    p.set_defaults(run=cmd_init)

    p = sub.add_parser("envelope", help="print a manifest's envelope, as compact JSON")
    p.add_argument("manifest")
    p.add_argument("--registry", help=registry_help + "; only its capability list is read")
    p.set_defaults(run=cmd_envelope)

    p = sub.add_parser("block", help="print the node's [[Runners]] block, or the dynamic registration JSON")
    p.add_argument("manifest")
    p.add_argument("--format", choices=("toml", "json"), default="toml")
    p.add_argument("--registry", help=registry_help + "; only its capability list is read")
    p.set_defaults(run=cmd_block)

    p = sub.add_parser("check", help="check a [[Runners]] block, a registration JSON or an envelope")
    p.add_argument("file")
    p.add_argument("--manifest", help="also recompute deployment_revision from this manifest")
    p.add_argument("--registry", help=registry_help)
    p.set_defaults(run=cmd_check)

    p = sub.add_parser("conform", help="run each profile's conformance suite against a running runner")
    p.add_argument("--manifest", help="the runner's manifest: its envelope, and the inputs to recompute its revision")
    given = p.add_mutually_exclusive_group()
    given.add_argument("--block", help="the runner's [[Runners]] block or registration JSON, as registered")
    given.add_argument("--envelope", help="the runner's envelope, as registered in Metadata")
    p.add_argument("--url", help="the runner's base URL on its own host (default: the manifest's or block's)")
    p.add_argument("--health-url", help="its health check (default: the manifest's or block's)")
    p.add_argument(
        "--profile", action="append", help="a profile to conform (repeatable; default: every one it advertises)"
    )
    p.add_argument("--registry", help=registry_help + "; its conformance/ holds the suites")
    p.add_argument(
        "--out", default="conformance-report.json", help="the report; with several profiles, one per profile"
    )
    p.add_argument("--runner-id", help="the runner ID for the report (default: the manifest's, block's or envelope's)")
    p.add_argument("--model", help="the model name requests send (default: the manifest's or envelope's)")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds to wait on each request (default 120)")
    p.add_argument(
        "--health-timeout", type=float, default=10.0, help="seconds to wait on the health check (default 10)"
    )
    p.add_argument(
        "--cancel-settle",
        type=float,
        help="seconds to wait after closing a request mid-answer before probing health (default: the common suite's)",
    )
    p.add_argument("--verbose", action="store_true", help="print passing checks too")
    p.set_defaults(run=cmd_conform)

    p = sub.add_parser("draft", help="draft a registry entry skeleton for a steward to review")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--openapi", metavar="FILE", help="the runner's OpenAPI document, as JSON")
    source.add_argument("--pydantic", metavar="MODULE:MODEL,...", help="request model, then response model")
    p.add_argument("--profile", metavar="ID", help="the entry's ID, <namespace>.<name>.v<major>")
    p.add_argument("--out", help="write it here instead of standard output")
    p.set_defaults(run=cmd_draft)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        status: int = args.run(args)
    except ManifestError as exc:
        where = getattr(args, "manifest", None) or "manifest"
        for problem in exc.problems:
            print(f"{where}: {problem}", file=sys.stderr)
        return 1
    except _Usage as exc:
        print(f"runner-contract: {exc}", file=sys.stderr)
        return USAGE_ERROR
    return status


if __name__ == "__main__":
    raise SystemExit(main())
