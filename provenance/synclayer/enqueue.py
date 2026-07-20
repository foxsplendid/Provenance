# -*- coding: utf-8 -*-
"""CC SessionEnd enqueue entrypoint (sync-layer.md §1/§8).

Registered as the SessionEnd hook command. Reads the hook's JSON payload from
STDIN (snake_case fields: session_id / transcript_path / cwd / hook_event_name /
reason — see docs/sync-layer-wiring.md), resolves the owning project
(§7), appends ONE event line to sync-state/queue.jsonl, and returns immediately.

It does the absolute minimum: ENQUEUE ONLY. No heavy work, no authoritative-store
write (those happen later in the single serializing worker). Fire-and-forget — it
MUST NOT raise or block teardown, so every failure is swallowed (best-effort logged
to sync-state/_enqueue-errors.log) and the process still exits 0.

Production wiring (docs/sync-layer-wiring.md): register SessionEnd
ONLY, invoke `python` directly (not pwsh) with $PROVENANCE_HOME set, pointing at
`python -m provenance.synclayer.enqueue`.

Self-feed guard (sync-layer.md §8). A session spawned by the sync layer (or any
tooling session run inside $PROVENANCE_HOME) also ends and fires this same SessionEnd
hook, and -- because $PROVENANCE_HOME is itself a resolvable project -- would
re-enqueue itself, the worker would scaffold it, and so on: an infinite self-feeding
loop that floods the store with self-referential noise (a cwd filter cannot stop it,
since Provenance is a real project). Such a session therefore sets
PROV_SYNC_NO_ENQUEUE=1 in its environment; when that var is truthy, enqueue() is a
no-op. Real user sessions never carry it and enqueue normally. This env guard is the
primary, robust defense; disabling/filtering the hook is only a secondary note.

Stdlib only, no network.
"""
import json
import os
import sys
from datetime import datetime, timezone

from provenance import common as C
from provenance.synclayer import paths, resolver

# the SessionEnd STDIN fields we persist (snake_case, per the findings doc)
DEFAULT_SOURCE = "claude-code"
EVENT_FIELDS = ("session_id", "transcript_path", "cwd", "hook_event_name", "reason", "source")

# Set truthy in an agent-driven maintenance session to suppress self-enqueue.
NO_ENQUEUE_ENV = "PROV_SYNC_NO_ENQUEUE"
_FALSEY = {"", "0", "false", "no", "off"}


def enqueue_suppressed():
    """True when NO_ENQUEUE_ENV is set to a truthy value (anything but a falsey
    sentinel). Honored by enqueue() to break the summarizer self-feed loop."""
    return os.environ.get(NO_ENQUEUE_ENV, "").strip().lower() not in _FALSEY


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_event(stdin_text):
    """Parse the hook STDIN payload into a normalized event dict. Tolerant: an
    empty / unparseable payload still yields an event, flagged stdin_received=False
    (surfaces the Windows pwsh-stdin bug rather than silently capturing nothing)."""
    raw = (stdin_text or "").strip()
    obj = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            pass
    event = {k: obj.get(k) for k in EVENT_FIELDS}
    event["source"] = event.get("source") or DEFAULT_SOURCE
    event["enqueued_at"] = _now_iso()
    event["stdin_received"] = bool(raw)
    return event


def enqueue(event, root=None, *, respect_suppression=True):
    """Resolve the project_id and append the event as one JSONL line. Returns the
    event (now carrying project_id). The cwd stays in the queue (protected layer);
    it is only ever dropped before the summarizer sees it (§6).

    Hook callers honor NO_ENQUEUE_ENV by default so a sync-layer session does not
    re-enqueue itself. Explicit local scanners pass ``respect_suppression=False``
    because a user-requested scan must not inherit the hook-only guard. Returns the
    event unchanged without touching the queue when suppression applies."""
    if respect_suppression and enqueue_suppressed():
        sys.stderr.write(
            f"[prov-sync] enqueue skipped: {NO_ENQUEUE_ENV} set "
            "(sync-layer self-feed guard)\n")
        return dict(event)
    event = dict(event)
    event["source"] = event.get("source") or DEFAULT_SOURCE
    try:
        event["project_id"] = resolver.resolve_project(event.get("cwd"), root)
    except Exception:
        event["project_id"] = None
    qf = paths.queue_file(root)
    qf.parent.mkdir(parents=True, exist_ok=True)
    with open(qf, "a", encoding="utf-8") as f:  # O_APPEND: one short single-line write
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def main():
    C.cli_help(__doc__)
    try:
        text = sys.stdin.read()
    except Exception:
        text = ""
    try:
        enqueue(read_event(text))
    except Exception as e:  # never raise: fire-and-forget must not block teardown
        try:
            ef = paths.errors_log()
            ef.parent.mkdir(parents=True, exist_ok=True)
            with open(ef, "a", encoding="utf-8") as f:
                f.write(f"{_now_iso()} enqueue error: {e!r}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

