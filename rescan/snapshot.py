"""落盘快照的读写与严格校验。

文件格式（UTF-8 无 BOM、LF、末行有换行）::

    #rescan-snapshot v1
    <条目数 n>
    <路径>\\t<size>\\t<mtime_ns>\\t<sha256>   （共 n 行，按路径码点升序且唯一）
    <正文摘要>                                （第 3..n+2 行原始字节含换行拼接后的 SHA-256）

任何一条不满足（读不到、版本号不对、条目数不符、字段写法不对、
路径非法或重复、正文摘要对不上、文件被截断）都抛 SnapshotError，
调用方不得把这样的文件当成有效基线。

内部按「升序行列表」存储，几十万条目也不必再复制出一整本字典。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Dict, Iterator, List, Mapping, Tuple, Union

FORMAT_VERSION = "v1"
HEADER_LINE = f"#rescan-snapshot {FORMAT_VERSION}"

# (path, size, mtime_ns, sha256)
EntryRow = Tuple[str, int, int, str]
EntryRows = List[EntryRow]
Entries = Dict[str, Tuple[int, int, str]]


class SnapshotError(Exception):
    """快照文件缺失、损坏或与格式不符。"""


class Snapshot:
    """一份落盘快照：版本号 + 按路径升序的 (path, size, mtime_ns, sha256) 行。"""

    __slots__ = ("version", "_rows", "_entries_cache")

    def __init__(
        self,
        entries: Union[Mapping[str, Tuple[int, int, str]], EntryRows],
        version: str = FORMAT_VERSION,
    ) -> None:
        self.version = version
        if isinstance(entries, Mapping):
            rows = [
                (path, values[0], values[1], values[2])
                for path, values in entries.items()
            ]
            rows.sort(key=lambda row: row[0])
            self._rows: EntryRows = rows
        else:
            self._rows = list(entries)
        self._entries_cache: Entries | None = None

    @property
    def rows(self) -> EntryRows:
        """按路径码点升序的条目行（不复制）。"""
        return self._rows

    @property
    def entries(self) -> Entries:
        """路径 -> (size, mtime_ns, sha256) 字典视图（按需构建并缓存）。"""
        if self._entries_cache is None:
            self._entries_cache = {
                path: (size, mtime_ns, sha)
                for path, size, mtime_ns, sha in self._rows
            }
        return self._entries_cache

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Snapshot)
            and self.version == other.version
            and self._rows == other._rows
        )

    def __len__(self) -> int:
        return len(self._rows)


def _validate_rel_path(path: str) -> None:
    if not path:
        raise SnapshotError("快照中存在空路径")
    if path.startswith("/"):
        raise SnapshotError(f"快照路径不是相对路径: {path!r}")
    if "\\" in path:
        raise SnapshotError(f"快照路径含有反斜杠: {path!r}")
    if "\t" in path or "\n" in path or "\r" in path:
        raise SnapshotError(f"快照路径含有非法字符: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SnapshotError(f"快照路径含有非法层级: {path!r}")


def _is_decimal(text: str) -> bool:
    return bool(text) and text.isascii() and text.isdigit()


def _is_sha256_hex(text: str) -> bool:
    return (
        len(text) == 64
        and text.isascii()
        and all(ch in "0123456789abcdef" for ch in text)
    )


def _read_required_line(handle, what: str) -> bytes:
    line = handle.readline()
    if not line:
        raise SnapshotError(f"快照被截断：缺少{what}")
    if not line.endswith(b"\n"):
        raise SnapshotError("快照被截断：末行缺少换行")
    if b"\r" in line:
        raise SnapshotError("快照含有 CR 字符，不是纯 LF 文本")
    return line


def _parse_snapshot_stream(handle) -> Snapshot:
    """逐行流式解析并校验，不为大快照再复制整份文本。"""
    version_line = _read_required_line(handle, "版本行")
    if version_line != HEADER_LINE.encode("utf-8") + b"\n":
        raise SnapshotError(f"快照版本行不符: {version_line!r}")

    count_line = _read_required_line(handle, "条目数行")[:-1]
    try:
        count_text = count_line.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"快照条目数不是十进制非负整数: {count_line!r}") from exc
    if not _is_decimal(count_text):
        raise SnapshotError(f"快照条目数不是十进制非负整数: {count_text!r}")
    declared = int(count_text)

    digest = hashlib.sha256()
    rows: EntryRows = []
    previous_path: str | None = None
    for index in range(declared):
        lineno = index + 3
        line = _read_required_line(handle, f"第 {lineno} 行条目")
        digest.update(line)
        try:
            text = line[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotError(f"第 {lineno} 行不是合法 UTF-8: {exc}") from exc
        fields = text.split("\t")
        if len(fields) != 4:
            raise SnapshotError(f"第 {lineno} 行不是 4 个制表符分隔字段")
        path, size_text, mtime_text, sha = fields
        _validate_rel_path(path)
        if not _is_decimal(size_text):
            raise SnapshotError(f"第 {lineno} 行 size 不是十进制: {size_text!r}")
        if not _is_decimal(mtime_text):
            raise SnapshotError(f"第 {lineno} 行 mtime_ns 不是十进制: {mtime_text!r}")
        if not _is_sha256_hex(sha):
            raise SnapshotError(f"第 {lineno} 行摘要不是 64 位小写十六进制")
        if previous_path is not None and path <= previous_path:
            if path == previous_path:
                raise SnapshotError(f"快照路径重复: {path!r}")
            raise SnapshotError(f"快照路径未按码点升序排列: {path!r}")
        previous_path = path
        rows.append((path, int(size_text), int(mtime_text), sha))

    digest_line = _read_required_line(handle, "正文摘要行")[:-1]
    try:
        digest_text = digest_line.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SnapshotError("快照正文摘要不是 64 位小写十六进制") from exc
    if not _is_sha256_hex(digest_text):
        raise SnapshotError("快照正文摘要不是 64 位小写十六进制")
    if digest.hexdigest() != digest_text:
        raise SnapshotError("快照正文摘要校验失败，文件已损坏或被篡改")

    if handle.read(1):
        raise SnapshotError("快照在正文摘要之后还有多余内容")
    return Snapshot(rows)


def parse_snapshot_bytes(raw: bytes) -> Snapshot:
    """从原始字节解析并校验快照；任何不符都抛 SnapshotError。"""
    import io

    if raw.startswith(b"\xef\xbb\xbf"):
        raise SnapshotError("快照带有 BOM，不符合格式")
    return _parse_snapshot_stream(io.BytesIO(raw))


def iter_snapshot_lines(snapshot: Snapshot) -> Iterator[str]:
    """把快照序列化为文本行（不含行尾换行）：版本行、条目数、条目、正文摘要。"""
    yield HEADER_LINE
    yield str(len(snapshot.rows))
    digest = hashlib.sha256()
    for path, size, mtime_ns, sha in snapshot.rows:
        _validate_rel_path(path)
        if not _is_sha256_hex(sha):
            raise SnapshotError(f"条目摘要非法: {path!r}")
        line = f"{path}\t{size}\t{mtime_ns}\t{sha}"
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
        yield line
    yield digest.hexdigest()


def dump_snapshot_lines(snapshot: Snapshot) -> List[str]:
    return list(iter_snapshot_lines(snapshot))


def dump_snapshot_bytes(snapshot: Snapshot) -> bytes:
    return ("\n".join(iter_snapshot_lines(snapshot)) + "\n").encode("utf-8")


def load_snapshot(path: str) -> Snapshot:
    """读取并严格校验快照文件；失败抛 SnapshotError。"""
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise SnapshotError(f"无法读取快照文件 {path!r}: {exc}") from exc
    with handle:
        head = handle.peek(3)[:3] if hasattr(handle, "peek") else b""
        if head == b"\xef\xbb\xbf":
            raise SnapshotError("快照带有 BOM，不符合格式")
        return _parse_snapshot_stream(handle)


def _atomic_write(path: str, data: bytes) -> None:
    """先写同目录临时文件再原子替换，失败不留半成品。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".rescan-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_snapshot(snapshot: Snapshot, path: str) -> None:
    """把快照原子写入 path（流式，不为大快照再复制一份字节串）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".rescan-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            for line in iter_snapshot_lines(snapshot):
                handle.write(line.encode("utf-8"))
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
