"""幂等：用新快照再扫同一目录必须全 =、0 次哈希，两轮快照逐字节相同。"""

from __future__ import annotations

import os
import unittest

from ._helper import (
    CASES,
    parse_stat,
    prepare_case,
    run_cli,
    snapshot_path,
    workdir,
)


class IdempotenceTests(unittest.TestCase):
    def test_second_scan_all_equal_zero_hashes_identical_snapshot(self) -> None:
        for case in CASES:
            with self.subTest(case=case):
                work = workdir(f"idem-{case}")
                live = prepare_case(case, work)
                snap1 = os.path.join(work, "s1.tsv")
                snap2 = os.path.join(work, "s2.tsv")
                chg1 = os.path.join(work, "c1.txt")
                chg2 = os.path.join(work, "c2.txt")

                p1 = run_cli(
                    [
                        "scan", "--tree", live,
                        "--snapshot", snapshot_path(case),
                        "--changes", chg1, "--data", os.path.join(work, "d1.json"),
                        "--write-snapshot", snap1,
                    ]
                )
                self.assertEqual(p1.returncode, 0, p1.stderr)

                p2 = run_cli(
                    [
                        "scan", "--tree", live,
                        "--snapshot", snap1,
                        "--changes", chg2, "--data", os.path.join(work, "d2.json"),
                        "--write-snapshot", snap2,
                    ]
                )
                self.assertEqual(p2.returncode, 0, p2.stderr)

                stat = parse_stat(p2.stdout.strip())
                self.assertEqual(stat["hashes"], "0")
                self.assertEqual(stat["added"], "0")
                self.assertEqual(stat["modified"], "0")
                self.assertEqual(stat["removed"], "0")

                with open(chg2, "rb") as handle:
                    lines = handle.read().splitlines()
                self.assertTrue(lines)
                self.assertTrue(all(line.startswith(b"= ") for line in lines))

                with open(snap1, "rb") as a, open(snap2, "rb") as b:
                    self.assertEqual(a.read(), b.read())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
