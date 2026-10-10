<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Add [version] policy: "semver" (default, unchanged) or "debian-upstream" for repositories
  that package upstream software. Versions are UPSTREAM-REVISION (8.0.2-1), ordered like dpkg,
  and used unchanged as tag, release-notes entry and Debian package version.
  - New `vlr version bump revision` and `vlr version upstream X.Y.Z` (revision restarts at 1);
    `vlr cut revision`. patch|minor|major are refused under debian-upstream.
  - check, cut and prepare refuse releases that go backwards or change upstream without
    restarting the revision at 1.
  - status --json and --github-output add version_policy, upstream_version and
    packaging_revision for debian-upstream repositories.
  - Not combinable with [npm], [homebrew], debian.revision or package_json/pyproject targets.
- Debian version comparison moved to vlrelease.debversion (apt_index re-exports it).
