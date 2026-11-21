# qxide-lang/registry

Package index for [Qxide](https://qxide.dev) ([qxide-lang/qxide](https://github.com/qxide-lang/qxide)), a programming language for classical and quantum computers (`.qx` files) with compile-time qubit safety.

This registry is a git-backed index, modeled on early Cargo and Homebrew. There is no server and there are no registry accounts (package owners are GitHub accounts); publishing a package version is a pull request that maintainers review and merge. The design is documented in `docs/design/qx-packages.md` in the [qxide](https://github.com/qxide-lang/qxide) repository.

Clients read the registry from `https://raw.githubusercontent.com/qxide-lang/registry/main/` by default. Override this with `--registry` or the `QXIDE_REGISTRY` environment variable.

## Publishing a package

### Package requirements

- **Name**: lowercase letters, digits, and single hyphens; must start with a letter; at most 64 characters; no underscores. Packages are imported in `.qx` files using underscores (for example, `import bell_utils.bell_pair;`).
- **Manifest (`qxide.toml`)**: must specify `description`, `license` (SPDX expression), and `qxide` (language version requirement, such as `>=0.6`).
- **Dependencies**: every dependency must exist in this registry and declare a version requirement. Path-only dependencies are not allowed.
- **Type and qubit checks**: `src/lib.qx` must pass `qxide check`.
- **Archive limits**: at most 10 MiB compressed, at most 2000 files, and at most 50 MiB unpacked.

### Archive format

Package archives are gzip-compressed tar files (`.tar.gz`) containing all files under `<name>-<version>/`:

- `qxide.toml`
- `src/**/*.qx` (at least `src/lib.qx`)
- Optional top-level `README` and `LICENSE` files

`qxide publish` builds archives deterministically so identical source files produce the same sha256 checksum.

### Workflow

1. From the package directory, inspect the files, checksum, and generated index line:
   ```sh
   qxide publish --dry-run
   ```
2. Publish the package:
   ```sh
   qxide publish
   ```
   - **With GitHub CLI (`gh`) installed and logged in**: `qxide publish` forks this repository, adds the archive and index line (and, for a new package, the owner file listing your account) on branch `publish/<name>-<version>`, and opens a pull request.
   - **Without `gh`**: `qxide publish` prints the files to add. Add them to your fork—or run `qxide publish --registry <path to your clone>` to write the index line and archive into your local clone, then add the owner file of a new package yourself—and open the pull request manually.

## Package owners

Each package has an owner file, `owners/<prefix>/<name>`, with one line per GitHub account that may publish new versions of it and change its owners:

```json
{"id":295735711,"login":"rigelx2b"}
```

CI compares only `id`, the numeric GitHub user id (`gh api users/<login> --jq .id` prints it). `login` is for readers: logins can be renamed and then registered by someone else, so a login grants nothing. A line with `"pending": true` is an invitation, not an owner. A file has 1 to 32 lines and at least one owner.

- **New package**: the pull request that adds the first version also adds the owner file, listing exactly the account that opened the pull request. Names are first come, first served.
  - `qxide publish` after 0.7.0 writes this file for you.
  - With `qxide` 0.7.0 or earlier, add it yourself before opening the pull request: create `owners/<prefix>/<name>` (same `<prefix>` as the index file) containing the single line `{"id":<your id>,"login":"<your login>"}`, where `gh api user --jq .id` prints your id.
- **New version or yank**: the pull request must be opened by an owner listed in the owner file on `main`.
- **Adding an owner** takes two pull requests. An owner adds the account as an invitation, `{"id":<id>,"login":"<login>","pending":true}`. Once that is merged, the invited account opens its own pull request that removes `"pending":true` from its line and changes nothing else. Nobody can add an owner without the account's own pull request.
- **Removing owners or transferring**: an owner can remove owners and withdraw invitations. To transfer a package, invite the new owner, let them accept, then remove the old line. The file cannot be left without an owner or deleted.
- **Packages without an owner file** (published before owner files existed) are locked until a maintainer adds one.
- Lost accounts and disputes: open an issue; maintainers change owner files in a commit to `main`.

CI takes the pull request author from the event GitHub sends for the pull request (`pull_request.user`), never from commits or text in the pull request. Open pull requests only from branches that nobody else can push to: CI accepts every commit in a pull request opened by an owner.

## Yanking a version

An owner yanks a version with a pull request that changes `"yanked": false` to `"yanked": true` on that version's index line and nothing else in it. CI rejects yanks from other accounts and any pull request that sets `yanked` back to false. Maintainers can also yank in a commit to `main` (for abuse or legal requests, or when the owner's account is lost), and only maintainers restore a yanked version.

- Yanked versions are not selected for new dependency resolutions.
- Projects whose `qxide.lock` already pins a yanked version continue to work.
- Published versions are never deleted or replaced; publish a new version instead.

## Immutability and integrity

- Published archives and index lines are immutable; owners can set `yanked` to true, and only maintainers set it back.
- `qxide` verifies the sha256 digest of every archive against `qxide.lock` before unpacking and refuses to download if the index checksum differs from the lockfile.
- The same index and archive files can be served by a dedicated registry server at different URLs in the future; updating `dl` in `config.json` moves archive downloads without rewriting index lines.

## Repository layout

| Path | Description |
| --- | --- |
| `config.json` | Registry configuration: `{"dl": "packages/{prefix}/{name}/{name}-{version}.tar.gz", "api": null}`. `dl` is the download URL template relative to the repository root; `api` is reserved for a future publish API. |
| `index/<prefix>/<name>` | Package metadata, one JSON object per line, one line per published version in publication order. |
| `packages/<prefix>/<name>/<name>-<version>.tar.gz` | Published package archives. |
| `owners/<prefix>/<name>` | Package owners, one `{"id", "login"}` JSON object per line (see Package owners). |
| `search.json` | Search index generated by CI after each merge for `qxide search`. Never edit by hand. |
| `scripts/validate.py` | Pull request validation script. |
| `scripts/build_search.py` | Generator for `search.json`. |
| `.github/workflows/validate.yml` | CI workflow that validates pull requests. |
| `.github/workflows/search.yml` | CI workflow that rebuilds `search.json` after merge. |
| `.github/pull_request_template.md` | Pull request template. |

### Prefix rules

`<prefix>` is derived from the package name length:

- 1-letter names: `1`
- 2-letter names: `2`
- 3-letter names: `3/<first letter>`
- 4 or more letters: `<first two letters>/<next two letters>` (for example, `index/be/ll/bell-utils`)

### Index line format

Example line:

```json
{"cksum":"d18afc9d9e5b2362b5ddf0db2007096f3556d38f3efe96d02b055e9718f7fc90","deps":[],"description":"Bell-pair helpers for Qxide","name":"bell-utils","qxide":">=0.6","v":1,"vers":"0.1.0","yanked":false}
```

| Field | Required | Description |
| --- | --- | --- |
| `name` | Yes | Package name. |
| `vers` | Yes | Semver version string without build metadata (no `+...` suffix). |
| `deps` | Yes | List of dependency objects `{name, req}`. |
| `cksum` | Yes | Hex-encoded sha256 checksum of the archive. |
| `qxide` | No | Qxide language version requirement. |
| `description` | No | Short package description, without control characters. |
| `dl` | No | Download URL override. The archive must still be added to this repository at its default path, where CI checks it. |
| `yanked` | Yes | Boolean indicating whether the version is yanked. |
| `v` | Yes | Schema version (`1`). |

## Validation and maintainer review

### Automated checks

Every pull request runs `scripts/validate.py` from the target branch (the workflow uses `pull_request_target`, so a pull request cannot change its own check; its files are read as data and never executed). The script checks:

- JSON syntax, required fields, and qxide client compatibility for every index line (`v` is the integer `1`; no build metadata in `vers`; versions, dependency requirements, and `qxide` checked with the same rules as the client's semver parser)
- Correct `index/<prefix>/<name>` path; the name is not a Windows device name such as `con`, `nul`, or `com1`
- No duplicate versions, compared by semver precedence
- Expanded download locations (`dl` and the `config.json` template) are `https://` URLs or plain paths inside the repository
- Archive exists in this repository (also when `dl` is an external URL), its sha256 matches `cksum`, it is a single gzip member in the plain tar form that `qxide publish` writes (no PAX or other extension headers, at most 66 MiB decompressed), it contains only regular files under `<name>-<version>/` with safe, portable paths (no trailing dots or spaces, no Windows device names such as `CON` or `NUL`, no characters Windows rejects) and no two entries differing only in letter case, and it respects size and file-count limits
- `qxide.toml` inside the archive matches `name`, `vers`, and `deps` and defines `description`, `license`, and `qxide`
- `src/lib.qx` is present in the archive
- All declared dependencies exist in the registry and a package does not depend on itself
- The pull request author owns every package whose index file, archives, or owner file the pull request changes, according to the owner files on `main`; a new package adds an owner file that lists exactly the author
- Owner files are well formed and belong to a package with an index file
- Every file added under `packages/` is the archive of a version in the index, and new versions keep their archive at the default `packages/<prefix>/<name>/` path
- A pull request that changes `index/`, `packages/`, or `owners/` changes no other files
- Existing published index lines are unchanged, except that an owner may change `yanked` from false to true
- No published version or index file is removed
- Every changed path can be checked out on Windows
- `description` contains no control characters
- No existing archive is modified or deleted
- `search.json` is untouched
- `config.json` is untouched
- New package names do not look like another package name, existing or added in the same pull request: names are compared after removing hyphens, mapping `0`→`o`, `1`→`l`, `2`→`z`, `3`→`e`, `4`→`a`, `5`→`s`, `6`→`b`, `7`→`t`, `8`→`b`, `9`→`g`, `i`→`l`, and then `rn`→`m`, `vv`→`w`, `cl`→`d` (look-alike prevention)
- New package names do not start like `qxide` or equal `std` after the same mapping (reserved for official packages)
- Invalid input (malformed or deeply nested JSON or TOML, numbers too long to be versions) is reported as an error rather than stopping the check

The workflow job summary lists the files included in each new version for reviewers. File names and error messages are shown as literal text: in the summary each is one code span, and in both the summary and the job log non-printable characters appear as `\u{XXXX}` escapes.

### Review policy

Maintainers review pull requests according to the following rules:

1. One package version per pull request is preferred.
2. CI checks that the pull request author owns the package; for a new package, the author should also be the package author or have permission from them.
3. Read the source files listed in the job summary before merging.
4. Reject names that impersonate other projects, organizations, or existing packages, or that reserve names without code (name squatting).
5. Reject code that is malicious, deceptive, or misrepresents what it does.
6. Require an OSI-approved or otherwise clearly redistributable license.
7. Otherwise, package names are granted first-come, first-served.
8. Owners yank their own versions; maintainers may yank or restore versions or change owner files in cases of abuse, lost accounts, or legal requests.

## License

This repository's own files (scripts and workflows) are licensed under `MIT OR Apache-2.0`, the same as Qxide. Each published package retains its own license as declared in its `qxide.toml`.