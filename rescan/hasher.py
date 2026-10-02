"""按块计算文件内容的 SHA-256，并统计本轮读过内容的文件个数。"""

from __future__ import annotations

import hashlib

_CHUNK_SIZE = 1024 * 1024


def hash_file(path: str) -> str:
    """流式读取文件，返回原始字节的 SHA-256 小写十六进制摘要。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class HashCounter:
    """记录一次扫描中「读过内容的文件」集合；同一文件只计一次。"""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def hash_file(self, rel_path: str, abs_path: str) -> str:
        """对 rel_path 对应的磁盘文件算摘要；重复调用同一 rel_path 仍只计一次。"""
        self._seen.add(rel_path)
        return hash_file(abs_path)

    @property
    def count(self) -> int:
        return len(self._seen)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(self._seen)
