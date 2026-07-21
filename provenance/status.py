#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance status — a quick snapshot of your second brain (read-only).

Shows the data root, per-source conversation counts, total, index/last-ingest,
the uncategorized rate, and whether profile / search index exist.

Usage: prov-status        (runs under $PROVENANCE_HOME, default current dir)
"""
import argparse
import csv
import json
from collections import Counter

from provenance import __version__, common as C


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    root = C.data_root()
    inbox = root / "inbox"
    print(f"Provenance status — data root: {root}")
    if not inbox.exists():
        print("  (no inbox yet — run prov-sync / prov-ingest-*)")
        return

    counts, total = {}, 0
    for d in sorted(inbox.glob("*")):
        if d.is_dir() and not d.name.startswith("_"):
            n = len([f for f in d.glob("*.json") if not f.name.startswith("_")])
            if n:
                counts[d.name], total = n, total + n
    print(f"  conversations: {total}")
    for k in sorted(counts):
        print(f"    {k:14} {counts[k]}")

    idx = inbox / "_index.json"
    if idx.exists():
        try:
            d = json.loads(idx.read_text(encoding="utf-8"))
            print(f"  index keys: {len(d.get('conversations', {}))}   last ingest: {d.get('updated_at', '?')}")
        except Exception:
            pass

    cat = root / "output" / "00_catalog.csv"
    if cat.exists():
        cc = Counter()
        with cat.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                cc[r["cat_id"]] += 1
        n, unc = sum(cc.values()), cc.get("00", 0)
        print(f"  categorized: {n}   uncategorized(00): {unc} ({(100 * unc // n) if n else 0}%)")
    else:
        print("  (no catalog — run prov-organize)")

    prof = root / "profile"
    pmd = sorted(prof.glob("*.md")) if prof.exists() else []
    print(f"  profile: {len(pmd)} docs" if pmd else "  profile: none — run the provenance-profile skill")
    print(f"  search index: {'present' if (root / 'search-index.db').exists() else 'missing (prov-search --build)'}")


if __name__ == "__main__":
    main()
