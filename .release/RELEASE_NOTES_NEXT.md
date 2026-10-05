<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Quieter npm builds, Python 3.12

- `vlr build-npm --json` prints clean JSON again: the output of the build commands and of
  `npm pack` now goes to stderr.
- `vlr build-npm` packs with npm's update check, audit and funding notices turned off.
- The Debian package build no longer lets the test suite's `npm pack` calls touch the network or
  the npm cache. In v0.2.0 the package build took over three minutes on GitHub runners instead of
  about half a minute. The build log now lists the slowest tests.
- vl-release now requires Python 3.12 or newer (Ubuntu 24.04, Debian 13 and current Homebrew
  all ship newer versions).
