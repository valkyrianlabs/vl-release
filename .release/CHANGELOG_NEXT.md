<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- cut: refuse to tag or push a release commit that is more than the version bump. Every published commit (fresh,
  resumed by subject, resumed from a local tag) is compared with replace_version_text applied to its base. Other
  paths, renames, deletions, mode changes, extra edits in version files, an unchanged canonical version and
  unreadable diffs are refused with a recovery hint. A fresh cut checks the index before committing.
