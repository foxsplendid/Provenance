# -*- coding: utf-8 -*-
"""Safely inspect sync scaffolds awaiting an in-session fill (sync-layer.md §4).

The M2 worker leaves a `sync-state/pending/<id>/scaffold.json` + `PROMPT.md` and
waits for a sibling `fill.json`. This command lists safe summary IDs and can return
one allowlisted, sanitized scaffold as JSON. The in-session agent submits the result
through `prov-sync-fill`; it never receives or constructs a protected filesystem
path. There is no headless summarizer or package-owned model call.

This module is the read-only inspection side of that flow. Once a fill exists,
`prov-sync-worker --run` assembles it into
`session-summary/1.0` and applies it (auto timeline -> project note; gated claims
-> draft).

Stdlib only, no network, no subprocess.
"""
import argparse
import json
import sys
from pathlib import Path

from provenance.synclayer import paths, pending_fill


def find_pending(root=None):
    """Pending dirs that have a scaffold but no fill yet (sorted; idempotent)."""
    pend = paths.pending_dir(root)
    return [pend / summary_id for summary_id in pending_fill.list_pending_ids(root=root)]


def build_parser():
    parser = argparse.ArgumentParser(
        description="List safe summary ids awaiting an in-session fill."
    )
    parser.add_argument("summary_id", nargs="?")
    parser.add_argument("--provenance-home", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.summary_id is not None:
            scaffold = pending_fill.read_pending_scaffold(
                args.summary_id, root=args.provenance_home
            )
            summary_ids = [args.summary_id]
        else:
            pending = find_pending(args.provenance_home)
            summary_ids = [pdir.name for pdir in pending]
            scaffold = None
    except pending_fill.PendingFillError as exc:
        operation = "pending.get" if args.summary_id is not None else "pending.list"
        if args.json_output:
            payload = {
                "format_version": 1,
                "operation": operation,
                "status": "error",
                "exit_code": 1,
                "code": exc.code,
            }
            if args.summary_id is None:
                payload["count"] = 0
                payload["summary_ids"] = []
            print(
                json.dumps(payload, ensure_ascii=False)
            )
        else:
            print(f"pending inspection refused: {exc.code}", file=sys.stderr)
        return 1
    if args.summary_id is not None and not args.json_output:
        print("pending scaffold is available; use --json to read it safely")
        return 0
    if args.json_output:
        operation = "pending.get" if scaffold is not None else "pending.list"
        payload = {
            "format_version": 1,
            "operation": operation,
            "status": "ok",
            "exit_code": 0,
        }
        if scaffold is not None:
            payload["scaffold"] = scaffold
        else:
            payload["count"] = len(summary_ids)
            payload["summary_ids"] = summary_ids
        print(
            json.dumps(payload, ensure_ascii=False)
        )
        return 0
    print(f"pending: {len(summary_ids)} scaffold(s) awaiting an in-session fill")
    for summary_id in summary_ids:
        print(f"  {summary_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
