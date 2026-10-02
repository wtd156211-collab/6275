"""页面静态校验：颜色与计数只能来自 data.json，删除节点也要画。"""

from __future__ import annotations

import json
import os
import re
import unittest

from ._helper import prepare_case, run_cli, snapshot_path, workdir

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WebPageTests(unittest.TestCase):
    def test_page_fetches_data_and_uses_engine_colors(self) -> None:
        index = os.path.join(REPO_ROOT, "web", "index.html")
        html = open(index, encoding="utf-8").read()
        self.assertIn('fetch("data.json"', html)
        self.assertIn("<svg", html)  # 内联 SVG 图标
        # 引擎四类颜色值不得被硬编码进页面。
        for hex_color in ("#1a7f37", "#9a6700", "#cf222e", "#6e7781"):
            self.assertNotIn(hex_color, html)
        for key in ("colors", "counts", "hashes", "budget", "nodes"):
            self.assertIn(key, html)

    def test_generated_data_json_well_formed(self) -> None:
        work = workdir("web-data")
        live = prepare_case("06-add-remove", work)
        data_path = os.path.join(work, "data.json")
        proc = run_cli(
            [
                "scan", "--tree", live,
                "--snapshot", snapshot_path("06-add-remove"),
                "--changes", os.path.join(work, "changes.txt"),
                "--data", data_path, "--budget", "4",
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        raw = open(data_path, "rb").read()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        data = json.loads(raw.decode("utf-8"))
        statuses = [n["status"] for n in data["nodes"] if n["kind"] == "file"]
        self.assertEqual(statuses.count("removed"), 2)
        self.assertEqual(statuses.count("added"), 3)
        # 删除节点（legacy/ 及其两个文件）都在节点表里。
        paths = {n["path"] for n in data["nodes"]}
        self.assertIn("legacy/dump-a.txt", paths)
        self.assertIn("legacy/dump-b.txt", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
