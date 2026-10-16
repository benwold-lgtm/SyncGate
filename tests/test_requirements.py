# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Guard against requirements.txt drifting from pyproject.toml (S2 finding F2).

requirements.txt is the pip-compiled lockfile used by the Docker image. It had
silently gone stale — missing redis/slowapi/pybreaker — so a clean install from
it produced an environment that couldn't import the app. This test fails if any
declared runtime dependency is absent from the lockfile.
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parent.parent

# Dependencies whose major version must never be reachable by a plain
# `pip install -e .`. `pip install` resolves against pyproject.toml and ignores
# requirements.txt entirely, so an unbounded spec here means a clean install (and
# CI, which installs the same way) silently picks up a major the lockfile has never
# seen. That is exactly how `mcp>=1.0.0` let mcp 2.0.0 in and removed
# `mcp.server.fastmcp` out from under pods/device_pod.py. (`mcp` has since moved to the
# `dev` extra, where _CI_GATING_DEV_TOOLS below holds it to the same rule.)
_MUST_EXCLUDE_NEXT_MAJOR = frozenset({"fastapi", "starlette", "pydantic", "cryptography", "redis"})


def _canonical(name: str) -> str:
    # PEP 503 canonicalisation: lowercase, runs of -/_/. collapse to a single -.
    return re.sub(r"[-_.]+", "-", name).lower()


def _dep_name(spec: str) -> str:
    # "redis[asyncio]>=5.0" -> "redis"
    return _canonical(re.split(r"[<>=!~\[ ]", spec.strip(), maxsplit=1)[0])


def _pyproject_runtime_specs() -> dict[str, Requirement]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return {_dep_name(d): Requirement(d) for d in data["project"]["dependencies"]}


def _pyproject_runtime_deps() -> set[str]:
    return set(_pyproject_runtime_specs())


def _locked_versions() -> dict[str, str]:
    """Map canonical package name -> pinned version from the pip-compiled lockfile."""
    pinned: dict[str, str] = {}
    for line in (_ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if sep:
            pinned[_dep_name(name)] = version.strip()
    return pinned


def _locked_names() -> set[str]:
    names = set()
    for line in (_ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(_dep_name(line))
    return names


def test_every_runtime_dependency_is_locked():
    missing = _pyproject_runtime_deps() - _locked_names()
    assert not missing, f"requirements.txt is missing pyproject deps: {sorted(missing)} — re-run pip-compile"


def test_runtime_critical_packages_present():
    locked = _locked_names()
    for pkg in ("redis", "pybreaker", "fastapi", "cryptography"):
        assert pkg in locked, f"{pkg} missing from requirements.txt"


def test_locked_version_satisfies_pyproject_specifier():
    """The pinned version must be inside the range pyproject.toml declares.

    Catches the two halves drifting apart in the ordinary direction — a lockfile
    re-pin that the declared range no longer allows.
    """
    locked = _locked_versions()
    mismatched = []
    for name, req in _pyproject_runtime_specs().items():
        version = locked.get(name)
        if version is None:
            continue  # absence is test_every_runtime_dependency_is_locked's job
        if not req.specifier.contains(version, prereleases=True):
            mismatched.append(f"{name}: locked {version} not in {req.specifier or '(any)'}")
    assert not mismatched, "requirements.txt pins versions pyproject.toml disallows: " + "; ".join(mismatched)


def test_critical_deps_exclude_the_next_major():
    """A load-bearing dependency must not let `pip install -e .` reach an untested major.

    The presence/range checks above both PASSED while `mcp>=1.0.0` was pinned to
    1.27.2 — the range was satisfied, so neither noticed that the spec also admitted
    2.0.0. This is the check that fails on that shape: for each critical dependency,
    assert the declared specifier rejects the first release of the next major above
    whatever the lockfile pins.
    """
    locked = _locked_versions()
    unbounded = []
    for name, req in _pyproject_runtime_specs().items():
        if name not in _MUST_EXCLUDE_NEXT_MAJOR:
            continue
        version = locked.get(name)
        if version is None:
            continue
        next_major = f"{Version(version).major + 1}.0.0"
        if req.specifier.contains(next_major, prereleases=True):
            unbounded.append(f"{name} (locked {version}) still admits {next_major} via '{req.specifier or 'any'}'")
    assert not unbounded, (
        "critical dependencies need an upper bound so a clean `pip install -e .` cannot "
        "resolve past the tested major: " + "; ".join(unbounded)
    )


# --- a declared floor must support the APIs the code actually imports --------

# The mirror of the guard above. `cryptography>=41.0.7` was declared long before
# ADR-0011 backup/restore began importing
# `cryptography.hazmat.primitives.kdf.argon2.Argon2id`, which does not exist before
# 44.0.0. Every check above still passed: the lockfile pins a version well inside the
# range, so nothing ever compared the declared *floor* against what the code needs.
# `pip install` resolves against pyproject.toml, so an environment already holding
# cryptography 43.x satisfied the spec and then raised ImportError on the first portable
# export. Upper bounds keep an untested major out; this keeps an unusable minimum from
# being declared.
_API_FLOORS = {
    # package: (minimum version, the import that requires it)
    "cryptography": ("44.0.0", "cryptography.hazmat.primitives.kdf.argon2.Argon2id (ADR-0011 portable backup)"),
}


def _declared_floor(req: Requirement) -> Version | None:
    """The highest lower bound the specifier declares, or None if it declares no floor."""
    floors = [Version(s.version) for s in req.specifier if s.operator in (">=", "==", "~=")]
    return max(floors) if floors else None


def test_declared_floor_supports_the_apis_the_code_imports():
    specs = _pyproject_runtime_specs()
    too_low = []
    for name, (required, why) in sorted(_API_FLOORS.items()):
        req = specs.get(name)
        assert req is not None, f"{name} is no longer a runtime dependency — update _API_FLOORS"
        floor = _declared_floor(req)
        if floor is None or floor < Version(required):
            too_low.append(f"{name}: declares >={floor or '(none)'} but needs >={required} for {why}")
    assert not too_low, (
        "pyproject.toml declares a minimum version that cannot run this code, so a clean "
        "install can resolve to an unusable version: " + "; ".join(too_low)
    )


# --- review item 12: the CI-gating dev tools need upper bounds too -----------

# `black --check` FAILS the build, and flake8/mypy gate it as well. An unbounded spec on
# those means an upstream major release turns an unrelated PR red with no change from us:
# a new black reformats the tree, a new flake8/mypy adds a rule. The runtime guard above
# does not cover them, because they live in the `dev` extra rather than in
# project.dependencies. pre-commit is excluded deliberately — it is developer convenience
# and gates nothing in CI.
#
# `mcp` is in the set for the reason it was in the runtime one: the SDK's majors move a lot
# of API (2.0 removed `mcp.server.fastmcp` outright), and the interop tests that use it as
# the reference implementation gate the build like any other test.
_CI_GATING_DEV_TOOLS = frozenset({"black", "flake8", "mypy", "pytest", "pytest-asyncio", "pytest-cov", "mcp"})


def _pyproject_dev_specs() -> dict[str, Requirement]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    return {_dep_name(d): Requirement(d) for d in dev}


def test_ci_gating_dev_tools_exclude_the_next_major():
    """Same shape as the runtime guard, applied to the tools that can fail the build."""
    specs = _pyproject_dev_specs()
    missing = _CI_GATING_DEV_TOOLS - set(specs)
    assert not missing, f"expected these in the dev extra: {sorted(missing)}"

    unbounded = []
    for name in sorted(_CI_GATING_DEV_TOOLS):
        req = specs[name]
        installed = _installed_version(name)
        if installed is None:
            continue
        next_major = f"{Version(installed).major + 1}.0.0"
        if req.specifier.contains(next_major, prereleases=True):
            unbounded.append(f"{name} (installed {installed}) still admits {next_major} via '{req.specifier or 'any'}'")
    assert (
        not unbounded
    ), "CI-gating dev tools need an upper bound so an upstream major cannot turn an " "unrelated PR red: " + "; ".join(
        unbounded
    )


def test_installed_dev_tools_satisfy_their_specifiers():
    """The bounds must admit what is actually installed — a too-tight bound is its own
    outage, and would mean CI and local dev disagree about which tool version is legal."""
    specs = _pyproject_dev_specs()
    violations = []
    for name in sorted(_CI_GATING_DEV_TOOLS):
        installed = _installed_version(name)
        if installed is None:
            continue
        if not specs[name].specifier.contains(installed, prereleases=True):
            violations.append(f"{name}: installed {installed} not in {specs[name].specifier}")
    assert not violations, "dev tool bounds exclude the installed version: " + "; ".join(violations)


def _installed_version(canonical_name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(canonical_name)
    except PackageNotFoundError:
        return None


# --- the MCP SDK is a test dependency, and must stay one ---------------------

# `mcp` lives in the `dev` extra, so CI — which installs `.[dev]` — always has it, while
# the image, which installs requirements.txt, never does. A runtime import of it would
# therefore pass every test and ship a container that fails on import: the drift
# `test_every_runtime_dependency_is_locked` exists for, in a shape it cannot see, because
# nothing would be missing from the lockfile that pyproject.toml declares at runtime.


def test_gateway_does_not_import_the_mcp_sdk():
    pattern = re.compile(r"^\s*(?:from\s+mcp(?:\.|\s)|import\s+mcp(?:\.|\s|$))", re.MULTILINE)
    offenders = [
        str(path.relative_to(_ROOT))
        for path in sorted((_ROOT / "device_mcp_gateway").rglob("*.py"))
        if pattern.search(path.read_text())
    ]
    assert not offenders, (
        "the gateway imports the MCP SDK at runtime, but `mcp` is a dev-only dependency and "
        "the image does not install it. Either drop the import or move `mcp` back into "
        "project.dependencies and re-run pip-compile: " + ", ".join(offenders)
    )
