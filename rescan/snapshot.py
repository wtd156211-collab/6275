"""On-disk snapshot format (version 1).

Layout, UTF-8 / LF / trailing newline, every field decimal unless noted::

    #rescan-snapshot v1
    <n>
    <path>\t<size>\t<mtime_ns>\t<sha256>   (n lines)
    <digest>

The trailing digest is the SHA-256 over the raw bytes of the n entry lines
*including* their ``\\n`` terminators.  Any deviation makes the snapshot
invalid; callers must never treat a half-read snapshot as a baseline.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from dataclasses import dataclass

from .errors import OutputError, SnapshotError

VERSION = 1
HEADER = "#rescan-snapshot v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NONNEG_RE = re.compile(r"0|[1-9][0-9]*")
_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
# Characters that cannot appear in a stored relative path: windows
# separators and control characters (which would make the line format
# ambiguous).  "/" is the separator and is checked structurally below.
_FORBIDDEN_PATH_CHARS = set("\\\t\n\r\x00")


@dataclass(frozen=True, slots=True)
class Entry:
    """One snapshot row: byte size, mtime in nanoseconds, content digest."""

    size: int
    mtime_ns: int
    sha256: str


class Snapshot:
    """An immutable baseline: ``version`` plus ordered path -> Entry rows."""

    __slots__ = ("version", "entries")

    def __init__(self, entries, version=VERSION):
        paths = list(entries)
        if paths != sorted(paths):
            raise ValueError("snapshot entries must be sorted by path")
        if len(set(paths)) != len(paths):
            raise ValueError("snapshot entries must be unique by path")
        self.version = version
        self.entries = dict(entries)

    def __eq__(self, other):
        return (
            isinstance(other, Snapshot)
            and self.version == other.version
            and self.entries == other.entries
        )

    def __repr__(self):
        return f"Snapshot(version={self.version!r}, n={len(self.entries)})"


def _valid_path(path):
    if not path:
        return False
    if any(ch in _FORBIDDEN_PATH_CHARS or ord(ch) < 0x20 for ch in path):
        return False
    if path.startswith("/") or path.endswith("/") or "//" in path:
        return False
    if path == "." or path.startswith("./") or "/./" in path or path.endswith("/."):
        return False
    if path == ".." or path.startswith("../") or "/../" in path or path.endswith("/.."):
        return False
    return True


def _iter_entry_lines(entries):
    for path in sorted(entries):
        entry = entries[path]
        if not _valid_path(path):
            raise SnapshotError(f"unsupported characters in path: {path!r}")
        yield f"{path}\t{entry.size}\t{entry.mtime_ns}\t{entry.sha256}\n".encode("utf-8")


def encode(snapshot):
    """Serialize a Snapshot to the exact on-disk bytes."""

    body = b"".join(_iter_entry_lines(snapshot.entries))
    digest = hashlib.sha256(body).hexdigest()
    header = f"{HEADER}\n{len(snapshot.entries)}\n".encode("utf-8")
    return header + body + digest.encode("ascii") + b"\n"


def decode(data):
    """Parse and fully validate snapshot bytes.

    Raises SnapshotError for any malformed, truncated or tampered input.
    """

    if not isinstance(data, bytes):
        raise SnapshotError("snapshot must be read as raw bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise SnapshotError("BOM is not allowed")
    text = data.decode("utf-8", errors="strict")
    # split() on text ending with "\n" yields one trailing empty element.
    if not text or not text.endswith("\n"):
        raise SnapshotError("snapshot is truncated or missing its final newline")
    lines = text.split("\n")[:-1]

    if not lines:
        raise SnapshotError("snapshot is empty")
    if lines[0] != HEADER:
        raise SnapshotError(f"bad snapshot header: {lines[0]!r}")
    if len(lines) < 2:
        raise SnapshotError("snapshot is missing the entry count")
    count_text = lines[1]
    if not _NONNEG_RE.fullmatch(count_text):
        raise SnapshotError("entry count must be a non-negative decimal integer")
    count = int(count_text)
    if len(lines) != count + 3:
        raise SnapshotError(
            f"entry count {count} does not match {len(lines) - 3} data rows"
        )

    entries = {}
    body = hashlib.sha256()
    previous = None
    for line in lines[2:-1]:
        fields = line.split("\t")
        if len(fields) != 4:
            raise SnapshotError(f"bad snapshot row: {line!r}")
        path, size_text, mtime_text, digest = fields
        if not _valid_path(path):
            raise SnapshotError(f"invalid path in snapshot: {path!r}")
        if not _NONNEG_RE.fullmatch(size_text):
            raise SnapshotError(f"size is not a decimal integer: {path}")
        if not _INT_RE.fullmatch(mtime_text):
            raise SnapshotError(f"mtime_ns is not a decimal integer: {path}")
        if not _DIGEST_RE.fullmatch(digest):
            raise SnapshotError(f"sha256 is not 64 lowercase hex chars: {path}")
        if previous is not None:
            if path == previous:
                raise SnapshotError(f"duplicate path in snapshot: {path}")
            if path < previous:
                raise SnapshotError(f"paths are not sorted: {previous} before {path}")
        previous = path
        entries[path] = Entry(int(size_text), int(mtime_text), digest)
        body.update(line.encode("utf-8") + b"\n")

    stored_digest = lines[-1]
    if not _DIGEST_RE.fullmatch(stored_digest):
        raise SnapshotError("body digest is not 64 lowercase hex chars")
    if stored_digest != body.hexdigest():
        raise SnapshotError("body digest does not match: snapshot is corrupt")
    return Snapshot(entries)


def load(path):
    """Read and validate a snapshot file."""

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    return decode(data)


@contextlib.contextmanager
def atomic_writer(path):
    """Yield a binary file for ``path`` written via temp file + os.replace.

    Nothing is visible at ``path`` until the block completes; on any failure
    the temp file is removed and no partial output is left behind.
    """

    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"cannot create output directory {directory}: {exc}") from exc
    tmp_path = f"{path}.tmp"
    try:
        handle = open(tmp_path, "wb")
    except OSError as exc:
        raise OutputError(f"cannot write {path}: {exc}") from exc
    try:
        try:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise OutputError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write(path, data):
    """Write bytes to a temp file in the same directory then atomically replace."""

    with atomic_writer(path) as handle:
        handle.write(data)


def save(path, snapshot):
    """Serialize a snapshot and write it atomically, streaming row by row."""

    digest = hashlib.sha256()
    with atomic_writer(path) as handle:
        handle.write(f"{HEADER}\n{len(snapshot.entries)}\n".encode("utf-8"))
        for line in _iter_entry_lines(snapshot.entries):
            digest.update(line)
            handle.write(line)
        handle.write(digest.hexdigest().encode("ascii") + b"\n")
