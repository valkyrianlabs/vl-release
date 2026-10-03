<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Initial release of vl-release (`vl-release`/`vlr`), the shared ValkyrianLabs
  release toolkit replacing the per-repository tools/release copies.
- release.toml (schema 1): Git-root discovery, strict validation, `--repo`,
  tool-version requirement; version targets: file, meson, package_json,
  pyproject, regex, homebrew.
- Agent-maintained staging (.release/CHANGELOG_NEXT.md,
  .release/RELEASE_NOTES_NEXT.md) and a deterministic, idempotent `vlr prepare`
  that renders debian/changelog stanzas and RELEASE_NOTES.md entries in the
  work tree only.
- Isolated Debian builds under build/deb (nothing is written outside the
  project), package-contract validation, SHA256SUMS and reproducible source
  archives built from the prepared work tree.
- Integrity-checked APT publication to Nexus (credentials never on argv,
  identical re-uploads skipped, different bytes for a published version
  refused) with index verification and polling.
- Idempotent GitHub releases, Homebrew formula rendering and tap publication,
  `vlr cut` and the post-publication `vlr finalize` backcommit.
- `vlr install-skill` installs the repository-local agent skill.
