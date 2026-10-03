<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Generated skill files no longer embed the vl-release version, so version
  bumps and tool upgrades stop flagging an unchanged SKILL.md/PROJECT.md as
  stale; files with the old versioned marker are still updated in place.
