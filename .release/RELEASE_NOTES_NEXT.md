<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# The archive keyring is recommended, never required

- vl-release now **recommends** `valkyrianlabs-archive-keyring`, which keeps the repository's signing
  key current through normal upgrades (it owns `/usr/share/keyrings/valkyrianlabs.gpg` and links an
  unmodified `/etc/apt/keyrings/valkyrianlabs.gpg` to it). APT installs it by default when vl-release
  comes from the repository, and the `.deb` from a GitHub release still installs on its own.
- **`vlr validate-artifacts` refuses a `.deb` that Depends (or Pre-Depends) on
  `valkyrianlabs-archive-keyring`.** Such a package fails to install anywhere the repository is not
  configured, including the clean-install smoke test every release runs in a stock container. The
  message says what to do: make it a Recommends.

