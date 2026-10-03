"""`vlr source-archive`: a reproducible source tarball of the *prepared* work tree.

GitHub's automatic tag archives are built from the tag commit, which predates `vlr prepare`, so
they never contain the promoted release documents. This archive is built from the filesystem
instead (tracked files as they exist now, plus the prepared documents), with normalized owners,
modes and timestamps, so re-running the same release produces byte-identical output.
"""

from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.gitutil import tracked_files
from vlrelease.state import read_release_state, release_datetime


def archive_name(config: Config, version: str) -> str:
    assert config.source_archive is not None
    return config.source_archive.name.format(package=config.project.package, version=version, tag=config.tag_for(version))


def archive_prefix(config: Config, version: str) -> str:
    return f"{config.project.package}-{version}"


def prepared_documents(config: Config) -> list[str]:
    paths = [config.release.notes, config.release.notes_next]
    if config.debian is not None:
        paths += [config.debian.changelog, config.release.changelog_next]
    return [path for path in paths if config.resolve(path).is_file()]


def _selected_files(config: Config) -> list[str]:
    assert config.source_archive is not None
    excluded_roots = {PurePosixPath(config.release.output_dir).parts[0], PurePosixPath(config.release.build_dir).parts[0]}
    selected = set(tracked_files(config.root)) | set(prepared_documents(config))
    result: list[str] = []
    for relative in sorted(selected):
        if PurePosixPath(relative).parts[0] in excluded_roots:
            continue
        if any(fnmatchcase(relative, pattern) or relative.startswith(pattern.rstrip("/") + "/") for pattern in config.source_archive.exclude):
            continue
        if not os.path.lexists(config.resolve(relative)):
            continue  # tracked but deleted in the work tree
        result.append(relative)
    return result


def build_source_archive(config: Config) -> Path:
    if config.source_archive is None:
        raise ReleaseError("[source_archive] is not enabled in release.toml")
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None:
        raise ReleaseError(
            f"Refusing to build the source archive: the work tree is not prepared (phase {state.phase}). "
            "Run `vlr prepare` first."
        )
    version = str(state.version)
    prefix = archive_prefix(config, version)
    mtime = int(release_datetime(config).timestamp())
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / archive_name(config, version)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        directory = tarfile.TarInfo(prefix)
        directory.type, directory.mode, directory.mtime = tarfile.DIRTYPE, 0o755, mtime
        archive.addfile(directory)
        for relative in _selected_files(config):
            path = config.resolve(relative)
            info = tarfile.TarInfo(f"{prefix}/{relative}")
            info.mtime, info.uid, info.gid, info.uname, info.gname = mtime, 0, 0, "", ""
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.readlink(path), 0o777
                archive.addfile(info)
            elif stat.S_ISREG(st.st_mode):
                info.mode = 0o755 if st.st_mode & 0o111 else 0o644
                data = path.read_bytes()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0, compresslevel=9) as gz:
        gz.write(buffer.getvalue())
    target.write_bytes(compressed.getvalue())
    return target
