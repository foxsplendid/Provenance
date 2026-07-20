#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance incremental diff (helper for the profile skill).

Compares the current inbox against a saved snapshot and reports what is NEW or
GROWN since last run, grouped by category — so a profile refresh can focus on
"what changed" instead of re-reading everything. Writes a markdown diff to
03-organize/_prep/_diff_latest.md and updates the snapshot.

Usage:
    python profile_diff.py            # diff ../inbox, update snapshot
    python profile_diff.py --peek     # diff without updating the snapshot
    python profile_diff.py <inbox>
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from provenance import common as C


def current_state(inbox, cats, label):
    state = {}
    for jf in sorted(inbox.glob("*/*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            obj = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = jf.parent.name
        m = C.extract_meta(obj, source, jf.stem)
        cid, _ = C.classify_with_fallback(f"{m['title']} {m['first_user']}", source, cats, label)
        key = f"{source}:{m['conversation_id']}"
        state[key] = {"source": source, "title": m["title"], "date": m["date"],
                      "node_count": m["node_count"], "cat": cid, "cat_label": label.get(cid, cid)}
    return state


def main():
    C.cli_help(__doc__)
    args = [a for a in sys.argv[1:]]
    peek = "--peek" in args
    args = [a for a in args if a != "--peek"]
    root = C.data_root()
    default_inbox = (root / "inbox").resolve()
    inbox = Path(args[0]).resolve() if args else default_inbox
    if not inbox.exists():
        print(f"No inbox at {inbox}.")
        return
    # snapshot/output follow the inbox, so running on a temp inbox never clobbers
    # the real _snapshot.json baseline (P0-5 / H4)
    out_dir = (root / "_prep") if inbox == default_inbox else inbox / "_prep"
    snapshot = out_dir / "_snapshot.json"
    cats, label, _ = C.load_categories()
    cur = current_state(inbox, cats, label)

    snap = {}
    if snapshot.exists():
        try:
            snap = json.loads(snapshot.read_text(encoding="utf-8")).get("keys", {})
        except Exception:
            snap = {}
    baseline = not snap

    new = [k for k in cur if k not in snap]
    grown = [k for k in cur if k in snap and cur[k]["node_count"] > snap[k].get("node_count", 0)]

    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Provenance profile diff — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    if baseline:
        lines += [f"> Baseline snapshot created from {len(cur)} conversations; no prior state to diff.", ""]
    else:
        lines += [f"> vs last snapshot: **{len(new)} new**, **{len(grown)} grown** (of {len(cur)} total).", ""]

    def block(title, keys):
        out = [f"## {title} ({len(keys)})", ""]
        if not keys:
            out += ["_(none)_", ""]
            return out
        by_cat = {}
        for k in keys:
            by_cat.setdefault(cur[k]["cat_label"], []).append(cur[k])
        for cat in sorted(by_cat):
            out.append(f"### {cat} ({len(by_cat[cat])})")
            for r in sorted(by_cat[cat], key=lambda x: x["date"]):
                out.append(f"- [{r['source']}] {r['date']}  {r['title']}  ·{r['node_count']} msgs")
            out.append("")
        return out

    if not baseline:
        lines += block("New conversations", new)
        lines += block("Grown conversations", grown)
    (out_dir / "_diff_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not peek:
        snapshot.write_text(json.dumps({"generated_at": datetime.now().isoformat(), "keys": cur},
                                       ensure_ascii=False, indent=2), encoding="utf-8")

    if baseline:
        print(f"Baseline snapshot saved: {len(cur)} conversations -> {snapshot}")
    else:
        print(f"Diff: {len(new)} new, {len(grown)} grown (of {len(cur)}). -> {out_dir / '_diff_latest.md'}")
        if peek:
            print("(--peek: snapshot NOT updated)")


if __name__ == "__main__":
    main()
