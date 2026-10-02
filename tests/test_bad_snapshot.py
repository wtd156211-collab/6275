"""坏快照：四类破坏都必须非零退出，且不写任何输出文件。"""

from __future__ import annotations

import os
import unittest

from rescan.snapshot import SnapshotError, parse_snapshot_bytes

from ._helper import prepare_case, run_cli, snapshot_path, workdir


class BadSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = workdir("bad-snapshot")
        self.live = prepare_case("02-mtime-only", self.work)
        with open(snapshot_path("02-mtime-only"), "rb") as handle:
            self.raw = handle.read()
        self.lines = self.raw.split(b"\n")

    def _scan(self, bad_snapshot: str) -> int:
        changes = os.path.join(self.work, "changes.txt")
        data = os.path.join(self.work, "data.json")
        proc = run_cli(
            [
                "scan", "--tree", self.live,
                "--snapshot", bad_snapshot,
                "--changes", changes, "--data", data,
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(changes))
        self.assertFalse(os.path.exists(data))
        return proc.returncode

    def test_checksum_changed(self) -> None:
        # 正文摘要改一个字符。
        lines = self.lines
        digest = bytearray(lines[-2])
        digest[0] = ord("a") if digest[0] != ord("a") else ord("b")
        lines[-2] = bytes(digest)
        path = os.path.join(self.work, "bad-digest.tsv")
        with open(path, "wb") as handle:
            handle.write(b"\n".join(lines))
        self._scan(path)

    def test_count_too_small(self) -> None:
        # 条目数改小（正文摘要同时失效也一样必须报错）。
        lines = list(self.lines)
        lines[1] = b"5"
        path = os.path.join(self.work, "bad-count.tsv")
        with open(path, "wb") as handle:
            handle.write(b"\n".join(lines))
        self._scan(path)

    def test_middle_line_deleted(self) -> None:
        # 删掉中间一条（条目数与正文摘要都会对不上）。
        lines = list(self.lines)
        del lines[5]
        path = os.path.join(self.work, "bad-missing-line.tsv")
        with open(path, "wb") as handle:
            handle.write(b"\n".join(lines))
        self._scan(path)

    def test_version_changed(self) -> None:
        lines = list(self.lines)
        lines[0] = b"#rescan-snapshot v9"
        path = os.path.join(self.work, "bad-version.tsv")
        with open(path, "wb") as handle:
            handle.write(b"\n".join(lines))
        self._scan(path)

    def test_missing_file_and_truncation(self) -> None:
        missing = os.path.join(self.work, "does-not-exist.tsv")
        self._scan(missing)

        truncated = os.path.join(self.work, "truncated.tsv")
        with open(truncated, "wb") as handle:
            handle.write(self.raw[: len(self.raw) // 2])
        self._scan(truncated)

    def test_parser_rejects_bad_entries(self) -> None:
        good = parse_snapshot_bytes(self.raw)
        self.assertEqual(len(good), 12)
        for label, payload in (
            ("bom", b"\xef\xbb\xbf" + self.raw),
            ("crlf", self.raw.replace(b"\n", b"\r\n")),
            ("absolute-path", self._replace_field(0, 0, "/etc/passwd")),
            ("backslash-path", self._replace_field(0, 0, "a\\b.txt")),
            ("bad-size", self._replace_field(0, 1, "abc")),
            ("bad-mtime", self._replace_field(0, 2, "12.5")),
            ("bad-sha", self._replace_field(0, 3, "z" * 64)),
            ("uppercase-sha", self._replace_field(0, 3, b"A" * 64)),
        ):
            with self.subTest(label):
                with self.assertRaises(SnapshotError):
                    parse_snapshot_bytes(payload)

    def _replace_field(self, entry_index: int, field_index: int, value) -> bytes:
        # 篡改字段后按新正文重算摘要，专门逼出字段格式/路径校验。
        import hashlib
        n = int(self.lines[1])
        lines = list(self.lines)
        fields = lines[2 + entry_index].split(b"\t")
        if isinstance(value, str):
            value = value.encode("utf-8")
        fields[field_index] = value
        lines[2 + entry_index] = b"\t".join(fields)
        body = b"".join(l + b"\n" for l in lines[2:2 + n])
        lines[2 + n] = hashlib.sha256(body).hexdigest().encode("ascii")
        return b"\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
