#!/usr/bin/env bash
# Install the named Debian packages only when they are missing, so jobs on the persistent
# self-hosted runner do not run `apt-get update` (or need the network) on every run.
# Usage: ensure-apt-packages.sh PACKAGE...
set -euo pipefail

missing=()
for package in "$@"; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing+=("$package")
done

if [ "${#missing[@]}" -eq 0 ]; then
  echo "already installed: $*"
  exit 0
fi

echo "installing: ${missing[*]}"
sudo -n apt-get update
sudo -n apt-get install -y --no-install-recommends "${missing[@]}"
