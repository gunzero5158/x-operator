"""Bump the version from Conventional Commits; also runnable before a local release.

Usage: python scripts/release_version.py --base origin/main --level minor
CI:    python scripts/release_version.py --base <previous-main-sha>
"""
from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = Path("x_operator/version.py")
VERSION_RE = re.compile(r'__version__ = "(\d+\.\d+\.\d+)"')


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def change_level(messages: str) -> str:
    if re.search(r"(?m)^[\w-]+(?:\([^\n)]*\))?!:|^BREAKING[ -]CHANGE:", messages):
        return "major"
    if re.search(r"(?m)^feat(?:\([^\n)]*\))?:", messages):
        return "minor"
    return "patch"


def bump(version: str, level: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown version level: {level}")


def base_version(ref: str) -> str:
    if git("ls-tree", "--name-only", ref, str(VERSION_FILE).replace("\\", "/")):
        return VERSION_RE.search(git("show", f"{ref}:x_operator/version.py"))[1]
    return tomllib.loads(git("show", f"{ref}:pyproject.toml"))["project"]["version"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="Git revision before these changes")
    parser.add_argument("--level", choices=("auto", "patch", "minor", "major"), default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    baseline = base_version(args.base)  # Invalid refs fail safely instead of resetting the version.
    path = ROOT / VERSION_FILE
    source = path.read_text(encoding="utf-8")
    current = VERSION_RE.search(source)[1]
    if tuple(map(int, current.split("."))) > tuple(map(int, baseline.split("."))):
        print(f"Already versioned: {current} (base {baseline}); unchanged")
        return
    if current != baseline:
        raise SystemExit(f"Version {current} is older than base {baseline}; update your checkout first")
    messages = git("log", "--format=%B", f"{args.base}..HEAD")
    changed = git("diff", "--name-only", args.base)
    if not changed and not messages:
        print("No changes; version unchanged")
        return
    level = change_level(messages) if args.level == "auto" else args.level
    new = bump(current, level)
    print(f"{current} -> {new} ({level})")
    if args.dry_run:
        return
    # Dynamic editable packages have no version in uv.lock. Hatch reads this file.
    path.write_text(VERSION_RE.sub(f'__version__ = "{new}"', source), encoding="utf-8")


if __name__ == "__main__":
    main()
