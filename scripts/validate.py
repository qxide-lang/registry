#!/usr/bin/env python3
"""Validates the Qxide registry: index format, archive checksums and contents, and (with
--base) that a pull request only appends versions or toggles `yanked`.

Usage: python scripts/validate.py [--base <git ref>] [--summary <file>] [--root <dir>]
Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,63}$")
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = {"name", "vers", "deps", "cksum", "yanked", "v"}
KNOWN = REQUIRED | {"qxide", "description", "dl"}
DEFAULT_DL = "packages/{prefix}/{name}/{name}-{version}.tar.gz"
MAX_ARCHIVE = 10 << 20
MAX_UNPACKED = 50 << 20
MAX_FILES = 2000

errors: list[str] = []
summary: list[str] = []


def error(where: str, what: str) -> None:
    errors.append(f"{where}: {what}")


def prefix(name: str) -> str:
    if len(name) == 1:
        return "1"
    if len(name) == 2:
        return "2"
    if len(name) == 3:
        return f"3/{name[0]}"
    return f"{name[:2]}/{name[2:4]}"


def dl_template() -> str:
    config = ROOT / "config.json"
    if not config.is_file():
        return DEFAULT_DL
    return json.loads(config.read_text(encoding="utf-8")).get("dl", DEFAULT_DL)


def archive_path(entry: dict, template: str) -> str:
    return (
        (entry.get("dl") or template)
        .replace("{name}", entry["name"])
        .replace("{version}", entry["vers"])
        .replace("{prefix}", prefix(entry["name"]))
        .replace("{sha256-checksum}", entry["cksum"])
    )


def check_entry(where: str, entry: dict, file_name: str) -> bool:
    if not isinstance(entry, dict):
        error(where, "not a JSON object")
        return False
    missing = REQUIRED - entry.keys()
    if missing:
        error(where, f"missing keys {sorted(missing)}")
        return False
    unknown = entry.keys() - KNOWN
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")
    ok = True
    if entry["v"] != 1:
        error(where, "`v` must be 1")
        ok = False
    if entry["name"] != file_name or not NAME.match(str(entry["name"])):
        error(where, f"name `{entry['name']}` does not match the index file `{file_name}`")
        ok = False
    if not SEMVER.match(str(entry["vers"])):
        error(where, f"`vers` {entry['vers']!r} is not a semantic version")
        ok = False
    if not SHA256.match(str(entry["cksum"])):
        error(where, "`cksum` must be 64 lowercase hex characters")
        ok = False
    if not isinstance(entry["yanked"], bool):
        error(where, "`yanked` must be true or false")
        ok = False
    if not isinstance(entry["deps"], list):
        error(where, "`deps` must be a list")
        return False
    for d in entry["deps"]:
        if not (isinstance(d, dict) and NAME.match(str(d.get("name", ""))) and isinstance(d.get("req"), str)):
            error(where, f"bad dependency {d!r}")
            ok = False
    return ok


def check_archive(where: str, entry: dict, data: bytes) -> list[str]:
    """Checks one archive; returns its file list for the review summary."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["cksum"]:
        error(where, f"sha256 of the archive is {digest}, the index says {entry['cksum']}")
        return []
    if len(data) > MAX_ARCHIVE:
        error(where, "archive is larger than 10 MiB")
        return []
    root = f"{entry['name']}-{entry['vers']}"
    files, total, manifest = [], 0, None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_FILES:
                error(where, f"more than {MAX_FILES} files")
            for m in members:
                p = PurePosixPath(m.name)
                if p.is_absolute() or ".." in p.parts or p.parts[:1] != (root,) or any(":" in s or "\\" in s for s in p.parts):
                    error(where, f"unsafe path `{m.name}`")
                    continue
                if m.isdir():
                    continue
                if not m.isfile():
                    error(where, f"`{m.name}` is not a regular file")
                    continue
                total += m.size
                rel = "/".join(p.parts[1:])
                files.append(f"{rel} ({m.size} B)")
                if rel == "qxide.toml":
                    manifest = tar.extractfile(m).read().decode("utf-8")
    except (tarfile.TarError, OSError, EOFError) as e:
        error(where, f"not a gzip tar archive: {e}")
        return []
    if total > MAX_UNPACKED:
        error(where, "unpacked size is larger than 50 MiB")
    if manifest is None:
        error(where, "archive has no qxide.toml")
        return files
    try:
        pkg = tomllib.loads(manifest).get("package", {})
    except tomllib.TOMLDecodeError as e:
        error(where, f"qxide.toml is not valid TOML: {e}")
        return files
    if pkg.get("name") != entry["name"] or pkg.get("version") != entry["vers"]:
        error(where, "qxide.toml name/version do not match the index entry")
    for key in ("description", "license", "qxide"):
        if not pkg.get(key):
            error(where, f"qxide.toml has no `{key}`")
    if not any(f.startswith("src/lib.qx ") for f in files):
        error(where, "archive has no src/lib.qx")
    return files


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def base_lines(base: str, rel: str) -> list[str]:
    try:
        return git("show", f"{base}:{rel}").splitlines()
    except subprocess.CalledProcessError:
        return []


def squash(name: str) -> str:
    return name.replace("-", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="git ref of the target branch, to check what a PR changes")
    parser.add_argument("--summary", help="markdown file for reviewers (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--root", help="registry checkout (default: this script's repository)")
    args = parser.parse_args()
    global ROOT
    if args.root:
        ROOT = Path(args.root).resolve()

    template = dl_template()
    index_dir = ROOT / "index"
    names: dict[str, list[dict]] = {}
    for path in sorted(p for p in index_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        rel = path.relative_to(ROOT).as_posix()
        name = path.name
        if rel != f"index/{prefix(name)}/{name}" or not NAME.match(name):
            error(rel, f"index files live at index/{prefix(name)}/{name} and use valid package names")
            continue
        entries, seen = [], set()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            where = f"{rel}:{i}"
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                error(where, f"invalid JSON: {e}")
                continue
            if not check_entry(where, entry, name):
                continue
            if entry["vers"] in seen:
                error(where, f"version {entry['vers']} is listed twice")
            seen.add(entry["vers"])
            entries.append(entry)
        names[name] = entries

    # Archives hosted in this repository must exist and match.
    new_files: dict[str, list[str]] = {}
    for name, entries in names.items():
        for entry in entries:
            target = archive_path(entry, template)
            if "://" in target:
                continue
            where = f"{name} {entry['vers']}"
            archive = ROOT / target
            if not archive.is_file():
                error(where, f"archive {target} is missing")
                continue
            new_files[where] = check_archive(where, entry, archive.read_bytes())

    # Dependencies must name packages in this registry.
    for name, entries in names.items():
        for entry in entries:
            for d in entry["deps"]:
                if d["name"] not in names:
                    error(f"{name} {entry['vers']}", f"dependency `{d['name']}` is not in the registry")

    changed = set(new_files)
    if args.base:
        changed = set()
        for name, entries in names.items():
            rel = f"index/{prefix(name)}/{name}"
            old = [l for l in base_lines(args.base, rel) if l.strip()]
            new = [json.dumps(e, sort_keys=True) for e in entries]
            old_entries = [json.loads(l) for l in old]
            # Published lines may only flip `yanked`; nothing is removed or rewritten.
            for i, o in enumerate(old_entries):
                if i >= len(entries):
                    error(rel, f"version {o.get('vers')} was removed; yank it instead")
                    continue
                n = dict(entries[i], yanked=o.get("yanked"))
                if json.dumps(n, sort_keys=True) != json.dumps(o, sort_keys=True):
                    error(rel, f"line {i + 1} changed; published versions are immutable except `yanked`")
            for e in entries[len(old_entries):]:
                changed.add(f"{name} {e['vers']}")
            if not old and new:
                # A new package name: reject look-alikes of existing names.
                for other in names:
                    if other != name and squash(other) == squash(name) and base_lines(args.base, f"index/{prefix(other)}/{other}"):
                        error(rel, f"`{name}` is too similar to the existing package `{other}`")
        diff = [l.split("\t") for l in git("diff", "--name-status", f"{args.base}...HEAD").splitlines()]
        touched = [p for _, *paths in diff for p in paths]
        # Publishing and yanking pull requests change registry data only, never scripts or workflows.
        if any(p.startswith(("index/", "packages/")) for p in touched):
            for p in touched:
                if not p.startswith(("index/", "packages/")):
                    error(p, "a pull request that changes index/ or packages/ may not change other files")
        for status, *paths in diff:
            for p in paths:
                if p.startswith("packages/") and status[0] in "MDR":
                    error(p, "published archives cannot be modified or deleted")
                if p == "search.json":
                    error(p, "search.json is generated after merge; do not edit it in a pull request")

    if args.summary:
        lines = ["## Registry check", ""]
        lines += [f"- {e}" for e in errors] or ["All checks passed."]
        for where in sorted(changed):
            lines += ["", f"### {where}", ""] + [f"- `{f}`" for f in new_files.get(where, [])]
        Path(args.summary).open("a", encoding="utf-8").write("\n".join(lines) + "\n")

    for e in errors:
        print(f"error: {e}")
    print(f"{len(names)} packages, {sum(len(v) for v in names.values())} versions, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
