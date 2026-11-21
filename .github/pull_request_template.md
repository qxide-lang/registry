<!--
CI (scripts/validate.py) checks the checksum and archive, checks that the
account opening this pull request owns the package (owners/<prefix>/<name>),
and lists the archive's files in the job summary for reviewers.
-->

- **Package & version:** `<name> <version>`
- **Source repository:** `<url>`

Owners yank a version by changing `"yanked": false` to `true` on its index line in a pull request; to restore a yanked version, open an issue.

## Checklist

- [ ] I am the author or have permission to publish this package.
- [ ] The archive and index line were produced by `qxide publish` (not edited by hand).
- [ ] New package: this pull request adds `owners/<prefix>/<name>` listing only my account. `qxide publish` after 0.7.0 writes it; with 0.7.0 or earlier, add the line `{"id":<id from gh api user --jq .id>,"login":"<login>"}` yourself. Existing package: my account is an owner (not only invited) in its owner file on `main`.
- [ ] `qxide publish --dry-run` / `qxide check` passed locally.
- [ ] The license in `qxide.toml` allows redistribution.
- [ ] The package does not impersonate another project and is not a name reservation without code.
