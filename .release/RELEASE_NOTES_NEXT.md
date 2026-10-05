<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# npm packages, from build to npmjs.com

vl-release can now release npm packages with the same guarantees as Debian packages, so a JavaScript
repository gets version sync, staged release notes, GitHub releases and publication from one
`release.toml`.

- `[npm]` describes the package: where `package.json` lives, `npm` or `pnpm` packing, commands to
  run before packing (for example the build), and a tarball contract: paths the package must ship,
  paths it must never ship, and files that must be byte-identical to the work tree.
- `vlr build-npm` packs the package from the prepared work tree into the release directory. That
  tarball is checksummed, validated by `vlr validate-artifacts`, attached to the GitHub release
  and published unchanged.
- `[[publish.npm]]` lists the registries. npmjs.com works with npm trusted publishing
  (`auth = "oidc"`, no token secret); `token` and `basic` auth cover private registries.
- `vlr publish-npm` checks every registry before uploading anywhere: an identical version
  (proven by `dist.integrity`) is skipped, a different one is refused, and a version older than
  `latest` is refused unless `--allow-older-version` publishes it under a maintenance tag. It then
  waits until each registry lists the exact tarball. `vlr verify-npm` repeats the verification.
- `vlr check` fails when `package.json` is not a version target, is `private`, or sets
  `publishConfig.registry`.

Debian and APT publication are unchanged. Run `vlr install-skill` to update the agent skill.
