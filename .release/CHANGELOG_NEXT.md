<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- `vlr install-skill` installs the generic base skill and no longer requires
  release.toml; the skill now covers adopting vl-release (`vlr init`, filling
  in release.toml, asking before choosing publication targets).
- New `vlr install-local-skill` writes PROJECT.md next to the installed skill
  with the repository's specifics rendered from release.toml; `vlr check`
  warns when the skill or PROJECT.md is missing or stale.
- New `vlr help config|staging|ci`: the release.toml, staging-format and CI
  references ship inside the package and work offline.
- `vlr cut X.Y.Z` releases the version already in the version file when it
  was never tagged or recorded, instead of refusing (first releases).
