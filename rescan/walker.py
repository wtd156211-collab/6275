"""确定性目录遍历：只收集普通文件的 (size, mtime_ns)，不跟随符号链接。"""

from __future__ import annotations

import os
import stat
from typing import List, Tuple

# 按路径码点升序的 (rel_path, size, mtime_ns) 行。
FileRow = Tuple[str, int, int]
FileRows = List[FileRow]


class WalkError(OSError):
    """目录树不可读或遍历时出错。"""


def walk_tree(root: str) -> FileRows:
    """递归遍历 root，返回按路径升序的 [(rel_path, size, mtime_ns), ...]。

    - 只收普通文件；符号链接、套接字、设备等一律不跟随、不记录；
    - 目录本身不产生记录，目录 mtime 不参与判定；
    - 相对路径用 "/" 分隔；遍历顺序不影响结果（返回前统一排序）。
    """
    if not os.path.isdir(root):
        raise WalkError(f"扫描根目录不存在或不是目录: {root!r}")

    rows: FileRows = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(abs_dir) as entries:
                children = list(entries)
        except OSError as exc:
            raise WalkError(f"无法读取目录 {abs_dir!r}: {exc}") from exc
        for entry in children:
            name = entry.name
            if name in (".", ".."):
                continue
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                # 遍历瞬间文件消失等情况：按不存在处理，不记条目。
                continue
            if stat.S_ISREG(st.st_mode):
                rows.append((rel_path, st.st_size, st.st_mtime_ns))
            elif stat.S_ISDIR(st.st_mode):
                stack.append(rel_path)
            # 其余类型（符号链接、FIFO、socket、设备）忽略。
    rows.sort(key=lambda row: row[0])
    return rows
