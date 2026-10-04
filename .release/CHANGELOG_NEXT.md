<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->

- publish-deb/verify-published: request dists/<suite>/InRelease (then Release)
  before every Packages poll, so lazily rebuilt indexes (Nexus apt-hosted)
  regenerate during verification; best effort, the Packages read decides.

