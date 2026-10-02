"""Incremental scan engine.

Compares a persisted Snapshot with the live directory tree and classifies
every path as added / modified / removed / unchanged.  The decision chain is
ordered from cheap to expensive and only falls through to a content hash when
size and mtime cannot settle the answer:

1. present in snapshot only  -> removed
   present on disk only      -> added
2. size differs              -> modified (content already proven different)
3. size and mtime_ns equal   -> unchanged (the only exit that skips content)
4. size equal, mtime differs -> read content, compare sha256, then decide

Rule 4 is what keeps timestamp-only churn (backup restores, tools that touch
mtimes) from being reported as modifications, while rule 2 catches content
edits whose mtime was forged back to the old value.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from .errors import TreeError
from .snapshot import Entry, Snapshot

STATUS_ADDED = "added"
STATUS_MODIFIED = "modified"
STATUS_REMOVED = "removed"
STATUS_UNCHANGED = "unchanged"

STATUSES = (STATUS_ADDED, STATUS_MODIFIED, STATUS_REMOVED, STATUS_UNCHANGED)
_MARKS = {
    STATUS_ADDED: "+",
    STATUS_MODIFIED: "~",
    STATUS_REMOVED: "-",
    STATUS_UNCHANGED: "=",
}

# The page must use exactly this palette; it is exported into data.json.
COLORS = {
    STATUS_ADDED: "#2ea043",
    STATUS_MODIFIED: "#d29922",
    STATUS_REMOVED: "#f85149",
    STATUS_UNCHANGED: "#8b949e",
}

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Node:
    """One row of the page tree: a directory or a classified file."""

    path: str
    kind: str  # "dir" | "file"
    depth: int
    status: str | None  # files only; directories are None


@dataclass
class ScanResult:
    """Outcome of one scan: ordered change sets, hash count and page nodes."""

    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    hashes: int = 0
    nodes: list = field(default_factory=list)

    def counts(self):
        return {
            STATUS_ADDED: len(self.added),
            STATUS_MODIFIED: len(self.modified),
            STATUS_REMOVED: len(self.removed),
            STATUS_UNCHANGED: len(self.unchanged),
        }

    def iter_change_lines(self):
        """Yield '<mark> <path>' rows in path-sorted order without copying."""

        streams = [
            iter(self.added),
            iter(self.modified),
            iter(self.removed),
            iter(self.unchanged),
        ]
        heads = [None, None, None, None]
        for index, stream in enumerate(streams):
            try:
                heads[index] = next(stream)
            except StopIteration:
                heads[index] = None
        status_of = [STATUS_ADDED, STATUS_MODIFIED, STATUS_REMOVED, STATUS_UNCHANGED]
        while True:
            best = None
            best_path = None
            for index, path in enumerate(heads):
                if path is not None and (best_path is None or path < best_path):
                    best_path = path
                    best = index
            if best is None:
                break
            yield f"{_MARKS[status_of[best]]} {best_path}"
            try:
                heads[best] = next(streams[best])
            except StopIteration:
                heads[best] = None

    def changes_lines(self):
        """The changeset file body: '<mark> <path>' rows, path-sorted."""

        return "".join(line + "\n" for line in self.iter_change_lines())


def hash_file(path):
    """SHA-256 of a file's raw bytes, read in fixed-size chunks."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _check_rel_path(rel_path):
    for char in ("\t", "\n", "\r", "\x00"):
        if char in rel_path:
            raise TreeError(f"unsupported characters in file name: {rel_path!r}")


def walk_tree(root):
    """Recursively collect (relative path -> (size, mtime_ns)) and live dirs.

    Only regular files are recorded; symlinks and anything else are ignored
    and never followed.  Raises TreeError if any directory cannot be read.
    """

    files = {}
    dirs = set()

    def visit(dir_abs, dir_rel):
        try:
            with os.scandir(dir_abs) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise TreeError(f"cannot read directory {dir_abs}: {exc}") from exc
        for entry in entries:
            name = entry.name
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_rel = name if not dir_rel else dir_rel + "/" + name
                    dirs.add(child_rel)
                    visit(entry.path, child_rel)
                elif entry.is_file(follow_symlinks=False):
                    child_rel = name if not dir_rel else dir_rel + "/" + name
                    _check_rel_path(child_rel)
                    stat = entry.stat(follow_symlinks=False)
                    files[child_rel] = (stat.st_size, stat.st_mtime_ns)
            except OSError as exc:
                raise TreeError(f"cannot stat {entry.path}: {exc}") from exc

    if not os.path.isdir(root):
        raise TreeError(f"not a readable directory: {root}")
    visit(root, "")
    return files, dirs


def _build_nodes(result, live_dirs):
    """Merge live directories and the four change lists into sorted nodes."""

    dirs = set(live_dirs)
    file_items = []
    for status in STATUSES:
        for path in getattr(result, status):
            file_items.append((path, status))
            if status == STATUS_REMOVED:
                # Deleted files still appear, and so do their parent dirs.
                parent = path.rpartition("/")[0]
                while parent:
                    dirs.add(parent)
                    parent = parent.rpartition("/")[0]
    file_items.sort(key=lambda item: item[0])
    items = [(path, "dir", None) for path in dirs]
    items += [(path, "file", status) for path, status in file_items]
    items.sort(key=lambda item: item[0])
    return [
        Node(path=path, kind=kind, depth=path.count("/"), status=status)
        for path, kind, status in items
    ]


def scan(snapshot, root, write_snapshot=False):
    """Compare ``snapshot`` against the tree at ``root``.

    Returns (ScanResult, new Snapshot or None).  ``hashes`` counts files whose
    content was read this run; a file is counted at most once, and the digest
    computed for the decision is reused for the new snapshot.
    """

    if not isinstance(snapshot, Snapshot):
        raise TypeError("scan() needs a validated Snapshot")
    root = os.fspath(root)
    live_files, live_dirs = walk_tree(root)

    result = ScanResult()
    digests = {}  # path -> sha256, for live files whose content was read

    def read_digest(rel_path):
        digest = digests.get(rel_path)
        if digest is None:
            digest = hash_file(os.path.join(root, rel_path.replace("/", os.sep)))
            digests[rel_path] = digest
            result.hashes += 1
        return digest

    # Merge-walk the two sorted path streams (snapshot entries are stored
    # sorted; live paths are sorted here) without building extra path lists.
    snap_iter = iter(snapshot.entries.items())
    live_iter = iter(sorted(live_files))
    snap_head = next(snap_iter, None)
    live_head = next(live_iter, None)
    while snap_head is not None or live_head is not None:
        if live_head is None or (snap_head is not None and snap_head[0] < live_head):
            result.removed.append(snap_head[0])
            snap_head = next(snap_iter, None)
        elif snap_head is None or live_head < snap_head[0]:
            result.added.append(live_head)
            if write_snapshot:
                read_digest(live_head)
            live_head = next(live_iter, None)
        else:
            path, old = snap_head
            size, mtime_ns = live_files[live_head]
            snap_head = next(snap_iter, None)
            live_head = next(live_iter, None)
            if old.size != size:
                # Size alone proves the content changed; never trust the mtime.
                result.modified.append(path)
                if write_snapshot:
                    read_digest(path)
            elif old.mtime_ns != mtime_ns:
                # Same size, different mtime: only the content can settle it.
                digest = read_digest(path)
                if digest == old.sha256:
                    result.unchanged.append(path)
                else:
                    result.modified.append(path)
            else:
                result.unchanged.append(path)

    result.nodes = _build_nodes(result, live_dirs)

    new_snapshot = None
    if write_snapshot:
        entries = {}
        for path in sorted(live_files):
            size, mtime_ns = live_files[path]
            digest = digests.get(path)
            if digest is None:
                # Unchanged by rule 3: reuse the snapshot digest, no read.
                digest = snapshot.entries[path].sha256
            entries[path] = Entry(size, mtime_ns, digest)
        new_snapshot = Snapshot(entries)

    return result, new_snapshot
