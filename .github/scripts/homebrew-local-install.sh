#!/usr/bin/env bash
# Install the release formula from a throwaway local tap, pointing it at the locally built
# source archive (file://), then run `brew test`. Proves the formula installs and works before
# it ever reaches valkyrianlabs/homebrew-tap. Expects `vlr` on PATH and a prepared-able tree.
set -euo pipefail

vlr prepare
vlr source-archive
formula="$(vlr homebrew formula --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')"
version="$(vlr version show)"
archive="$PWD/release/vl-release-${version}.tar.gz"

tap="vlr-ci/local"
brew untap "$tap" >/dev/null 2>&1 || true
brew tap-new --no-git "$tap"
tap_formula="$(brew --repository "$tap")/Formula/vl-release.rb"

# Style-check the exact formula that will be published, as a tap formula (a bare file path would
# be checked against Homebrew's own core-code rules instead).
cp "$formula" "$tap_formula"
brew style "$tap/vl-release"

# Then install it from the locally built archive and run its test block.
sed -E "s#^  url \".*\"#  url \"file://${archive}\"#" "$formula" > "$tap_formula"

export HOMEBREW_NO_INSTALL_FROM_API=1 HOMEBREW_NO_AUTO_UPDATE=1
brew install --build-from-source "$tap/vl-release"
brew test "$tap/vl-release"
"$(brew --prefix)/bin/vlr" --version
brew uninstall --formula "$tap/vl-release"
brew untap "$tap"
