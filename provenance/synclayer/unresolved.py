"""Read-only inspection of sync events awaiting project resolution.

The default report exposes only stable opaque identifiers and opaque repository
labels. Pass ``--show-paths`` for an explicit local view of the unresolved cwd
values needed to add a project mapping. The command never writes sync state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from provenance import __version__, memory as M
from provenance.synclayer import paths, resolver, worker


FORMAT_VERSION = 1


class UnresolvedError(RuntimeError):
    """Protected sync state could not be inspected safely."""


def _required_root(explicit=None):
    value = explicit if explicit is not None else os.environ.get("PROVENANCE_HOME")
    if value is None or not str(value).strip():
        raise UnresolvedError("Provenance home is required")
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise UnresolvedError("Provenance home is invalid") from exc
    if not root.is_dir():
        raise UnresolvedError("Provenance home must be an existing directory")
    return root


def _queued_events(root):
    state = paths.sync_state(root)
    events = worker._read_jsonl(paths.inflight_file(root))
    if state.exists():
        for claim in sorted(state.glob("queue.claim.*.jsonl")):
            events += worker._read_jsonl(claim)
    events += worker._read_jsonl(paths.queue_file(root))
    return events


def _resolution_id(event):
    identity = worker.summary_id_for(event)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"resolution-{digest}"


def _repo_label(event, resolution_id):
    normalized = resolver._norm((event or {}).get("cwd"))
    identity = normalized or resolution_id
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"repo-{digest}"


def inspect(root=None, *, show_paths=False):
    """Return unresolved events without modifying protected or authoritative state."""
    root = _required_root(root)
    try:
        projects = M.load_projects(root)
        by_summary = {}
        for event in _queued_events(root):
            by_summary[worker.summary_id_for(event)] = event
    except (OSError, UnicodeError, ValueError, worker.WorkerStateError) as exc:
        raise UnresolvedError("Protected sync state could not be read safely") from exc

    unresolved = []
    for _summary_id, event in sorted(by_summary.items()):
        if worker.project_for_event(event, root, projects=projects) is not None:
            continue
        resolution_id = _resolution_id(event)
        item = {
            "resolution_id": resolution_id,
            "repo_label": _repo_label(event, resolution_id),
        }
        if show_paths:
            cwd = event.get("cwd")
            item["cwd"] = cwd if isinstance(cwd, str) else None
        unresolved.append(item)
    return unresolved


def report(root=None, *, show_paths=False):
    items = inspect(root, show_paths=show_paths)
    return {
        "format_version": FORMAT_VERSION,
        "operation": "sync.unresolved",
        "generated_by": {"name": "provenance", "version": __version__},
        "status": "ok",
        "exit_code": 0,
        "count": len(items),
        "paths": "included" if show_paths else "suppressed",
        "writes": False,
        "unresolved": items,
    }


def _error_report():
    return {
        "format_version": FORMAT_VERSION,
        "operation": "sync.unresolved",
        "generated_by": {"name": "provenance", "version": __version__},
        "status": "blocked",
        "exit_code": 1,
        "count": 0,
        "paths": "suppressed",
        "writes": False,
        "unresolved": [],
        "errors": [{"code": "unresolved_state_failed"}],
    }


def _format_text(payload):
    lines = [f"Unresolved project mappings: {payload['count']}"]
    for item in payload["unresolved"]:
        line = f"- {item['resolution_id']}  {item['repo_label']}"
        if "cwd" in item:
            line += "  cwd=" + json.dumps(item["cwd"], ensure_ascii=False)
        lines.append(line)
    lines.append("Read-only: no sync state was changed.")
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance-home", type=Path)
    parser.add_argument("--show-paths", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        payload = report(args.provenance_home, show_paths=args.show_paths)
    except UnresolvedError:
        payload = _error_report()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif payload["exit_code"]:
        print("Unresolved project state could not be inspected safely.", file=sys.stderr)
    else:
        print(_format_text(payload))
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
