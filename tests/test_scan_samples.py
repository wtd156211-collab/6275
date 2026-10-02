"""六组样例：复原 mtime 后跑 scan，逐字节比对变更集合与哈希上界。"""

from __future__ import annotations

import json
import os
import unittest

from ._helper import (
    BUDGETS,
    CASES,
    expected_changes,
    parse_stat,
    prepare_case,
    run_cli,
    snapshot_path,
    workdir,
)


class SampleScanTests(unittest.TestCase):
    def test_every_case_matches_expected_and_budget(self) -> None:
        for case in CASES:
            with self.subTest(case=case):
                work = workdir(case)
                live = prepare_case(case, work)
                changes = os.path.join(work, "changes.txt")
                data_json = os.path.join(work, "data.json")
                new_snap = os.path.join(work, "new-snapshot.tsv")

                proc = run_cli(
                    [
                        "scan",
                        "--tree", live,
                        "--snapshot", snapshot_path(case),
                        "--changes", changes,
                        "--data", data_json,
                        "--budget", str(BUDGETS[case]),
                        "--write-snapshot", new_snap,
                    ]
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

                with open(changes, "rb") as handle:
                    self.assertEqual(handle.read(), expected_changes(case))

                stat = parse_stat(proc.stdout.strip())
                self.assertEqual(stat["budget"], str(BUDGETS[case]))
                hashes = int(stat["hashes"])
                # 全量哈希（=文件数）必然超上界；必要次数与上界留一次余量。
                self.assertLessEqual(hashes, BUDGETS[case])
                self.assertLess(hashes, len(expected_changes(case).splitlines()))

                with open(data_json, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertEqual(data["tree"], "live")
                self.assertEqual(data["budget"], BUDGETS[case])
                self.assertEqual(data["hashes"], hashes)
                self.assertEqual(
                    data["counts"],
                    {
                        "added": int(stat["added"]),
                        "modified": int(stat["modified"]),
                        "removed": int(stat["removed"]),
                        "unchanged": int(stat["unchanged"]),
                    },
                )
                # 计数与变更集合行数一致。
                self.assertEqual(
                    sum(data["counts"].values()),
                    len(expected_changes(case).splitlines()),
                )
                # 页面只能用引擎下发的这组颜色。
                self.assertEqual(
                    set(data["colors"]),
                    {"added", "modified", "removed", "unchanged"},
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
