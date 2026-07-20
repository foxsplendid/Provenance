#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance local read-only MCP server — expose the second brain to coding agents.

A minimal Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0),
implemented in the standard library only (NO network, NO dependencies, read-only).
Register it in Claude Code / Cursor so a live agent can query your archived,
pseudonymized cross-AI history and your de-identified profile — closing the
capture -> internalize -> feed-back loop.

Tools (all read the LOCAL pseudonymized data under $PROVENANCE_HOME):
  - search_brain(query, limit)  full-text search across conversations + literature
  - get_profile()               the de-identified profile docs (persona/report/...)
  - list_topics()               category counts from the organize catalog
  - get_portfolio()             the research portfolio (总纲): projects + stage + next steps
  - get_current_context(project_id)  one project's literature + status

Register (Claude Code example, run from your data dir or set PROVENANCE_HOME):
  claude mcp add provenance -- prov-mcp
Usage: prov-mcp   (or: python -m provenance.mcp_server)
"""
import csv
import json
import re
import sys
from collections import Counter

from provenance import common as C, memory as M
from provenance.search import core as S

VERSION = "0.17.0"
PROTOCOL = "2024-11-05"

TOOLS = [
    {"name": "search_brain",
     "description": "Full-text search the user's archived cross-AI conversations (local, pseudonymized). Returns ranked source/date/title/snippet.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer", "default": 8}},
                     "required": ["query"]}},
    {"name": "get_profile",
     "description": "Return the user's de-identified profile documents (persona card, detailed report, timeline, ideas).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_topics",
     "description": "List conversation categories and their counts from the organize catalog.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_portfolio",
     "description": "The research portfolio (总纲): every project with status, stage, priority, next actions and blockers. Call at session start to see what the user is working on and where each project stands.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_current_context",
     "description": "A single project's working context: its linked literature and latest status / next actions. Call when starting work on a project to load what the user is reading and doing.",
     "inputSchema": {"type": "object",
                     "properties": {"project_id": {"type": "string"}},
                     "required": ["project_id"]}},
]


def _search_brain(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "Provide a 'query'."
    limit = int(args.get("limit") or 8)
    hits = S.query(q, limit)
    if not hits:
        return f"No matches for: {q}"
    out = [f"{len(hits)} hits for: {q}", ""]
    for h in hits:
        out.append(f"- [{h['source']}] {h['date']} · {h['title']}\n  {h['snippet']}\n  id={h['conversation_id']}")
    return "\n".join(out)


def _get_profile(args):
    pdir = C.data_root() / "profile"
    files = sorted(pdir.glob("*.md")) if pdir.exists() else []
    if not files:
        return "No profile yet. Run the provenance-profile skill to generate profile/."
    return "\n\n---\n\n".join(f"# FILE: {f.name}\n\n{f.read_text(encoding='utf-8')}" for f in files)


def _list_topics(args):
    cat = C.data_root() / "output" / "00_catalog.csv"
    if not cat.exists():
        return "No catalog. Run prov-organize first."
    counts = Counter()
    with cat.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            counts[f"{r['cat_id']} {r['cat_label']}"] += 1
    return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "(empty)"


_PRIO = {"high": 0, "medium": 1, "low": 2, "": 3}
_STATUS = {"active": 0, "paused": 1, "planned": 2, "done": 3, "archived": 4, "": 5}
_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")


def _fmt_list(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if str(x).strip())
    return str(v or "")


def _get_portfolio(args):
    projects = M.load_projects()
    if not projects:
        return "No projects yet. Run prov-ingest-vault on your Obsidian vault (Projects/)."
    rows = sorted(projects.values(),
                  key=lambda p: (_PRIO.get((p.get("priority") or "").lower(), 3),
                                 _STATUS.get((p.get("status") or "").lower(), 5),
                                 str(p.get("project_id") or "")))
    out = [f"Research portfolio — {len(rows)} project(s):", ""]
    for p in rows:
        out.append(f"- [{p.get('status') or '?'}] {p.get('title') or p.get('project_id')} "
                   f"(id={p.get('project_id')})")
        meta = [f"{k}={p[k]}" for k in ("stage", "priority", "updated") if p.get(k)]
        if meta:
            out.append("  " + " · ".join(meta))
        na = _fmt_list(p.get("next_actions"))
        if na:
            out.append(f"  next: {na}")
        if p.get("blocked_by"):
            out.append(f"  blocked: {p['blocked_by']}")
    return "\n".join(out)


def _match_literature(linked, library):
    """Resolve a project's linked_literature (Steward item keys OR collection paths)
    to library items."""
    if not linked or not library:
        return []
    keys = {x for x in linked if _KEY_RE.match(str(x))}
    paths = [str(x) for x in linked if not _KEY_RE.match(str(x))]
    hits = []
    for it in library:
        if it.get("key") in keys:
            hits.append(it)
            continue
        folders = it.get("folders") or []
        if any(f == pth or str(f).startswith(pth + "/") for pth in paths for f in folders):
            hits.append(it)
    return hits


def _get_current_context(args):
    pid = (args.get("project_id") or "").strip()
    if not pid:
        return "Provide a 'project_id' (see get_portfolio)."
    projects = M.load_projects()
    p = projects.get(pid)
    if not p:  # tolerant: case-insensitive id match
        p = next((v for k, v in projects.items() if k.lower() == pid.lower()), None)
    if not p:
        return f"No project '{pid}'. Known: {', '.join(sorted(projects)) or '(none)'}"

    out = [f"# Project: {p.get('title') or pid} (id={p.get('project_id') or pid})",
           f"status: {p.get('status') or '?'}"
           + (f" · stage: {p['stage']}" if p.get("stage") else "")
           + (f" · priority: {p['priority']}" if p.get("priority") else "")
           + (f" · updated: {p['updated']}" if p.get("updated") else "")]
    na = _fmt_list(p.get("next_actions"))
    if na:
        out.append(f"next actions: {na}")
    if p.get("blocked_by"):
        out.append(f"blocked by: {p['blocked_by']}")
    if p.get("linked_repo"):
        out.append(f"repo: {p['linked_repo']}")

    # literature: from linked_literature (keys or collection paths)
    lit = _match_literature(p.get("linked_literature") or [], M.load_library())
    if lit:
        out.append("\n## Literature")
        for it in lit:
            yr = f" ({it['year']})" if it.get("year") else ""
            rs = f" [{it['readStatus']}]" if it.get("readStatus") else ""
            out.append(f"- {it.get('title')}{yr}{rs}"
                       + (f"\n  {it['tldr']}" if it.get("tldr") else ""))
    elif p.get("linked_literature"):
        out.append("\n## Literature\n(linked: " + _fmt_list(p.get("linked_literature"))
                   + " — not found in library; run prov-ingest-library)")

    if p.get("body"):
        out.append("\n## Recent log\n" + p["body"])
    return "\n".join(out)


HANDLERS = {"search_brain": _search_brain, "get_profile": _get_profile, "list_topics": _list_topics,
            "get_portfolio": _get_portfolio, "get_current_context": _get_current_context}


def handle(req):
    """Map a JSON-RPC request to a response dict (or None for notifications)."""
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": "provenance", "version": VERSION}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name, args = params.get("name"), (params.get("arguments") or {})
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            text = fn(args)
        except Exception as e:  # tool errors are reported in-band, not as protocol errors
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
    if mid is None:
        return None  # notification (e.g. notifications/initialized) — no response
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    # MCP is UTF-8 JSON over stdio; don't trust the console codepage (Windows
    # pipes default to GBK/OEM and mangle CJK + BOM).
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # <3.7 — not supported anyway
    for line in sys.stdin:
        i = line.find("{")  # skip BOM/garbage bytes however the pipe decoded them
        if i < 0:
            continue
        try:
            req = json.loads(line[i:].strip())
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
