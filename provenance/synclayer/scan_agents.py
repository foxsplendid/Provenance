# -*- coding: utf-8 -*-
"""On-demand local coding-agent scan for the sync layer.

This is the manual, agent-callable bridge for tools such as Codex that do not use
the Claude Code SessionEnd hook. It scans stable local rollout logs, turns each
new finished session into the same queue event shape as the hook, and stops.

It does not summarize, approve, edit Obsidian notes, register scheduled tasks, or
run in the background. The normal worker / approval flow does those later when a
user asks an agent to update the board.

Default safety filters are conservative: only sessions that resolve to registered
projects are queued, Desktop scratch paths are skipped, and the scan looks back 7
days. Use --project to narrow further, or explicit --include-unresolved,
--include-desktop, and --all-time flags to widen the scan.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C
from provenance.ingest import agents as IA
from provenance.synclayer import enqueue, paths, resolver

DEFAULT_STABLE_SECONDS = 300
DEFAULT_SINCE_DAYS = 7
DEFAULT_EXCLUDE_PARTS = {"desktop"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _home(home=None):
    return Path(home or os.environ.get("USERPROFILE") or Path.home())


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _sig(path):
    st = Path(path).stat()
    return [st.st_size, int(st.st_mtime)]


def _stable(path, *, now_ts=None, stable_seconds=DEFAULT_STABLE_SECONDS):
    now_ts = time.time() if now_ts is None else now_ts
    return now_ts - Path(path).stat().st_mtime >= stable_seconds


def codex_rollouts(home=None, *, codex_home=None):
    """Return rollout logs from a CODEX_HOME or a legacy profile home.

    ``codex_home`` is the actual Codex data root, so sessions live directly under
    ``<codex_home>/sessions``. ``home`` remains the backward-compatible profile
    input and therefore resolves to ``<home>/.codex/sessions``. With neither
    argument, ``CODEX_HOME`` is honored before deriving ``.codex`` from the current
    profile.
    """
    actual_home = codex_home
    if actual_home is None and home is None:
        actual_home = os.environ.get("CODEX_HOME") or None
    if actual_home is not None:
        base = Path(actual_home) / "sessions"
    else:
        base = _home(home) / ".codex" / "sessions"
    return sorted(base.glob("**/rollout-*.jsonl")) if base.exists() else []


def codex_event(path, *, now_iso=None):
    parsed = IA.parse_codex(path)
    if not parsed:
        return None
    return {
        "source": "codex",
        "session_id": parsed.get("session_id") or Path(path).stem,
        "transcript_path": str(Path(path)),
        "cwd": parsed.get("project"),
        "hook_event_name": "LocalScan",
        "reason": "stable-log-scan",
        "enqueued_at": now_iso or _now_iso(),
        "stdin_received": False,
    }


def _path_parts(value):
    if not value:
        return []
    return [p.lower() for p in Path(str(value)).parts]


def _is_excluded_cwd(cwd, *, include_desktop=False):
    if include_desktop:
        return False
    return any(part in DEFAULT_EXCLUDE_PARTS for part in _path_parts(cwd))


def _session_allowed(ev, *, project=None, include_unresolved=False, include_desktop=False):
    if _is_excluded_cwd(ev.get("cwd"), include_desktop=include_desktop):
        return False
    project_id = ev.get("project_id")
    if project and project_id != project:
        return False
    if project_id is None and not include_unresolved:
        return False
    return True


def scan_codex(root=None, *, home=None, stable_seconds=DEFAULT_STABLE_SECONDS,
               now_ts=None, run=False, limit=None, project=None,
               include_unresolved=False, include_desktop=False, since_days=DEFAULT_SINCE_DAYS,
               projects=None, codex_home=None):
    """Scan stable Codex rollout logs and optionally enqueue new sessions."""
    root = root or C.data_root()
    index_path = paths.agent_scan_index(root)
    index = _read_json(index_path)
    seen = index.setdefault("codex", {})
    events = []
    enqueued = 0
    repo_index = ids = None
    if projects is not None:
        repo_index, ids = resolver.build_index(projects)

    for fp in codex_rollouts(home, codex_home=codex_home):
        if limit is not None and len(events) >= limit:
            break
        if not _stable(fp, now_ts=now_ts, stable_seconds=stable_seconds):
            continue
        if since_days is not None:
            now = time.time() if now_ts is None else now_ts
            if now - fp.stat().st_mtime > since_days * 86400:
                continue
        sig = _sig(fp)
        key = str(fp)
        if seen.get(key) == sig:
            continue
        ev = codex_event(fp)
        if ev is None:
            continue
        if projects is None:
            ev["project_id"] = resolver.resolve_project(ev.get("cwd"), root)
        else:
            ev["project_id"] = resolver.resolve(ev.get("cwd"), repo_index, ids)
        if not _session_allowed(ev, project=project, include_unresolved=include_unresolved,
                                include_desktop=include_desktop):
            continue
        events.append(ev)
        if run:
            enqueue.enqueue(ev, root=root, respect_suppression=False)
            seen[key] = sig
            enqueued += 1

    if run:
        index["updated_at"] = _now_iso()
        _write_json(index_path, index)
    return {"source": "codex", "found": len(events), "enqueued": enqueued, "events": events}


def _parse_argv(argv):
    run = "--run" in argv
    include_unresolved = "--include-unresolved" in argv
    include_desktop = "--include-desktop" in argv
    all_time = "--all-time" in argv
    stable_seconds = DEFAULT_STABLE_SECONDS
    home = None
    limit = None
    project = None
    since_days = DEFAULT_SINCE_DAYS
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--stable-seconds" and i + 1 < len(argv):
            stable_seconds = int(argv[i + 1])
            i += 1
        elif a == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 1
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
            i += 1
        elif a == "--project" and i + 1 < len(argv):
            project = argv[i + 1]
            i += 1
        elif a == "--since-days" and i + 1 < len(argv):
            since_days = float(argv[i + 1])
            i += 1
        elif a == "--all-time":
            since_days = None
        i += 1
    return run, stable_seconds, home, limit, project, include_unresolved, include_desktop, since_days


def main():
    C.cli_help(__doc__)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run, stable_seconds, home, limit, project, include_unresolved, include_desktop, since_days = _parse_argv(sys.argv[1:])
    res = scan_codex(home=home, stable_seconds=stable_seconds, run=run, limit=limit,
                     project=project, include_unresolved=include_unresolved,
                     include_desktop=include_desktop, since_days=since_days)
    tail = "" if run else "  (dry-run; pass --run to enqueue)"
    print(f"scan codex: found={res['found']} enqueued={res['enqueued']}{tail}")
    for ev in res["events"][:20]:
        print(f"  {ev.get('project_id') or '-':18} {ev.get('session_id')}  {ev.get('cwd') or ''}")
    if len(res["events"]) > 20:
        print(f"  ... {len(res['events']) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
