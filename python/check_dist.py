"""Check that built wheels and sdists carry the repository root's files, byte for byte.

    uv build --out-dir dist python/runner-registry    # an sdist, then a wheel built from it
    uv build --out-dir dist python/runner-contract
    python python/check_dist.py dist

A wheel built from an sdist is where data copied from ../../ gets lost, so this reads both.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each package's root-relative files, and where its wheel carries them.
WHEEL_DATA = {
    "livepeer_runner_registry": {
        "registry": "livepeer_runner_registry/data/registry",
        "schemas": "livepeer_runner_registry/data/schemas",
        "capabilities.json": "livepeer_runner_registry/data/capabilities.json",
        "conformance": "livepeer_runner_registry/data/conformance",
        "LICENSE": "livepeer_runner_registry/LICENSE",
    },
    "livepeer_runner_contract": {
        "schemas/envelope.v1.schema.json": "livepeer_runner_contract/data/envelope.v1.schema.json",
        "schemas/conformance-report.v1.schema.json": "livepeer_runner_contract/data/conformance-report.v1.schema.json",
        "capabilities.json": "livepeer_runner_contract/data/capabilities.json",
        "LICENSE": "livepeer_runner_contract/LICENSE",
    },
}


def root_files(source: str) -> dict[str, bytes]:
    """Each file under a root-relative path, by its path below that one."""
    path = ROOT / source
    if path.is_file():
        return {"": path.read_bytes()}
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def compare(name: str, members: dict[str, bytes], source: str, target: str) -> list[str]:
    failures = []
    for rel, data in root_files(source).items():
        member = f"{target}/{rel}" if rel else target
        if member not in members:
            failures.append(f"{name}: {member} is missing")
        elif members[member] != data:
            failures.append(f"{name}: {member} differs from {source}")
    return failures


def check_wheel(path: Path, package: str) -> list[str]:
    with zipfile.ZipFile(path) as wheel:
        members = {n: wheel.read(n) for n in wheel.namelist() if not n.endswith("/")}
    failures = []
    for source, target in WHEEL_DATA[package].items():
        failures += compare(path.name, members, source, target)
    if f"{package}/__init__.py" not in members:
        failures.append(f"{path.name}: has no {package} package")
    return failures


def check_sdist(path: Path, package: str) -> list[str]:
    with tarfile.open(path) as sdist:
        top = sdist.getnames()[0].split("/")[0]
        members = {}
        for info in sdist.getmembers():
            handle = sdist.extractfile(info) if info.isfile() else None
            if handle is not None:
                members[info.name.removeprefix(f"{top}/")] = handle.read()
    failures = []
    for source in WHEEL_DATA[package]:
        failures += compare(path.name, members, source, f"_repo/{source}")
    if "hatch_build.py" not in members:
        failures.append(f"{path.name}: has no hatch_build.py")
    return failures


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    failures = []
    for package in WHEEL_DATA:
        dashed = package.replace("_", "-")
        wheels = sorted(dist.glob(f"{package}-*.whl"))
        sdists = sorted(dist.glob(f"{package}-*.tar.gz")) + sorted(dist.glob(f"{dashed}-*.tar.gz"))
        if not wheels:
            failures.append(f"{dist}: no {package} wheel")
        if not sdists:
            failures.append(f"{dist}: no {package} sdist")
        for wheel in wheels:
            failures += check_wheel(wheel, package)
        for sdist in sdists:
            failures += check_sdist(sdist, package)
    for failure in failures:
        print(failure)
    print(f"check_dist: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
