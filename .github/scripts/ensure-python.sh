#!/usr/bin/env bash
# Make Python >= VERSION (default 3.14) the `python`/`python3` of this job on the self-hosted
# runner: reuse an installed interpreter when one is new enough, otherwise install
# python<VERSION> from the deadsnakes PPA (Ubuntu). Prepends a shim directory to GITHUB_PATH.
# Usage: ensure-python.sh [VERSION]
set -euo pipefail

want="${1:-3.14}"

new_enough() {
  "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= tuple(map(int, '$want'.split('.'))) else 1)" 2>/dev/null
}

python=""
for candidate in "python$want" python3; do
  path="$(command -v "$candidate" 2>/dev/null || true)"
  if [ -n "$path" ] && new_enough "$path"; then
    python="$path"
    break
  fi
done

if [ -z "$python" ]; then
  echo "Python >= $want not found; installing python$want from ppa:deadsnakes/ppa"
  if ! grep -rqs deadsnakes /etc/apt/sources.list.d/; then
    sudo -n apt-get update
    sudo -n apt-get install -y --no-install-recommends software-properties-common
    sudo -n add-apt-repository -y ppa:deadsnakes/ppa
  fi
  sudo -n apt-get update
  sudo -n apt-get install -y --no-install-recommends "python$want" "python$want-venv"
  python="$(command -v "python$want")"
  new_enough "$python" || { echo "installed $python is still older than $want" >&2; exit 1; }
fi

shims="${RUNNER_TEMP:-$(mktemp -d)}/python-shims"
mkdir -p "$shims"
ln -sf "$python" "$shims/python"
ln -sf "$python" "$shims/python3"
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$shims" >> "$GITHUB_PATH"
fi
"$python" --version
