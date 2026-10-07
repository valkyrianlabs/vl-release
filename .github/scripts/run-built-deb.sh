#!/usr/bin/env bash
# Run the freshly built vl-release .deb from an extracted copy of its installed layout, without
# installing it: on the persistent self-hosted runner, installing a CI build would replace the
# host's released vl-release.
# Usage: run-built-deb.sh COMMAND [ARGS...]   (COMMAND: vl-release or vlr)
set -euo pipefail

root="${RUNNER_TEMP:-$(mktemp -d)}/vl-release-deb"
if [ ! -x "$root/usr/bin/vlr" ]; then
  rm -rf "$root"
  mkdir -p "$root"
  dpkg-deb -x "$(ls "${GITHUB_WORKSPACE:-.}"/release/vl-release_*_all.deb)" "$root"
fi

command="$1"
shift
PYTHONPATH="$root/usr/lib/python3/dist-packages" exec /usr/bin/python3 "$root/usr/bin/$command" "$@"
