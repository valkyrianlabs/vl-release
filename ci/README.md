# CI container

vl-release's CI and release jobs on the self-hosted runner run every step in a disposable,
rootless Podman container, so the runner needs no host sudo, host packages, deadsnakes PPA or
Docker. The conventions are shared with the other repositories on the runner; the full
description (mounts, privileges, caches, cleanup, the `ci-host` broker) is in
[payload-markdown's `ci/README.md`](https://github.com/valkyrianlabs/payload-markdown/blob/main/ci/README.md).

- `ci/run-ci` is copied **verbatim** from payload-markdown (it is repository-agnostic); update it
  there and copy it back.
- `ci/Containerfile`: Ubuntu 24.04 (pinned by digest), Python 3.14 from the deadsnakes PPA as
  `python`/`python3` (the system `/usr/bin/python3` stays 3.12 for dh-python package builds),
  build-essential, dpkg-dev, debhelper, dh-python, ruby, Node/npm (`tests/test_npm.py` runs
  `npm pack`), git and gh. `bin/` of the checkout is first on `PATH`, so CI always runs the
  vl-release under test.
- Workflows set `defaults.run.shell: bash ./ci/run-ci bash -euo pipefail {0}` on their Linux jobs,
  so each `run:` step executes in the container. Clean-install checks of the `.deb` use a stock
  image instead: `./ci/run-ci --image "$UBUNTU_IMAGE" --root bash -c 'apt-get install -y ./release/…'`.

Run the suite locally the way CI does (needs Podman):

```sh
./ci/run-ci python -m unittest discover -s tests -t . -v
```
