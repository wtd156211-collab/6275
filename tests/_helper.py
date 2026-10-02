"""测试公用工具：样例树只从 samples/ 读，所有写入都落在 var/ 下。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples", "trees")
VAR = os.path.join(REPO_ROOT, "var")

CASES = [
    "01-content-only",
    "02-mtime-only",
    "03-rename",
    "04-dir-move",
    "05-restore-mtime",
    "06-add-remove",
]

BUDGETS: Dict[str, int] = {
    "01-content-only": 3,
    "02-mtime-only": 4,
    "03-rename": 2,
    "04-dir-move": 4,
    "05-restore-mtime": 5,
    "06-add-remove": 4,
}


def workdir(name: str) -> str:
    path = os.path.join(VAR, "unittest", name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def prepare_case(case: str, work: Optional[str] = None) -> str:
    """把样例 live/ 复制到 var/ 并按 live.times.tsv 复原每个文件的 mtime。"""
    work = work or workdir(case)
    live = os.path.join(work, "live")
    shutil.copytree(os.path.join(SAMPLES, case, "live"), live)
    times_path = os.path.join(SAMPLES, case, "live.times.tsv")
    with open(times_path, encoding="utf-8") as handle:
        for line in handle:
            rel, mtime_ns = line.rstrip("\n").split("\t")
            os.utime(os.path.join(live, rel), ns=(int(mtime_ns), int(mtime_ns)))
    return live


def snapshot_path(case: str) -> str:
    return os.path.join(SAMPLES, case, "snapshot.tsv")


def expected_changes(case: str) -> bytes:
    with open(
        os.path.join(REPO_ROOT, "samples", "expected", f"{case}.txt"), "rb"
    ) as handle:
        return handle.read()


def run_cli(args: List[str], env: Optional[dict] = None) -> subprocess.CompletedProcess:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rescan", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged_env,
    )


def parse_stat(line: str) -> Dict[str, str]:
    assert line.startswith("STAT|"), line
    fields = {}
    for item in line.rstrip("\n").split("|")[1:]:
        key, value = item.split("=", 1)
        fields[key] = value
    return fields
