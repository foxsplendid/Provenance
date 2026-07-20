#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openclaw mobile-drop router — least-trust ingest of the VM's quarantine inbox (M5).

openclaw is the mobile front door (product-direction.md §7): a dedicated, LEAST-TRUST
Hyper-V VM whose only host write surface is the quarantine drop `_openclaw-inbox`.
This host-side, trusted router reads that drop area and, for EVERY delivery:

  1. validates size (abuse cap, sync-layer.md §9) — oversize/malformed/empty -> _rejected;
  2. routes a structured `kind: proposal` drop into the APPROVALS pipeline (below);
  3. otherwise wraps it as a `note/1.0` record — a note-envelope JSON contributes its
     body/title/tags/project; everything else (markdown, txt, non-envelope JSON) is
     free text with the whole payload as the body. In BOTH cases source="openclaw",
     private=true and note_id are HOST-forced (a VM-declared source/privacy/id is
     never trusted);
  4. redacts/pseudonymizes the human free-text via redact.sanitize_json (VM = untrusted);
  5. stores it in the protected layer `sync-state/notes/` (NTFS deny-read for
     <restricted-user>, outside inbox/ -> excluded from the profile pipeline by construction);
  6. moves the raw drop into a budget-pruned `_openclaw-processed/` backup.

Proposal route (sync-layer.md §5.1): a `kind: proposal` drop (markdown+YAML or the
equivalent JSON object — project + optional status/next_actions/conclusion/blocked_by/
confidence) STAGES a `session-summary/1.0` DRAFT (approval_state=draft) under
`sync-state/drafts/` instead of becoming a private note, so it surfaces in the vault
Approvals queue for one-click approval. It is NEVER applied here. An unknown/invalid
project is REJECTED (never silently applied, never invents a project); a proposal that
carries no high-value field falls back to the note route.

LEAST-TRUST boundary (sync-layer.md §1, product-direction.md §7 R18): a drop NEVER
appends into a project note and NEVER edits an authoritative store. A free-text drop
becomes a private note in the protected layer; a proposal only stages a human-gated
DRAFT. The human Approvals tick (the existing M3 `approve` committer) is the ONLY path
into a project note / memory. The vault's authoritative zones are untouched — this only
reads and relocates the VM's own write area.

Stdlib only, no network.

Usage:
    prov-ingest-openclaw <inbox_dir>            # one-shot drain (e.g. a scheduled drain)
    PROVENANCE_OPENCLAW_INBOX=...  prov-ingest-openclaw
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C, memory as M
from provenance.redact import sanitize_json
from provenance.synclayer import paths, summary

SCHEMA = "note/1.0"
SUMMARY_SCHEMA = "session-summary/1.0"   # proposal route stages this DRAFT shape
SOURCE = "openclaw"
ENV_INBOX = "PROVENANCE_OPENCLAW_INBOX"

# Abuse limits (sync-layer.md §9): bound a misbehaving/compromised VM.
MAX_FILE_BYTES = 256 * 1024     # per-file size cap (a mobile note/idea is small)
MAX_PER_SCAN = 50               # per-scan count cap (throttle a flood; rest deferred)
# Quarantine/backup budget: prune so the _rejected/_processed dirs cannot grow forever.
BUDGET_MAX_FILES = 200
BUDGET_MAX_AGE_DAYS = 30

# Files in the drop area we never treat as a payload (dirs are skipped separately).
SKIP_SUFFIX = (".tmp", ".part", ".crdownload", ".reason")

_ENVELOPE_RE = re.compile(r"^note/1\.[0-9]+$")
_HEADING_RE = re.compile(r"#{1,6}\s+(.*)")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def note_id_for(raw):
    """Host-stamped, collision-safe id from the RAW bytes. Two byte-identical drops
    collapse to one record (idempotent re-delivery); different content is a new note.
    Host-minted on purpose — a VM-declared id is never trusted."""
    return "openclaw-" + hashlib.sha256(raw).hexdigest()[:12]


def _first_heading(body):
    for line in body.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return ""


def _coerce_created(value):
    """Accept a producer-supplied ISO `created` only if it is a non-empty string;
    otherwise stamp the ingest time. The value is never trusted to be well-formed."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _now_iso()


def route_record(name, raw):
    """Build (note_id, note/1.0 record) from a raw drop's (filename, bytes), or None
    if the decoded payload is empty (nothing to ingest).

    A `.json` payload that declares itself a note/1.0 envelope contributes its
    body/title/tags/project/created; any other payload (markdown, txt, non-envelope
    JSON, undecodable bytes) is treated as free text with the whole payload as `body`.
    source, private and note_id are HOST-forced in both cases, and every human
    free-text field is run through sanitize_json (the VM is untrusted)."""
    text = raw.decode("utf-8-sig", errors="replace")
    title = tags = project = None
    created = _now_iso()
    body = text.strip()

    if name.lower().endswith(".json"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict) and _ENVELOPE_RE.match(str(obj.get("schema_version", ""))):
            body = str(obj.get("body", "")).strip()
            created = _coerce_created(obj.get("created"))
            if obj.get("title"):
                title = str(obj["title"])
            if isinstance(obj.get("tags"), list) and obj["tags"]:
                tags = [str(t) for t in obj["tags"]]
            if obj.get("project"):
                project = str(obj["project"])
        # a non-envelope JSON falls through: the raw text stays the body.

    if not body:
        return None

    content_sha = hashlib.sha256(raw).hexdigest()
    note_id = "openclaw-" + content_sha[:12]
    rec = {
        "schema_version": SCHEMA,
        "note_id": note_id,            # host-minted; a VM id is never trusted
        "source": SOURCE,              # host-forced (least-trust)
        "created": created,
        "body": body,
        "private": True,               # host-forced: every VM drop is private (§6)
        "generated_by": "provenance ingest-openclaw",
    }
    if not title:
        title = _first_heading(body)
    if title:
        rec["title"] = str(title)
    if tags:
        rec["tags"] = tags
    if project:
        rec["project"] = str(project)
    # redact/pseudonymize ONLY the VM-controlled free-text; machine keys stay verbatim
    for k in ("body", "title", "tags", "project"):
        if k in rec:
            rec[k] = sanitize_json(rec[k])
    rec["_origin_name"] = sanitize_json(name)   # VM-chosen label -> sanitized too
    rec["_content_sha"] = content_sha           # raw digest -> exact idempotency key
    rec["_ingested_at"] = _now_iso()
    return note_id, rec


# --- proposal route (drop -> approvable DRAFT) ------------------------------
# A `kind: proposal` drop does NOT become a private note: it STAGES a
# session-summary/1.0 DRAFT (approval_state=draft) into sync-state/drafts/, so it
# surfaces in the vault Approvals queue. LEAST-TRUST INVARIANT: a proposal only
# stages a draft for human approval — it NEVER auto-applies, never creates a
# project, never runs code. The human Approvals tick is the only path into
# notes/memory (the existing M3 `approve` committer). A proposal whose project is
# unknown is REJECTED (never silently applied, never invents a project).


def _parse_envelope(name, raw):
    """Parse a drop into an envelope dict, or None if it is not one. Accepts both
    a markdown file with leading YAML frontmatter and an equivalent JSON object;
    the free-text after the frontmatter (or a JSON `body`/`rationale`) rides along
    as `_rationale`. A VM-declared envelope is data, never trusted authority."""
    text = raw.decode("utf-8-sig", errors="replace")
    if name.lower().endswith(".json"):
        try:
            obj = json.loads(text)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        env = dict(obj)
        if "_rationale" not in env:
            env["_rationale"] = str(obj.get("rationale") or obj.get("body") or "")
        return env
    fm, body = M.parse_frontmatter(text)
    if not fm:
        return None
    env = dict(fm)
    env["_rationale"] = body.strip()
    return env


def _is_proposal(env):
    return isinstance(env, dict) and str(env.get("kind", "")).strip().lower() == "proposal"


def _proposal_high_value(env):
    """Extract the sanitized high-value claims a proposal actually carries.
    An invalid `status` (not in summary.STATUS_ENUM) is OMITTED, not rejected.
    Returns {} if the proposal carries no high-value field (caller falls back to
    the note route)."""
    out = {}
    st = str(env.get("status") or "").strip()
    if st and st in summary.STATUS_ENUM:           # invalid status -> omit (not trusted)
        out["status"] = st
    na = env.get("next_actions")
    if isinstance(na, list):
        na = [str(x).strip() for x in na if str(x).strip()]
    elif isinstance(na, str) and na.strip():
        na = [na.strip()]
    else:
        na = []
    if na:
        out["next_actions"] = na
    for k in ("stage", "conclusion", "blocked_by"):   # stage is free-text (no enum)
        v = str(env.get(k) or "").strip()
        if v:
            out[k] = v
    return {k: sanitize_json(v) for k, v in out.items()}


def build_proposal_draft(env, raw, *, root, created=None):
    """Build (summary_id, draft) for a valid proposal envelope, or None if it is not
    approvable (unknown project, or no high-value field). The draft is a minimal
    session-summary/1.0 with approval_state='draft' — NO timeline, NO auto-apply.
    `summary_id` / `_content_sha` are host-minted from the RAW bytes (a VM id is
    never trusted) so a re-drop overwrites the same draft (idempotent)."""
    project = str(env.get("project") or "").strip()
    if not project or project not in M.load_projects(root):   # unknown -> not a draft
        return None
    high = _proposal_high_value(env)
    if not high:                                              # nothing to approve
        return None
    content_sha = hashlib.sha256(raw).hexdigest()
    summary_id = "openclaw-" + content_sha[:12]               # host-minted, not the VM's
    confidence = str(env.get("confidence") or "").strip().lower()
    if confidence not in summary.CONFIDENCE_ENUM:
        confidence = "medium"                                 # default; never trusted blind
    draft = {
        "schema_version": SUMMARY_SCHEMA,
        "summary_id": summary_id,
        "project": sanitize_json(project),
        "created": created or _now_iso(),
        "approval_state": "draft",                            # human-gated; never applied here
        "confidence": confidence,
        "generated_by": "provenance ingest-openclaw",
        "_source": "openclaw-proposal",                       # provenance marker
        "_content_sha": content_sha,                          # raw digest -> idempotency key
        "_ingested_at": _now_iso(),
    }
    draft.update(high)                                        # status/next_actions/...
    return summary_id, draft


def _candidates(inbox):
    """Drop files worth considering (no dirs, dotfiles, tmp/sidecars), ordered
    oldest-first so the per-scan cap takes a stable, fair slice."""
    out = []
    for f in inbox.iterdir():
        if not f.is_file():
            continue
        n = f.name
        if n.startswith(".") or n.lower().endswith(SKIP_SUFFIX):
            continue
        out.append(f)
    out.sort(key=lambda p: ((p.stat().st_mtime if p.exists() else 0.0), p.name))
    return out


def _stable_read(f):
    """Read bytes only if the (size, mtime) signature is unchanged across the read
    (half-write guard, sync-layer.md §2.4). Returns bytes, or None if not ready."""
    try:
        st0 = f.stat()
        raw = f.read_bytes()
        st1 = f.stat()
    except OSError:
        return None
    if (st0.st_size, st0.st_mtime) != (st1.st_size, st1.st_mtime):
        return None
    return raw


def _unique_dest(dirp, name):
    dirp.mkdir(parents=True, exist_ok=True)
    dest = dirp / name
    if dest.exists():
        dest = dirp / f"{Path(name).stem}__{int(time.time())}{Path(name).suffix}"
    return dest


def _prune_budget(dirp, *, now=None, max_files=BUDGET_MAX_FILES,
                  max_age_days=BUDGET_MAX_AGE_DAYS):
    """Keep a quarantine/backup dir bounded (sync-layer.md §9): drop entries older
    than max_age_days, then trim oldest until at most max_files remain. A `.reason`
    sidecar rides with its file. Best-effort; an unremovable file is left in place."""
    if not dirp.is_dir():
        return
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400

    def _rm(p):
        for q in (p, p.with_name(p.name + ".reason")):
            try:
                q.unlink()
            except OSError:
                pass

    survivors = []
    for p in dirp.iterdir():
        if not p.is_file() or p.name.endswith(".reason"):
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt < cutoff:
            _rm(p)
        else:
            survivors.append((mt, p))
    if len(survivors) > max_files:
        survivors.sort()                                  # oldest first
        for _, p in survivors[:len(survivors) - max_files]:
            _rm(p)


def _reject(f, reason, *, root, now=None):
    """Move a bad drop into the quarantine with a reason note, then prune the budget."""
    rdir = paths.rejected_dir(root)
    dest = _unique_dest(rdir, f.name)
    try:
        shutil.move(str(f), str(dest))
    except OSError:
        return None
    try:
        dest.with_name(dest.name + ".reason").write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass
    _prune_budget(rdir, now=now)
    return dest


def _archive(f, *, root, now=None):
    """Move a consumed drop into the processed backup, then prune the budget."""
    pdir = paths.openclaw_processed_dir(root)
    dest = _unique_dest(pdir, f.name)
    try:
        shutil.move(str(f), str(dest))
    except OSError:
        return None
    _prune_budget(pdir, now=now)
    return dest


def _drop_created(f):
    """The drop file's mtime as ISO (the proposal's `created`), or the ingest time
    if it cannot be read."""
    try:
        return datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return _now_iso()


def _handle_proposal(f, name, raw, *, root, now=None):
    """If `raw` is a `kind: proposal` drop, stage/reject it and return a result dict;
    otherwise return None so the caller falls through to the note route.

    LEAST-TRUST: a valid proposal only STAGES a DRAFT in sync-state/drafts/ (the M3
    approvals queue picks it up); it is NEVER applied here. An unknown/invalid project
    is REJECTED (never silently applied, never a note that could leak to memory as an
    approved change). A valid envelope with no high-value field falls back to a note."""
    try:
        env = _parse_envelope(name, raw)
    except Exception:
        env = None
    if not _is_proposal(env):
        return None                                  # not a proposal -> note route
    project = str(env.get("project") or "").strip()
    if not project or project not in M.load_projects(root):
        _reject(f, f"proposal: unknown project {project!r}", root=root, now=now)
        return {"name": name, "state": "rejected", "reason": "unknown-project"}
    built = build_proposal_draft(env, raw, root=root, created=_drop_created(f))
    if built is None:                                # proposal w/o a high-value field
        return None                                  # -> fall back to the note route
    summary_id, draft = built
    ddir = paths.drafts_dir(root)
    out = ddir / f"{summary_id}.json"
    if out.exists():                                 # identical re-drop -> idempotent
        prev = None
        try:
            prev = json.loads(out.read_text(encoding="utf-8-sig")).get("_content_sha")
        except Exception:
            prev = None
        if prev == draft["_content_sha"]:
            _archive(f, root=root, now=now)
            return {"name": name, "summary_id": summary_id, "state": "duplicate"}
    ddir.mkdir(parents=True, exist_ok=True)          # overwrite-stable by content sha
    out.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    _archive(f, root=root, now=now)
    return {"name": name, "summary_id": summary_id, "state": "drafted",
            "project": draft["project"]}


def ingest_inbox(inbox_dir, *, root=None, now=None):
    """Drain the openclaw drop area once. Enforces the §9 caps, writes note/1.0 into
    the protected layer, and relocates every consumed/rejected drop out of the inbox.
    Returns a list of per-file result dicts. The vault's authoritative zones are never
    touched (this only reads + moves the VM's own write surface)."""
    inbox = Path(inbox_dir)
    root = root or C.data_root()
    results = []
    if not inbox.is_dir():
        return results
    ndir = paths.notes_dir(root)

    cands = _candidates(inbox)
    if len(cands) > MAX_PER_SCAN:                          # throttle a flood; keep the rest
        for f in cands[MAX_PER_SCAN:]:
            results.append({"name": f.name, "state": "deferred"})
        cands = cands[:MAX_PER_SCAN]

    for f in cands:
        name = f.name
        try:
            size = f.stat().st_size
        except OSError:
            results.append({"name": name, "state": "unstable"})
            continue
        if size > MAX_FILE_BYTES:                          # abuse cap (§9)
            _reject(f, f"oversize: {size} > {MAX_FILE_BYTES} bytes", root=root, now=now)
            results.append({"name": name, "state": "rejected", "reason": "oversize"})
            continue
        raw = _stable_read(f)
        if raw is None:                                    # half-written -> retry next scan
            results.append({"name": name, "state": "unstable"})
            continue
        try:                                               # proposal route: drop -> DRAFT
            pres = _handle_proposal(f, name, raw, root=root, now=now)
        except Exception as e:                             # a bad proposal must not stop the rest
            _reject(f, f"proposal-error: {e!r}", root=root, now=now)
            results.append({"name": name, "state": "rejected", "reason": "malformed"})
            continue
        if pres is not None:                               # handled as a proposal
            results.append(pres)
            continue
        try:                                               # otherwise -> existing note route
            built = route_record(name, raw)
        except Exception as e:                             # never let one bad drop stop the rest
            _reject(f, f"route-error: {e!r}", root=root, now=now)
            results.append({"name": name, "state": "rejected", "reason": "malformed"})
            continue
        if built is None:                                  # empty payload -> nothing to ingest
            _reject(f, "empty payload", root=root, now=now)
            results.append({"name": name, "state": "rejected", "reason": "empty"})
            continue
        note_id, rec = built
        out = ndir / f"{note_id}.json"
        if out.exists():                                   # identical re-delivery -> idempotent
            prev = None
            try:
                prev = json.loads(out.read_text(encoding="utf-8-sig")).get("_content_sha")
            except Exception:
                prev = None
            if prev == rec["_content_sha"]:
                _archive(f, root=root, now=now)
                results.append({"name": name, "note_id": note_id, "state": "duplicate"})
                continue
        ndir.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        _archive(f, root=root, now=now)
        results.append({"name": name, "note_id": note_id, "state": "ingested"})
    return results


def main():
    C.cli_help(__doc__)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    inbox = args[0] if args else os.environ.get(ENV_INBOX)
    if not inbox:
        print(f"Usage: prov-ingest-openclaw <inbox_dir>  (or set ${ENV_INBOX})")
        return 1
    inbox = Path(inbox)
    if not inbox.is_dir():
        print(f"openclaw inbox not found: {inbox}")
        return 1
    results = ingest_inbox(inbox)
    ing = sum(1 for r in results if r["state"] == "ingested")
    dft = sum(1 for r in results if r["state"] == "drafted")
    dup = sum(1 for r in results if r["state"] == "duplicate")
    rej = sum(1 for r in results if r["state"] == "rejected")
    dfr = sum(1 for r in results if r["state"] == "deferred")
    print(f"openclaw drops: ingested={ing} drafted={dft} duplicate={dup} rejected={rej} "
          f"deferred={dfr} -> {paths.notes_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


