<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Package builds are fast again

The Debian package build runs the test suite, and since 0.2.0 that took about four minutes on
GitHub runners instead of about half a minute. 0.2.1 blamed npm; the test durations it added to
the build log showed the real cost: the Debian build tests, which run a nested
`dpkg-buildpackage` and take minutes inside the package build. Those tests are now skipped there
(the regular test runs, including CI, still run them), so building the package is quick again.
