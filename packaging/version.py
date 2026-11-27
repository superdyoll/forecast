#!/usr/bin/env python3
"""One version for every Scout app, kept in lockstep.

The apps are released together (one set of wheels, one set of debs), so they
share a version: each package's ``__version__`` and the ``==`` pins between
them in the pyproject files.

    packaging/version.py               # print the version (fails if they differ)
    packaging/version.py check         # same, quietly
    packaging/version.py bump patch    # 0.1.4 -> 0.1.5 everywhere; prints it
    packaging/version.py bump minor    # 0.1.4 -> 0.2.0
    packaging/version.py set 1.0.0

CI runs ``bump patch`` on every merge into main (.github/workflows/release.yml).
Standard library only. Paths are relative to the repository root (the parent
of this script's directory), so it can run from anywhere in the checkout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
INIT_RE = re.compile(r'^__version__ = "([^"]+)"$', re.M)
PIN_RE = re.compile(r'"(scout-[a-z]+)==([^"]+)"')


def _inits() -> list[Path]:
    return sorted(ROOT.glob("apps/*/src/*/__init__.py"))


def _pyprojects() -> list[Path]:
    return sorted(ROOT.glob("apps/*/pyproject.toml"))


def versions() -> dict[str, str]:
    """Every place a version is written, mapped to the version found there."""
    found = {}
    for f in _inits():
        m = INIT_RE.search(f.read_text())
        if not m:
            raise SystemExit(f"{f.relative_to(ROOT)}: no __version__ line")
        found[str(f.relative_to(ROOT))] = m.group(1)
    for f in _pyprojects():
        for name, v in PIN_RE.findall(f.read_text()):
            found[f"{f.relative_to(ROOT)} ({name} pin)"] = v
    if not found:
        raise SystemExit("no apps found under apps/")
    return found


def current() -> str:
    found = versions()
    distinct = set(found.values())
    if len(distinct) != 1:
        lines = "\n".join(f"  {v:10} {k}" for k, v in sorted(found.items()))
        raise SystemExit(f"versions differ; fix with `version.py set X.Y.Z`:\n{lines}")
    v = distinct.pop()
    if not SEMVER.match(v):
        raise SystemExit(f"version {v!r} is not X.Y.Z")
    return v


def write(v: str) -> None:
    if not SEMVER.match(v):
        raise SystemExit(f"version {v!r} is not X.Y.Z")
    for f in _inits():
        f.write_text(INIT_RE.sub(f'__version__ = "{v}"', f.read_text(), count=1))
    for f in _pyprojects():
        f.write_text(PIN_RE.sub(lambda m: f'"{m.group(1)}=={v}"', f.read_text()))


def bumped(v: str, part: str) -> str:
    major, minor, patch = (int(x) for x in SEMVER.match(v).groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"bump what? patch, minor or major, not {part!r}")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd in ("show", "check"):
        v = current()
        if cmd == "show":
            print(v)
    elif cmd == "bump" and len(argv) == 2:
        v = bumped(current(), argv[1])
        write(v)
        print(v)
    elif cmd == "set" and len(argv) == 2:
        write(argv[1])
        print(current())
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
