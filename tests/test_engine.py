"""引擎级单测：分级链边界、时间戳 1 纳秒、mtime 回写、符号链接与节点结构。"""

from __future__ import annotations

import os
import unittest

from rescan import Snapshot, diff, load_snapshot
from rescan.hasher import hash_file
from rescan.report import build_data, build_nodes
from rescan.scanner import (
    STATUS_ADDED,
    STATUS_MODIFIED,
    STATUS_REMOVED,
    STATUS_UNCHANGED,
)
from rescan.walker import walk_tree

from ._helper import prepare_case, snapshot_path, workdir


def _write(path: str, content: bytes, mtime_ns: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    os.utime(path, ns=(mtime_ns, mtime_ns))


class ChainEdgeTests(unittest.TestCase):
    def test_one_nanosecond_difference_forces_hash_but_stays_equal(self) -> None:
        # mtime 只差 1 纳秒：必须升级读内容，内容一致记 =，恰好 1 次哈希。
        work = workdir("one-ns")
        live = os.path.join(work, "live")
        f = os.path.join(live, "a.txt")
        _write(f, b"hello\n", 1_000_000_000)
        sha = hash_file(f)
        snapshot = Snapshot({"a.txt": (6, 999_999_999, sha)})
        result = diff(snapshot, walk_tree(live), live, write_snapshot=False)
        self.assertEqual(result.unchanged, ["a.txt"])
        self.assertEqual(result.hashes, 1)

    def test_size_change_catches_mtime_rewrite(self) -> None:
        # 改完又被工具把 mtime 写回旧值：靠 size 一步判 ~，且不读内容。
        work = workdir("mtime-reset")
        live = os.path.join(work, "live")
        f = os.path.join(live, "a.txt")
        _write(f, b"longer new content\n", 1_000_000_000)
        snapshot = Snapshot({"a.txt": (4, 1_000_000_000, "0" * 64)})
        result = diff(snapshot, walk_tree(live), live, write_snapshot=False)
        self.assertEqual(result.modified, ["a.txt"])
        self.assertEqual(result.hashes, 0)

    def test_same_size_same_mtime_is_the_only_skip_exit(self) -> None:
        work = workdir("skip-exit")
        live = os.path.join(work, "live")
        f = os.path.join(live, "a.txt")
        _write(f, b"abc", 123)
        sha = hash_file(f)
        snapshot = Snapshot({"a.txt": (3, 123, sha)})
        result = diff(snapshot, walk_tree(live), live, write_snapshot=True)
        self.assertEqual(result.unchanged, ["a.txt"])
        self.assertEqual(result.hashes, 0)
        # 写新快照时沿用旧摘要，不读内容。
        self.assertEqual(
            result.new_snapshot.entries["a.txt"], (3, 123, sha)
        )

    def test_same_size_changed_mtime_changed_content(self) -> None:
        work = workdir("same-size-mod")
        live = os.path.join(work, "live")
        f = os.path.join(live, "a.txt")
        _write(f, b"xyz", 200)
        # 用一份不同的同长内容构造旧摘要。
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"abc")
            tmp_path = tmp.name
        try:
            snapshot = Snapshot({"a.txt": (3, 100, hash_file(tmp_path))})
        finally:
            os.unlink(tmp_path)
        result = diff(snapshot, walk_tree(live), live, write_snapshot=True)
        self.assertEqual(result.modified, ["a.txt"])
        self.assertEqual(result.hashes, 1)
        self.assertEqual(result.new_snapshot.entries["a.txt"][2], hash_file(f))

    def test_added_and_removed_not_hashed_without_new_snapshot(self) -> None:
        work = workdir("add-remove")
        live = os.path.join(work, "live")
        _write(os.path.join(live, "new.txt"), b"new", 10)
        snapshot = Snapshot({"gone.txt": (3, 1, "0" * 64)})
        result = diff(snapshot, walk_tree(live), live, write_snapshot=False)
        self.assertEqual(result.added, ["new.txt"])
        self.assertEqual(result.removed, ["gone.txt"])
        self.assertEqual(result.hashes, 0)

    def test_added_hashed_only_for_new_snapshot(self) -> None:
        work = workdir("added-hash")
        live = os.path.join(work, "live")
        _write(os.path.join(live, "new.txt"), b"new", 10)
        snapshot = Snapshot({})
        result = diff(snapshot, walk_tree(live), live, write_snapshot=True)
        self.assertEqual(result.hashes, 1)
        self.assertIn("new.txt", result.new_snapshot.entries)

    def test_symlinks_ignored(self) -> None:
        work = workdir("symlinks")
        live = os.path.join(work, "live")
        _write(os.path.join(live, "real.txt"), b"data", 10)
        os.symlink("real.txt", os.path.join(live, "link.txt"))
        os.symlink("/tmp", os.path.join(live, "dirlink"))
        index = walk_tree(live)
        self.assertEqual({row[0] for row in index}, {"real.txt"})


class NodeAndSampleTests(unittest.TestCase):
    def test_nodes_include_removed_parents_with_null_dir_status(self) -> None:
        work = workdir("nodes")
        live = prepare_case("04-dir-move", work)
        snapshot = load_snapshot(snapshot_path("04-dir-move"))
        result = diff(snapshot, walk_tree(live), live)
        nodes = build_nodes(result)
        by_path = {node["path"]: node for node in nodes}

        # 删除文件的父目录（磁盘上已不存在）也要出现。
        self.assertEqual(by_path["old/util"]["kind"], "dir")
        self.assertIsNone(by_path["old/util"]["status"])
        self.assertEqual(by_path["old/util"]["depth"], 1)
        self.assertEqual(by_path["old/util/a.txt"]["status"], STATUS_REMOVED)

        self.assertEqual(by_path["lib/util"]["kind"], "dir")
        self.assertEqual(by_path["lib/util/a.txt"]["status"], STATUS_ADDED)

        # 路径码点升序；根的直接子项 depth=0。
        paths = [node["path"] for node in nodes]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(by_path["old"]["depth"], 0)
        self.assertEqual(by_path["old/keep.txt"]["depth"], 1)

        data = build_data(result, "live", 4)
        self.assertEqual(data["hashes"], result.hashes)
        self.assertEqual(
            sum(data["counts"].values()),
            sum(1 for n in nodes if n["kind"] == "file"),
        )

    def test_case_05_restore_mtime_chain_on_live_engine(self) -> None:
        work = workdir("engine-05")
        live = prepare_case("05-restore-mtime", work)
        result = diff(load_snapshot(snapshot_path("05-restore-mtime")), walk_tree(live), live)
        self.assertEqual(result.modified, ["archive/2022/q1.txt"])
        # 3 个只改时间戳的文件必须读内容，且全部记 =。
        self.assertEqual(result.hashes, 3)
        self.assertNotIn("archive/2022/q1.txt", result.unchanged)
        for path in ("backup/alpha.txt", "backup/beta.txt", "backup/gamma.txt"):
            self.assertIn(path, result.unchanged)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
