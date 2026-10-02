"""Builds data.json for the web page from an engine ScanResult."""

from __future__ import annotations

import json

from .engine import COLORS, STATUSES


def build_data(root_name, result, budget=None):
    """Assemble the deterministic dict that the page renders."""

    counts = result.counts()
    nodes = [
        {
            "path": node.path,
            "kind": node.kind,
            "depth": node.depth,
            "status": node.status,
        }
        for node in result.nodes
    ]
    return {
        "tree": root_name,
        "budget": budget,
        "hashes": result.hashes,
        "counts": {status: counts[status] for status in STATUSES},
        "colors": {status: COLORS[status] for status in STATUSES},
        "nodes": nodes,
    }


def _dump_string(value):
    return json.dumps(value, ensure_ascii=False)


def dump_data(write, root_name, result, budget=None):
    """Stream the data.json document through ``write(str)``.

    Produces exactly the same bytes as ``encode_data(build_data(...))``
    (json.dumps with indent=2) without materializing the node dicts,
    so huge trees stay within the memory budget.
    """

    counts = result.counts()
    write("{\n")
    write(f'  "tree": {_dump_string(root_name)},\n')
    write(f'  "budget": {"null" if budget is None else int(budget)},\n')
    write(f'  "hashes": {result.hashes},\n')
    write('  "counts": {\n')
    for index, status in enumerate(STATUSES):
        comma = "," if index < len(STATUSES) - 1 else ""
        write(f'    {_dump_string(status)}: {counts[status]}{comma}\n')
    write("  },\n")
    write('  "colors": {\n')
    for index, status in enumerate(STATUSES):
        comma = "," if index < len(STATUSES) - 1 else ""
        write(f'    {_dump_string(status)}: {_dump_string(COLORS[status])}{comma}\n')
    write("  },\n")
    if not result.nodes:
        write('  "nodes": []\n')
    else:
        write('  "nodes": [\n')
        for index, node in enumerate(result.nodes):
            comma = "," if index < len(result.nodes) - 1 else ""
            status = "null" if node.status is None else _dump_string(node.status)
            write(
                "    {\n"
                f'      "path": {_dump_string(node.path)},\n'
                f'      "kind": {_dump_string(node.kind)},\n'
                f'      "depth": {node.depth},\n'
                f'      "status": {status}\n'
                f"    }}{comma}\n"
            )
        write("  ]\n")
    write("}\n")


def encode_data(data):
    """Deterministic UTF-8 JSON bytes, LF, with a trailing newline."""

    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    return (text + "\n").encode("utf-8")


def encode_result(root_name, result, budget=None):
    """Encode a ScanResult straight to data.json bytes (streaming equivalent)."""

    parts = []
    dump_data(parts.append, root_name, result, budget)
    return "".join(parts).encode("utf-8")
