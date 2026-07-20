#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance ingest (M2) — manual run.

Pulls compatible ZIPs (named `provenance__*.zip`) from Downloads into the data root,
extracts the raw conversation JSON, dedups by (platform, conversation_id) keeping
the more complete / newer copy, and moves the processed ZIP into inbox/_zips as a
backup (so it leaves Downloads but nothing is lost).

Usage:
    python ingest.py                 # scan ~/Downloads
    python ingest.py "C:/ScriptoriumDemo/imports"   # scan a custom directory
"""
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C
from provenance.redact import sanitize_json

ROOT = C.data_root()                            # user data root ($PROVENANCE_HOME or CWD)
INBOX = ROOT / "inbox"
ZIP_BACKUP = INBOX / "_zips"
INDEX = INBOX / "_index.json"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
GLOB = "provenance__*.zip"


def load_index(index_path):
    if index_path.exists():
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"conversations": {}, "updated_at": None}


def conv_id_of(obj, fallback):
    return obj.get("conversation_id") or obj.get("id") or obj.get("uuid") or fallback


def node_count(obj):
    m = obj.get("mapping")
    if isinstance(m, dict):
        return len(m)
    msgs = obj.get("chat_messages")
    if isinstance(msgs, list):
        return len(msgs)
    return 0


def update_time(obj):
    v = obj.get("update_time") or obj.get("updated_at") or 0
    try:
        return float(v)  # epoch seconds (ChatGPT)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()  # ISO (Claude)
    except Exception:
        return 0


def main():
    C.cli_help(__doc__)
    downloads = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DOWNLOADS
    inbox = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else INBOX
    zip_backup = inbox / "_zips"
    index_path = inbox / "_index.json"
    zips = sorted(downloads.glob(GLOB))
    if not zips:
        print(f"No new exports found: {downloads}\\{GLOB}")
        return

    inbox.mkdir(parents=True, exist_ok=True)
    zip_backup.mkdir(parents=True, exist_ok=True)
    index = load_index(index_path)
    convs = index["conversations"]
    added = updated = unchanged = 0

    for zp in zips:
        try:
            with zipfile.ZipFile(zp) as z:
                meta = {}
                if "_export_meta.json" in z.namelist():
                    try:
                        meta = json.loads(z.read("_export_meta.json"))
                    except Exception:
                        pass
                source = meta.get("source", "chatgpt")
                exported_at = meta.get("exported_at")
                dest = inbox / source
                dest.mkdir(parents=True, exist_ok=True)

                for name in z.namelist():
                    if not name.endswith(".json") or name.endswith("_export_meta.json"):
                        continue
                    try:
                        obj = json.loads(z.read(name))
                    except Exception:
                        continue
                    cid = conv_id_of(obj, Path(name).stem)
                    key = f"{source}:{cid}"
                    out = dest / f"{cid}.json"
                    nc, ut = node_count(obj), update_time(obj)
                    prev = convs.get(key)

                    if out.exists() and prev:
                        if nc > prev.get("node_count", 0) or ut > prev.get("update_time", 0):
                            out.write_text(json.dumps(sanitize_json(obj), ensure_ascii=False, indent=2), encoding="utf-8")
                            updated += 1
                            keep = True
                        else:
                            unchanged += 1
                            keep = False
                    else:
                        out.write_text(json.dumps(sanitize_json(obj), ensure_ascii=False, indent=2), encoding="utf-8")
                        added += 1
                        keep = True

                    if keep:
                        convs[key] = {
                            "platform": source,
                            "conversation_id": cid,
                            "title": obj.get("title") or obj.get("name"),
                            "node_count": nc,
                            "update_time": ut,
                            "exported_at": exported_at,
                            "ingested_at": datetime.now(timezone.utc).isoformat(),
                            "file": (str(out.relative_to(ROOT)).replace("\\", "/") if ROOT in out.parents else str(out)),
                        }

            # move processed ZIP out of Downloads into the project (backup)
            target = zip_backup / zp.name
            if target.exists():
                target = zip_backup / f"{zp.stem}__{int(datetime.now().timestamp())}{zp.suffix}"
            shutil.move(str(zp), str(target))
            print(f"processed: {zp.name}")
        except Exception as e:
            print(f"ERROR {zp.name}: {e}")

    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. added={added} updated={updated} unchanged={unchanged}; tracked total={len(convs)}")
    print(f"raw conversations -> {inbox}")
    print(f"zip backups       -> {zip_backup}")


if __name__ == "__main__":
    main()
