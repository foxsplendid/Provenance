# -*- coding: utf-8 -*-
"""Sync-layer on-disk locations (all under $PROVENANCE_HOME/sync-state/).

`sync-state/` is the protected layer (NTFS deny-read for <restricted-user>; it holds the
unredacted cwd + the drafts, sync-layer.md §6). Centralizing the paths here keeps
the hook (enqueue), the worker, and the M1 progress-log journal all agreeing on one
tree. Every helper takes an optional `root` so tests point at a tmp dir.

Stdlib only, no network.
"""
from provenance import common as C


def sync_state(root=None):
    return (root or C.data_root()) / "sync-state"


def queue_file(root=None):
    """Append-only event log the hook writes and the worker drains."""
    return sync_state(root) / "queue.jsonl"


def inflight_file(root=None):
    """Worker working set: events claimed from the queue but not yet fully applied
    (e.g. awaiting an external fill). Durable so a crash re-runs idempotently."""
    return sync_state(root) / "inflight.jsonl"


def lock_file(root=None):
    """Single-serializing-worker lock (sync-layer.md §1)."""
    return sync_state(root) / "worker.lock"


def pull_lock_file(root=None):
    """Whole-pipeline lock for the public on-demand pull entrypoint."""
    return sync_state(root) / "pull.lock"


def pending_dir(root=None):
    """Per-summary scaffold + PROMPT + the component-accepted fill."""
    return sync_state(root) / "pending"


def drafts_dir(root=None):
    """session-summary/1.0 staging (approval_state=draft; sync-layer.md §4)."""
    return sync_state(root) / "drafts"


def notes_dir(root=None):
    """note/1.0 store (sync-layer.md §4/§6) — personal-zone free-text (Inbox/,
    _planning/) ingested as `private` notes. Under sync-state/ so it is in the
    protected layer (NTFS deny-read for <restricted-user>) and outside inbox/, which keeps
    these notes out of the profile/persona pipeline."""
    return sync_state(root) / "notes"


def journal_dir(root=None):
    """Pre-state journal for the M1 append-only writer (matches
    progress_log.default_journal_dir() under the default root)."""
    return sync_state(root) / "journal"


def rejected_dir(root=None):
    """Quarantine for malformed/oversize openclaw drops (sync-layer.md §9). Each
    rejected drop keeps a sibling `<name>.reason` note; the dir is budget-pruned
    (age + count) so a misbehaving VM cannot grow disk without bound."""
    return sync_state(root) / "_rejected"


def openclaw_processed_dir(root=None):
    """Backup of raw openclaw drops after a successful ingest (the drop area is the
    VM's own write surface, not an authoritative file). Inside sync-state so the raw,
    un-sanitized text is covered by the NTFS deny-read (§6); budget-pruned."""
    return sync_state(root) / "_openclaw-processed"


def errors_log(root=None):
    """Best-effort error sink for the fire-and-forget enqueue hook."""
    return sync_state(root) / "_enqueue-errors.log"


def agent_scan_index(root=None):
    """State file for on-demand local-agent log scans."""
    return sync_state(root) / "agent-scan-index.json"


