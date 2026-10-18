<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->

- Add [[npm.packages]]: further independently built npm packages (own package_dir, pre_pack,
  contract, dist-tags, access, aliases) released in the same version as [npm].
  - build-npm packs each in its own directory; validate-artifacts checks each against its contract.
  - publish-npm/verify-npm cover every package: all names planned before any upload; aliases, then
    extra packages, then the [npm] package.
  - check requires each package.json to be a version target and names to be unique across packages;
    status --json adds channels.npm_package_dirs.
