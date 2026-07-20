#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance profile prep — extract each conversation's USER turns (densest profile
signal), group by category, write per-category digests the profile SKILL reads.
Non-destructive.

Usage:
    prov-prep                 # data_root()/inbox -> data_root()/_prep
    prov-prep <inbox> <out>   # custom dirs (isolated self-tests)
    python -m provenance.organize.prep [...]
"""
import json
import sys
from pathlib import Path

from provenance import common as C


def main():
    C.cli_help(__doc__)
    root = C.data_root()
    inbox = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "inbox"
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else root / "_prep"
    if not inbox.exists():
        print(f"No inbox at {inbox}. Run capture + ingest first.")
        return
    cats, label, _ = C.load_categories()

    buckets = {}
    total_conv = total_turns = 0
    for jf in sorted(inbox.glob("*/*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            obj = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = obj.get("title") or obj.get("name") or "(untitled)"
        date = (C.to_date(obj.get("update_time")) or C.to_date(obj.get("create_time"))
                or C.to_date(obj.get("updated_at")) or C.to_date(obj.get("created_at")))
        turns = C.user_turns(obj)
        cid, _ = C.classify_with_fallback(f"{title} {' '.join(turns[:3])}", jf.parent.name, cats, label)
        buckets.setdefault(cid, []).append((date, jf.parent.name, title, turns))
        total_conv += 1
        total_turns += len(turns)

    out.mkdir(parents=True, exist_ok=True)
    for cid, convs in buckets.items():
        lines = [f"# User-turn digest - {cid} {label.get(cid, cid)}", "",
                 f"> {len(convs)} conversations; the user's own messages only (highest profile signal).", ""]
        for date, source, title, turns in sorted(convs, key=lambda r: r[0]):
            lines.append(f"## [{source}] {title}  ({date}, {len(turns)} user turns)\n")
            for i, t in enumerate(turns, 1):
                lines.append(f"**U{i}:** {C.clip(t)}\n")
        (out / f"digest_{cid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Prep done: {total_conv} conversations, {total_turns} user turns, {len(buckets)} categories")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
