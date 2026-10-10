<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Upstream versions with packaging revisions

Repositories that package someone else's software can now version their releases the way Debian
does: the upstream release plus their own packaging revision.

```toml
[version]
canonical = "VERSION"        # 8.0.2-1
policy = "debian-upstream"
```

- `8.0.2-1` is upstream 8.0.2, first packaging revision. The same string is the tag (`v8.0.2-1`),
  the release-notes entry, the GitHub release title and the Debian package version.
- `vlr version bump revision` / `vlr cut revision` make a packaging-only release (`8.0.2-2`);
  `vlr version upstream 8.0.3` adopts a new upstream release and restarts the revision at 1
  (`8.0.3-1`). `patch|minor|major` are refused under this policy because they are ambiguous.
- Versions are ordered like `dpkg --compare-versions` (`8.0.2-10` is newer than `8.0.2-9`), never
  by SemVer precedence. `vlr check`, `vlr cut` and `vlr prepare` refuse releases that go
  backwards or change upstream without restarting the revision.

**Nothing changes for existing repositories.** SemVer stays the default, and repositories
without `policy` behave exactly as with 0.4.0: same versions, tags, changelogs, artifacts and
command output. `vlr help config` has the details. Repositories that adopt the policy should
require `vl-release >= 0.5`.
