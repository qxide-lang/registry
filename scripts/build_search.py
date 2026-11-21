#!/usr/bin/env python3
"""Regenerates search.json (read by `qxide search`): every package with its newest non-yanked
version and description. Run after merges to main; the output is sorted and stable.

Usage: python scripts/build_search.py [--root <dir>]
"""

import argparse
import json
import re
from pathlib import Path

SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?")


def key(vers: str):
    """Sort key for semver precedence (ignoring build metadata), or `None` if the version does not
    parse.
    """
    m = SEMVER.fullmatch(vers.split("+", 1)[0]) if isinstance(vers, str) else None
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        # A release sorts after all of its pre-releases.
        return (int(major), int(minor), int(patch), 1, ())
    # Numeric pre-release identifiers compare numerically and sort before alphanumeric ones; with an
    # equal prefix, the longer identifier list is newer (1.0.0-rc.2 < 1.0.0-rc.10 < 1.0.0-rc.10.1).
    # validate.py rejects leading zeros, so length then digits is numeric order, without `int()`,
    # which refuses more than 4300 digits.
    ids = tuple((0, len(p), p) if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(major), int(minor), int(patch), 0, ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    root = Path(parser.parse_args().root)
    packages = []
    for path in sorted(p for p in (root / "index").rglob("*") if p.is_file() and p.name != ".gitkeep"):
        # Split on `\n` only, like the client (not on `\r`, `\f` or other line breaks).
        text = path.read_bytes().decode("utf-8")
        entries = [json.loads(l) for l in text.split("\n") if l.strip()]
        live = [e for e in entries if not e.get("yanked") and key(e.get("vers")) is not None]
        if not live:
            continue
        best = max(live, key=lambda e: key(e["vers"]))
        packages.append(
            {"name": best["name"], "version": best["vers"], "description": best.get("description", "")}
        )
    packages.sort(key=lambda p: p["name"])
    out = json.dumps({"version": 1, "packages": packages}, indent=2, ensure_ascii=False) + "\n"
    (root / "search.json").write_text(out, encoding="utf-8", newline="\n")
    print(f"search.json: {len(packages)} packages")


if __name__ == "__main__":
    main()
