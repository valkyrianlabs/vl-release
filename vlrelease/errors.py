"""Exit codes and the exception hierarchy every command maps onto them."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1  # a check failed or an operation could not complete
EXIT_USAGE = 2  # bad arguments, missing/invalid release.toml, incompatible tool version
EXIT_INTEGRITY = 3  # publication refused: it would contradict something already published


class ReleaseError(Exception):
    """An expected, user-facing failure. The message is printed without a traceback."""

    exit_code = EXIT_FAILURE


class ConfigError(ReleaseError):
    exit_code = EXIT_USAGE


class IntegrityError(ReleaseError):
    """Publishing would replace or contradict bytes that are already published."""

    exit_code = EXIT_INTEGRITY
