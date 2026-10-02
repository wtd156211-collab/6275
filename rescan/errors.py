"""Exceptions raised by the rescan package."""


class RescanError(Exception):
    """Base class for every expected rescan failure.

    The command line interface turns these into a non-zero exit code and
    never writes partial outputs.
    """


class SnapshotError(RescanError):
    """A snapshot file is missing, truncated, corrupt or has the wrong format."""


class TreeError(RescanError):
    """The scanned directory cannot be read."""


class OutputError(RescanError):
    """An output location is invalid or cannot be written atomically."""
