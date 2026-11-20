#!/usr/bin/env python3
"""Regenerates search.json (read by `qxide search`): every package with its newest non-yanked
version and description. Run after merges to main; the output is sorted and stable.

Usage: python scripts/build_search.py [--root <dir>]
"""

import argparse
import json
import re
from pathlib import Path

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def key(vers: str):
    m = SEMVER.match(vers)
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch, pre = m.groups()
    # A release sorts after its pre-releases.
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    root = Path(parser.parse_args().root)
    packages = []
    for path in sorted(p for p in (root / "index").rglob("*") if p.is_file() and p.name != ".gitkeep"):
        entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        live = [e for e in entries if not e.get("yanked")]
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
