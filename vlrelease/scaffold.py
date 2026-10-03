"""`vlr init`: create the repository contract files that are missing (never overwrites)."""

from __future__ import annotations

import json
from pathlib import Path

from vlrelease import staging
from vlrelease.config import CONFIG_FILENAME

STARTER = """\
# vl-release repository contract: https://github.com/valkyrianlabs/vl-release
schema_version = 1

[tool]
requires = ">=0.1,<1"

[project]
name = "{name}"
package = "{package}"
# repository = "valkyrianlabs/{package}"

[version]
canonical = {canonical}
targets = [
  # {{ kind = "meson", path = "meson.build" }},
  # {{ kind = "package_json", path = "package.json" }},
  # {{ kind = "regex", path = "src/version.ts", pattern = 'VERSION = "(?P<version>[^"]+)"' }},
]

[release]
branch = "main"
# test_command = ["make", "test"]
{debian}"""

DEBIAN_STARTER = """
[debian]
distribution = "unstable"
urgency = "medium"

[[debian.packages]]
name = "{package}"
required_paths = []
forbidden_paths = []

# [publish.apt]
# repository_url = "https://apt.valkyrianlabs.com"
# suite = "stable"
# components = ["main"]
# architectures = ["amd64"]
"""


def scaffold(root: Path, *, name: str | None, package: str | None, debian: bool | None) -> list[str]:
    created: list[str] = []
    default_name = root.name
    package = package or (name or default_name).lower().replace(" ", "-")
    name = name or default_name
    if debian is None:
        debian = (root / "debian" / "control").is_file()

    config_path = root / CONFIG_FILENAME
    if not config_path.exists():
        if (root / "VERSION").is_file():
            canonical = '"VERSION"'
        elif (root / "package.json").is_file() and "version" in json.loads((root / "package.json").read_text(encoding="utf-8")):
            canonical = '{ kind = "package_json", path = "package.json" }'
        else:
            canonical = '"VERSION"'
            (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            created.append("VERSION")
        config_path.write_text(
            STARTER.format(
                name=name,
                package=package,
                canonical=canonical,
                debian=DEBIAN_STARTER.format(package=package) if debian else "",
            ),
            encoding="utf-8",
        )
        created.append(CONFIG_FILENAME)

    files = {".release/RELEASE_NOTES_NEXT.md": staging.RELEASE_NOTES_NEXT_TEMPLATE}
    if debian:
        files[".release/CHANGELOG_NEXT.md"] = staging.CHANGELOG_NEXT_TEMPLATE
    for relative, content in files.items():
        path = root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            created.append(relative)
    return created
