<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- build-npm: send build and pack output to stderr so --json stays parseable; pack with npm's
  update check, audit and funding notices off.
- Tests: run real npm calls offline with a private cache; the package build runs the suite with
  --durations 10.
- Require Python >= 3.12; CI tests Python 3.12 and 3.14.
