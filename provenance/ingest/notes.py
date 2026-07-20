#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
note/1.0 ingester — personal-zone free-text into the protected memory layer (M4).

Reads an Obsidian human-input zone (Inbox/, _planning/) and ingests each Markdown
note as a `note/1.0` record (scriptorium-spec schemas/note/v1.json) so it can serve
as project context. READ-ONLY on the vault.

Privacy (sync-layer.md §6): every personal-zone note is forced `private=true` and
stored under `$PROVENANCE_HOME/sync-state/notes/` — the protected layer (NTFS
deny-read for <restricted-user>), outside `inbox/`. The profile/persona pipeline reads
`inbox/*/*.json` only, so these notes are EXCLUDED from profiling by construction.

Like the other ingesters: stdlib only, secret/PII scrubbed + pseudonymized via
`redact.sanitize_json`, incremental (a note whose content hash is unchanged is
skipped), and re-ingesting the same file updates one stable record (no duplicates).

Usage:
    prov-ingest-notes <vault> <zone> [<zone> ...]   # e.g. prov-ingest-notes V Inbox
    prov-ingest-notes <vault>                        # defaults to Inbox + _planning
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C, memory as M
from provenance.redact import sanitize_json
from provenance.synclayer import paths

SCHEMA = "note/1.0"
DEFAULT_ZONES = ("Inbox", "_planning")
_HEADING_RE = re.compile(r"#{1,6}\s+(.*)")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def note_id_for(rel_path):
    """Stable, collision-safe id from the vault-relative path (host-stamped on first
    ingest). Re-ingesting the same file updates this one record; a different path is
    a different note."""
    return "obsidian-" + hashlib.sha1(str(rel_path).encode("utf-8")).hexdigest()[:12]


def _sig(md):
    """(size, mtime) readiness signature for the half-save guard in build_note."""
    st = md.stat()
    return (st.st_size, st.st_mtime)


def _created(fm, md):
    """frontmatter `created` if the author set one, else the file's mtime (ISO).
    `created` is a required ISO field (note/1.0), so a stat failure falls back to the
    ingest time rather than an invalid empty string."""
    c = fm.get("created")
    if c:
        return str(c)
    try:
        return datetime.fromtimestamp(md.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return _now_iso()


def _first_heading(body):
    for line in body.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return ""


def build_note(vault, md, *, source="obsidian", private=True):
    """Build (note_id, content_sha, note/1.0 record) from a Markdown file, or None if
    the file changed during the read (an Obsidian half-save). The watcher debounces,
    but the ingester also enforces readiness at the read boundary (sync-layer.md §2.4:
    act only on a stable file).

    `content_sha` is the RAW file digest — the incremental-skip key. Only the human
    free-text values (body/title/tags/project) are run through sanitize_json; the
    machine identity/bookkeeping keys (note_id, _content_sha, _ingested_at) are set
    afterwards, so the dedup key and on-disk filename never depend on the redaction
    ruleset."""
    try:
        s0 = _sig(md)
        raw = md.read_bytes()
        s1 = _sig(md)
    except OSError:
        return None                        # vanished mid-read -> not ready; re-fire later
    if s0 != s1:                           # mutated during the read -> half-saved
        return None

    text = raw.decode("utf-8-sig", errors="replace")
    fm, body = M.parse_frontmatter(text)
    rel = md.relative_to(vault).as_posix()
    note_id = note_id_for(rel)
    content_sha = hashlib.sha256(raw).hexdigest()

    rec = {
        "schema_version": SCHEMA,
        "note_id": note_id,                # machine id (filename stem) — not sanitized
        "source": source,
        "created": _created(fm, md),
        "body": body.strip(),
        "private": bool(private),          # personal zones: always private (§6)
        "origin_path": rel,
        "generated_by": "provenance ingest-notes",
    }
    title = fm.get("title") or _first_heading(body) or md.stem
    if title:
        rec["title"] = str(title)
    tags = fm.get("tags")
    if isinstance(tags, list) and tags:
        rec["tags"] = [str(t) for t in tags]
    project = fm.get("project")
    if project:
        rec["project"] = str(project)
    # redact/pseudonymize ONLY the human free-text; machine keys stay verbatim
    for k in ("body", "title", "tags", "project"):
        if k in rec:
            rec[k] = sanitize_json(rec[k])
    rec["_content_sha"] = content_sha      # raw digest -> stable incremental-skip key
    rec["_ingested_at"] = _now_iso()
    return note_id, content_sha, rec


def ingest_zone(vault, zone, *, root=None, source="obsidian", private=True, run=True):
    """Ingest every Markdown note under <vault>/<zone> into the protected note store.
    Skips dot-leading paths (incl. .obsidian/) and *.tmp; incremental by content hash.
    Returns a list of per-file result dicts. Read-only on the vault."""
    vault = Path(vault)
    root = root or C.data_root()
    zdir = vault / zone
    results = []
    if not zdir.is_dir():
        return results
    ndir = paths.notes_dir(root)
    for md in sorted(zdir.rglob("*.md")):
        rel = md.relative_to(vault)
        if any(p.startswith(".") for p in rel.parts) or md.name.endswith(".tmp"):
            continue
        try:
            built = build_note(vault, md, source=source, private=private)
        except Exception as e:               # one unreadable note must not stop the rest
            results.append({"origin_path": rel.as_posix(), "state": "error", "error": repr(e)})
            continue
        if built is None:                    # half-saved during the read -> skip this round
            results.append({"origin_path": rel.as_posix(), "state": "unstable"})
            continue
        note_id, content_sha, rec = built
        out = ndir / f"{note_id}.json"
        prev_sha = None
        if out.exists():
            try:
                prev_sha = json.loads(out.read_text(encoding="utf-8-sig")).get("_content_sha")
            except Exception:
                prev_sha = None
        if prev_sha == content_sha:          # unchanged -> incremental skip (idempotent)
            results.append({"note_id": note_id, "origin_path": rec["origin_path"], "state": "skipped"})
            continue
        state = "updated" if prev_sha else "ingested"
        if run:
            ndir.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            state = "would_update" if prev_sha else "would_ingest"
        results.append({"note_id": note_id, "origin_path": rec["origin_path"],
                        "state": state})
    return results


def ingest(vault, zones, *, root=None, source="obsidian", private=True, run=True):
    out = []
    for zone in zones:
        out += ingest_zone(
            vault, zone, root=root, source=source, private=private, run=run
        )
    return out


def main():
    C.cli_help(__doc__)
    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not pos:
        print("Usage: prov-ingest-notes <vault> <zone> [<zone> ...]")
        return 1
    vault = Path(pos[0])
    zones = pos[1:] or list(DEFAULT_ZONES)
    if not vault.exists():
        print(f"Vault not found: {vault}")
        return 1
    results = ingest(vault, zones)
    new = sum(1 for r in results if r["state"] == "ingested")
    upd = sum(1 for r in results if r["state"] == "updated")
    skip = sum(1 for r in results if r["state"] == "skipped")
    print(f"notes ingested ({', '.join(zones)}): new={new} updated={upd} unchanged={skip} "
          f"-> {paths.notes_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


