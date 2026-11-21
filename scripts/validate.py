#!/usr/bin/env python3
"""Validates the Qxide registry: index format, archive checksums and contents, owner files, and
(with --base) that a pull request only appends versions to packages its author owns.

Usage: python scripts/validate.py [--base <git ref> --author-id <id> --author-login <login>]
                                  [--summary <file>] [--root <dir>]
Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import unicodedata
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Patterns are used with fullmatch (`$` would also accept a trailing newline).
NAME = re.compile(r"[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,63}")
# Names the language itself uses (`import std.fs;`), never packages.
RESERVED = {"std"}
SHA256 = re.compile(r"[0-9a-f]{64}")
U64_MAX = (1 << 64) - 1
MAX_COMPARATORS = 32
# Download locations after template expansion: an https URL or a plain path in this repository.
DL_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%+!$&'()*,;=:@/-]*)?")
DL_SEGMENT = re.compile(r"[A-Za-z0-9._+-]+")
REQUIRED = {"name", "vers", "deps", "cksum", "yanked", "v"}
KNOWN = REQUIRED | {"qxide", "description", "dl"}
DEFAULT_DL = "packages/{prefix}/{name}/{name}-{version}.tar.gz"
# Archive limits, the same as the client's in src/pkg/archive.rs.
MAX_ARCHIVE = 10 << 20
MAX_UNPACKED = 50 << 20
MAX_FILES = 2000
# Decompressed tar stream: MAX_UNPACKED plus room for headers, padding and long names.
MAX_TAR = MAX_UNPACKED + (16 << 20)
# GNU long-name entry (`qxide publish` writes one for paths over 100 bytes).
MAX_LONG_NAME = 4096
BLOCK = 512

# Windows device names, reserved with or without an extension (mirrors src/pkg/archive.rs).
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{dev}{n}" for dev in ("COM", "LPT") for n in "123456789¹²³"
}
WINDOWS_INVALID = set('<>:"/\\|?*')
ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
INVALID_VALUE = "`{key}` has invalid value {value}"
CONFIG_LOCKED = "cannot be modified in a pull request"
SELF_DEPENDENCY = "cannot depend on itself"
# Deepest JSON nesting accepted (serde_json in the client allows 128).
MAX_JSON_DEPTH = 100
# toml_edit's limit in the client; this count includes the document and `[package]` tables.
MAX_TOML_DEPTH = 80
# A string (to the end of the text if unterminated, so the scan stays linear) or a bracket.
JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"?|[\[\]{}]', re.DOTALL)
# Folded before new package names are compared (B-P7): hyphens are dropped, then characters and
# pairs that look alike in common fonts are mapped to one form.
LOOK_ALIKE_CHARS = str.maketrans("0123456789i", "olzeasbtbgl")
LOOK_ALIKE_PAIRS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
# New names may not pass for official packages: none may start like `qxide` or equal `std`.
RESERVED_PREFIXES = ("qxide",)
RESERVED_NAMES = ("std",)
# Owner files: one `{"id": <GitHub user id>, "login": <login>}` line per owner, plus
# `"pending": true` for an invited account that has not accepted yet. The numeric id is what is
# compared; logins can be renamed and then registered again by someone else.
OWNER_KEYS = {"id", "login"}
INVITE_KEYS = OWNER_KEYS | {"pending"}
MAX_OWNERS = 32
GITHUB_ID = re.compile(r"[1-9][0-9]{0,18}")
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
# Pull request authors may also be GitHub Apps (`dependabot[bot]`); owner files list users only.
AUTHOR_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
REGISTRY_DATA = ("index/", "packages/", "owners/")

errors: list[str] = []
summary: list[str] = []


def shown(text: str) -> str:
    """`text` with every character that is not printable (controls, line and paragraph separators,
    format characters such as bidirectional overrides) written as `\\u{XXXX}`. Text from a pull
    request then stays on its own line in the job log, where a line starting with `::` is a
    workflow command, and in the job summary (B-P8).
    """
    return "".join(c if c.isprintable() else f"\\u{{{ord(c):04x}}}" for c in text)


def code(text: str) -> str:
    """`text` as one Markdown code span: `shown`, then fenced by more backticks than its longest
    run of backticks, so no Markdown, HTML, link, or mention inside it is rendered (B-P8).
    """
    t = shown(text)
    fence = "`" * (max(map(len, re.findall("`+", t)), default=0) + 1)
    # CommonMark strips one space from each end of a span when both are present.
    pad = " " if t[:1] in ("`", " ") or t[-1:] in ("`", " ") else ""
    return f"{fence}{pad}{t}{pad}{fence}"


def skeleton(name: str) -> str:
    """Form of a package name in which look-alike names are equal (B-P7)."""
    s = name.replace("-", "").translate(LOOK_ALIKE_CHARS)
    for pair, single in LOOK_ALIKE_PAIRS:
        s = s.replace(pair, single)
    return s


def portable(part: str) -> bool:
    """Matches the client's `portable_component` rule: a path component every OS stores unchanged.
    """
    if not part or part.endswith((".", " ")):
        return False
    if any(c in WINDOWS_INVALID or unicodedata.category(c) == "Cc" for c in part):
        return False
    return part.split(".")[0].rstrip(" ").translate(ASCII_UPPER) not in RESERVED


def fold(part: str) -> str:
    """Case folding used to detect archive entries that collide on case-insensitive file systems."""
    return part.lower().upper()


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


def owners_path(name: str) -> str:
    return f"owners/{prefix(name)}/{name}"


def confirmed(owners: list[dict]) -> set[int]:
    """Ids of the owners in `owners`; invited accounts (`pending`) are not owners yet."""
    return {o["id"] for o in owners if "pending" not in o}


def invited(owners: list[dict]) -> set[int]:
    return {o["id"] for o in owners if "pending" in o}


def parse_owners(where: str, data: bytes) -> list[dict] | None:
    """Returns the owners listed in an owner file, or `None` after reporting why it is invalid."""
    try:
        lines = [l for l in index_lines(data) if l.strip()]
    except UnicodeDecodeError as e:
        error(where, f"not UTF-8: {e}")
        return None
    owners, ids, ok = [], set(), True
    for i, line in enumerate(lines, 1):
        try:
            owner = strict_json(line)
        except ValueError as e:
            error(f"{where}:{i}", f"invalid JSON: {e}")
            ok = False
            continue
        if (
            not isinstance(owner, dict)
            or owner.keys() not in (OWNER_KEYS, INVITE_KEYS)
            or owner.get("pending", True) is not True
            # `True == 1` in Python, so check the type before the value.
            or type(owner["id"]) is not int
            or not GITHUB_ID.fullmatch(str(owner["id"]))
            or not isinstance(owner["login"], str)
            or not GITHUB_LOGIN.fullmatch(owner["login"])
        ):
            error(f"{where}:{i}", 'an owner line is {"id": <GitHub user id>, "login": "<GitHub login>"}, with "pending": true for an invitation')
            ok = False
            continue
        if owner["id"] in ids:
            error(f"{where}:{i}", f"owner id {owner['id']} is listed twice")
            ok = False
        ids.add(owner["id"])
        owners.append(owner)
    if ok and not confirmed(owners):
        error(where, "an owner file lists at least one owner who is not only invited")
        ok = False
    if len(owners) > MAX_OWNERS:
        error(where, f"more than {MAX_OWNERS} owners")
        ok = False
    return owners if ok else None


def dl_template() -> str:
    config = ROOT / "config.json"
    if not config.is_file():
        return DEFAULT_DL
    try:
        doc = strict_json(config.read_bytes().decode("utf-8"))
    except ValueError as e:
        error("config.json", f"invalid JSON: {e}")
        return DEFAULT_DL
    if not isinstance(doc, dict):
        error("config.json", "not a JSON object")
        return DEFAULT_DL
    dl = doc.get("dl", DEFAULT_DL)
    if not isinstance(dl, str):
        error("config.json", INVALID_VALUE.format(key="dl", value=repr(dl)))
        return DEFAULT_DL
    return dl


def archive_path(entry: dict, template: str) -> str:
    return (
        (entry.get("dl") or template)
        .replace("{name}", entry["name"])
        .replace("{version}", entry["vers"])
        .replace("{prefix}", prefix(entry["name"]))
        .replace("{sha256-checksum}", entry["cksum"])
    )


def valid_dl(target: str) -> bool:
    """Reports whether an expanded download location is valid: an `https` URL or a relative path of
    plain segments inside the registry.
    """
    if target.startswith("https://"):
        return DL_URL.fullmatch(target) is not None
    parts = target.split("/")
    return all(DL_SEGMENT.fullmatch(p) and p not in (".", "..") and portable(p) for p in parts)


# Port of the Rust `semver` crate parser used by the qxide client, so this checker accepts the exact
# same versions and requirements: 64-bit numbers, no leading zeros, ASCII only.
class _Bad(Exception):
    pass


def _number(s: str, i: int) -> tuple[int, int]:
    j = i
    while j < len(s) and "0" <= s[j] <= "9":
        j += 1
    # 20 digits hold any u64; Python refuses to convert more than 4300 (B-P6).
    if j == i or (j - i > 1 and s[i] == "0") or j - i > 20:
        raise _Bad
    value = int(s[i:j])
    if value > U64_MAX:
        raise _Bad
    return value, j


def _dot(s: str, i: int) -> int:
    if s[i : i + 1] != ".":
        raise _Bad
    return i + 1


def _identifier(s: str, i: int, pre: bool) -> tuple[str, int]:
    """Parses dot-separated `[0-9A-Za-z-]` segments of a pre-release or build identifier; numeric
    pre-release segments may not have a leading zero.
    """
    start = i
    while True:
        j = i
        while j < len(s) and s[j].isascii() and (s[j].isalnum() or s[j] == "-"):
            j += 1
        segment = s[i:j]
        if not segment or (pre and len(segment) > 1 and segment.isdigit() and segment[0] == "0"):
            raise _Bad
        if s[j : j + 1] == ".":
            i = j + 1
            continue
        return s[start:j], j


def parse_version(s: str):
    """Returns `(major, minor, patch, pre, build)` as parsed by the client, or `None`."""
    try:
        major, i = _number(s, 0)
        minor, i = _number(s, _dot(s, i))
        patch, i = _number(s, _dot(s, i))
        pre = build = ""
        if s[i : i + 1] == "-":
            pre, i = _identifier(s, i + 1, True)
        if s[i : i + 1] == "+":
            build, i = _identifier(s, i + 1, False)
        if i != len(s):
            raise _Bad
        return major, minor, patch, pre, build
    except _Bad:
        return None


def registry_version(s) -> bool:
    """Reports whether `s` is a valid `vers`: semver without build metadata, which would tie in
    precedence with the plain version.
    """
    v = parse_version(s) if isinstance(s, str) else None
    return v is not None and v[4] == ""


def _comparator(s: str) -> tuple[tuple, int]:
    """Parses one comparator at the start of `s`. Returns it as `semver::Comparator` stores it,
    `(op, major, minor, patch, pre)` with `None` for a missing or wildcard part and build metadata
    dropped, and the index past it and its trailing spaces.
    """
    i = 0
    op = "^"  # `Op::DEFAULT`
    if s.startswith((">=", "<=")):
        op, i = s[:2], 2
    elif s[:1] in ("=", ">", "<", "~", "^"):
        op, i = s[:1], 1
    default_op = i == 0
    while s[i : i + 1] == " ":
        i += 1
    major, i = _number(s, i)
    wild = False
    minor = patch = None
    pre = ""
    if s[i : i + 1] == ".":
        i += 1
        if s[i : i + 1] in ("*", "x", "X"):
            wild, i = True, i + 1
            if default_op:
                op = "*"
        else:
            minor, i = _number(s, i)
    if s[i : i + 1] == ".":
        i += 1
        if s[i : i + 1] in ("*", "x", "X"):
            i += 1
            if default_op:
                op = "*"
        elif wild:
            raise _Bad
        else:
            patch, i = _number(s, i)
    if patch is not None and s[i : i + 1] == "-":
        pre, i = _identifier(s, i + 1, True)
    if patch is not None and s[i : i + 1] == "+":
        _, i = _identifier(s, i + 1, False)
    while s[i : i + 1] == " ":
        i += 1
    return (op, major, minor, patch, pre), i


def parse_req(s):
    """Returns the comparators of requirement `s` as the client's `semver::VersionReq` holds them,
    so two requirements are equal for the client exactly when the results are equal, or `None` if
    the client rejects `s`.
    """
    if not isinstance(s, str):
        return None
    try:
        s = s.lstrip(" ")
        if s[:1] in ("*", "x", "X"):
            return () if s[1:].lstrip(" ") == "" else None
        out = []
        while True:
            comparator, i = _comparator(s)
            out.append(comparator)
            s = s[i:]
            if not s:
                return tuple(out)
            if s[0] != "," or len(out) == MAX_COMPARATORS:
                raise _Bad
            s = s[1:].lstrip(" ")
    except _Bad:
        return None


def valid_req(s) -> bool:
    """Reports whether the client accepts `s` as a version requirement."""
    return parse_req(s) is not None


def same_req(a, b) -> bool:
    """Reports whether the client parses `a` and `b` to equal requirements."""
    ra = parse_req(a)
    return ra is not None and ra == parse_req(b)


def precedence(vers: str):
    """Sort key giving semver precedence; versions with equal keys represent the same release."""
    major, minor, patch, pre, _ = parse_version(vers)
    if not pre:
        return (major, minor, patch, 1, ())
    # Numeric identifiers have no leading zeros, so length then digits is numeric order, without
    # `int()`, which refuses more than 4300 digits (B-P6).
    ids = tuple((0, len(p), p) if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (major, minor, patch, 0, ids)


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
    # `True == 1` in Python, so check the type before the value.
    if type(entry["v"]) is not int or entry["v"] != 1:
        error(where, "`v` must be 1")
        ok = False
    if entry["name"] != file_name or not isinstance(entry["name"], str) or not NAME.fullmatch(entry["name"]):
        error(where, f"name `{entry['name']}` does not match the index file `{file_name}`")
        ok = False
    elif entry["name"] in RESERVED:
        error(where, f"name `{entry['name']}` is reserved")
        ok = False
    if not isinstance(entry["vers"], str) or parse_version(entry["vers"]) is None:
        error(where, f"`vers` {entry['vers']!r} is not a semantic version")
        ok = False
    elif not registry_version(entry["vers"]):
        error(where, INVALID_VALUE.format(key="vers", value=repr(entry["vers"])))
        ok = False
    if not isinstance(entry["cksum"], str) or not SHA256.fullmatch(entry["cksum"]):
        error(where, "`cksum` must be 64 lowercase hex characters")
        ok = False
    if not isinstance(entry["yanked"], bool):
        error(where, "`yanked` must be true or false")
        ok = False
    if "qxide" in entry and not valid_req(entry["qxide"]):
        error(where, INVALID_VALUE.format(key="qxide", value=repr(entry["qxide"])))
        ok = False
    for key in ("description", "dl"):
        if key in entry and not isinstance(entry[key], str):
            error(where, INVALID_VALUE.format(key=key, value=repr(entry[key])))
            ok = False
    # `qxide search` prints descriptions to terminals: no C0, DEL or C1 controls (escape sequences).
    if isinstance(entry.get("description"), str) and any(unicodedata.category(c) == "Cc" for c in entry["description"]):
        error(where, "`description` contains control characters")
        ok = False
    if not isinstance(entry["deps"], list):
        error(where, "`deps` must be a list")
        return False
    for d in entry["deps"]:
        if not (
            isinstance(d, dict)
            and isinstance(d.get("name"), str)
            and NAME.fullmatch(d["name"])
            and valid_req(d.get("req"))
        ):
            error(where, f"bad dependency {d!r}")
            ok = False
    return ok


class BadArchive(Exception):
    pass


def gunzip(data: bytes) -> bytes:
    """Decompresses the single gzip member the client reads, refusing anything after it (Python's
    gzip would read further members, the client would not) and output over `MAX_TAR`.
    """
    d = zlib.decompressobj(wbits=31)
    try:
        out = d.decompress(data, MAX_TAR + 1)
    except zlib.error as e:
        raise BadArchive(f"not a gzip file: {e}") from None
    if len(out) > MAX_TAR:
        raise BadArchive(f"decompressed archive is larger than {MAX_TAR >> 20} MiB")
    if not d.eof:
        raise BadArchive("gzip data is truncated")
    if d.unused_data:
        raise BadArchive("data after the first gzip member")
    return out


def _octal(field: bytes, what: str) -> int:
    # Plain octal digits only: no base-256 numbers, signs, or other forms some readers accept.
    text = field.split(b"\0", 1)[0].strip(b" ")
    if not text or any(c not in b"01234567" for c in text):
        raise BadArchive(f"tar header has an invalid {what} field")
    return int(text.decode("ascii"), 8)


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadArchive(f"path {raw!r} is not UTF-8") from None


def tar_entries(tar: bytes):
    """Yields `(type, path, data)` for each tar entry, reading headers like the client (the `tar`
    crate in raw mode): a GNU long name of at most `MAX_LONG_NAME` bytes names the next entry, and
    every other extension header (PAX, GNU long link, sparse) is an entry of its own, which the
    caller rejects as not a regular file. Anything other readers could see differently is refused:
    a bad checksum, a non-octal size, truncated data, a ustar prefix on a non-ustar header, and
    non-zero bytes after the end-of-archive block.
    """
    pos, long_name = 0, None
    while pos < len(tar):
        header = tar[pos : pos + BLOCK]
        if len(header) < BLOCK:
            raise BadArchive("tar data is truncated")
        if not any(header):
            if any(tar[pos:]):
                raise BadArchive("data after the end of the tar archive")
            break
        if sum(header[:148]) + sum(header[156:]) + 8 * 32 != _octal(header[148:156], "checksum"):
            raise BadArchive("tar header checksum mismatch")
        kind = header[156:157]
        size = _octal(header[124:136], "size")
        start = pos + BLOCK
        pos = start + size + (-size % BLOCK)
        if pos > len(tar):
            raise BadArchive("tar data is truncated")
        data = memoryview(tar)[start : start + size]
        if kind == b"L":
            if size > MAX_LONG_NAME or long_name is not None:
                raise BadArchive("invalid GNU long name entry")
            long_name = _text(bytes(data).rstrip(b"\0"))
            continue
        name = header[:100].split(b"\0", 1)[0]
        if header[257:265] == b"ustar\x0000":
            field = header[345:500].split(b"\0", 1)[0]
            if field:
                name = field + b"/" + name
        elif any(header[345:500]):
            # Python's tarfile reads this area as a path prefix, the client does not.
            raise BadArchive("non-ustar tar header has data in the prefix field")
        if long_name is not None:
            path, long_name = long_name, None
        else:
            path = _text(name)
        yield kind, path, data
    if long_name is not None:
        raise BadArchive("GNU long name entry is not followed by an entry")


def manifest_deps(where: str, table) -> Counter | None:
    """Returns the `[dependencies]` of a package's qxide.toml as `(name, requirement)` pairs, the
    way `installed_deps` in the client reads them, or `None` after reporting an error.
    """
    if not isinstance(table, dict):
        error(where, "qxide.toml `dependencies` is not a table")
        return None
    deps = Counter()
    for name, value in table.items():
        # The client refuses a manifest whose `path` is not a relative string (B-P13).
        path = value.get("path", "") if isinstance(value, dict) else ""
        if not isinstance(path, str) or path.startswith(("/", "\\")) or ":" in path:
            error(where, f"qxide.toml dependency `{name}` has a `path` the client rejects")
            return None
        req = value.get("version") if isinstance(value, dict) else value
        parsed = parse_req(req)
        if not NAME.fullmatch(name) or parsed is None:
            error(where, f"qxide.toml has an invalid or path-only dependency `{name}`")
            return None
        deps[(name, parsed)] += 1
    return deps


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
    shipped: set[str] = set()
    folded_files: set[tuple[str, ...]] = set()
    folded_dirs: set[tuple[str, ...]] = set()
    try:
        count = 0
        for kind, name, content in tar_entries(gunzip(data)):
            count += 1
            if count > MAX_FILES:
                error(where, f"more than {MAX_FILES} files")
                break
            parts = name.split("/")
            if kind == b"5" and len(parts) > 1 and parts[-1] == "":
                parts.pop()
            if (
                parts[0] != root
                or any(s in ("", ".", "..") or ":" in s or "\\" in s for s in parts)
                or not all(portable(s) for s in parts[1:])
            ):
                error(where, f"unsafe path `{name}`")
                continue
            if kind == b"5":
                continue
            if kind not in (b"0", b"\0"):
                error(where, f"`{name}` is not a regular file")
                continue
            rel = "/".join(parts[1:])
            if not rel:
                error(where, f"unsafe path `{name}`")
                continue
            folded = tuple(fold(s) for s in parts[1:])
            prefixes = [folded[:k] for k in range(1, len(folded))]
            if folded in folded_files or folded in folded_dirs or any(d in folded_files for d in prefixes):
                error(where, "entry `{path}` collides case-insensitively with another entry".format(path=name))
                continue
            folded_files.add(folded)
            folded_dirs.update(prefixes)
            shipped.add(rel)
            total += len(content)
            if total > MAX_UNPACKED:
                error(where, "unpacked size is larger than 50 MiB")
                break
            files.append(f"{rel} ({len(content)} B)")
            if rel == "qxide.toml":
                manifest = bytes(content)
    except BadArchive as e:
        error(where, f"not a valid archive: {e}")
        return []
    if manifest is None:
        error(where, "archive has no qxide.toml")
        return files
    try:
        doc = tomllib.loads(manifest.decode("utf-8"))
    # ValueError: also integers too long to convert; RecursionError: deeply nested values (B-P6).
    except (ValueError, RecursionError) as e:
        error(where, f"qxide.toml is not valid UTF-8 TOML: {type(e).__name__}: {e}")
        return files
    # The client's TOML parser (toml_edit) refuses nesting deeper than 80 levels.
    if nesting(doc) > MAX_TOML_DEPTH:
        error(where, f"qxide.toml is nested deeper than {MAX_TOML_DEPTH} levels")
        return files
    pkg = doc.get("package", {})
    if not isinstance(pkg, dict):
        error(where, "qxide.toml `package` is not a table")
        return files
    if pkg.get("name") != entry["name"] or pkg.get("version") != entry["vers"]:
        error(where, "qxide.toml name/version do not match the index entry")
    for key in ("description", "license", "qxide"):
        if not pkg.get(key):
            error(where, f"qxide.toml has no `{key}`")
    # The client resolves with the index line but checks `qxide.lock` against qxide.toml (M-08).
    deps = manifest_deps(where, doc.get("dependencies", {}))
    if deps is not None and deps != Counter((d["name"], parse_req(d["req"])) for d in entry["deps"]):
        error(where, "qxide.toml [dependencies] do not match `deps` in the index entry")
    if "src/lib.qx" not in shipped:
        error(where, "archive has no src/lib.qx")
    return files


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def index_lines(data: bytes) -> list[str]:
    """Decodes UTF-8 and splits lines like Rust's `str::lines` in the client: only on a newline
    character, stripping a single trailing carriage return.
    """
    lines = data.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    return [l[:-1] if l.endswith("\r") else l for l in lines]


def _reject_constant(name: str):
    raise ValueError(name)


def json_depth(text: str) -> int:
    """Deepest nesting of `[` and `{` in JSON text, found without parsing it (strings removed
    first). Python's parsers recurse once per level (B-P6).
    """
    depth = deepest = 0
    for m in JSON_TOKEN.finditer(text):
        token = m.group()
        if token in ("[", "{"):
            depth += 1
            deepest = max(deepest, depth)
        elif token in ("]", "}"):
            depth -= 1
    return deepest


def nesting(value) -> int:
    """Deepest nesting of dicts and lists in a parsed value, the outermost counting as 1; computed
    without recursion.
    """
    deepest, stack = 0, [(value, 1)]
    while stack:
        v, level = stack.pop()
        if isinstance(v, (dict, list)):
            deepest = max(deepest, level)
            stack.extend((c, level + 1) for c in (v.values() if isinstance(v, dict) else v))
    return deepest


def strict_json(line: str):
    """Wrapper around `json.loads` restricted to what the client's `serde_json` accepts; raises
    `ValueError` otherwise.
    """
    # Checked before parsing: deeper text would raise RecursionError instead (B-P6).
    if json_depth(line) > MAX_JSON_DEPTH:
        raise ValueError(f"nested deeper than {MAX_JSON_DEPTH} levels")
    value = json.loads(line, parse_constant=_reject_constant)
    # serde_json rejects lone surrogates (`"\\ud800"`).
    json.dumps(value, ensure_ascii=False).encode("utf-8")
    return value


def base_lines(base: str, rel: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{rel}"], capture_output=True)
    return index_lines(r.stdout) if r.returncode == 0 else []


def name_status(out: str) -> list[list[str]]:
    """Parses `git diff --name-status -z` into `[status, path...]` rows. Without `-z`, git quotes
    paths with unusual characters, and a quoted path would not start with `packages/` and the like.
    """
    fields, rows = out.split("\0"), []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        n = 2 if status[0] in "RC" else 1
        rows.append([status, *fields[i + 1 : i + 1 + n]])
        i += 1 + n
    return rows


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def ls_base(base: str, top: str) -> set[str]:
    return {p for p in git("ls-tree", "-r", "-z", "--name-only", base, "--", top).split("\0") if p}


def check_ownership(base: str, author, diff, names, head_owners, archive_owner) -> None:
    """Allows a pull request to change a package's index file, archives, or owner file only when its
    author is an owner in the package's owner file on the base branch, or is invited there and the
    pull request only accepts that invitation. A new package (no index file on the base branch) must
    add an owner file that lists exactly its author.
    """
    # Ownership comes from the base branch only, never from the pull request; `git` fails closed.
    base_index = ls_base(base, "index/")
    base_owner_files = ls_base(base, "owners/")
    touched: dict[str, set[str]] = {}
    for status, *paths in diff:
        for p in paths:
            if p.startswith(("index/", "owners/")):
                touched.setdefault(p.rsplit("/", 1)[-1], set()).add(p)
            elif p.startswith("packages/"):
                if p in archive_owner:
                    touched.setdefault(archive_owner[p], set()).add(p)
                elif status[0] not in "MDR":
                    # A file nobody's index line points to, such as an archive placed ahead of
                    # another package's next version. Changes to published archives are reported above.
                    error(p, "file under packages/ is not the archive of a version in the registry")
    for name, paths in sorted(touched.items()):
        rel = f"index/{prefix(name)}/{name}"
        orel = owners_path(name)
        if author is None:
            error(rel, "ownership cannot be checked without the pull request author")
        elif rel in base_index:
            if orel not in base_owner_files:
                error(rel, f"`{name}` has no owner file on the base branch; a maintainer must add {orel} first")
                continue
            current = parse_owners(f"{orel} (base)", git_bytes("show", f"{base}:{orel}"))
            if current is None:
                continue
            if not (ROOT / orel).is_file():
                error(orel, "the owner file of a published package cannot be removed")
                continue
            head = head_owners.get(name)
            if author["id"] in confirmed(current):
                # Owners invite (`"pending": true`) and remove accounts; only the invited account
                # can turn its own invitation into ownership.
                added = confirmed(head) - confirmed(current) if head is not None else set()
                if added:
                    error(orel, f"ids {sorted(added)} become owners only by accepting an invitation in their own pull request")
            elif author["id"] in invited(current):
                # Accepting: the pull request changes only the owner file, and only the author's line,
                # which loses `pending` and takes the author's current login.
                accepted = [{"id": author["id"], "login": author["login"]} if o["id"] == author["id"] else o for o in current]
                if paths != {orel}:
                    error(rel, f"{author['login']} (id {author['id']}) is invited, not an owner, of `{name}`; accept the invitation first")
                elif head is not None and head != accepted:
                    error(orel, "a pull request from an invited account only accepts its own invitation")
            else:
                error(rel, f"pull request author {author['login']} (id {author['id']}) is not an owner of `{name}`")
        elif name in names:
            if not (ROOT / orel).is_file():
                error(rel, f"a new package needs the owner file {orel} listing the pull request author")
            elif name in head_owners and head_owners[name] is not None and head_owners[name] != [author]:
                error(orel, "the owner file of a new package lists exactly the pull request author")
        # Otherwise an owner file without an index file, reported above.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="git ref of the target branch, to check what a PR changes")
    parser.add_argument("--summary", help="markdown file for reviewers (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--root", help="registry checkout (default: this script's repository)")
    # From the `pull_request_target` event (`pull_request.user`), which a pull request cannot set.
    parser.add_argument("--author-id", help="GitHub user id of the pull request author (with --base)")
    parser.add_argument("--author-login", help="GitHub login of the pull request author (with --base)")
    args = parser.parse_args()
    global ROOT
    if args.root:
        ROOT = Path(args.root).resolve()
    author = None
    if args.base:
        # Fail closed: without a known author no pull request can show that it owns a package.
        if (
            isinstance(args.author_id, str)
            and GITHUB_ID.fullmatch(args.author_id)
            and isinstance(args.author_login, str)
            and AUTHOR_LOGIN.fullmatch(args.author_login)
        ):
            author = {"id": int(args.author_id), "login": args.author_login}
        else:
            error("--author-id/--author-login", "the pull request author is required with --base")

    template = dl_template()
    index_dir = ROOT / "index"
    names: dict[str, list[dict]] = {}
    for path in sorted(p for p in index_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        rel = path.relative_to(ROOT).as_posix()
        name = path.name
        if rel != f"index/{prefix(name)}/{name}" or not NAME.fullmatch(name):
            error(rel, f"index files live at index/{prefix(name)}/{name} and use valid package names")
            continue
        if not portable(name):
            error(rel, f"`{name}` is a Windows device name")
            continue
        try:
            lines = index_lines(path.read_bytes())
        except UnicodeDecodeError as e:
            error(rel, f"not UTF-8: {e}")
            continue
        entries, seen = [], set()
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            where = f"{rel}:{i}"
            try:
                entry = strict_json(line)
            except ValueError as e:
                error(where, f"invalid JSON: {e}")
                continue
            if not check_entry(where, entry, name):
                continue
            # Compared by precedence, so no two lines can ever stand for the same release.
            key = precedence(entry["vers"])
            if key in seen:
                error(where, f"version {entry['vers']} is listed twice")
            seen.add(key)
            entries.append(entry)
        names[name] = entries

    # Every archive must be in this repository and pass the checks, including one whose `dl` is an
    # https URL: clients verify `cksum`, so they can only install the bytes checked here (B-P3).
    new_files: dict[str, list[str]] = {}
    # In-repository archive path -> package name, to tie files added under packages/ to a package.
    archive_owner: dict[str, str] = {}
    for name, entries in names.items():
        for entry in entries:
            target = archive_path(entry, template)
            where = f"{name} {entry['vers']}"
            if not valid_dl(target):
                error(where, INVALID_VALUE.format(key="dl", value=repr(target)))
                continue
            if "://" in target:
                local = archive_path(dict(entry, dl=None), template if "://" not in template else DEFAULT_DL)
                if not valid_dl(local):
                    error(where, INVALID_VALUE.format(key="dl", value=repr(local)))
                    continue
            else:
                local = target
            archive_owner[local] = name
            archive = ROOT / local
            try:
                # Python 3.12's `is_file` raises for some errors, such as a component over 255 bytes.
                data = archive.read_bytes() if archive.is_file() else None
            except OSError as e:
                error(where, f"archive {local} cannot be read: {e}")
                continue
            if data is None:
                if local == target:
                    error(where, f"archive {local} is missing")
                else:
                    error(where, f"archive {local} is missing (required for the external `dl` {target})")
                continue
            new_files[where] = check_archive(where, entry, data)

    # Dependencies must name packages in this registry.
    for name, entries in names.items():
        for entry in entries:
            for d in entry["deps"]:
                if d["name"] == name:
                    error(f"{name} {entry['vers']}", SELF_DEPENDENCY)
                if d["name"] not in names:
                    error(f"{name} {entry['vers']}", f"dependency `{d['name']}` is not in the registry")

    # Owner files: well formed, and only for packages that exist. A package without one is locked
    # for pull requests until a maintainer adds it (see `check_ownership`).
    head_owners: dict[str, list[dict] | None] = {}
    for path in sorted(p for p in (ROOT / "owners").rglob("*") if p.is_file() and p.name != ".gitkeep"):
        rel = path.relative_to(ROOT).as_posix()
        name = path.name
        if rel != owners_path(name) or not NAME.fullmatch(name) or not portable(name):
            error(rel, f"owner files live at {owners_path(name)} and use valid package names")
            continue
        if name not in names:
            error(rel, f"owner file for `{name}`, which has no index file")
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            error(rel, f"cannot be read: {e}")
            continue
        head_owners[name] = parse_owners(rel, data)

    changed = set(new_files)
    if args.base:
        changed = set()
        # Index files that are gone at HEAD are never compared below (B-P2); `git` fails closed.
        base_index = git("ls-tree", "-r", "-z", "--name-only", args.base, "--", "index/").split("\0")
        for rel in sorted(p for p in base_index if p and not p.endswith("/.gitkeep")):
            if not (ROOT / rel).is_file():
                error(rel, "index file was removed; yank its versions instead")
        new_names = []
        for name, entries in names.items():
            rel = f"index/{prefix(name)}/{name}"
            old = [l for l in base_lines(args.base, rel) if l.strip()]
            new = [json.dumps(e, sort_keys=True) for e in entries]
            old_entries = [json.loads(l) for l in old]
            # Published lines are immutable, except that an owner may set `yanked` to true (B-P5).
            for i, o in enumerate(old_entries):
                if i >= len(entries):
                    error(rel, f"version {o.get('vers')} was removed; ask a maintainer to yank it instead")
                    continue
                if json.dumps(entries[i], sort_keys=True) == json.dumps(o, sort_keys=True):
                    continue
                if json.dumps(dict(entries[i], yanked=o.get("yanked")), sort_keys=True) == json.dumps(o, sort_keys=True):
                    # Owners may yank (`check_ownership` checks the author, as for any change to the
                    # index file); restoring a yanked version is left to maintainers.
                    if not (o.get("yanked") is False and entries[i]["yanked"] is True):
                        error(rel, f"line {i + 1} un-yanks a version; only maintainers do that, in a commit to main")
                else:
                    error(rel, f"line {i + 1} changed; published versions are immutable")
            for e in entries[len(old_entries):]:
                changed.add(f"{name} {e['vers']}")
            if not old and new:
                new_names.append(name)
        # New names may not look like any other name, existing or added in the same pull request,
        # or like an official package (B-P7).
        reserved_prefixes = tuple(skeleton(p) for p in RESERVED_PREFIXES)
        reserved_names = {skeleton(n) for n in RESERVED_NAMES}
        for name in new_names:
            rel = f"index/{prefix(name)}/{name}"
            key = skeleton(name)
            if key.startswith(reserved_prefixes) or key in reserved_names:
                error(rel, f"`{name}` is reserved for official packages")
            for other in names:
                if other != name and skeleton(other) == key:
                    error(rel, f"`{name}` is too similar to the package `{other}`")
        diff = name_status(git("diff", "--name-status", "-z", f"{args.base}...HEAD"))
        touched = [p for _, *paths in diff for p in paths]
        # Publishing and owner pull requests change registry data only, never scripts or workflows.
        if any(p.startswith(REGISTRY_DATA) for p in touched):
            for p in touched:
                if not p.startswith(REGISTRY_DATA):
                    error(p, "a pull request that changes index/, packages/ or owners/ may not change other files")
        # The archive of a new version sits at the default path, which contains its own package name;
        # a relative `dl` could otherwise place it at the path of another package's next version.
        local_template = template if "://" not in template else DEFAULT_DL
        for name, entries in names.items():
            for e in entries:
                where = f"{name} {e['vers']}"
                target = archive_path(e, template)
                if where in changed and "://" not in target and target != archive_path(dict(e, dl=None), local_template):
                    error(where, INVALID_VALUE.format(key="dl", value=repr(target)) + "; new versions use the default archive path")
        check_ownership(args.base, author, diff, names, head_owners, archive_owner)
        for status, *paths in diff:
            for p in paths:
                # Paths such as `index/3/c/con` cannot be checked out on Windows (B-P7).
                if not all(portable(part) for part in p.split("/")):
                    error(p, "path cannot be checked out on Windows")
                if p == "config.json":
                    error(p, CONFIG_LOCKED)
                if p.startswith("packages/") and status[0] in "MDR":
                    error(p, "published archives cannot be modified or deleted")
                if p == "search.json":
                    error(p, "search.json is generated after merge; do not edit it in a pull request")

    if args.summary:
        lines = ["## Registry check", ""]
        if author is not None:
            lines += [f"Pull request author: {code(author['login'])} (id {author['id']})", ""]
        # Errors and file names carry text from the pull request: written as literal text (B-P8).
        lines += [f"- {code(e)}" for e in errors] or ["All checks passed."]
        for where in sorted(changed):
            lines += ["", f"### {code(where)}", ""] + [f"- {code(f)}" for f in new_files.get(where, [])]
        Path(args.summary).open("a", encoding="utf-8").write("\n".join(lines) + "\n")

    for e in errors:
        print(f"error: {shown(e)}")
    print(f"{len(names)} packages, {sum(len(v) for v in names.values())} versions, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
