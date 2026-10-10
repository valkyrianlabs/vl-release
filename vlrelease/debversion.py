"""Debian version ordering (deb-version(5)), exactly as `dpkg --compare-versions` orders versions.

Shared by APT publication (is a newer version already published?) and the `debian-upstream`
version policy (release ordering). Pure Python: no dpkg needed at run time.
"""

from __future__ import annotations


def compare_debian_versions(left: str, right: str) -> int:
    """Return <0, 0, >0 like `dpkg --compare-versions` (epoch, upstream, revision; `~` sorts first)."""
    l_epoch, l_upstream, l_revision = _split_debian_version(left)
    r_epoch, r_upstream, r_revision = _split_debian_version(right)
    if l_epoch != r_epoch:
        return -1 if l_epoch < r_epoch else 1
    result = _compare_fragment(l_upstream, r_upstream)
    if result:
        return result
    return _compare_fragment(l_revision, r_revision)


def _split_debian_version(version: str) -> tuple[int, str, str]:
    raw = version.strip()
    epoch = 0
    if ":" in raw:
        epoch_raw, raw = raw.split(":", 1)
        epoch = int(epoch_raw) if epoch_raw.isdigit() else 0
    revision = "0"
    if "-" in raw:
        raw, revision = raw.rsplit("-", 1)
    return epoch, raw, revision


def _char_order(char: str) -> int:
    if char == "~":
        return -1
    if char.isalpha():
        return ord(char)
    return ord(char) + 256


def _compare_fragment(left: str, right: str) -> int:
    i = j = 0
    while i < len(left) or j < len(right):
        first_diff = 0
        while (i < len(left) and not left[i].isdigit()) or (j < len(right) and not right[j].isdigit()):
            lc = _char_order(left[i]) if i < len(left) and not left[i].isdigit() else 0
            rc = _char_order(right[j]) if j < len(right) and not right[j].isdigit() else 0
            if lc != rc:
                return -1 if lc < rc else 1
            i += 1
            j += 1
        while i < len(left) and left[i] == "0":
            i += 1
        while j < len(right) and right[j] == "0":
            j += 1
        while i < len(left) and left[i].isdigit() and j < len(right) and right[j].isdigit():
            if not first_diff:
                first_diff = ord(left[i]) - ord(right[j])
            i += 1
            j += 1
        if i < len(left) and left[i].isdigit():
            return 1
        if j < len(right) and right[j].isdigit():
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0
