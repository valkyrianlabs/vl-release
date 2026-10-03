"""`vlr doctor`: the toolchain and (when inside one) the repository, at a glance."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import vlrelease
from vlrelease.config import Config

MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "required": self.required}


def _tool(name: str, purpose: str, *, required: bool, version_args: tuple[str, ...] = ("--version",)) -> Check:
    path = shutil.which(name)
    if path is None:
        return Check(name, False, f"not found ({purpose})", required)
    try:
        output = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=10, check=False)
        first = (output.stdout or output.stderr).strip().splitlines()[:1]
        return Check(name, True, f"{first[0] if first else path} ({purpose})", required)
    except Exception:
        return Check(name, True, f"{path} ({purpose})", required)


def run_doctor(config: Config | None, *, strict: bool = False) -> list[Check]:
    checks = [
        Check(
            "python",
            sys.version_info[:2] >= MIN_PYTHON,
            f"{platform.python_version()} at {sys.executable} (need >= {'.'.join(map(str, MIN_PYTHON))})",
            True,
        ),
        Check("vl-release", True, f"{vlrelease.__version__} from {Path(vlrelease.__file__).parent}", True),
        _tool("git", "required", required=True),
    ]
    debian = config is not None and config.debian is not None
    checks.append(_tool("dpkg-buildpackage", "build-deb", required=strict and debian))
    checks.append(_tool("dpkg-deb", "validate-artifacts", required=strict and debian, version_args=("--version",)))
    checks.append(_tool("curl", "publish-deb uploads", required=strict and config is not None and config.apt is not None))
    checks.append(_tool("gh", "github-release", required=False))
    checks.append(_tool("ruby", "Homebrew formula syntax check", required=False, version_args=("-v",)))
    return checks
