#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance local full-text search (stage 04, fully local — no data leaves machine).

Builds a SQLite FTS5 index over the (pseudonymized) inbox and queries it. Uses the
`trigram` tokenizer so Chinese / code substrings are searchable. Index lives at
04-search/index.db (git-ignored). Reads the privacy-safe inbox only.

Usage:
    python search.py --build              # (re)build the index from ../inbox
    python search.py "数据泄漏"            # search (auto-builds if missing)
    python search.py -n 20 "随机森林 SHAP" # cap results
    python search.py --build <inbox>
"""
import json
import sqlite3
import sys
from pathlib import Path

from provenance import common as C, memory as M

DEFAULT_INBOX = C.data_root() / "inbox"
DEFAULT_DB = C.data_root() / "search-index.db"
CATS, LABEL, _ORDER = C.load_categories()


def db_for(inbox):
    """Real inbox -> shared DEFAULT_DB; any other (test) inbox -> its own db file,
    so `--build <tmp_inbox>` never clobbers the real index (P0-5)."""
    return DEFAULT_DB if Path(inbox).resolve() == DEFAULT_INBOX.resolve() else Path(inbox) / ".search_index.db"


def fts_query(q):
    """Quote each token so FTS5 reserved chars (+ - ( ) \" AND/OR/NOT/NEAR) inside
    code/ML terms are treated as literals, not query syntax (P0-3)."""
    toks = [t for t in q.split() if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks) or '""'


def _load_synonyms():
    path = C.PKG_DIR / "synonyms.json"
    if not path.exists():
        return {}
    try:
        groups = json.loads(path.read_text(encoding="utf-8")).get("groups", [])
    except Exception:
        return {}
    m = {}
    for g in groups:
        terms = [t for t in g if t]
        for t in terms:
            m.setdefault(t.lower(), set()).update(terms)
    return {k: sorted(v) for k, v in m.items()}


SYNONYMS = _load_synonyms()


def expand_query(q):
    """OR each query token with its synonym group (CN/EN/abbrev bridge), AND the
    groups — a stdlib lexical recall boost over FTS5 (no model, no network)."""
    groups = []
    for tok in q.split():
        terms = list(dict.fromkeys(SYNONYMS.get(tok.lower()) or [tok]))
        groups.append("(" + " OR ".join('"' + t.replace('"', '""') + '"' for t in terms) + ")")
    return " ".join(groups) or '""'


def build(inbox, db=None):
    db = Path(db) if db else db_for(inbox)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE IF EXISTS conv")
    con.execute("DROP TABLE IF EXISTS idxmeta")
    con.execute("CREATE TABLE idxmeta(k TEXT, v TEXT)")
    try:
        con.execute("CREATE VIRTUAL TABLE conv USING fts5("
                    "conversation_id, source, date, category, title, body, tokenize='trigram')")
        tok = "trigram"
    except sqlite3.OperationalError:
        con.execute("CREATE VIRTUAL TABLE conv USING fts5("
                    "conversation_id, source, date, category, title, body)")
        tok = "unicode61"
        print("WARNING: this SQLite lacks the FTS5 'trigram' tokenizer (needs SQLite>=3.34). "
              "Falling back to unicode61 — CJK / substring search will NOT work. "
              "Upgrade Python/SQLite, or query whole ASCII words only.")
    con.execute("INSERT INTO idxmeta VALUES ('tokenizer', ?)", (tok,))
    rows, n = [], 0
    for jf in sorted(inbox.glob("*/*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            obj = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = jf.parent.name
        m = C.extract_meta(obj, source, jf.stem)
        cid, _ = C.classify_with_fallback(f"{m['title']} {m['first_user']}", source, CATS, LABEL)
        body = m["title"] + "\n" + "\n".join(C.user_turns(obj))
        rows.append((m["conversation_id"], source, m["date"], f"{cid} {LABEL.get(cid, cid)}",
                     m["title"], body))
        n += 1
    # Phase A: also index the reference library so search_brain returns papers
    # alongside conversations. memory/ sits beside inbox/ under the data root.
    for it in M.load_library(inbox.parent):
        key = it.get("key") or ""
        title = it.get("title") or "(untitled)"
        date = it.get("year") or C.to_date(it.get("dateAdded")) or ""
        body = "\n".join(p for p in [
            title, " ".join(it.get("authors") or []), it.get("tldr") or "",
            it.get("abstract") or "", " ".join(it.get("aiTags") or it.get("tags") or []),
            " ".join(it.get("folders") or []), it.get("readStatus") or ""] if p)
        rows.append((f"lit:{key}", "literature", str(date), "literature", title, body))
        n += 1
    # Research artifacts are already privacy-sanitized at ingest. Index their
    # complete contract content so paper sections, reading findings, review gaps
    # and lineage relations are discoverable through the same local FTS surface.
    for record in M.load_research_artifacts(inbox.parent):
        if not isinstance(record, dict) or not isinstance(record.get("content"), dict):
            continue
        content = record["content"]
        artifact_type = record.get("artifact_type") or "research-artifact"
        artifact_id = record.get("artifact_id") or ""
        direction = content.get("direction") if isinstance(content.get("direction"), dict) else {}
        metadata = content.get("metadata") if isinstance(content.get("metadata"), dict) else {}
        title = (metadata.get("title") or direction.get("query") or content.get("id")
                 or artifact_id or "(untitled research artifact)")
        date = content.get("created") or direction.get("created") or ""
        body = json.dumps(content, ensure_ascii=False, sort_keys=True)
        rows.append((f"research:{artifact_id}", artifact_type, str(date),
                     "research-artifact", str(title), body))
        n += 1
    con.executemany("INSERT INTO conv VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    print(f"indexed {n} conversations ({tok} tokenizer) -> {db}")


def query(q, limit=8, db=None, raw=False, exact=False):
    """Return ranked hits as a list of dicts (data API; used by CLI + MCP).
    Default expands synonyms (recall); exact=True uses literal tokens; raw=True
    passes the query straight to FTS5."""
    db = Path(db) if db else DEFAULT_DB
    if not db.exists():
        build(DEFAULT_INBOX)
    con = sqlite3.connect(db)
    match = q if raw else (fts_query(q) if exact else expand_query(q))
    try:
        rows = con.execute(
            "SELECT source, date, title, conversation_id, "
            "snippet(conv, 5, '[', ']', ' … ', 12) FROM conv "
            "WHERE conv MATCH ? ORDER BY bm25(conv) LIMIT ?", (match, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [{"source": s, "date": d, "title": t, "conversation_id": c, "snippet": (snip or "").strip()}
            for s, d, t, c, snip in rows]


def tokenizer_of(db):
    try:
        con = sqlite3.connect(db)
        row = con.execute("SELECT v FROM idxmeta WHERE k='tokenizer'").fetchone()
        con.close()
        return row[0] if row else "?"
    except sqlite3.OperationalError:
        return "?"


def search(q, limit, db=None, raw=False, exact=False):
    db = Path(db) if db else DEFAULT_DB
    if not db.exists():
        print("index missing; building first...")
        build(DEFAULT_INBOX)
    if tokenizer_of(db) != "trigram":
        print("WARNING: index not built with the 'trigram' tokenizer; CJK / substring "
              "matches may be missing. Rebuild on SQLite>=3.34.")
    hits = query(q, limit, db=db, raw=raw, exact=exact)
    if not hits:
        print(f"no matches for: {q}")
        return
    print(f"{len(hits)} hits for: {q}\n")
    for h in hits:
        print(f"[{h['source']}] {h['date']}  {h['title']}")
        print(f"    {h['snippet']}")
        print(f"    id={h['conversation_id']}\n")


def main():
    C.cli_help(__doc__)
    args = sys.argv[1:]
    raw = "--raw" in args
    exact = "--exact" in args
    args = [a for a in args if a not in ("--raw", "--exact")]
    if "--build" in args:
        args = [a for a in args if a != "--build"]
        inbox = Path(args[0]).resolve() if args else DEFAULT_INBOX
        build(inbox)
        return
    limit = 12
    if "-n" in args:
        i = args.index("-n")
        limit = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        return
    search(" ".join(args), limit, raw=raw, exact=exact)


if __name__ == "__main__":
    main()
