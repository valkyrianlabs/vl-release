<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Release commits must be pure version bumps

`vlr cut` now publishes a release commit only when it is exactly the version bump `vlr version` makes: the canonical
version file and the configured version targets, each changed by nothing but the version substitution. Resuming an
interrupted cut used to push whatever commit was at HEAD, so a release commit amended with other changes (or a
hand-tagged commit) shipped under the release subject without its own review or CI. Now `vlr cut` refuses before
tagging or pushing and says what else the commit changes and how to split it out.

- The check is exact, not a pattern match: each version file must equal its previous content with the version
  replaced, other paths, renames, deletions and mode changes are refused, and the canonical version must change.
- It fails closed: a version file that can't be read or parsed refuses the release.
- Nothing is left half done. A refused resume creates no tag and pushes nothing. A fresh cut checks its staged bump
  before committing.
- Pure version bumps release exactly as before, fresh or resumed.
