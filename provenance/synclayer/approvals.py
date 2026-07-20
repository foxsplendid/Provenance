# -*- coding: utf-8 -*-
"""Approval surface — Obsidian queue note + the `approve` committer (sync-layer.md §5).

M3 closes the CC session loop: M2 stages each session's gated high-value claims
(`status`/`stage`/`next_actions`/`conclusion`/`blocked_by`) in `sync-state/drafts/<id>.json`
(`approval_state=draft`). This module turns those into a one-click-approvable queue
and commits the approved ones.

Two verbs (both reuse M1/M2, never re-author a human file):

  generate  Render/regenerate the vault's `Approvals.md` — a plugin-free `- [ ]`
            checkbox queue (a documented exception: a derived view that is ALSO
            user-editable). Regeneration PRESERVES still-pending items, the user's
            edits, and their ticks; drops items whose draft became applied/vanished;
            refuses to overwrite a same-named note that lacks our marker.

  approve   The actual committer (host-side; M4's save-watcher will shell out to it).
            Parses the ticks (or a top-of-note `APPROVE ALL`); for each approved draft:
              1. appends an "approved" block to the project-note progress-log via the
                 M1 append-only primitive (journaled, idempotent). Its idempotency key
                 is `<summary_id>#approved` — DISTINCT from the auto-timeline's
                 `<summary_id>` block, so both coexist and re-approve is a no-op.
              2. updates Provenance memory (SSoT) via the applied-claims overlay
                 (memory/applied_claims.json — survives a vault snapshot rewrite).
              3. writes the approved status/stage/next_actions/blocked_by back into the
                 project-note frontmatter (so the Dataview board reflects it). ONLY
                 those named project/1.0 keys + `updated` are touched; the note body,
                 the append-only progress-log region, prose, and unrelated frontmatter
                 keys are preserved byte-for-byte (a note lacking frontmatter is
                 skipped — the log+overlay still applied). `conclusion` has no
                 frontmatter field and stays log-only.
              4. flips `drafts/<id>.json` to `approval_state=applied` (kept as audit).
            edit-before-approve: the values COMMITTED are the (possibly edited) note
            values, not the draft's original proposal; the draft keeps the proposal.
            The append-only progress-log + the note prose are never rewritten; the
            frontmatter writeback is surgical (named keys only). dry-run by default;
            `--run` writes.

openclaw/Telegram MAY notify "N pending" but MUST NOT commit (write-narrow, §5) —
that notifier is out of scope here; this committer is host-side only.

Stdlib only, no network.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C, memory as M
from provenance.synclayer import paths, summary, worker
from provenance.synclayer import progress_log as PL

APPROVALS_MARKER = "<!-- scriptorium:approvals -->"   # identifies OUR generated note
QUEUE_FILENAME = "Approvals.md"
# render/parse order; map 1:1 to the gated high-value claims (session-summary/1.0).
# stage is FREE-TEXT (no enum), unlike status — never enum-validated on commit.
HIGH_VALUE_FIELDS = ("status", "stage", "next_actions", "conclusion", "blocked_by")
NA_SEP = " | "                                        # next_actions separator in the note

_CHECKBOX_RE = re.compile(r"-\s*\[([ xX])\]\s*(.*)$")
_FIELD_RE = re.compile(r"-\s*([a-z_]+):\s*(.*)$")
_DRAFT_RE = re.compile(r"<!-- draft:(.+?) -->")

_INSTRUCTIONS = (
    "Tick a box and **save**, then rerun the same pull command with `--run` (approves + refreshes). You may edit any\n"
    "proposed value before approving. Plugin-free: plain `- [ ]` checkboxes only.\n"
    f"Separate multiple next actions with `{NA_SEP.strip()}`."
)


def approvals_path(vault):
    return Path(vault) / QUEUE_FILENAME


def draft_marker(summary_id):
    return f"<!-- draft:{summary_id} -->"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _box(checked):
    return "x" if checked else " "


# --- draft selection --------------------------------------------------------

def _draft_fields(draft):
    """{field: render-string} for the high-value claims a draft actually proposed.
    Absent/empty claims are omitted (an empty field is not a proposal)."""
    out = {}
    if (draft.get("status") or "").strip():
        out["status"] = str(draft["status"]).strip()
    na = [str(x).strip() for x in (draft.get("next_actions") or []) if str(x).strip()]
    if na:
        out["next_actions"] = NA_SEP.join(na)
    for k in ("stage", "conclusion", "blocked_by"):
        v = (draft.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def _has_high_value(draft):
    return bool(_draft_fields(draft))


def _pending_drafts(root):
    """{summary_id: draft} for drafts that are approvable: still `draft`, with a
    resolved project, and carrying >=1 high-value claim. A project=None draft has no
    note/memory destination (§7 Inbox route) so it never enters the queue."""
    out = {}
    ddir = paths.drafts_dir(root)
    if not ddir.exists():
        return out
    for jf in sorted(ddir.glob("*.json")):
        try:
            d = json.loads(jf.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if d.get("approval_state") != "draft" or not d.get("project") or not _has_high_value(d):
            continue
        out[d.get("summary_id") or jf.stem] = d
    return out


def _sorted_items(pending):
    """(summary_id, draft) pairs, newest `created` first, stable by id."""
    return sorted(pending.items(),
                  key=lambda kv: (kv[1].get("created") or "", kv[0]), reverse=True)


# --- rendering --------------------------------------------------------------

def render_queue(items, *, approve_all=False):
    """Render the full Approvals.md text. `items` is an ordered list of dicts:
    {sid, project, created, confidence, fields:{field:str}, checked:bool}."""
    out = [APPROVALS_MARKER, "", "# Approvals — pending session writebacks", "",
           _INSTRUCTIONS, "",
           f"- [{_box(approve_all)}] **APPROVE ALL** pending items below", "", "---", ""]
    if not items:
        out += ["_No pending writebacks._", ""]
        return "\n".join(out)
    for it in items:
        head = f"- [{_box(it['checked'])}] **{it['project']}**"
        if it.get("created"):
            head += f" · {it['created']}"
        if it.get("confidence"):
            head += f" · confidence: {it['confidence']}"
        head += f" {draft_marker(it['sid'])}"
        out.append(head)
        for field in HIGH_VALUE_FIELDS:
            v = it["fields"].get(field)
            if v:
                out.append(f"    - {field}: {v}")
        out.append("")
    return "\n".join(out)


# --- parsing ----------------------------------------------------------------

def parse_queue(text):
    """Parse a (possibly user-edited) Approvals.md back into approval state:
    {"approve_all": bool, "items": {sid: {"checked": bool, "fields": {field: value}}}}.
    Tolerant of edits — unknown lines are ignored; field values may be empty."""
    approve_all = False
    items = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        cb = _CHECKBOX_RE.match(s)
        if cb:
            checked = cb.group(1).lower() == "x"
            rest = cb.group(2)
            if "APPROVE ALL" in rest:
                approve_all, cur = checked, None
                continue
            m = _DRAFT_RE.search(rest)
            if m:
                cur = m.group(1).strip()
                items[cur] = {"checked": checked, "fields": {}}
            else:
                cur = None
            continue
        if cur is not None:
            fm = _FIELD_RE.match(s)
            if fm and fm.group(1) in HIGH_VALUE_FIELDS:
                items[cur]["fields"][fm.group(1)] = fm.group(2).strip()
    return {"approve_all": approve_all, "items": items}


# --- generate / regenerate --------------------------------------------------

def regenerate(vault, root=None, *, run=False):
    """Render/regenerate Approvals.md from the pending drafts, MERGING the prior
    note's user edits + ticks for still-pending items (never discards an edited but
    unapproved claim); drops items whose draft is gone/applied. Refuses to overwrite
    a same-named note that lacks our marker. dry-run by default."""
    root = root or C.data_root()
    path = approvals_path(vault)
    pending = _pending_drafts(root)

    prior = {"approve_all": False, "items": {}}
    if path.exists():
        text_before = path.read_text(encoding="utf-8-sig")
        if APPROVALS_MARKER not in text_before:
            return {"state": "refused_not_generated", "path": str(path), "pending": len(pending)}
        prior = parse_queue(text_before)

    merged = []
    for sid, d in _sorted_items(pending):
        if sid in prior["items"]:                       # preserve edits + tick
            fields = {k: v for k, v in prior["items"][sid]["fields"].items() if v}
            checked = prior["items"][sid]["checked"]
        else:                                           # new draft -> proposed values
            fields, checked = _draft_fields(d), False
        merged.append({"sid": sid, "project": d.get("project"),
                       "created": C.to_date(d.get("created")) or "",
                       "confidence": d.get("confidence"), "fields": fields, "checked": checked})

    text = render_queue(merged, approve_all=prior["approve_all"])
    plan = {"state": "written" if run else "would_write", "path": str(path),
            "pending": len(merged),
            "dropped": sorted(set(prior["items"]) - set(pending))}
    if run:
        path.parent.mkdir(parents=True, exist_ok=True)
        PL._atomic_write(path, text.encode("utf-8"))
    else:
        plan["preview"] = text
    return plan


# --- approve (commit) -------------------------------------------------------

def _committed_fields(sid, parsed, draft):
    """The values to APPLY for an approved draft: the (possibly edited) note values
    win (edit-before-approve); fall back to the draft's proposal if the item was not
    in the note (e.g. APPROVE ALL on a draft staged after the last regenerate)."""
    src = parsed["items"].get(sid, {}).get("fields") or _draft_fields(draft)
    out = {}
    st = (src.get("status") or "").strip()
    if st:
        out["status"] = st
    na = [x.strip() for x in (src.get("next_actions") or "").split("|") if x.strip()]
    if na:
        out["next_actions"] = na
    for k in ("stage", "conclusion", "blocked_by"):   # stage is free-text (no enum)
        v = (src.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def _render_approved_block(draft, committed):
    """The block appended to the project-note progress-log for an approved claim."""
    date = C.to_date(draft.get("created")) or (draft.get("created") or "")[:10]
    lines = [f"**{date}** · approved"]
    if committed.get("status"):
        lines.append(f"- status: {committed['status']}")
    if committed.get("stage"):
        lines.append(f"- stage: {committed['stage']}")
    if committed.get("next_actions"):
        lines.append("- next: " + "; ".join(committed["next_actions"]))
    if committed.get("conclusion"):
        lines.append(f"- conclusion: {committed['conclusion']}")
    if committed.get("blocked_by"):
        lines.append(f"- blocked by: {committed['blocked_by']}")
    return "\n".join(lines)


# committed high-value claims that ARE project/1.0 frontmatter fields (so the
# Dataview board reflects an approval). `conclusion` has no frontmatter field -> it
# stays log-only. next_actions/blocked_by are project/1.0 frontmatter keys too.
FRONTMATTER_FIELDS = ("status", "stage", "next_actions", "blocked_by")


def _frontmatter_updates(committed, date):
    """The subset of `committed` that maps to project/1.0 frontmatter fields, plus a
    bumped `updated`. next_actions stays a list; the rest are scalars. Only carries a
    field the claim actually committed — never blanks an omitted field."""
    out = {}
    for k in FRONTMATTER_FIELDS:
        if k in committed:
            out[k] = committed[k]
    if out and date:
        out["updated"] = date          # only bump `updated` when something changed
    return out


def _upsert_applied_claim(root, project, committed, sid):
    """Write the approved high-value claims into the memory overlay (SSoT). Latest
    approval wins per project; survives a vault snapshot rewrite (see memory.py)."""
    path = M.mem_dir(root) / M.APPLIED_CLAIMS
    doc = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            doc = {}
    claims = doc.get("claims")
    if not isinstance(claims, dict):
        claims = {}
    entry = {k: committed[k] for k in M.APPLIED_FIELDS if k in committed}
    entry["_summary_id"] = sid
    entry["_approved_at"] = _now_iso()
    claims[project] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    PL._atomic_write(path, json.dumps(
        {"updated_at": _now_iso(), "count": len(claims), "claims": claims},
        ensure_ascii=False, indent=2).encode("utf-8"))


def _mark_applied(root, sid, committed):
    """Flip the draft to applied and record what was actually committed (audit)."""
    path = paths.drafts_dir(root) / f"{sid}.json"
    if not path.exists():
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    d["approval_state"] = "applied"
    d["approved_at"] = _now_iso()
    d["applied_claims"] = committed
    PL._atomic_write(path, json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8"))


def _commit_one(sid, committed, draft, root, vault, run):
    project = draft.get("project")
    warnings = []
    if "status" in committed and committed["status"] not in summary.STATUS_ENUM:
        warnings.append(f"invalid status '{committed['status']}' (omitted)")
        committed = {k: v for k, v in committed.items() if k != "status"}
    note_path = worker._resolve_note_path(project, root=root, vault=vault)
    res = {"summary_id": sid, "project": project, "committed": committed, "warnings": warnings}
    if not run:
        res["state"] = "would_approve"
        res["note"] = str(note_path) if note_path else None
        return res
    # 1. approved high-value claims -> project-note progress-log via the M1 primitive
    if note_path is not None and committed:
        ar = PL.append_entry(note_path, f"{sid}#approved", _render_approved_block(draft, committed),
                             journal_dir=str(paths.journal_dir(root)))
        res["append_status"] = ar.status
    else:
        res["append_status"] = "no_note"      # unresolved/missing note -> memory-only
    # 2. Provenance memory overlay (SSoT)
    if project and committed:
        _upsert_applied_claim(root, project, committed, sid)
        res["memory"] = "updated"
    # 3. project-note frontmatter writeback (so the Dataview board reflects it). Only
    #    the named project/1.0 fields (status/stage/next_actions/blocked_by + updated);
    #    NEVER the body, the progress-log region, or unrelated frontmatter keys. A note
    #    with no frontmatter is skipped — the log+overlay above still landed.
    if note_path is not None and committed:
        date = C.to_date(draft.get("created")) or (draft.get("created") or "")[:10]
        fm_fields = _frontmatter_updates(committed, date)
        if fm_fields:
            fr = PL.update_frontmatter_fields(note_path, fm_fields,
                                              journal_dir=str(paths.journal_dir(root)))
            res["frontmatter_status"] = fr.status
        else:
            res["frontmatter_status"] = "noop"
    else:
        res["frontmatter_status"] = "no_note"
    # 4. flip the draft (audit trail)
    _mark_applied(root, sid, committed)
    res["state"] = "approved"
    return res


def approve(vault, root=None, *, run=False):
    """Commit the approved drafts. Reads Approvals.md ticks (or APPROVE ALL), commits
    each approved+pending draft (note append via M1 + memory overlay + draft flip),
    then regenerates the queue so applied items drop off. dry-run by default."""
    root = root or C.data_root()
    path = approvals_path(vault)
    if not path.exists():
        return {"state": "no_queue", "path": str(path), "count": 0, "items": []}
    text = path.read_text(encoding="utf-8-sig")
    if APPROVALS_MARKER not in text:
        return {"state": "refused_not_generated", "path": str(path), "count": 0, "items": []}

    parsed = parse_queue(text)
    pending = _pending_drafts(root)
    approved = {sid for sid in pending
                if parsed["approve_all"] or parsed["items"].get(sid, {}).get("checked")}

    results = []
    for sid, draft in _sorted_items(pending):
        if sid in approved:
            results.append(_commit_one(sid, _committed_fields(sid, parsed, draft),
                                       draft, root, vault, run))

    plan = {"state": "approved" if run else "would_approve",
            "approve_all": parsed["approve_all"], "count": len(results), "items": results}
    if run and results:
        regenerate(vault, root=root, run=True)   # applied items leave the queue
    return plan


# --- CLI --------------------------------------------------------------------

def _parse_argv(argv):
    run = "--run" in argv
    vault = os.environ.get("PROVENANCE_VAULT")
    verb, i = "approve", 0
    rest = []
    while i < len(argv):
        a = argv[i]
        if a == "--vault" and i + 1 < len(argv):
            vault, i = argv[i + 1], i + 1
        elif not a.startswith("-"):
            rest.append(a)
        i += 1
    if rest:
        verb = rest[0]
    return verb, vault, run


def main():
    C.cli_help(__doc__)
    verb, vault, run = _parse_argv(sys.argv[1:])
    if not vault:
        print("No vault. Set $PROVENANCE_VAULT or pass --vault <dir>.")
        return 1
    tail = "" if run else "  (dry-run; pass --run to write)"
    if verb == "generate":
        plan = regenerate(vault, run=run)
        if plan["state"] == "refused_not_generated":
            print(f"approvals: refused — {plan['path']} exists without our marker")
            return 1
        print(f"approvals generate: {plan['pending']} pending; dropped={len(plan['dropped'])}{tail}")
        print(f"  -> {plan['path']}")
        return 0
    if verb == "approve":
        plan = approve(vault, run=run)
        if plan["state"] in ("no_queue", "refused_not_generated"):
            print(f"approvals approve: {plan['state']} ({plan['path']})")
            return 1
        print(f"approvals approve: {plan['count']} approved (approve_all={plan['approve_all']}){tail}")
        for r in plan["items"]:
            extra = f" append={r['append_status']}" if r.get("append_status") else ""
            if r.get("frontmatter_status"):
                extra += f" frontmatter={r['frontmatter_status']}"
            print(f"  {r['state']:14} {r['summary_id']} -> {r['project']}{extra}")
        return 0
    print(f"unknown verb: {verb} (use 'generate' or 'approve')")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
