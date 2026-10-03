"""SHA256SUMS for the release output directory (coreutils `sha256sum` format).

Operators can check downloaded assets with `sha256sum -c SHA256SUMS --ignore-missing`.
Every regular top-level file in the output directory is covered except SHA256SUMS itself,
logs and dot-files; subdirectories (e.g. `meta/`) hold bookkeeping and are not release assets.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SHA256SUMS_NAME = "SHA256SUMS"
_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64}) [ *](?P<name>[^/\\]+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_assets(output_dir: Path) -> tuple[Path, ...]:
    if not output_dir.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and path.name != SHA256SUMS_NAME
            and not path.name.startswith(".")
            and not path.name.endswith(".log")
        )
    )


def render(entries: dict[str, str]) -> str:
    return "".join(f"{entries[name]}  {name}\n" for name in sorted(entries))


def write_sha256sums(output_dir: Path) -> Path:
    entries = {path.name: sha256_file(path) for path in release_assets(output_dir)}
    if not entries:
        raise ValueError(f"Cannot write {SHA256SUMS_NAME}: no release assets under {output_dir}")
    target = output_dir / SHA256SUMS_NAME
    target.write_text(render(entries), encoding="utf-8")
    return target


def read_sha256sums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        match = _LINE.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: malformed {SHA256SUMS_NAME} line: {raw!r}")
        if match.group("name") in entries:
            raise ValueError(f"{path}:{number}: duplicate entry for {match.group('name')}")
        entries[match.group("name")] = match.group("digest")
    return entries


def verify_sha256sums(output_dir: Path) -> list[str]:
    """Integrity problems; empty when every listed and every present asset matches."""
    sums = output_dir / SHA256SUMS_NAME
    if not sums.is_file():
        return [f"{SHA256SUMS_NAME} is missing under {output_dir} (run `vlr checksums`)"]
    try:
        entries = read_sha256sums(sums)
    except ValueError as exc:
        return [str(exc)]
    issues: list[str] = []
    for name, expected in sorted(entries.items()):
        candidate = output_dir / name
        if not candidate.is_file():
            issues.append(f"{SHA256SUMS_NAME} lists {name}, but the file is missing")
        elif (actual := sha256_file(candidate)) != expected:
            issues.append(f"{name}: sha256 {actual} does not match {SHA256SUMS_NAME} ({expected})")
    for asset in release_assets(output_dir):
        if asset.name not in entries:
            issues.append(f"{asset.name} is not listed in {SHA256SUMS_NAME}")
    return issues
