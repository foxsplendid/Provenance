#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance vault ingest (Phase A) — pull the project portfolio into the hub.

Reads the shared Obsidian research vault's project portfolio:
  - Projects/*.md      frontmatter = project/1.0     (project_id / status / stage /
                       next_actions / linked_literature / linked_repo / ...)

Parses the YAML frontmatter with a stdlib subset parser (NO PyYAML — zero
dependency), sanitizes it (secret/PII scrub + pseudonymization), and writes
`memory/projects.json`. The read-only MCP tools
`get_portfolio` (总纲: all projects + stage + next steps) and
`get_current_context(project_id)` (a project's literature + status)
then expose this to any session at open.

Snapshot, not append: each run rewrites the file from the current vault.
Stdlib only, no network, read-only on the vault.

Vault location: argv[1], else $PROVENANCE_VAULT.

Usage:
    prov-ingest-vault <vault_dir>
    PROVENANCE_VAULT=D:/path/to/vault  prov-ingest-vault
    prov-ingest-vault <vault_dir> <memdir>   # custom memory dir (isolated test)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C, memory as M
from provenance.redact import sanitize_json

# project notes can carry sensitive decisions in the body; keep a short,
# sanitized excerpt for "latest status" context without storing the whole log.
BODY_CHARS = 800
PROGRESS_CHARS = 4000
PROGRESS_BEGIN = "<!-- scriptorium:progress-log:begin -->"
PROGRESS_END = "<!-- scriptorium:progress-log:end -->"
PROGRESS_ENTRY_RE = re.compile(r"<!--\s*entry:[^>\r\n]+-->")


def _progress_excerpt(body):
    """Keep the newest generated progress separately from the short body excerpt."""
    begin = body.find(PROGRESS_BEGIN)
    if begin < 0:
        return ""
    end = body.find(PROGRESS_END, begin + len(PROGRESS_BEGIN))
    if end < 0:
        return ""
    region = body[begin + len(PROGRESS_BEGIN):end]
    if len(region) > PROGRESS_CHARS:
        matches = list(PROGRESS_ENTRY_RE.finditer(region))
        if not matches:
            region = region[:PROGRESS_CHARS]
        else:
            prefix = region[:matches[0].start()]
            blocks = []
            used = len(prefix)
            for index, match in enumerate(matches):
                block_end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(region)
                )
                block = region[match.start():block_end]
                if used + len(block) > PROGRESS_CHARS:
                    break
                blocks.append(block)
                used += len(block)
            if blocks:
                region = prefix + "".join(blocks)
            else:
                # A single pathological entry may exceed the bound. Keep its
                # newest prefix instead of retaining older entries from the tail.
                region = region[:PROGRESS_CHARS]
    return f"{PROGRESS_BEGIN}{region}{PROGRESS_END}"


def _collect(note_dir, glob, schema, id_field):
    """Parse <note_dir>/*.md, keep those whose frontmatter matches `schema` and has
    `id_field`. Returns {id: record} where record = sanitized frontmatter + body
    excerpt + source filename."""
    out = {}
    if not note_dir.exists():
        return out
    for md in sorted(note_dir.glob(glob)):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = M.parse_frontmatter(text)
        if not M.schema_ok(fm.get("schema_version"), schema):
            continue
        rid = fm.get(id_field)
        if not rid:
            continue
        rec = dict(fm)
        excerpt = C.clip(body, BODY_CHARS)
        if excerpt:
            rec["body"] = excerpt
        progress_excerpt = _progress_excerpt(body)
        if progress_excerpt:
            rec["_progress_log"] = progress_excerpt
        rec["_source_file"] = md.name
        out[str(rid)] = sanitize_json(rec)
    return out


def ingest(vault, *, root=None, memdir=None, run=True, now=None):
    """Collect the project portfolio and optionally persist its memory snapshot."""
    vault = Path(vault)
    projects = _collect(vault / "Projects", "*.md", "project", "project_id")
    plan = {
        "state": "written" if run else "would_write",
        "count": len(projects),
        "projects": projects,
    }
    if not run:
        return plan

    memdir = Path(memdir) if memdir is not None else M.mem_dir(root or C.data_root())
    ingested_at = now or datetime.now(timezone.utc).isoformat()
    memdir.mkdir(parents=True, exist_ok=True)
    (memdir / M.PROJECTS).write_text(
        json.dumps(
            {"ingested_at": ingested_at, "count": len(projects), "projects": projects},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return plan


def main():
    C.cli_help(__doc__)
    args = sys.argv[1:]
    vault = Path(args[0]) if args else (
        Path(os.environ["PROVENANCE_VAULT"]) if os.environ.get("PROVENANCE_VAULT") else None)
    memdir = Path(args[1]) if len(args) > 1 else M.mem_dir(C.data_root())

    if vault is None:
        print("No vault given. Pass a vault dir or set $PROVENANCE_VAULT.")
        return
    if not vault.exists():
        print(f"Vault not found: {vault}")
        return

    plan = ingest(vault, memdir=memdir, run=True)
    print(f"vault portfolio ingested: {plan['count']} projects -> {memdir}")


if __name__ == "__main__":
    main()
