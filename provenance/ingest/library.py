#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance library-KB ingest (Phase A) — pull the reference library into the hub.

Reads a Scriptorium `library-kb/1.x` JSON snapshot (1.0 or 1.1, produced by
`steward export`, default at `$PROVENANCE_HOME/kb/library.json`): per-item title /
year / doi / folders / aiTags / tldr / readStatus / abstract, plus the optional
`citekey` added in 1.1. Normalizes + sanitizes each item
(secret/PII scrub + pseudonymization, like every other ingester) and writes a
snapshot to `memory/library.json`. The items are also picked up by the search
index (`prov-search --build`) so `search_brain` returns literature hits alongside
conversations, and by the MCP `get_current_context` tool (a project's literature).

Snapshot, not append: each run overwrites memory/library.json with the latest
export. Stdlib only, no network, read-only on the source JSON.

Usage:
    prov-ingest-library                          # read kb/library.json under data root
    prov-ingest-library <library.json>           # explicit snapshot path
    prov-ingest-library <library.json> <memdir>  # custom memory dir (isolated test)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C, memory as M
from provenance.redact import sanitize_json


def main():
    C.cli_help(__doc__)
    args = sys.argv[1:]
    root = C.data_root()
    src = Path(args[0]) if args else (root / "kb" / "library.json")
    memdir = Path(args[1]) if len(args) > 1 else M.mem_dir(root)

    if not src.exists():
        print(f"No library KB found: {src}\n(run `steward export` first, or pass an explicit path.)")
        return 2
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR reading {src}: {e}")
        return 2

    if not isinstance(data, dict):
        print(f"ERROR: {src} is not a versioned library-kb object.")
        return 2

    ver = data.get("schema_version")
    if not M.schema_ok(ver, "library-kb"):
        print(f"ERROR: {src} is not a library-kb/1.x snapshot (schema_version={ver!r}).")
        return 2

    items = data.get("items")
    if not isinstance(items, list):
        print(f"ERROR: {src} is not a library-kb/1.x snapshot (items must be an array).")
        return 2
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            print(f"ERROR: {src} items[{index}] must be an object.")
            return 2
        if not isinstance(item.get("key"), str) or not item["key"]:
            print(f"ERROR: {src} items[{index}].key must be a non-empty string.")
            return 2
        if not isinstance(item.get("title"), str):
            print(f"ERROR: {src} items[{index}].title must be a string.")
            return 2
    # Sanitize each item: pdfPaths carry the home username, author lists may carry
    # aliased real names — same privacy layer the conversation ingesters apply.
    clean = [sanitize_json(it) for it in items]
    snapshot = {
        "schema_version": ver,
        "generated_by": data.get("generated_by"),
        "library": data.get("library"),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "count": len(clean),
        "items": clean,
    }
    memdir.mkdir(parents=True, exist_ok=True)
    (memdir / M.LIBRARY).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"library KB ingested: {len(clean)} items -> {memdir / M.LIBRARY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
