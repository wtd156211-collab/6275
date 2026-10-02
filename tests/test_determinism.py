"""确定性：同输入连跑两遍（含换 PYTHONHASHSEED），各产物逐字节相同。"""

from __future__ import annotations

import os
import unittest

from ._helper import prepare_case, run_cli, snapshot_path, workdir


class DeterminismTests(unittest.TestCase):
    def test_outputs_byte_identical_across_runs_and_seeds(self) -> None:
        for seed in (None, "1", "31337"):
            pass
        work = workdir("determinism")
        live = prepare_case("06-add-remove", work)

        outputs = {}
        for tag, seed in (("a", None), ("b", "1"), ("c", "31337")):
            paths = {
                name: os.path.join(work, f"{tag}-{name}")
                for name in ("changes.txt", "data.json", "snap.tsv")
            }
            env = {} if seed is None else {"PYTHONHASHSEED": seed}
            proc = run_cli(
                [
                    "scan", "--tree", live,
                    "--snapshot", snapshot_path("06-add-remove"),
                    "--changes", paths["changes.txt"],
                    "--data", paths["data.json"],
                    "--budget", "4",
                    "--write-snapshot", paths["snap.tsv"],
                ],
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs[tag] = (paths, proc.stdout)

        first = outputs["a"]
        for tag in ("b", "c"):
            other = outputs[tag]
            self.assertEqual(first[1], other[1], "STAT 行不一致")
            for name in ("changes.txt", "data.json", "snap.tsv"):
                with open(first[0][name], "rb") as fa, open(other[0][name], "rb") as fb:
                    self.assertEqual(fa.read(), fb.read(), f"{name} 不一致 ({tag})")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
