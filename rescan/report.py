"""由扫描结果生成页面数据（data.json 结构）与变更集合文本。"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterator, List, Optional, Tuple

from .scanner import COLORS, ScanResult

# (path, kind, depth, status)
NodeRow = Tuple[str, str, int, Optional[str]]


def iter_node_rows(result: ScanResult) -> Iterator[NodeRow]:
    """把四类文件状态展开成树节点行（按文件路径序流出，未全局排序）。

    - 每个文件一个 ``file`` 节点，其所有祖先目录各一个 ``dir`` 节点；
    - 已删除文件的父目录（哪怕磁盘上已不存在）也会出现；
    - ``depth`` 是相对扫描根的层级，根的直接子项为 0；
    - 目录 ``status`` 为 None，文件取四类之一。
    """
    seen_dirs = set()
    for path, status in result.iter_status():
        parts = path.split("/")
        for i in range(1, len(parts)):
            dir_path = "/".join(parts[:i])
            if dir_path not in seen_dirs:
                seen_dirs.add(dir_path)
                yield (dir_path, "dir", i - 1, None)
        yield (path, "file", len(parts) - 1, status)


def sorted_node_rows(result: ScanResult) -> List[NodeRow]:
    """节点行按路径码点升序（目录与文件统一排序）。"""
    return sorted(iter_node_rows(result), key=lambda row: row[0])


def _node_to_dict(row: NodeRow) -> dict:
    path, kind, depth, status = row
    return {"path": path, "kind": kind, "depth": depth, "status": status}


def build_nodes(result: ScanResult) -> List[dict]:
    """节点字典数组，按路径码点升序。"""
    return [_node_to_dict(row) for row in sorted_node_rows(result)]


def build_data(
    result: ScanResult,
    tree_name: str,
    budget: Optional[int],
) -> dict:
    """组装 data.json 的完整结构（字段顺序固定，保证输出确定）。"""
    return {
        "tree": tree_name,
        "budget": budget,
        "hashes": result.hashes,
        "counts": result.counts(),
        "colors": dict(COLORS),
        "nodes": build_nodes(result),
    }


def iter_data_json_chunks(
    result: ScanResult,
    tree_name: str,
    budget: Optional[int],
    chunk_size: int = 1 << 20,
) -> Iterator[bytes]:
    """流式产出 data.json 的 UTF-8 字节块（紧凑格式，不为大树整棵驻留内存）。"""
    head = {
        "tree": tree_name,
        "budget": budget,
        "hashes": result.hashes,
        "counts": result.counts(),
        "colors": dict(COLORS),
    }
    buffer = json.dumps(head, ensure_ascii=False)[:-1] + ',"nodes":['
    first = True
    for row in sorted_node_rows(result):
        if not first:
            buffer += ","
        first = False
        buffer += json.dumps(_node_to_dict(row), ensure_ascii=False)
        if len(buffer) >= chunk_size:
            yield buffer.encode("utf-8")
            buffer = ""
    buffer += "]}\n"
    yield buffer.encode("utf-8")


def dump_data_json(data: dict) -> bytes:
    """序列化为 UTF-8 无 BOM、LF、末行有换行的 JSON 字节串。"""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    return (text + "\n").encode("utf-8")


def iter_changes_lines(result: ScanResult) -> Iterator[str]:
    """变更集合文本行：``<标记> <路径>``，按路径升序。"""
    for path, mark in result.iter_marked():
        yield f"{mark} {path}"


def dump_changes(result: ScanResult) -> bytes:
    """变更集合文本：每行 ``<标记> <路径>``，按路径升序，末行有换行。"""
    lines = result.changes_lines()
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def tree_name_of(root: str) -> str:
    """扫描根目录名；根目录本身（如 '/'）退化为规范化后的路径串。"""
    normalized = os.path.normpath(root)
    name = os.path.basename(normalized)
    return name if name else normalized
