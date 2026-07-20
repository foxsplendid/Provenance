#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance one-shot full sync — refresh the whole local pipeline in one command.

Drains the drop/ folder (extension + Takeout exports), pulls local coding-agent
logs, then rebuilds organize / prep / diff / search index / Obsidian vault. Each
step runs as an isolated subprocess. Stdlib only; runs under $PROVENANCE_HOME
(default: current directory). Run **on demand** (e.g. via the `memory-refresh`
skill) — no scheduled task. (Profile synthesis stays a separate, deliberate step
— run the provenance-profile skill.)

Usage:
    prov-sync                 # full sync
    prov-sync --no-export     # skip the Obsidian export
    prov-sync --prune         # also prune orphan agent sessions
"""
import os
import subprocess
import sys
from pathlib import Path

from provenance import common as C

PY = sys.executable


def run(mod, *args, label=None):
    print(f"\n=== {label or mod} ===")
    return subprocess.run([PY, "-m", mod, *map(str, args)]).returncode


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return
    no_export = "--no-export" in sys.argv
    prune = "--prune" in sys.argv
    root = C.data_root()
    drop = root / "drop"

    # 1. drain drop/: provenance__*.zip (extension) + Takeout*.zip (Gemini)
    if drop.exists():
        if list(drop.glob("provenance__*.zip")):
            run("provenance.ingest.core", drop, label="ingest drop (extension)")
        for tk in sorted(drop.glob("Takeout*.zip")):
            run("provenance.ingest.takeout", tk, label=f"ingest {tk.name}")
    # 2. local coding-agent logs
    run("provenance.ingest.agents", *(["--prune"] if prune else []), label="ingest agents")
    # 2b. structured research context (Phase A): reference library + project portfolio
    if (root / "kb" / "library.json").exists():
        run("provenance.ingest.library", label="ingest library KB")
    if os.environ.get("PROVENANCE_VAULT"):
        run("provenance.ingest.vault", os.environ["PROVENANCE_VAULT"], label="ingest vault portfolio")
    # 3. downstream (deterministic)
    run("provenance.organize.core", label="organize")
    run("provenance.organize.prep", label="prep")
    run("provenance.organize.diff", label="diff")
    run("provenance.search.core", "--build", label="search index")
    if not no_export:
        run("provenance.export.obsidian", label="obsidian export")
    print("\nsync complete. Profile synthesis is a separate step — run the "
          "provenance-profile skill when you want to (re)generate profile/.")


if __name__ == "__main__":
    main()
