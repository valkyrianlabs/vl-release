<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# No more stale-skill warnings after every release

`vlr check` used to report the agent skill and `PROJECT.md` as stale after every version bump or
vl-release upgrade, even when nothing in them had changed. Skills are now only stale when their
content actually differs. Run `vlr install-skill` and `vlr install-local-skill` once to switch
existing files to the new format.
