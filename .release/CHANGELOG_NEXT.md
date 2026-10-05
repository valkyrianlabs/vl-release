<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.
-->
- Add npm package releases: [npm] tarball contracts, `vlr build-npm`, `vlr publish-npm` and
  `vlr verify-npm` for [[publish.npm]] registries.
  - Idempotent, integrity-checked publication: every registry is planned before any upload; identical
    dist.integrity is skipped, different bytes are refused, dist-tags never move backwards.
  - npm trusted publishing (OIDC), token and basic auth; credentials only in a temporary 0600 userconfig.
- `vlr check` validates package.json for [npm]; `validate-artifacts`, `artifacts`, `status`,
  `doctor` and the agent skill know about npm packages.
