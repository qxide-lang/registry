<!--
CI (scripts/validate.py) checks the checksum and archive, and lists the
archive's files in the job summary for reviewers.
-->

- **Package & version:** `<name> <version>`
- **Source repository:** `<url>`

**Change type:**
- [ ] New version
- [ ] Yank

## Checklist

- [ ] I am the author or have permission to publish/yank this package.
- [ ] The archive and index line were produced by `qxide publish` (not edited by hand).
- [ ] `qxide publish --dry-run` / `qxide check` passed locally.
- [ ] The license in `qxide.toml` allows redistribution.
- [ ] The package does not impersonate another project and is not a name reservation without code.
- [ ] For yanks, the PR changes only the `yanked` field (`false` to `true`).