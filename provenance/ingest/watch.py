#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance drop-folder watcher.

Watch ONE drop folder (default: <project>/drop). When you manually move an
exported ZIP into it, it is auto-detected and routed to the right ingester:
  - provenance__*.zip  (compatible conversation archives)    -> ingest.py
  - Takeout*.zip       (Google Takeout, Gemini)              -> ingest_takeout.py
Processed ZIPs are backed up into inbox/_zips and leave the drop folder.

Replaces the old ingest.bat / ingest_takeout.bat: start this once (or register it
to run at logon) and just drop files in. Stdlib only; polls every few seconds and
waits for a file to finish copying (size/mtime stable) before processing.

Usage:
    python watch.py                 # watch <project>/drop
    python watch.py "C:/ScriptoriumDemo/imports"   # watch a custom folder
"""
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from provenance import common as C

ROOT = C.data_root()
DROP = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "drop"
INBOX = ROOT / "inbox"
ZIP_BACKUP = INBOX / "_zips"
INGEST_MOD = "provenance.ingest.core"
TAKEOUT_MOD = "provenance.ingest.takeout"
INTERVAL = 5  # seconds between scans
PY = sys.executable


def sig_of(path):
    try:
        st = path.stat()
        return (st.st_size, int(st.st_mtime))
    except FileNotFoundError:
        return None


def zip_ready(path):
    """A zip whose central directory (stored at EOF) reads back is fully copied;
    a still-copying file fails here, so we never ingest a partial archive (L8)."""
    try:
        with zipfile.ZipFile(path) as z:
            z.namelist()
        return True
    except Exception:
        return False


def run(mod, *args):
    print(f"  $ python -m {mod} {' '.join(str(a) for a in args)}")
    return subprocess.run([PY, "-m", mod, *map(str, args)]).returncode


def main():
    C.cli_help(__doc__)
    DROP.mkdir(parents=True, exist_ok=True)
    print(f"Provenance watcher running. Drop exported ZIPs into:\n  {DROP}\n"
          f"Routes provenance__*.zip and Takeout*.zip automatically. Ctrl+C to stop.\n")
    sigs = {}  # name -> last (size, mtime); a file is processed once its sig is stable
    while True:
        try:
            present = {f.name: f for f in DROP.glob("*.zip")}
            ready = []
            for name, f in present.items():
                s = sig_of(f)
                if s is None:
                    continue
                if sigs.get(name) == s and zip_ready(f):
                    ready.append(f)  # stable + fully readable -> safe to ingest
                else:
                    sigs[name] = s   # still settling/copying; check again next round
            # forget files that left the folder
            for gone in [n for n in sigs if n not in present]:
                sigs.pop(gone, None)

            prov = [f for f in ready if f.name.startswith("provenance__")]
            takeout = [f for f in ready if f.name.startswith("Takeout")]

            if prov:
                print("detected: " + ", ".join(f.name for f in prov))
                run(INGEST_MOD, DROP)  # scans drop, ingests all provenance zips, moves them to inbox/_zips
                for f in prov:
                    sigs.pop(f.name, None)
            for f in takeout:
                print(f"detected: {f.name}")
                if run(TAKEOUT_MOD, f) == 0 and f.exists():
                    ZIP_BACKUP.mkdir(parents=True, exist_ok=True)
                    dest = ZIP_BACKUP / f.name
                    if dest.exists():
                        dest = ZIP_BACKUP / f"{f.stem}__{int(time.time())}{f.suffix}"
                    shutil.move(str(f), str(dest))
                    print(f"  backed up -> {dest}")
                sigs.pop(f.name, None)

            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\nwatcher stopped.")
            break


if __name__ == "__main__":
    main()
