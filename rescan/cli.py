"""命令行入口：``python -m rescan snapshot|scan``。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import Iterator, List, Optional

from .hasher import hash_file
from .report import (
    iter_changes_lines,
    iter_data_json_chunks,
    tree_name_of,
)
from .scanner import diff
from .snapshot import (
    Snapshot,
    SnapshotError,
    iter_snapshot_lines,
    load_snapshot,
    save_snapshot,
)
from .walker import WalkError, walk_tree


def _error(message: str) -> int:
    print(f"rescan: error: {message}", file=sys.stderr)
    return 1


def _atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".rescan-", suffix=".tmp", dir=directory)
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


def _atomic_write_lines(path: str, lines: Iterator[str]) -> None:
    """流式写文本行（UTF-8、LF），先落临时文件再原子替换。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".rescan-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            for line in lines:
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


def _atomic_write_chunks(path: str, chunks: Iterator[bytes]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".rescan-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _check_output_not_in_tree(root: str, outputs: List[Optional[str]]) -> None:
    """输出文件不得写进被扫目录，否则下一次扫描会被自己的产物污染。"""
    root_abs = os.path.abspath(root)
    for out in outputs:
        if out is None:
            continue
        out_abs = os.path.abspath(out)
        if out_abs == root_abs or out_abs.startswith(root_abs + os.sep):
            raise ValueError(f"输出文件不能写进被扫目录: {out!r}")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    root = args.tree
    try:
        _check_output_not_in_tree(root, [args.out])
        current = walk_tree(root)
    except (WalkError, ValueError) as exc:
        return _error(str(exc))
    rows = []
    for path, size, mtime_ns in current:
        sha = hash_file(os.path.join(root, *path.split("/")))
        rows.append((path, size, mtime_ns, sha))
    try:
        save_snapshot(Snapshot(rows), args.out)
    except (OSError, SnapshotError) as exc:
        return _error(f"写快照失败: {exc}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    root = args.tree
    try:
        _check_output_not_in_tree(
            root, [args.changes, args.data, args.write_snapshot]
        )
    except ValueError as exc:
        return _error(str(exc))

    try:
        snapshot = load_snapshot(args.snapshot)
    except SnapshotError as exc:
        return _error(str(exc))

    try:
        current = walk_tree(root)
    except WalkError as exc:
        return _error(str(exc))

    result = diff(
        snapshot,
        current,
        root,
        write_snapshot=args.write_snapshot is not None,
    )

    tree_name = tree_name_of(root)
    try:
        _atomic_write_lines(args.changes, iter_changes_lines(result))
        _atomic_write_chunks(
            args.data, iter_data_json_chunks(result, tree_name, args.budget)
        )
        if result.new_rows is not None:
            _atomic_write_lines(
                args.write_snapshot, iter_snapshot_lines(Snapshot(result.new_rows))
            )
    except (OSError, SnapshotError) as exc:
        return _error(f"写输出失败: {exc}")

    budget_text = "-" if args.budget is None else str(args.budget)
    counts = result.counts()
    print(
        "STAT|hashes={hashes}|budget={budget}|added={added}"
        "|modified={modified}|removed={removed}|unchanged={unchanged}".format(
            hashes=result.hashes,
            budget=budget_text,
            added=counts["added"],
            modified=counts["modified"],
            removed=counts["removed"],
            unchanged=counts["unchanged"],
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rescan",
        description="增量目录扫描：拿上一次的快照与现在的目录比出四类变更。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_snapshot = sub.add_parser("snapshot", help="为目录建立基线快照")
    p_snapshot.add_argument("--tree", required=True, help="被扫描的目录")
    p_snapshot.add_argument("--out", required=True, help="快照输出路径")
    p_snapshot.set_defaults(func=_cmd_snapshot)

    p_scan = sub.add_parser("scan", help="与快照比对，输出变更集合与页面数据")
    p_scan.add_argument("--tree", required=True, help="被扫描的目录")
    p_scan.add_argument("--snapshot", required=True, help="上一轮快照文件")
    p_scan.add_argument("--changes", required=True, help="变更集合输出路径")
    p_scan.add_argument("--data", required=True, help="页面数据 data.json 输出路径")
    p_scan.add_argument("--budget", type=int, default=None, help="哈希次数预算")
    p_scan.add_argument(
        "--write-snapshot", default=None, help="把本轮新快照写到该路径"
    )
    p_scan.set_defaults(func=_cmd_scan)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
