#!/usr/bin/env bash
# Install the named Debian packages only when they are missing, so jobs on the persistent
# self-hosted runner do not touch apt on every run. The install is tried against the existing
# package lists first; `apt-get update` (slow when an upstream mirror is) runs at most once, only
# when that fails.
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
if ! sudo -n apt-get install -y --no-install-recommends "${missing[@]}"; then
  sudo -n apt-get update
  sudo -n apt-get install -y --no-install-recommends "${missing[@]}"
fi
