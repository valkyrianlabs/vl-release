<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Release-doc fragments: `.release/notes.d/*.md` and `.release/changelog.d/*.md` (`[release] notes_fragments`,
  `changelog_fragments`) are assembled with the `_NEXT` files by `state.load_staged` (used by status, check, cut and
  prepare). `prepare` deletes the promoted fragments (`PrepareResult.removed`), and `finalize` removes the fragments
  tracked at the release commit from the branch if unchanged there, keeping later ones.
  `parse_notes_next(allow_empty_body=)`, `parse_notes_fragment`, `assemble_notes`, `merge_changelog`;
  `README.md` and `_*`/`.*` files are ignored. Tests: `tests/test_fragments.py`.
