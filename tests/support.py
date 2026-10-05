"""Shared fixtures: throwaway Git repositories, CLI invocation and fake .deb packages."""

from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from vlrelease import staging
from vlrelease.cli import main
from vlrelease.config import load_config

SOURCE_ROOT = Path(__file__).resolve().parent.parent

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_DATE": "2026-10-01T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-10-01T12:00:00+00:00",
}
os.environ.update(GIT_ENV)
# Real `npm pack` calls in the tests must not depend on the network, the user's npm cache or
# config, or npm's update check: Debian package builds run this suite in a sandboxed HOME.
_NPM_CACHE = tempfile.mkdtemp(prefix="vlr-test-npm-cache-")
atexit.register(shutil.rmtree, _NPM_CACHE, True)
os.environ.update(
    NPM_CONFIG_CACHE=_NPM_CACHE,
    NPM_CONFIG_OFFLINE="true",
    NPM_CONFIG_UPDATE_NOTIFIER="false",
    NPM_CONFIG_AUDIT="false",
    NPM_CONFIG_FUND="false",
)
os.environ.pop("SOURCE_DATE_EPOCH", None)
for _name in ("RELEASE_PUBLISH_MODE", "NEXUS_REPO_URL", "NEXUS_APT_REPO", "NEXUS_USER", "NEXUS_PASS", "NEXUS_PASSWORD", "RELEASE_APT_REPOSITORY_URL",
              "RELEASE_DEBIAN_DISTRIBUTION", "RELEASE_DEBIAN_URGENCY", "DEBFULLNAME", "DEBEMAIL", "GITHUB_OUTPUT",
              "HOMEBREW_TAP_BRANCH"):
    os.environ.pop(_name, None)

BASIC_CONFIG = """\
schema_version = 1

[project]
name = "Demo"
package = "demo"

[version]
canonical = "VERSION"
"""

DEBIAN_CONFIG = """\
schema_version = 1

[project]
name = "Demo"
package = "demo"
repository = "example/demo"

[version]
canonical = "VERSION"
targets = [{ kind = "package_json", path = "package.json" }]

[debian]
maintainer = "Demo Maintainer <demo@example.com>"

[[debian.packages]]
name = "demo"
architecture = "all"
required_paths = ["usr/bin/demo"]
forbidden_paths = ["*.pyc"]
identical_files = [{ member = "usr/share/doc/demo/RELEASE_NOTES.md", source = "RELEASE_NOTES.md" }]
"""

STAGED_CHANGELOG = "- Add the frobnicator.\n- Fix a crash when the widget is empty.\n"
STAGED_NOTES = "# Frobnication arrives\n\nYou can now frobnicate.\n\n## Upgrading\n\nNothing to do.\n"

DEBIAN_CONTROL = """\
Source: demo
Section: utils
Priority: optional
Maintainer: Demo Maintainer <demo@example.com>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.7.0
Rules-Requires-Root: no

Package: demo
Architecture: all
Depends: ${misc:Depends}
Description: demo package
 A tiny package used by the vl-release tests.
"""

DEBIAN_RULES = """\
#!/usr/bin/make -f
%:
\tdh $@

override_dh_compress:
\tdh_compress -X.md
"""


def git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


class RepoTestCase(unittest.TestCase):
    """Each test gets a temporary directory; `make_repo` creates a committed Git repository in it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="vlr-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_repo(
        self,
        files: dict[str, str] | None = None,
        *,
        config: str = BASIC_CONFIG,
        name: str = "repo",
        version: str = "1.0.0",
        staged: bool = True,
        debian: bool = False,
        commit: bool = True,
    ) -> Path:
        root = self.tmp / name
        suffix = 1
        while root.exists():
            suffix += 1
            root = self.tmp / f"{name}{suffix}"
        root.mkdir(parents=True)
        git(root, "init", "-q", "-b", "main")
        content: dict[str, str] = {
            "release.toml": config,
            "VERSION": f"{version}\n",
            ".release/RELEASE_NOTES_NEXT.md": staging.RELEASE_NOTES_NEXT_TEMPLATE + (STAGED_NOTES if staged else ""),
        }
        if debian:
            content.update(
                {
                    ".release/CHANGELOG_NEXT.md": staging.CHANGELOG_NEXT_TEMPLATE + (STAGED_CHANGELOG if staged else ""),
                    "package.json": '{\n  "name": "demo",\n  "version": "%s",\n  "private": true\n}\n' % version,
                    "debian/control": DEBIAN_CONTROL,
                    "debian/rules": DEBIAN_RULES,
                    "debian/source/format": "3.0 (native)\n",
                    "debian/demo.install": "demo usr/bin/\n",
                    "debian/demo.docs": "RELEASE_NOTES.md\n",
                    "demo": "#!/bin/sh\necho demo\n",
                }
            )
        content.update(files or {})
        for relative, text in content.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if debian:
            (root / "debian/rules").chmod(0o755)
            (root / "demo").chmod(0o755)
        if commit:
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "initial")
        return root

    def config(self, root: Path):
        return load_config(root)

    def write(self, root: Path, relative: str, text: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text) if text.startswith("\n") else text, encoding="utf-8")

    def read(self, root: Path, relative: str) -> str:
        return (root / relative).read_text(encoding="utf-8")

    def vlr(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            if cwd is not None:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(args))
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()


def requires_tools(*tools: str):
    missing = [tool for tool in tools if shutil.which(tool) is None]
    return unittest.skipIf(bool(missing), f"requires {', '.join(missing)}")


def build_fake_deb(directory: Path, *, package: str, version: str, files: dict[str, bytes], arch: str = "all") -> Path:
    """Build a real .deb with dpkg-deb from an in-memory file map."""
    stage = directory / f"stage-{package}-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "DEBIAN").mkdir(parents=True)
    (stage / "DEBIAN" / "control").write_text(
        f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: T <t@example.com>\n"
        "Description: test\n test package\n",
        encoding="utf-8",
    )
    for relative, data in files.items():
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    target = directory / f"{package}_{version}_{arch}.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)], check=True, capture_output=True)
    shutil.rmtree(stage)
    return target
