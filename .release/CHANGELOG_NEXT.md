<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Add [[npm.aliases]]: publish the same npm build under more package names.
  - build-npm derives each alias tarball from the canonical tarball (package.json name and listed
    rewrite members only; deterministic gzip).
  - validate-artifacts re-derives every alias and fails on any member, mode or byte difference.
  - publish-npm plans every name on every registry before uploading, uploads aliases before the
    canonical package and skips identical re-uploads; verify-npm checks every name.
  - check rejects an alias equal to the package name or sharing its tarball file name.
