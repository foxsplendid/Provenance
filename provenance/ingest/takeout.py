#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance Takeout ingest (M5, Gemini) - deterministic, manual run.

Google Takeout has NO clean per-conversation Gemini API; the only complete
official source is "My Activity -> Gemini Apps", a FLAT activity log (HTML) with
one entry per turn and NO conversation/thread id. We therefore treat each
"Prompted" entry as one standalone record (faithful to what Takeout provides).

This script:
  1. opens a Takeout *.zip (handles Google's nested split zips),
  2. parses the Gemini Apps activity HTML into entries (prompt + response +
     timestamp + attachment links),
  3. normalizes each "Prompted" entry into the SAME shape as the Claude exporter
     ({source, conversation_id, name, updated_at, chat_messages:[...]}) so the
     downstream organize.py / profile_prep.py work unchanged,
  4. writes inbox/gemini/{conversation_id}.json + copies attachments into
     inbox/gemini/_assets/, and merges keys into inbox/_index.json.

Non-destructive to the Takeout zip. Usage:
    python ingest_takeout.py                       # find newest Takeout*.zip in Downloads or project root
    python ingest_takeout.py <takeout.zip>         # explicit zip
    python ingest_takeout.py <takeout.zip> <inbox> # custom inbox (isolated self-test)
"""
import hashlib
import html
import io
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from provenance import common as C
from provenance.redact import sanitize_json

ROOT = C.data_root()                            # user data root ($PROVENANCE_HOME or CWD)
DEFAULT_INBOX = ROOT / "inbox"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"

# zh month names not needed; activity timestamps are numeric. Drop tz abbrev.
# trailing (?:...) optionally consumes the timezone token (e.g. "EDT", "GMT+8")
# so it does not leak into the response text after the split.
_TZ = r"(?:\s+[A-Za-z]{2,5}(?:[+\-]\d{1,2})?)?"
TS_ZH = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+\s*(\d{1,2}):(\d{2}):(\d{2})" + _TZ)
TS_EN = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)" + _TZ)
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
LINK = re.compile(r'<a href="([^"]+)">([^<]*)</a>')
TAG = re.compile(r"<[^>]+>")


def find_takeout_zip():
    for base in (DEFAULT_DOWNLOADS, ROOT):
        hits = sorted(base.glob("Takeout*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    return None


def iter_leaves(src, name_filter=None):
    """Yield (name, bytes) for files in the zip (recursing into nested zips),
    reading ONLY entries whose name passes name_filter. `src` is a path or a
    file-like. Filtering by name before reading avoids loading a multi-GB Takeout
    (Drive/Photos/...) fully into memory — we materialize only what we need (L7)."""
    with zipfile.ZipFile(src) as z:
        for n in z.namelist():
            if n.endswith("/"):
                continue
            if n.lower().endswith(".zip"):
                with z.open(n) as fh:  # nested zip: read to recurse (rare in Takeout)
                    nested = io.BytesIO(fh.read())
                yield from iter_leaves(nested, name_filter)
            elif name_filter is None or name_filter(n):
                yield n, z.read(n)


def parse_ts(s):
    """Return ISO 'YYYY-MM-DDTHH:MM:SS' (naive) or '' from a localized timestamp."""
    m = TS_ZH.search(s)
    if m:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss).isoformat()
        except ValueError:
            return ""
    m = TS_EN.search(s)
    if m:
        mon, d, y, hh, mm, ss, ap = m.groups()
        h = int(hh) % 12 + (12 if ap == "PM" else 0)
        try:
            return datetime(int(y), MONTHS[mon], int(d), h, int(mm), int(ss)).isoformat()
        except (ValueError, KeyError):
            return ""
    return ""


def strip_html(s):
    return re.sub(r"\s+\n", "\n", html.unescape(TAG.sub("", s))).strip()


def parse_activity(text):
    """Yield dicts for each 'Prompted' entry: prompt, response, iso, attachments."""
    blocks = text.split('<div class="outer-cell')[1:]
    for b in blocks:
        m = re.search(r'body-1">(.*?)</div>\s*<div class="content-cell', b, re.S)
        cell = m.group(1) if m else ""
        verb = re.match(r"\s*([A-Za-z]+)", cell)
        if not verb or verb.group(1) != "Prompted":
            continue
        ts = parse_ts(cell)
        # split the cell at the timestamp: before = prompt(+attachments), after = response
        tsm = TS_ZH.search(cell) or TS_EN.search(cell)
        pre = cell[: tsm.start()] if tsm else cell
        post = cell[tsm.end():] if tsm else ""
        # store the REAL on-disk filename (href basename, URL-decoded) so it
        # matches the copied asset file; display text drops the hash suffix.
        attachments = [unquote(href).split("/")[-1] for href, _ in LINK.findall(pre)]
        attach_hrefs = [href for href, _ in LINK.findall(pre)]
        # drop the leading "Prompted " verb and any "Attached N files." tail from the prompt
        pre = re.sub(r"^\s*Prompted\s*", "", pre, count=1)
        pre = re.split(r"<br\s*/?>\s*Attached\b", pre, maxsplit=1)[0]
        prompt = strip_html(pre)
        response = strip_html(post)
        if not prompt and not attachments:
            continue
        yield {"prompt": prompt, "response": response, "iso": ts,
               "attachments": attachments, "hrefs": attach_hrefs}


# Takeout has NO thread id (verified: no conversation-identifying links in the
# activity HTML). We reconstruct "conversations" heuristically: consecutive
# prompts less than SESSION_GAP apart belong to the same session. This can
# mis-merge two quick unrelated chats or split a session resumed much later --
# it is the best available approximation of ChatGPT-like per-conversation units.
SESSION_GAP = 30 * 60  # seconds


def to_dt(iso):
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def group_sessions(entries):
    entries = sorted(entries, key=lambda e: e["iso"] or "")
    groups, cur, prev = [], [], None
    for e in entries:
        dt = to_dt(e["iso"])
        start_new = bool(cur) and (prev is None or dt is None
                                   or (dt - prev).total_seconds() > SESSION_GAP)
        if start_new:
            groups.append(cur)
            cur = []
        cur.append(e)
        prev = dt
    if cur:
        groups.append(cur)
    return groups


def make_record(group):
    first, last = group[0], group[-1]
    cid = "gem-" + hashlib.sha1(
        (first["iso"] + "|" + first["prompt"][:200]).encode("utf-8")).hexdigest()[:16]
    first_line = (first["prompt"].splitlines() or [""])[0].strip()
    name = (first_line[:60] + ("..." if len(first_line) > 60 else "")) or "(gemini conversation)"
    msgs, atts = [], []
    for e in group:
        if e["prompt"]:
            msgs.append({"sender": "human", "text": e["prompt"], "created_at": e["iso"]})
        if e["response"]:
            msgs.append({"sender": "assistant", "text": e["response"], "created_at": e["iso"]})
        for a in e["attachments"]:
            if a not in atts:
                atts.append(a)
    return cid, {
        "source": "gemini",
        "conversation_id": cid,
        "name": name,
        "created_at": first["iso"],
        "updated_at": last["iso"] or first["iso"],
        "turns": len(group),
        "chat_messages": msgs,
        "attachments": atts,
    }


def main():
    C.cli_help(__doc__)
    args = [a for a in sys.argv[1:]]
    zpath = Path(args[0]).resolve() if len(args) >= 1 else find_takeout_zip()
    inbox = Path(args[1]).resolve() if len(args) >= 2 else DEFAULT_INBOX
    if not zpath or not zpath.exists():
        print("No Takeout zip found. Pass a path: python ingest_takeout.py <takeout.zip>")
        return
    print(f"reading {zpath.name}")

    leaves = list(iter_leaves(zpath, lambda n: "Gemini Apps/" in n))
    htmls = [(n, b) for n, b in leaves if "Gemini Apps/" in n and n.lower().endswith(".html")]
    if not htmls:
        print("No 'Gemini Apps' activity HTML inside. Did you export My Activity -> Gemini Apps?")
        return
    act_name, act_bytes = max(htmls, key=lambda nb: len(nb[1]))  # activity log = the big html
    print(f"activity log: {act_name.split('/')[-1]} ({len(act_bytes)} bytes)")

    gem_dir = inbox / "gemini"
    assets = gem_dir / "_assets"
    gem_dir.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)

    # index for cross-run dedup (shared with ingest.py)
    index_path = inbox / "_index.json"
    index = {"conversations": {}, "updated_at": None}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    convs = index.setdefault("conversations", {})

    text = act_bytes.decode("utf-8", "replace")
    entries = list(parse_activity(text))
    sessions = group_sessions(entries)
    print(f"parsed {len(entries)} prompts -> {len(sessions)} conversations "
          f"(<{SESSION_GAP // 60}min gap = same session)")
    added = unchanged = 0
    for group in sessions:
        cid, rec = make_record(group)
        key = f"gemini:{cid}"
        out = gem_dir / f"{cid}.json"
        if out.exists() and key in convs:
            unchanged += 1
            continue
        out.write_text(json.dumps(sanitize_json(rec), ensure_ascii=False, indent=2), encoding="utf-8")
        convs[key] = {"platform": "gemini", "conversation_id": cid, "title": rec["name"],
                      "node_count": len(rec["chat_messages"]), "update_time": rec["updated_at"],
                      "ingested_at": datetime.now(timezone.utc).isoformat()}
        added += 1

    # copy attachments (basename; skip if already present / same name)
    copied = 0
    by_base = {}
    for n, b in leaves:
        if "Gemini Apps/" in n and n != act_name:
            by_base.setdefault(n.split("/")[-1], b)
    for base, b in by_base.items():
        dest = assets / base
        if dest.exists() and dest.read_bytes() != b:
            p = Path(base)  # different file, same basename -> keep both (L11)
            dest = assets / f"{p.stem}-{hashlib.sha1(b).hexdigest()[:8]}{p.suffix}"
        if not dest.exists():
            dest.write_bytes(b)
            copied += 1

    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. added={added} unchanged={unchanged}; attachments copied={copied}")
    print(f"records    -> {gem_dir}")
    print(f"attachments-> {assets}")

    # back up the source Takeout zip into the raw layer — Gemini has no
    # re-export-on-demand, so deleting it from drop/Downloads stays non-destructive
    try:
        backup_dir = inbox / "_zips"
        if zpath.parent.resolve() != backup_dir.resolve():
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / zpath.name
            if dest.exists():
                dest = backup_dir / f"{zpath.stem}__{int(zpath.stat().st_mtime)}{zpath.suffix}"
            shutil.move(str(zpath), str(dest))
            print(f"backed up  -> {dest}")
    except Exception as e:
        print(f"(zip backup skipped: {e})")


if __name__ == "__main__":
    main()
