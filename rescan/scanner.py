"""分级判断链：拿旧快照与当前目录行表归并比对，产出四类变更集合。

判断链（逐路径按序走，零漏检指大小或时间戳有差异的改动一条不漏）：

1. 快照有、磁盘没有 -> removed；磁盘有、快照没有 -> added；
2. size 不同 -> modified（已证明内容不同，不读内容）；
3. size 相同、mtime_ns 相同 -> unchanged（唯一允许跳过内容比对的出口）；
4. size 相同、mtime_ns 不同 -> 必须读内容算 sha256 再裁：
   与快照摘要一致记 unchanged，否则记 modified。

哈希次数口径：一次扫描里「读过内容的文件个数」，同一文件只计一次；
为写新快照而读取判定摘要时与判定读算同一次。

实现上旧快照与当前目录都是按路径升序的行表，一趟归并完成比对，
不为大目录额外复制整本字典。
"""

from __future__ import annotations

import heapq
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .hasher import HashCounter
from .snapshot import EntryRows, Snapshot
from .walker import FileRows

STATUS_ADDED = "added"
STATUS_MODIFIED = "modified"
STATUS_REMOVED = "removed"
STATUS_UNCHANGED = "unchanged"

STATUSES = (STATUS_ADDED, STATUS_MODIFIED, STATUS_REMOVED, STATUS_UNCHANGED)

MARKS = {
    STATUS_ADDED: "+",
    STATUS_MODIFIED: "~",
    STATUS_REMOVED: "-",
    STATUS_UNCHANGED: "=",
}

# 四类颜色：页面只能用这组，由引擎统一下发。
COLORS = {
    STATUS_ADDED: "#1a7f37",
    STATUS_MODIFIED: "#9a6700",
    STATUS_REMOVED: "#cf222e",
    STATUS_UNCHANGED: "#6e7781",
}


@dataclass
class ScanResult:
    """一次扫描的产物：四类有序路径表、哈希计数与可选的新快照行表。"""

    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    hashes: int = 0
    hashed_paths: frozenset = frozenset()
    new_rows: Optional[EntryRows] = None

    @property
    def new_snapshot(self) -> Optional[Snapshot]:
        """本轮新快照（仅当 diff 时 write_snapshot=True）。"""
        if self.new_rows is None:
            return None
        return Snapshot(self.new_rows)

    def iter_marked(self) -> Iterator[Tuple[str, str]]:
        """按路径码点升序产出 (路径, 标记)，标记为 + ~ - = 之一。"""
        streams = [
            ((path, MARKS[STATUS_ADDED]) for path in self.added),
            ((path, MARKS[STATUS_MODIFIED]) for path in self.modified),
            ((path, MARKS[STATUS_REMOVED]) for path in self.removed),
            ((path, MARKS[STATUS_UNCHANGED]) for path in self.unchanged),
        ]
        yield from heapq.merge(*streams, key=lambda item: item[0])

    def iter_status(self) -> Iterator[Tuple[str, str]]:
        """按路径码点升序产出 (路径, 状态)。"""
        streams = [
            ((path, STATUS_ADDED) for path in self.added),
            ((path, STATUS_MODIFIED) for path in self.modified),
            ((path, STATUS_REMOVED) for path in self.removed),
            ((path, STATUS_UNCHANGED) for path in self.unchanged),
        ]
        yield from heapq.merge(*streams, key=lambda item: item[0])

    def status_of(self) -> Dict[str, str]:
        """返回 路径 -> 状态 的映射（仅普通文件）。"""
        return dict(self.iter_status())

    def counts(self) -> Dict[str, int]:
        return {
            STATUS_ADDED: len(self.added),
            STATUS_MODIFIED: len(self.modified),
            STATUS_REMOVED: len(self.removed),
            STATUS_UNCHANGED: len(self.unchanged),
        }

    def changes_lines(self) -> List[str]:
        """变更集合文本行：``<标记> <路径>``，按路径码点升序。"""
        return [f"{mark} {path}" for path, mark in self.iter_marked()]


def _abs_path(root: str, rel_path: str) -> str:
    return os.path.join(root, *rel_path.split("/"))


def diff(
    snapshot: Snapshot,
    current: FileRows,
    root: str,
    *,
    write_snapshot: bool = False,
    counter: Optional[HashCounter] = None,
) -> ScanResult:
    """按分级判断链归并比对旧快照行表与当前目录行表。

    - ``current`` 来自 :func:`rescan.walker.walk_tree`（按路径升序）；
    - ``write_snapshot`` 为真时产出 ``new_rows``：条目等于本轮真实存在
      的文件，``~`` 与 ``+`` 用本轮算的摘要，``=`` 沿用快照摘要；
    - 哈希计数通过 ``counter`` 累计（判定读与写快照读同一文件只计一次）。
    """
    if counter is None:
        counter = HashCounter()

    old_rows = snapshot.rows
    result = ScanResult()
    new_rows: EntryRows = []

    i = 0
    j = 0
    old_len = len(old_rows)
    new_len = len(current)
    while i < old_len or j < new_len:
        if j >= new_len or (i < old_len and old_rows[i][0] < current[j][0]):
            # 第 1 级：快照有、磁盘没有。
            result.removed.append(old_rows[i][0])
            i += 1
            continue
        if i >= old_len or current[j][0] < old_rows[i][0]:
            # 第 1 级：磁盘有、快照没有。
            path, size, mtime_ns = current[j]
            result.added.append(path)
            if write_snapshot:
                sha = counter.hash_file(path, _abs_path(root, path))
                new_rows.append((path, size, mtime_ns, sha))
            j += 1
            continue

        path, old_size, old_mtime, old_sha = old_rows[i]
        _, size, mtime_ns = current[j]
        i += 1
        j += 1
        if size != old_size:
            # 第 2 级：大小不同，内容必然不同，不必读。
            result.modified.append(path)
            if write_snapshot:
                sha = counter.hash_file(path, _abs_path(root, path))
                new_rows.append((path, size, mtime_ns, sha))
        elif mtime_ns == old_mtime:
            # 第 3 级：大小与时间戳都一致，唯一允许跳过内容比对的出口。
            result.unchanged.append(path)
            if write_snapshot:
                new_rows.append((path, size, mtime_ns, old_sha))
        else:
            # 第 4 级：只有时间戳不一致，必须读内容算摘要再裁。
            sha = counter.hash_file(path, _abs_path(root, path))
            if sha == old_sha:
                result.unchanged.append(path)
            else:
                result.modified.append(path)
            if write_snapshot:
                new_rows.append((path, size, mtime_ns, sha))

    result.hashes = counter.count
    result.hashed_paths = counter.paths
    if write_snapshot:
        result.new_rows = new_rows
    return result


def scan(
    root: str,
    snapshot: Snapshot,
    *,
    write_snapshot: bool = False,
) -> ScanResult:
    """遍历目录并与快照比对（walk + diff 的便捷组合）。"""
    from .walker import walk_tree

    current = walk_tree(root)
    return diff(snapshot, current, root, write_snapshot=write_snapshot)
