#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot: apply the privacy layer (secret/PII redaction + pseudonymization) to
already-ingested inbox JSON, in place.

The ingesters sanitize on write going forward; run this after editing
aliases.json (or after first adding redaction) to bring existing conversations
up to date. Idempotent (re-running is safe).

Usage:
    python scrub_inbox.py            # ../inbox
    python scrub_inbox.py <inbox>    # custom inbox
"""
import json
import sys
from pathlib import Path

from provenance import common as C
from provenance.redact import sanitize_json

DEFAULT_INBOX = C.data_root() / "inbox"


def main():
    C.cli_help(__doc__)
    inbox = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_INBOX
    if not inbox.exists():
        print(f"No inbox at {inbox}")
        return
    changed = scanned = 0
    for jf in sorted(inbox.glob("*/*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            text = jf.read_text(encoding="utf-8")
            obj = json.loads(text)
        except Exception:
            continue
        scanned += 1
        red = json.dumps(sanitize_json(obj), ensure_ascii=False, indent=2)
        if red != text:
            jf.write_text(red, encoding="utf-8")
            changed += 1
    print(f"scrubbed {changed}/{scanned} files in {inbox}")


if __name__ == "__main__":
    main()
