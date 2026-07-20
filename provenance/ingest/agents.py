#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance local coding-agent ingest.

Imports conversation history from local coding-agent session logs into inbox/, in
the same Claude-shape the other sources use, so organize.py / profile_prep.py work
unchanged. Reads each agent's on-disk history directly (these are live local logs,
not downloaded ZIPs); extracts text turns (user + assistant) and skips tool/system
noise. Incremental: a session file is re-parsed only if its size/mtime changed
since the last run (tracked in inbox/_index.json). Non-destructive (read-only on
the agent dirs).

Sources (verified on disk):
  - claude-code : ~/.claude/projects/**/*.jsonl                         (type user/assistant -> message.content)
  - codex       : ~/.codex/sessions/**/rollout-*.jsonl                  (event_msg user_message + response_item assistant message)
  - antigravity : ~/.gemini/antigravity*/brain/*/.../transcript.jsonl   (USER_INPUT / PLANNER_RESPONSE -> content)

Usage:
    python ingest_agents.py                       # all sources -> ../inbox
    python ingest_agents.py <inbox>               # custom inbox (isolated self-test)
    python ingest_agents.py <inbox> codex         # only selected source(s)
    python ingest_agents.py codex claude-code     # selected sources -> ../inbox
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import common as C
from provenance.redact import sanitize_json

HOME = Path(os.environ.get("USERPROFILE") or Path.home())   # where the agent LOGS live
APPDATA = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
DEFAULT_INBOX = C.data_root() / "inbox"                     # where DATA is written


def to_iso(ts):
    if ts is None or ts == "":
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc).isoformat()
        except Exception:
            return ""
    return str(ts)  # ISO-ish string; to_date downstream is tolerant


def title_of(messages):
    for ts, sender, text in messages:
        if sender == "human" and text.strip():
            ln = text.strip().splitlines()[0].strip()
            return (ln[:60] + ("..." if len(ln) > 60 else "")) or "(untitled)"
    return "(untitled)"


def claude_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def parse_claude(path):
    msgs, title, cwd = [], None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "custom-title":
                title = o.get("customTitle") or title
            elif t == "ai-title" and not title:
                title = o.get("aiTitle")
            elif t in ("user", "assistant"):
                cwd = cwd or o.get("cwd")
                text = claude_text((o.get("message") or {}).get("content"))
                if text:
                    msgs.append((to_iso(o.get("timestamp")),
                                 "human" if t == "user" else "assistant", text))
    if not msgs:
        return None
    return dict(session_id=path.stem, name=title or title_of(msgs), project=cwd, messages=msgs)


def codex_message_text(content):
    """Extract plain text from Codex message content across rollout formats."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return codex_message_text([content])
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") in ("output_text", "input_text", "text"):
                    parts.append(str(b.get("text") or ""))
                elif isinstance(b.get("text"), str):
                    parts.append(b["text"])
        return "\n".join(p for p in parts if p).strip()
    return ""


def parse_codex(path):
    msgs, sid, cwd = [], path.stem, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            pl = o.get("payload") or {}
            ts = to_iso(o.get("timestamp"))
            if t == "session_meta":
                sid = pl.get("session_id") or pl.get("id") or sid
                cwd = pl.get("cwd") or cwd
            elif t == "turn_context":
                cwd = pl.get("cwd") or cwd
            elif t == "event_msg":
                pt = pl.get("type")
                if pt == "user_message":
                    text = (pl.get("message") or "").strip()
                    if text:
                        msgs.append((ts, "human", text))
                elif pt == "agent_message":
                    text = (pl.get("message") or "").strip()
                    if text:
                        msgs.append((ts, "assistant", text))
            elif t == "response_item" and pl.get("type") == "message":
                # Newer Codex rollouts carry assistant text here as
                # content=[{"type":"output_text","text":"..."}].
                if pl.get("role") == "assistant":
                    text = codex_message_text(pl.get("content"))
                    if text:
                        msgs.append((ts, "assistant", text))
    if not msgs:
        return None
    return dict(session_id=sid, name=title_of(msgs), project=cwd, messages=msgs)


def parse_antigravity(path):
    # path: .../antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl
    sid = path.parents[2].name
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t not in ("USER_INPUT", "PLANNER_RESPONSE"):
                continue
            c = o.get("content")
            text = c.strip() if isinstance(c, str) else (c.get("text", "").strip() if isinstance(c, dict) else "")
            text = re.sub(r"</?USER_REQUEST>", "", text).strip()  # strip Antigravity wrapper tags
            if text:
                msgs.append((to_iso(o.get("created_at")),
                             "human" if t == "USER_INPUT" else "assistant", text))
    if not msgs:
        return None
    return dict(session_id=sid, name=title_of(msgs), project=None, messages=msgs)


def parse_cursor(db):
    """Yield parsed sessions from Cursor's globalStorage state.vscdb
    (cursorDiskKV: composerData:<id> sessions + bubbleId:<composer>:<bubble> messages)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        bubbles = {}
        for k, v in con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            bubbles[k] = v
        for k, v in con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            try:
                meta = json.loads(v)
            except Exception:
                continue
            composer_id = k.split(":", 1)[1]
            headers = meta.get("fullConversationHeadersOnly") or meta.get("conversation") or []
            msgs = []
            for h in headers:
                if not isinstance(h, dict):
                    continue
                if h.get("text"):                                  # inline message
                    txt, typ = str(h["text"]).strip(), h.get("type")
                elif "bubbleId" in h:                              # referenced bubble
                    bv = bubbles.get(f"bubbleId:{composer_id}:{h['bubbleId']}")
                    if not bv:
                        continue
                    try:
                        b = json.loads(bv)
                    except Exception:
                        continue
                    txt, typ = str(b.get("text") or "").strip(), b.get("type")
                else:
                    continue
                if txt:
                    msgs.append(("", "human" if typ == 1 else "assistant", txt))
            if not msgs:
                continue
            yield dict(session_id=composer_id, name=(meta.get("name") or title_of(msgs)),
                       project=None, messages=msgs,
                       created_at=to_iso(meta.get("createdAt")),
                       updated_at=to_iso(meta.get("lastUpdatedAt") or meta.get("createdAt")))
    finally:
        con.close()


SOURCES = {
    "claude-code": dict(bases=[HOME / ".claude" / "projects"],
                        glob="**/*.jsonl", parse=parse_claude),
    "codex": dict(bases=[HOME / ".codex" / "sessions"],
                  glob="**/rollout-*.jsonl", parse=parse_codex),
    "antigravity": dict(bases=[HOME / ".gemini" / "antigravity", HOME / ".gemini" / "antigravity-ide"],
                        glob="brain/*/.system_generated/logs/transcript.jsonl", parse=parse_antigravity),
}


ATT_RE = re.compile(r"[^\s\"'`()\[\]<>|]+\.(?:docx|pptx|pdf|xlsx)", re.I)
ATT_MAX = 25 * 1024 * 1024  # skip huge files


def attach_referenced(parsed, source, inbox):
    """Copy generated/reference files (docx/pptx/pdf/xlsx) explicitly named in the
    session text, resolved against the session cwd, into inbox/<source>/_assets."""
    cwd = parsed.get("project") or ""
    found = {}
    for _ts, _sender, text in parsed["messages"]:
        for tok in ATT_RE.findall(text):
            name = tok.replace("\\", "/").split("/")[-1]
            cands = []
            p = Path(tok)
            if p.is_absolute():
                cands.append(p)
            if cwd:
                cands += [Path(cwd) / tok, Path(cwd) / name]
            real = next((c for c in cands if c.is_file() and c.stat().st_size <= ATT_MAX), None)
            if real:
                found[name] = real
    if not found:
        return []
    assets = inbox / source / "_assets"
    assets.mkdir(parents=True, exist_ok=True)
    out = []
    for name, real in found.items():
        try:
            data = real.read_bytes()
        except Exception:
            continue
        dest = assets / name
        if dest.exists() and dest.read_bytes() != data:
            # different file, same basename -> disambiguate so bytes aren't lost (L11)
            p = Path(name)
            name = f"{p.stem}-{hashlib.sha1(data).hexdigest()[:8]}{p.suffix}"
            dest = assets / name
        if not dest.exists():
            try:
                dest.write_bytes(data)
            except Exception:
                continue
        out.append(name)
    return out


def record_of(source, parsed):
    msgs = parsed["messages"]
    cid = f"{source}-{parsed['session_id']}"
    chat = [{"sender": s, "text": txt, "created_at": ts} for ts, s, txt in msgs]
    rec = {
        "source": source,
        "conversation_id": cid,
        "name": parsed["name"],
        "created_at": parsed.get("created_at") or (msgs[0][0] if msgs else ""),
        "updated_at": parsed.get("updated_at") or (msgs[-1][0] if msgs else ""),
        "chat_messages": chat,
    }
    if parsed.get("project"):
        rec["project"] = parsed["project"]
    if parsed.get("attachments"):
        rec["attachments"] = parsed["attachments"]
    return cid, rec


def reconcile(inbox, convs, sources, prune):
    """Report (and optionally --prune) inbox files with no current index key, for
    the file-based sources fully scanned this run (cursor is not 1:1, skipped)."""
    for source in [s for s in sources if s in SOURCES]:
        dest = inbox / source
        if not dest.exists():
            continue
        expected = set()
        for k, v in convs.items():
            if k.startswith(f"{source}:"):
                sid = v["conversation_id"].split(f"{source}-", 1)[-1]
                expected.add(dest / f"{sid}.json")
        orphans = [f for f in dest.glob("*.json") if f not in expected and not f.name.startswith("_")]
        if orphans:
            print(f"{source:12} orphan files: {len(orphans)}" + (" -> pruned" if prune else " (use --prune to remove)"))
            if prune:
                for f in orphans:
                    try:
                        f.unlink()
                    except OSError:
                        pass


def main():
    C.cli_help(__doc__)
    args = sys.argv[1:]
    prune = "--prune" in args
    args = [a for a in args if a != "--prune"]
    all_sources = list(SOURCES) + ["cursor"]
    sel = [a for a in args if a in all_sources]
    paths = [a for a in args if a not in all_sources]
    inbox = Path(paths[0]).resolve() if paths else DEFAULT_INBOX
    sources = sel or all_sources

    index_path = inbox / "_index.json"
    index = {"conversations": {}, "updated_at": None}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    convs = index.setdefault("conversations", {})

    grand_added = grand_updated = grand_unchanged = 0
    for source in [s for s in sources if s in SOURCES]:
        cfg = SOURCES[source]
        files = []
        for base in cfg["bases"]:
            if base.exists():
                files += list(base.glob(cfg["glob"]))
        dest = inbox / source
        added = updated = unchanged = 0
        for fp in sorted(files):
            try:
                st = fp.stat()
                sig = [st.st_size, int(st.st_mtime)]
            except FileNotFoundError:
                continue
            # incremental skip: same file signature and output already present
            try:
                parsed = cfg["parse"](fp)
            except Exception as e:
                print(f"  parse error in {fp.name}: {e}")
                continue
            if parsed is None:
                continue
            # Unique session id PER SOURCE FILE (H1): the same UUID stem recurs
            # across project dirs (resume / sub-agent / reuse); bare stem collided ->
            # files overwrote each other and src_sig mis-skipped. A path hash makes
            # file name AND index key one-to-one with the source file. (Must be set
            # BEFORE cid/key/out are derived.)
            parsed["session_id"] = f"{parsed['session_id']}-{hashlib.sha1(str(fp).encode('utf-8')).hexdigest()[:8]}"
            cid = f"{source}-{parsed['session_id']}"
            key = f"{source}:{cid}"
            out = dest / f"{parsed['session_id']}.json"
            prev = convs.get(key)
            if out.exists() and prev and prev.get("src_sig") == sig:
                unchanged += 1
                continue
            parsed["attachments"] = attach_referenced(parsed, source, inbox)
            _, rec = record_of(source, parsed)
            dest.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(sanitize_json(rec), ensure_ascii=False, indent=2), encoding="utf-8")
            convs[key] = {"platform": source, "conversation_id": cid, "title": rec["name"],
                          "node_count": len(rec["chat_messages"]), "update_time": rec["updated_at"],
                          "src_sig": sig, "ingested_at": datetime.now(timezone.utc).isoformat()}
            if prev:
                updated += 1
            else:
                added += 1
        print(f"{source:12} files={len(files):<4} added={added} updated={updated} unchanged={unchanged} -> {dest}")
        grand_added += added
        grand_updated += updated
        grand_unchanged += unchanged

    if "cursor" in sources:
        db = APPDATA / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        dest = inbox / "cursor"
        added = updated = unchanged = 0
        if db.exists():
            try:
                cursor_sessions = list(parse_cursor(db))
            except Exception as e:
                cursor_sessions = []
                print(f"cursor       parse error (schema drift?): {e}")
            for parsed in cursor_sessions:
                cid = f"cursor-{parsed['session_id']}"
                key = f"cursor:{cid}"
                out = dest / f"{parsed['session_id']}.json"
                ut = parsed["updated_at"]
                prev = convs.get(key)
                if out.exists() and prev and prev.get("update_time") == ut:
                    unchanged += 1
                    continue
                _, rec = record_of("cursor", parsed)
                dest.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(sanitize_json(rec), ensure_ascii=False, indent=2), encoding="utf-8")
                convs[key] = {"platform": "cursor", "conversation_id": cid, "title": rec["name"],
                              "node_count": len(rec["chat_messages"]), "update_time": ut,
                              "ingested_at": datetime.now(timezone.utc).isoformat()}
                if prev:
                    updated += 1
                else:
                    added += 1
        else:
            print("cursor       db not found:", db)
        print(f"{'cursor':12} sessions     added={added} updated={updated} unchanged={unchanged} -> {dest}")
        grand_added += added
        grand_updated += updated
        grand_unchanged += unchanged

    reconcile(inbox, convs, sources, prune)
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    inbox.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. added={grand_added} updated={grand_updated} unchanged={grand_unchanged}")


if __name__ == "__main__":
    main()
