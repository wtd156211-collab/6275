"""Command line entry: ``python -m rescan ...``"""

from __future__ import annotations

import argparse
import os
import sys

from .engine import hash_file, scan, walk_tree
from .errors import RescanError
from .page import dump_data
from .snapshot import Entry, Snapshot, atomic_writer, load, save


def _root_name(tree):
    return os.path.basename(os.path.abspath(os.fspath(tree)).rstrip(os.sep)) or tree


def _not_inside_tree(path, tree):
    out = os.path.abspath(os.fspath(path))
    root = os.path.abspath(os.fspath(tree))
    try:
        common = os.path.commonpath([out, root])
    except ValueError:
        return
    if common == root and out != root:
        raise RescanError(f"output path must not be inside the scanned tree: {path}")


def _nonneg_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return value


def cmd_snapshot(args):
    _not_inside_tree(args.out, args.tree)
    files, _dirs = walk_tree(args.tree)
    entries = {}
    for path in sorted(files):
        size, mtime_ns = files[path]
        entries[path] = Entry(size, mtime_ns, hash_file(os.path.join(args.tree, path)))
    save(args.out, Snapshot(entries))
    return 0


def cmd_scan(args):
    for out_path in (args.changes, args.data, args.write_snapshot):
        if out_path is not None:
            _not_inside_tree(out_path, args.tree)

    snapshot = load(args.snapshot)
    result, new_snapshot = scan(
        snapshot, args.tree, write_snapshot=args.write_snapshot is not None
    )
    del snapshot  # free the old baseline before writing outputs

    with atomic_writer(args.changes) as handle:
        for line in result.iter_change_lines():
            handle.write(line.encode("utf-8") + b"\n")
    with atomic_writer(args.data) as handle:
        dump_data(
            lambda text: handle.write(text.encode("utf-8")),
            _root_name(args.tree),
            result,
            budget=args.budget,
        )
    if args.write_snapshot is not None:
        save(args.write_snapshot, new_snapshot)

    budget = "-" if args.budget is None else str(args.budget)
    counts = result.counts()
    stat = (
        f"STAT|hashes={result.hashes}|budget={budget}"
        f"|added={counts['added']}|modified={counts['modified']}"
        f"|removed={counts['removed']}|unchanged={counts['unchanged']}"
    )
    print(stat)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m rescan")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="build a baseline snapshot from a tree")
    snap.add_argument("--tree", required=True)
    snap.add_argument("--out", required=True)
    snap.set_defaults(func=cmd_snapshot)

    scn = sub.add_parser("scan", help="diff a tree against a snapshot")
    scn.add_argument("--tree", required=True)
    scn.add_argument("--snapshot", required=True)
    scn.add_argument("--changes", required=True)
    scn.add_argument("--data", required=True)
    scn.add_argument("--budget", type=_nonneg_int, default=None)
    scn.add_argument("--write-snapshot", default=None, dest="write_snapshot")
    scn.set_defaults(func=cmd_scan)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RescanError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
