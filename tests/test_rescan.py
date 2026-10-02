"""rescan test suite.  Reads only samples/**, writes only var/."""

import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rescan import (  # noqa: E402
    Entry,
    Snapshot,
    SnapshotError,
    decode,
    encode,
    load,
    save,
    scan,
    walk_tree,
)
from rescan.page import build_data, dump_data, encode_data  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")
TREES = os.path.join(SAMPLES, "trees")
EXPECTED = os.path.join(SAMPLES, "expected")
VAR = os.path.join(ROOT, "var", "test-work")

CASES = [
    "01-content-only",
    "02-mtime-only",
    "03-rename",
    "04-dir-move",
    "05-restore-mtime",
    "06-add-remove",
]


def load_budgets():
    budgets = {}
    with open(os.path.join(SAMPLES, "expected-budget.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, limit = line.split("\t")
            budgets[name] = int(limit)
    return budgets


BUDGETS = load_budgets()


def restore_mtimes(live_dir, times_file):
    with open(times_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            rel, mtime = line.split("\t")
            os.utime(os.path.join(live_dir, rel), ns=(int(mtime), int(mtime)))


def prepare_case(case):
    """Copy a sample tree into var/ and restore mtimes; return live dir."""

    src = os.path.join(TREES, case)
    work = os.path.join(VAR, case)
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    live = os.path.join(work, "live")
    shutil.copytree(os.path.join(src, "live"), live)
    restore_mtimes(live, os.path.join(src, "live.times.tsv"))
    return live


def run_case(case, write_snapshot=False):
    live = prepare_case(case)
    snapshot = load(os.path.join(TREES, case, "snapshot.tsv"))
    return scan(snapshot, live, write_snapshot=write_snapshot)


class SampleCaseTests(unittest.TestCase):
    def test_changes_match_expected(self):
        for case in CASES:
            with self.subTest(case=case):
                result, _ = run_case(case)
                with open(os.path.join(EXPECTED, case + ".txt"), encoding="utf-8") as fh:
                    self.assertEqual(result.changes_lines(), fh.read())

    def test_hash_count_within_budget(self):
        total = 0
        for case in CASES:
            with self.subTest(case=case):
                result, _ = run_case(case, write_snapshot=True)
                self.assertLessEqual(result.hashes, BUDGETS[case])
                total += result.hashes
        self.assertLessEqual(total, BUDGETS["total"])

    def test_unchanged_files_are_never_read(self):
        # 02-mtime-only: every file keeps its content; only the touched
        # mtimes may trigger a read, and every verdict must be '='.
        result, _ = run_case("02-mtime-only")
        self.assertEqual(result.modified, [])
        self.assertEqual(len(result.unchanged), 12)
        self.assertLessEqual(result.hashes, BUDGETS["02-mtime-only"])

    def test_size_change_beats_restored_mtime(self):
        # 05-restore-mtime: q1.txt was edited and its mtime forged back;
        # the size difference must still flag it as modified.
        result, _ = run_case("05-restore-mtime")
        self.assertEqual(result.modified, ["archive/2022/q1.txt"])

    def test_counts_match_change_lines(self):
        for case in CASES:
            with self.subTest(case=case):
                result, _ = run_case(case)
                counts = result.counts()
                lines = [l for l in result.changes_lines().splitlines() if l]
                self.assertEqual(sum(counts.values()), len(lines))


class IdempotencyTests(unittest.TestCase):
    def test_second_run_all_unchanged_zero_hashes(self):
        for case in CASES:
            with self.subTest(case=case):
                live = prepare_case(case)
                snapshot = load(os.path.join(TREES, case, "snapshot.tsv"))
                _, new_snapshot = scan(snapshot, live, write_snapshot=True)
                result2, new_snapshot2 = scan(new_snapshot, live, write_snapshot=True)
                self.assertEqual(result2.hashes, 0)
                self.assertEqual(result2.added, [])
                self.assertEqual(result2.modified, [])
                self.assertEqual(result2.removed, [])
                self.assertEqual(
                    len(result2.unchanged), len(new_snapshot.entries)
                )
                self.assertEqual(encode(new_snapshot), encode(new_snapshot2))


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_outputs(self):
        live = prepare_case("06-add-remove")
        snapshot = load(os.path.join(TREES, "06-add-remove", "snapshot.tsv"))
        result1, snap1 = scan(snapshot, live, write_snapshot=True)
        result2, snap2 = scan(snapshot, live, write_snapshot=True)
        self.assertEqual(result1.changes_lines(), result2.changes_lines())
        self.assertEqual(result1.hashes, result2.hashes)
        self.assertEqual(encode(snap1), encode(snap2))
        data1 = encode_data(build_data("live", result1, budget=4))
        data2 = encode_data(build_data("live", result2, budget=4))
        self.assertEqual(data1, data2)


class SnapshotFormatTests(unittest.TestCase):
    def _valid_bytes(self):
        return open(os.path.join(TREES, "06-add-remove", "snapshot.tsv"), "rb").read()

    def test_round_trip(self):
        for case in CASES:
            with self.subTest(case=case):
                with open(os.path.join(TREES, case, "snapshot.tsv"), "rb") as fh:
                    raw = fh.read()
                self.assertEqual(encode(decode(raw)), raw)

    def test_empty_snapshot_round_trip(self):
        snapshot = Snapshot({})
        self.assertEqual(decode(encode(snapshot)), snapshot)

    def test_corruptions_rejected(self):
        good = self._valid_bytes()
        lines = good.decode("utf-8").split("\n")

        def mutated(edits):
            bad = list(lines)
            for index, value in edits.items():
                bad[index] = value
            return "\n".join(bad).encode("utf-8")

        digest = lines[-2]
        flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
        variants = {
            "body digest flipped": mutated({len(lines) - 2: flipped}),
            "entry count shrunk": mutated({1: "6"}),
            "middle line removed": "\n".join(
                lines[:5] + lines[6:]
            ).encode("utf-8"),
            "version changed": mutated({0: "#rescan-snapshot v2"}),
            "truncated": good[: len(good) // 2],
            "no trailing newline": good.rstrip(b"\n"),
            "bom": b"\xef\xbb\xbf" + good,
            "unsorted rows": mutated({2: lines[3], 3: lines[2]}),
            "bad sha length": mutated({2: lines[2][:-1]}),
            "windows separator": mutated({2: lines[2].replace("/", "\\", 1)}),
        }
        for name, blob in variants.items():
            with self.subTest(variant=name):
                with self.assertRaises(SnapshotError):
                    decode(blob)

    def test_save_and_load(self):
        snapshot = load(os.path.join(TREES, "03-rename", "snapshot.tsv"))
        path = os.path.join(VAR, "roundtrip.tsv")
        os.makedirs(VAR, exist_ok=True)
        save(path, snapshot)
        self.assertEqual(load(path), snapshot)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), encode(snapshot))


class WalkTests(unittest.TestCase):
    def test_symlinks_and_special_files_ignored(self):
        base = os.path.join(VAR, "walk")
        if os.path.exists(base):
            shutil.rmtree(base)
        os.makedirs(os.path.join(base, "sub"))
        with open(os.path.join(base, "a.txt"), "wb") as fh:
            fh.write(b"hello")
        os.symlink("a.txt", os.path.join(base, "link.txt"))
        os.symlink("sub", os.path.join(base, "linkdir"))
        files, dirs = walk_tree(base)
        self.assertEqual(sorted(files), ["a.txt"])
        self.assertEqual(dirs, {"sub"})

    def test_missing_root_raises(self):
        from rescan import TreeError

        with self.assertRaises(TreeError):
            walk_tree(os.path.join(VAR, "does-not-exist"))


class PageDataTests(unittest.TestCase):
    def test_data_json_shape(self):
        result, _ = run_case("06-add-remove")
        data = build_data("live", result, budget=4)
        self.assertEqual(
            list(data), ["tree", "budget", "hashes", "counts", "colors", "nodes"]
        )
        self.assertEqual(data["tree"], "live")
        self.assertEqual(data["budget"], 4)
        self.assertEqual(data["hashes"], result.hashes)
        self.assertEqual(data["counts"], result.counts())
        self.assertEqual(set(data["colors"]), {"added", "modified", "removed", "unchanged"})
        paths = [node["path"] for node in data["nodes"]]
        self.assertEqual(paths, sorted(paths))
        # removed files and their parent directories must appear
        removed = [n for n in data["nodes"] if n["status"] == "removed"]
        self.assertEqual([n["path"] for n in removed], ["legacy/dump-a.txt", "legacy/dump-b.txt"])
        self.assertIn({"path": "legacy", "kind": "dir", "depth": 0, "status": None},
                      data["nodes"])
        for node in data["nodes"]:
            self.assertEqual(node["depth"], node["path"].count("/"))
            if node["kind"] == "dir":
                self.assertIsNone(node["status"])
        # JSON must round-trip and be deterministic
        blob = encode_data(data)
        self.assertTrue(blob.endswith(b"\n"))
        self.assertEqual(json.loads(blob.decode("utf-8")), data)


    def test_streamed_json_matches_canonical(self):
        for case in CASES:
            with self.subTest(case=case):
                result, _ = run_case(case, write_snapshot=True)
                for budget in (None, BUDGETS[case]):
                    parts = []
                    dump_data(parts.append, "live", result, budget=budget)
                    streamed = "".join(parts).encode("utf-8")
                    canonical = encode_data(build_data("live", result, budget=budget))
                    self.assertEqual(streamed, canonical)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-m", "rescan", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    def test_scan_stat_line_and_outputs(self):
        live = prepare_case("01-content-only")
        out = os.path.join(VAR, "cli")
        os.makedirs(out, exist_ok=True)
        changes = os.path.join(out, "changes.txt")
        data = os.path.join(out, "data.json")
        newsnap = os.path.join(out, "new.tsv")
        proc = self.run_cli(
            "scan", "--tree", live,
            "--snapshot", os.path.join(TREES, "01-content-only", "snapshot.tsv"),
            "--changes", changes, "--data", data,
            "--budget", "3", "--write-snapshot", newsnap,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "STAT|hashes=2|budget=3|added=0|modified=2|removed=0|unchanged=8",
        )
        with open(changes, encoding="utf-8") as fh:
            with open(os.path.join(EXPECTED, "01-content-only.txt"), encoding="utf-8") as exp:
                self.assertEqual(fh.read(), exp.read())
        payload = json.load(open(data, encoding="utf-8"))
        self.assertEqual(payload["hashes"], 2)
        load(newsnap)  # must be a valid snapshot

    def test_scan_without_budget_prints_dash(self):
        live = prepare_case("03-rename")
        out = os.path.join(VAR, "cli2")
        os.makedirs(out, exist_ok=True)
        proc = self.run_cli(
            "scan", "--tree", live,
            "--snapshot", os.path.join(TREES, "03-rename", "snapshot.tsv"),
            "--changes", os.path.join(out, "c.txt"),
            "--data", os.path.join(out, "d.json"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("|budget=-|", proc.stdout)

    def test_corrupt_snapshot_exit_nonzero_no_outputs(self):
        live = prepare_case("06-add-remove")
        good = open(os.path.join(TREES, "06-add-remove", "snapshot.tsv")).read()
        out = os.path.join(VAR, "cli-bad")
        os.makedirs(out, exist_ok=True)
        lines = good.split("\n")
        variants = []
        digest = lines[-2]
        bad = list(lines)
        bad[-2] = ("0" if digest[0] != "0" else "1") + digest[1:]
        variants.append("\n".join(bad))
        bad = list(lines)
        bad[1] = "6"
        variants.append("\n".join(bad))
        variants.append("\n".join(lines[:5] + lines[6:]))
        bad = list(lines)
        bad[0] = "#rescan-snapshot v2"
        variants.append("\n".join(bad))
        for index, blob in enumerate(variants):
            with self.subTest(variant=index):
                snap_path = os.path.join(out, f"bad{index}.tsv")
                with open(snap_path, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                changes = os.path.join(out, f"c{index}.txt")
                data = os.path.join(out, f"d{index}.json")
                proc = self.run_cli(
                    "scan", "--tree", live, "--snapshot", snap_path,
                    "--changes", changes, "--data", data,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(os.path.exists(changes))
                self.assertFalse(os.path.exists(data))

    def test_output_inside_tree_rejected(self):
        live = prepare_case("06-add-remove")
        proc = self.run_cli(
            "scan", "--tree", live,
            "--snapshot", os.path.join(TREES, "06-add-remove", "snapshot.tsv"),
            "--changes", os.path.join(live, "changes.txt"),
            "--data", os.path.join(VAR, "nowhere.json"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(live, "changes.txt")))


    def test_unreadable_tree_fails_without_outputs(self):
        out = os.path.join(VAR, "cli-noroot")
        os.makedirs(out, exist_ok=True)
        changes = os.path.join(out, "c.txt")
        data = os.path.join(out, "d.json")
        proc = self.run_cli(
            "scan", "--tree", os.path.join(VAR, "no-such-tree-xyz"),
            "--snapshot", os.path.join(TREES, "06-add-remove", "snapshot.tsv"),
            "--changes", changes, "--data", data,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(changes))
        self.assertFalse(os.path.exists(data))

    def test_snapshot_command_round_trip(self):
        live = prepare_case("02-mtime-only")
        out = os.path.join(VAR, "cli3")
        os.makedirs(out, exist_ok=True)
        snap_path = os.path.join(out, "base.tsv")
        proc = self.run_cli("snapshot", "--tree", live, "--out", snap_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snapshot = load(snap_path)
        result, _ = scan(snapshot, live)
        self.assertEqual(result.hashes, 0)
        self.assertEqual(len(result.unchanged), 12)
        self.assertEqual(result.added, [])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.removed, [])


class EngineEdgeTests(unittest.TestCase):
    def _make_tree(self, files):
        base = os.path.join(VAR, "edge")
        if os.path.exists(base):
            shutil.rmtree(base)
        os.makedirs(base)
        for rel, content in files.items():
            path = os.path.join(base, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(content)
        return base

    def test_same_size_same_mtime_skips_content(self):
        base = self._make_tree({"a.txt": b"same-bytes"})
        stat = os.stat(os.path.join(base, "a.txt"))
        snapshot = Snapshot({"a.txt": Entry(10, stat.st_mtime_ns, "0" * 64)})
        # Digest deliberately wrong: rule 3 must not read content at all.
        result, new_snapshot = scan(snapshot, base, write_snapshot=True)
        self.assertEqual(result.unchanged, ["a.txt"])
        self.assertEqual(result.hashes, 0)
        self.assertEqual(new_snapshot.entries["a.txt"].sha256, "0" * 64)

    def test_one_nanosecond_difference_triggers_hash(self):
        base = self._make_tree({"a.txt": b"content"})
        stat = os.stat(os.path.join(base, "a.txt"))
        from rescan import hash_file

        digest = hash_file(os.path.join(base, "a.txt"))
        snapshot = Snapshot({"a.txt": Entry(7, stat.st_mtime_ns - 1, digest)})
        result, _ = scan(snapshot, base)
        self.assertEqual(result.hashes, 1)
        self.assertEqual(result.unchanged, ["a.txt"])

    def test_empty_tree(self):
        base = self._make_tree({})
        result, new_snapshot = scan(Snapshot({}), base, write_snapshot=True)
        self.assertEqual(result.hashes, 0)
        self.assertEqual(result.changes_lines(), "")
        self.assertEqual(new_snapshot.entries, {})


if __name__ == "__main__":
    unittest.main()
