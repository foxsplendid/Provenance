# -*- coding: utf-8 -*-
"""Single serializing sync worker (sync-layer.md §1/§2).

On-demand and short-lived (no daemon, no sub-second loop). Under a lockfile it
drains sync-state/queue.jsonl; per event it advances a small state machine as far
as it can, idempotently:

  (1) ensure the raw transcript is captured  -> incremental, read-only ingest
                                                (python -m provenance.ingest.agents)
  (2) build the session-summary scaffold      -> pending/<id>/scaffold.json + PROMPT.md
        [ an external LLM/skill submits a fill through prov-sync-fill — the worker
          NEVER calls an LLM itself (like Steward review scaffold -> assemble) ]
  (3) once a fill is present: assemble session-summary/1.0 and APPLY:
        - auto `timeline`  -> append_entry() into the project-note progress-log (M1)
        - high-value claims -> staged in drafts/<id>.json (approval_state=draft, M3)

Safety (sync-layer.md §2): the note append journals pre-state and is dry-run by
default (`--run` to write). Idempotent on summary_id — a crash mid-drain re-runs
with no double effect. The timeline append is idempotent, and the draft is
written atomically only after that step completes; its existence is the durable
queue-retirement credential.

Privacy (§6): the scaffold never receives the absolute cwd.

Stdlib only, no network.
"""
import errno
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from provenance import common as C, memory as M
from provenance.synclayer import paths, resolver, summary
from provenance.synclayer import progress_log as PL


class WorkerBusy(RuntimeError):
    """Another worker already holds the lock."""


class WorkerStateError(RuntimeError):
    """Durable queue or lock state could not be changed safely."""


class LockReleaseError(WorkerStateError):
    """A held kernel lock did not release cleanly."""


# --- lockfile ---------------------------------------------------------------

LOCK_MAGIC = b"scriptorium-kernel-lock-v1\n"
LOCK_OFFSET = len(LOCK_MAGIC)


def _initialize_lock_file(fd):
    """Publish the complete protocol marker before this creator can contend."""
    os.lseek(fd, 0, os.SEEK_SET)
    written = 0
    while written < len(LOCK_MAGIC):
        count = os.write(fd, LOCK_MAGIC[written:])
        if count <= 0:
            raise OSError("short lock marker write")
        written += count
    os.fsync(fd)


def _acquire_file_lock(fd):
    try:
        # Windows byte-range locks are mandatory. Keep the sentinel outside the
        # marker bytes so an initializing creator can publish LOCK_MAGIC even if
        # an existing-file contender reaches the kernel lock first.
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN):
            raise WorkerBusy("another worker is active") from None
        raise WorkerStateError("sync lock could not be acquired") from exc


def _release_file_lock(fd):
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

class _Lock:
    """Cross-platform kernel lock backed by a persistent local lock file."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        created = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.fd = os.open(
                    str(self.path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
                created = True
            except FileExistsError:
                self.fd = os.open(
                    str(self.path), os.O_RDWR | getattr(os, "O_BINARY", 0)
                )
        except OSError as exc:
            raise WorkerStateError("sync lock file could not be opened") from exc
        try:
            if created:
                # Only the O_EXCL creator can prove that an empty file belongs to
                # this protocol. Publish the full marker before competing for the
                # kernel lock so a contender can never strand an empty lock file.
                _initialize_lock_file(self.fd)
            _acquire_file_lock(self.fd)
            os.lseek(self.fd, 0, os.SEEK_SET)
            existing = os.read(self.fd, len(LOCK_MAGIC) + 1)
            if existing != LOCK_MAGIC:
                # Empty is ambiguous: it may be an old O_EXCL worker between
                # create and PID write. Never adopt an existing legacy file.
                raise WorkerBusy("a legacy sync lock requires manual review")
        except (OSError, WorkerBusy, WorkerStateError) as exc:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            if isinstance(exc, (WorkerBusy, WorkerStateError)):
                raise
            raise WorkerStateError("sync lock file could not be initialized") from exc
        return self

    def __exit__(self, *exc_info):
        release_error = None
        if self.fd is not None:
            try:
                _release_file_lock(self.fd)
            except OSError as exc:
                release_error = exc
            try:
                os.close(self.fd)
            except OSError as exc:
                release_error = release_error or exc
            self.fd = None
        if release_error is not None:
            if exc_info[1] is not None:
                message = "The sync lock also failed to release cleanly."
                if hasattr(exc_info[1], "add_note"):
                    exc_info[1].add_note(message)
                else:
                    try:
                        setattr(
                            exc_info[1],
                            "_sync_lock_release_error",
                            LockReleaseError(message),
                        )
                    except (AttributeError, TypeError):
                        pass
            elif exc_info[0] is None:
                raise LockReleaseError("sync lock could not be released") from release_error
        return False


# --- jsonl helpers ----------------------------------------------------------

def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise WorkerStateError("sync JSONL could not be read safely") from exc
    out = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise WorkerStateError(
                f"sync JSONL contains an invalid record at line {line_number}"
            ) from exc
        if not isinstance(item, dict):
            raise WorkerStateError(
                f"sync JSONL contains a non-object record at line {line_number}"
            )
        out.append(item)
    return out


def _write_jsonl(path, items):
    path = Path(path)
    tmpname = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
        fd, tmpname = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmpname, str(path))
    except Exception as exc:
        if tmpname is not None:
            try:
                os.unlink(tmpname)
            except OSError:
                pass
        raise WorkerStateError("sync JSONL could not be written safely") from exc


def _reserve_claim_path(sync_state):
    try:
        fd, name = tempfile.mkstemp(
            dir=str(sync_state),
            prefix=f"queue.claim.{os.getpid()}.",
            suffix=".jsonl",
        )
    except OSError as exc:
        raise WorkerStateError("sync queue claim could not be reserved") from exc
    try:
        os.close(fd)
    except OSError as exc:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise WorkerStateError("sync queue claim could not be reserved") from exc
    return Path(name)


def _take_queue(root):
    """Atomically claim queued + inflight events into the durable inflight set and
    return them. A snapshot-rename of the queue (os.replace) avoids losing a
    concurrent hook append to a read-then-clear gap; orphan claim files from a prior
    crash are re-absorbed (dedup downstream by summary_id makes that harmless)."""
    ss = paths.sync_state(root)
    qf, inf = paths.queue_file(root), paths.inflight_file(root)
    items = _read_jsonl(inf)
    claims = sorted(ss.glob("queue.claim.*.jsonl"))      # leftovers from a prior crash
    for c in claims:
        items += _read_jsonl(c)
    if qf.exists():
        snap = _reserve_claim_path(ss)
        try:
            os.replace(str(qf), str(snap))               # atomic claim + clear
            claims.append(snap)
            items += _read_jsonl(snap)
        except OSError as exc:
            try:
                snap.unlink()
            except OSError:
                pass
            raise WorkerStateError("sync queue could not be claimed") from exc
    if items:
        _write_jsonl(inf, items)                         # persist BEFORE deleting claims
    for c in claims:
        try:
            c.unlink()
        except OSError as exc:
            raise WorkerStateError("sync queue claim could not be retired") from exc
    return items


def _write_inflight(root, remaining):
    inf = paths.inflight_file(root)
    if remaining:
        _write_jsonl(inf, remaining)
    elif inf.exists():
        try:
            inf.unlink()
        except OSError as exc:
            raise WorkerStateError("sync inflight state could not be retired") from exc


# --- event -> session identity ----------------------------------------------

def _stem_for(event):
    """The inbox stem for the event's session: '<session_id>-<sha1(transcript)[:8]>'
    — exactly how provenance.ingest.agents names the captured file."""
    sid = event.get("session_id") or ""
    tp = event.get("transcript_path") or ""
    if tp:
        h = hashlib.sha1(str(tp).encode("utf-8")).hexdigest()[:8]
        return f"{sid}-{h}"
    return sid


def source_for_event(event):
    """Return the coding-agent source for an event, preserving old CC events."""
    src = str((event or {}).get("source") or "claude-code").strip()
    return src if src in C.AGENT_SOURCES else "claude-code"


def summary_id_for(event):
    """Stable, idempotent summary id = the inbox conversation_id."""
    return f"{source_for_event(event)}-{_stem_for(event)}"


def _find_inbox_record(event, root):
    """Locate the captured (sanitized) inbox JSON for this session. Prefer the exact
    stem; fall back to a session_id glob (robust to transcript-path string drift)."""
    inbox = (root or C.data_root()) / "inbox" / source_for_event(event)
    exact = inbox / f"{_stem_for(event)}.json"
    if exact.exists():
        return exact
    sid = event.get("session_id") or ""
    if sid and inbox.exists():
        cands = sorted(inbox.glob(f"{sid}-*.json")) or sorted(inbox.glob(f"{sid}.json"))
        if cands:
            return cands[0]
    return None


def _created_for(event, record):
    return (record.get("updated_at") or record.get("created_at")
            or event.get("enqueued_at") or "")


def project_for_event(event, root, projects=None):
    """Return a registered event project, resolving legacy null ids lazily."""
    project_id = (event or {}).get("project_id")
    portfolio = M.load_projects(root) if projects is None else projects
    repo_index, ids = resolver.build_index(portfolio)
    for hint in (project_id, (event or {}).get("cwd")):
        resolved = resolver.resolve(hint, repo_index, ids)
        if resolved:
            return resolved
    return None


def _resolve_note_path(project_id, root=None, vault=None):
    """Map a project_id to its vault note path via projects.json `_source_file`.
    Returns None if the registered project has no writable vault note."""
    if not project_id:
        return None
    rec = M.load_projects(root).get(project_id)
    if not isinstance(rec, dict):
        return None
    fname = rec.get("_source_file")
    if not fname:
        return None
    vdir = Path(vault) if vault else (
        Path(os.environ["PROVENANCE_VAULT"]) if os.environ.get("PROVENANCE_VAULT") else None)
    if vdir is None:
        return None
    return vdir / "Projects" / fname


# --- ingest seam ------------------------------------------------------------

def _default_ingest(root):
    """Ensure the transcript is captured: incremental, read-only agent ingest.
    A seam — tests inject a no-op (the worker stays deterministic)."""
    import subprocess
    env = dict(os.environ)
    if root is not None:
        env["PROVENANCE_HOME"] = str(root)
    completed = subprocess.run(
        [sys.executable, "-m", "provenance.ingest.agents", "claude-code", "codex"],
        env=env,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"agent ingest failed with exit code {completed.returncode}"
        )


# --- apply ------------------------------------------------------------------

def apply_summary(summ, *, root=None, run=True, note_path=None):
    """Apply an assembled session-summary/1.0.

    1. append the auto timeline to the project-note progress-log via the M1
       append-only primitive (journaled, idempotent). If `note_path` is None
       (project note unavailable) or the note lacks the marker pair, the timeline is NOT
       written to any note — it lives in the draft only (Inbox route, §7).

    2. atomically stage drafts/<id>.json after the timeline step. It holds the
       complete summary and acts as the durable queue-retirement credential.

    dry-run by default returns the plan without writing."""
    root = root or C.data_root()
    project = summ.get("project") if isinstance(summ, dict) else None
    if not isinstance(project, str) or not project.strip():
        raise WorkerStateError("session summary requires a resolved project")
    sid = summ["summary_id"]
    draft_path = paths.drafts_dir(root) / f"{sid}.json"
    plan = {"summary_id": sid, "project": summ.get("project"),
            "draft": str(draft_path), "note": str(note_path) if note_path else None}

    if not run:
        plan["state"] = "would_apply"
        return plan

    # 1. auto timeline -> project note via the M1 append-only primitive
    if note_path is not None:
        block = summary.render_timeline_block(summ)
        res = PL.append_entry(note_path, sid, block, journal_dir=str(paths.journal_dir(root)))
        plan["append_status"] = res.status
        if res.status in (PL.REFUSED_NO_MARKER, PL.REFUSED_NO_FILE):
            plan["routed"] = "inbox-draft"   # no marker / no note: draft-only, never grow it
    else:
        plan["routed"] = "inbox-draft"       # valid project, but no writable note

    # 2. the draft is the completion credential, so write it atomically and last.
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        PL._atomic_write(
            draft_path,
            json.dumps(summ, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise WorkerStateError("sync draft could not be written safely") from exc

    plan["state"] = "applied"
    return plan


def _load_applied_draft(path, summary_id, session_id, expected_created):
    try:
        draft = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerStateError("sync draft could not be resumed safely") from exc
    if (
        not isinstance(draft, dict)
        or draft.get("schema_version") != summary.SUMMARY_SCHEMA
        or draft.get("summary_id") != summary_id
        or not isinstance(draft.get("created"), str)
        or not draft.get("created").strip()
        or draft.get("created") != expected_created
        or draft.get("approval_state") not in {"draft", "approved", "applied"}
        or draft.get("session_id") != session_id
        or not isinstance(draft.get("project"), str)
        or not draft.get("project").strip()
        or not isinstance(draft.get("timeline"), list)
        or not all(isinstance(item, str) for item in draft["timeline"])
        or not any(item.strip() for item in draft["timeline"])
    ):
        raise WorkerStateError("sync draft completion credential is invalid")
    return draft


def _timeline_needs_repair(note_path, summary_id):
    if note_path is None or not Path(note_path).exists():
        return False
    try:
        text = Path(note_path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise WorkerStateError("project timeline could not be inspected safely") from exc
    begin = text.find(PL.BEGIN_MARKER)
    if begin == -1:
        return False
    end = text.find(PL.END_MARKER, begin + len(PL.BEGIN_MARKER))
    if end == -1:
        return False
    return PL.entry_marker(summary_id) not in text[begin:end]


def _validate_scaffold_binding(
    scaffold, *, summary_id, project_id, session_id, expected_created
):
    """Bind all authoritative writes to the event that owns this pending item."""
    if (
        not isinstance(scaffold, dict)
        or scaffold.get("schema_version") != summary.SCAFFOLD_SCHEMA
        or scaffold.get("summary_id") != summary_id
        or "project" not in scaffold
        or (
            scaffold.get("project") is not None
            and (
                not isinstance(scaffold.get("project"), str)
                or not scaffold.get("project").strip()
            )
        )
        or scaffold.get("project") != project_id
        or scaffold.get("session_id") != session_id
        or not isinstance(scaffold.get("created"), str)
        or not scaffold.get("created").strip()
        or scaffold.get("created") != expected_created
    ):
        raise WorkerStateError("sync scaffold identity is invalid")
    return scaffold["project"]


def _load_inbox_record(path):
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerStateError("sync inbox record could not be read safely") from exc
    if not isinstance(record, dict):
        raise WorkerStateError("sync inbox record is invalid")
    return record


# --- per-event state machine ------------------------------------------------

def _advance(event, root, *, run, ingest_fn, vault=None, projects=None):
    """Advance ONE event as far as it can go this run. Returns a result dict whose
    `state` drives queue retention (only 'applied' is dropped)."""
    sid = summary_id_for(event)
    project_id = project_for_event(event, root, projects=projects)
    pdir = paths.pending_dir(root) / sid
    scaffold_path, fill_path = pdir / "scaffold.json", pdir / "fill.json"
    draft_path = paths.drafts_dir(root) / f"{sid}.json"

    if not isinstance(project_id, str) or not project_id.strip():
        return {"summary_id": sid, "state": "unresolved_project"}

    if draft_path.exists():
        record_path = _find_inbox_record(event, root)
        if record_path is None:
            raise WorkerStateError("sync draft completion source is unavailable")
        replay_record = _load_inbox_record(record_path)
        draft = _load_applied_draft(
            draft_path,
            sid,
            event.get("session_id"),
            _created_for(event, replay_record),
        )
        draft_project = draft["project"]
        if project_id != draft_project:
            raise WorkerStateError("sync draft project identity changed")
        note_path = _resolve_note_path(draft_project, root=root, vault=vault)
        if _timeline_needs_repair(note_path, sid):
            if not run:
                return {
                    "summary_id": sid,
                    "state": "would_apply",
                    "note": "timeline repair required",
                }
            return apply_summary(draft, root=root, run=True, note_path=note_path)
        return {"summary_id": sid, "state": "applied", "note": "already applied"}

    record_path = _find_inbox_record(event, root)
    if record_path is None and run:
        ingest_fn(root)                          # capture, then look again
        record_path = _find_inbox_record(event, root)
    if record_path is None:
        return {"summary_id": sid, "state": "no_transcript",
                "note": "transcript not captured yet"}

    # phase A: no fill yet -> (re)build the scaffold + PROMPT, then wait for the fill
    if not fill_path.exists():
        if not run:
            return {"summary_id": sid, "state": "would_scaffold",
                    "project": project_id}
        record = _load_inbox_record(record_path)
        scaffold = summary.build_scaffold(
            record, summary_id=sid, project_id=project_id,
            created=_created_for(event, record), session_id=event.get("session_id"))
        pdir.mkdir(parents=True, exist_ok=True)
        scaffold_path.write_text(json.dumps(scaffold, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        (pdir / "PROMPT.md").write_text(summary.PROMPT, encoding="utf-8")
        return {"summary_id": sid, "state": "scaffolded",
                "project": project_id, "pending": str(pdir)}

    # phase B: fill present -> assemble + apply
    if not run:
        return {"summary_id": sid, "state": "would_apply", "project": project_id}
    record = _load_inbox_record(record_path)
    if scaffold_path.exists():
        try:
            scaffold = json.loads(scaffold_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerStateError("sync scaffold could not be resumed safely") from exc
    else:  # scaffold lost but fill present -> rebuild deterministically
        scaffold = summary.build_scaffold(
            record, summary_id=sid, project_id=project_id,
            created=_created_for(event, record), session_id=event.get("session_id"))
    bound_project = _validate_scaffold_binding(
        scaffold,
        summary_id=sid,
        project_id=project_id,
        session_id=event.get("session_id"),
        expected_created=_created_for(event, record),
    )
    fill = json.loads(fill_path.read_text(encoding="utf-8-sig"))
    summ = summary.assemble(scaffold, fill)
    note_path = _resolve_note_path(bound_project, root=root, vault=vault)
    return apply_summary(summ, root=root, run=True, note_path=note_path)


# --- drain ------------------------------------------------------------------

def _advance_events(events, root, *, run, ingest_fn, vault, projects):
    by_id = {}
    for ev in events:
        by_id[summary_id_for(ev)] = ev

    results = []
    remaining = []
    for sid, ev in by_id.items():
        try:
            result = _advance(
                ev,
                root,
                run=run,
                ingest_fn=ingest_fn,
                vault=vault,
                projects=projects,
            )
        except Exception as exc:
            result = {"summary_id": sid, "state": "error", "error": repr(exc)}
        results.append(result)
        if result["state"] != "applied":
            remaining.append(ev)
    return results, remaining


def drain(root=None, *, run=False, ingest_fn=None, vault=None, projects=None):
    """Preview without writes, or drain under the worker lock with ``run=True``."""
    root = root or C.data_root()
    ingest_fn = _default_ingest if ingest_fn is None else ingest_fn
    if not run:
        events = _read_jsonl(paths.inflight_file(root))
        for claim in sorted(paths.sync_state(root).glob("queue.claim.*.jsonl")):
            events += _read_jsonl(claim)
        events += _read_jsonl(paths.queue_file(root))
        results, _ = _advance_events(
            events,
            root,
            run=False,
            ingest_fn=ingest_fn,
            vault=vault,
            projects=projects,
        )
        return results

    with _Lock(paths.lock_file(root)):
        events = _take_queue(root)
        results, remaining = _advance_events(
            events,
            root,
            run=True,
            ingest_fn=ingest_fn,
            vault=vault,
            projects=projects,
        )
        _write_inflight(root, remaining)
    return results


def main():
    C.cli_help(__doc__)
    run = "--run" in sys.argv[1:]
    try:
        results = drain(run=run)
    except WorkerBusy as e:
        print(f"worker: {e}")
        return 1
    except WorkerStateError:
        print("worker: durable sync state could not be changed safely")
        return 1
    scaffolded = sum(1 for r in results if r.get("state") == "scaffolded")
    applied = sum(1 for r in results if r.get("state") == "applied")
    tail = "" if run else "  (dry-run; pass --run to write)"
    print(f"worker: {len(results)} event(s); scaffolded={scaffolded} applied={applied}{tail}")
    for r in results:
        print(f"  {r.get('state', '?'):14} {r.get('summary_id', '?')}")
    return 1 if any(r.get("state") == "error" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
