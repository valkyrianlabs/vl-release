#!/usr/bin/env bash
# Debian packages a job needs on the persistent self-hosted runner, in two separate steps:
#
#   apt-packages.sh refresh PACKAGE...   `apt-get update`, but only when a missing package has no
#                                        install candidate in the current package lists
#   apt-packages.sh install PACKAGE...   install the missing packages; never refreshes
#
# Installed packages cost nothing, and a slow upstream mirror is only waited on when the lists
# really are too old to install from.
set -euo pipefail

mode="${1:-}"
shift || true
if [ "$mode" != refresh ] && [ "$mode" != install ] || [ "$#" -eq 0 ]; then
  echo "usage: $0 refresh|install PACKAGE..." >&2
  exit 2
fi

missing=()
for package in "$@"; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing+=("$package")
done

if [ "${#missing[@]}" -eq 0 ]; then
  echo "already installed: $*"
  exit 0
fi

if [ "$mode" = install ]; then
  echo "installing: ${missing[*]}"
  sudo -n apt-get install -y --no-install-recommends "${missing[@]}"
  exit 0
fi

unknown=()
for package in "${missing[@]}"; do
  candidate="$(apt-cache policy "$package" 2>/dev/null | sed -n 's/^ *Candidate: //p')"
  [ -n "$candidate" ] && [ "$candidate" != "(none)" ] || unknown+=("$package")
done

if [ "${#unknown[@]}" -eq 0 ]; then
  echo "package lists already know ${missing[*]}; no refresh needed"
  exit 0
fi

echo "no install candidate for ${unknown[*]}; refreshing the package lists"
sudo -n apt-get update
