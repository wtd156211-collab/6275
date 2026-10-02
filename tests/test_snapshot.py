"""快照子命令与落盘格式：自建基线、重载校验、空树、输出位置限制。"""

from __future__ import annotations

import hashlib
import os
import unittest

from rescan.snapshot import (
    HEADER_LINE,
    SnapshotError,
    load_snapshot,
    parse_snapshot_bytes,
)

from ._helper import prepare_case, run_cli, snapshot_path, workdir


class SnapshotCommandTests(unittest.TestCase):
    def test_snapshot_command_roundtrip_and_self_diff(self) -> None:
        work = workdir("snapshot-cmd")
        live = prepare_case("06-add-remove", work)
        snap = os.path.join(work, "baseline.tsv")

        proc = run_cli(["snapshot", "--tree", live, "--out", snap])
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # 手工按格式重算正文摘要，确认落盘字节自洽。
        with open(snap, "rb") as handle:
            raw = handle.read()
        lines = raw.split(b"\n")
        self.assertEqual(lines[0].decode("utf-8"), HEADER_LINE)
        count = int(lines[1])
        body = b"".join(line + b"\n" for line in lines[2 : 2 + count])
        self.assertEqual(
            lines[2 + count].decode("ascii"), hashlib.sha256(body).hexdigest()
        )
        self.assertEqual(raw, raw.rstrip(b"\n") + b"\n")  # 末行有换行
        self.assertNotIn(b"\r", raw)

        snapshot = load_snapshot(snap)
        self.assertEqual(len(snapshot), 8)

        # 自己跟自己比：全部未变、0 次哈希。
        changes = os.path.join(work, "changes.txt")
        proc2 = run_cli(
            [
                "scan", "--tree", live, "--snapshot", snap,
                "--changes", changes, "--data", os.path.join(work, "data.json"),
            ]
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("hashes=0", proc2.stdout)
        with open(changes, "rb") as handle:
            self.assertTrue(
                all(line.startswith(b"= ") for line in handle.read().splitlines())
            )

    def test_snapshot_command_matches_scan_new_snapshot(self) -> None:
        # 同一棵 live：snapshot 子命令与 scan --write-snapshot 逐字节相同。
        work = workdir("snapshot-rebuild")
        live = prepare_case("01-content-only", work)
        snap_a = os.path.join(work, "a.tsv")
        snap_b = os.path.join(work, "b.tsv")
        p1 = run_cli(["snapshot", "--tree", live, "--out", snap_a])
        self.assertEqual(p1.returncode, 0, p1.stderr)
        p2 = run_cli(
            [
                "scan", "--tree", live,
                "--snapshot", snapshot_path("01-content-only"),
                "--changes", os.path.join(work, "changes.txt"),
                "--data", os.path.join(work, "data.json"),
                "--write-snapshot", snap_b,
            ]
        )
        self.assertEqual(p2.returncode, 0, p2.stderr)
        with open(snap_a, "rb") as a, open(snap_b, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_empty_tree(self) -> None:
        work = workdir("empty-tree")
        live = os.path.join(work, "live")
        os.makedirs(live)
        snap = os.path.join(work, "empty.tsv")
        proc = run_cli(["snapshot", "--tree", live, "--out", snap])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snapshot = load_snapshot(snap)
        self.assertEqual(len(snapshot), 0)

        changes = os.path.join(work, "changes.txt")
        proc2 = run_cli(
            [
                "scan", "--tree", live, "--snapshot", snap,
                "--changes", changes, "--data", os.path.join(work, "data.json"),
            ]
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("hashes=0", proc2.stdout)
        with open(changes, "rb") as handle:
            self.assertEqual(handle.read(), b"")

    def test_output_inside_tree_rejected(self) -> None:
        work = workdir("output-in-tree")
        live = prepare_case("03-rename", work)
        inside = os.path.join(live, "changes.txt")
        proc = run_cli(
            [
                "scan", "--tree", live,
                "--snapshot", snapshot_path("03-rename"),
                "--changes", inside,
                "--data", os.path.join(work, "data.json"),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(inside))

        proc2 = run_cli(
            ["snapshot", "--tree", live, "--out", os.path.join(live, "s.tsv")]
        )
        self.assertNotEqual(proc2.returncode, 0)

    def test_missing_tree_and_missing_snapshot(self) -> None:
        work = workdir("missing-inputs")
        proc = run_cli(
            ["snapshot", "--tree", os.path.join(work, "nope"), "--out", os.path.join(work, "s.tsv")]
        )
        self.assertNotEqual(proc.returncode, 0)
        proc2 = run_cli(
            [
                "scan", "--tree", os.path.join(work, "nope"),
                "--snapshot", os.path.join(work, "nope.tsv"),
                "--changes", os.path.join(work, "c.txt"),
                "--data", os.path.join(work, "d.json"),
            ]
        )
        self.assertNotEqual(proc2.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(work, "c.txt")))

    def test_parse_rejects_duplicate_and_unsorted(self) -> None:
        work = workdir("snapshot-parse")
        live = prepare_case("04-dir-move", work)
        snap = os.path.join(work, "base.tsv")
        self.assertEqual(run_cli(["snapshot", "--tree", live, "--out", snap]).returncode, 0)
        with open(snap, "rb") as handle:
            lines = handle.read().split(b"\n")
        n = int(lines[1])

        def rebuild(body_lines):
            body = b"".join(line + b"\n" for line in body_lines)
            digest = hashlib.sha256(body).hexdigest().encode("ascii")
            return b"\n".join(
                [lines[0], str(len(body_lines)).encode(), *body_lines, digest, b""]
            )

        entries = lines[2 : 2 + n]
        with self.assertRaises(SnapshotError):
            parse_snapshot_bytes(rebuild([entries[0], entries[0], *entries[1:]]))
        with self.assertRaises(SnapshotError):
            parse_snapshot_bytes(rebuild(list(reversed(entries))))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
