"""rescan: 增量目录扫描库。

拿上一次落盘的快照与当前目录树比对，按分级判断链产出
新增 / 修改 / 删除 / 未变 四类变更集合，并统计本轮读取内容的文件数。
只依赖 Python 标准库。
"""

from .scanner import ScanResult, diff, scan
from .snapshot import Snapshot, SnapshotError, load_snapshot, save_snapshot

__all__ = [
    "ScanResult",
    "Snapshot",
    "SnapshotError",
    "diff",
    "load_snapshot",
    "save_snapshot",
    "scan",
]

__version__ = "1.0.0"
