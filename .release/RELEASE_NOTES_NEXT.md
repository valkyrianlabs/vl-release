<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->

# Publication verification no longer waits on a lazily rebuilt APT index

`vlr publish-deb` and `vlr verify-published` now request the suite's `InRelease` (falling back to `Release`) before
each read of the `Packages` indexes. Repository managers that rebuild their `dists/` metadata only when a client asks
for the Release files, such as Sonatype Nexus apt-hosted repositories, regenerate the index right away instead of
leaving a successful upload unlisted until some other client runs `apt-get update`. Previously a release could publish
its package and still fail verification after `verify_timeout`.

