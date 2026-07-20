#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the Provenance pipeline.

Single source of truth for category classification, metadata extraction, user-turn
extraction and data-root resolution (previously duplicated across organize/prep and
reached via sys.path hacks).
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# coding-agent sources whose untopical sessions default to category 08
AGENT_SOURCES = {"claude-code", "codex", "antigravity", "cursor"}

PKG_DIR = Path(__file__).resolve().parent
CATS_FILE = PKG_DIR / "categories.json"


def cli_help(doc):
    """If -h/--help is on argv, print the module docstring and exit. Lets every
    prov-* command answer --help without a full argparse setup."""
    import sys
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print((doc or "").strip())
        raise SystemExit(0)


def data_root():
    """Where user DATA lives (inbox/, drop/, output/, _prep/, profile/, vault/,
    search-index.db). Defaults to the current working directory; override with
    $PROVENANCE_HOME. Keeps data out of the installed package."""
    return Path(os.environ.get("PROVENANCE_HOME") or Path.cwd())


def load_categories(path=None):
    """Return (cats, label_by_id, ordered_ids). A categories.json in the data root
    ($PROVENANCE_HOME) overrides the package's generic ruleset entirely."""
    if path is None:
        override = data_root() / "categories.json"
        path = override if override.exists() else CATS_FILE
    cats = json.loads(Path(path).read_text(encoding="utf-8"))
    default = cats.get("default", "00")
    label = {c["id"]: c["label"] for c in cats["categories"]}
    label[default] = cats.get("default_label", "uncategorized")
    order = [c["id"] for c in cats["categories"]] + [default]
    return cats, label, order


def to_date(v):
    if not v:
        return ""
    try:  # epoch seconds (ChatGPT)
        return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        pass
    try:  # ISO 8601 (Claude / agents)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return ""


def clip(s, n=1600):
    s = re.sub(r"\n{3,}", "\n\n", s or "").strip()
    return s if len(s) <= n else s[:n] + f"\n...[truncated, {len(s)} chars]"


def classify(text, cats):
    t = (text or "").lower()
    for c in cats["categories"]:
        if any(kw.lower() in t for kw in c["keywords"]):
            return c["id"], c["label"]
    return cats.get("default", "00"), cats.get("default_label", "uncategorized")


def classify_with_fallback(text, source, cats, label):
    """classify(), but coding-agent sources that fall to default land in 08."""
    cid, lab = classify(text, cats)
    if cid == cats.get("default", "00") and source in AGENT_SOURCES:
        return "08", label.get("08", "AI编程/工程实操")
    return cid, lab


def extract_meta(obj, source, fallback_id):
    title = obj.get("title") or obj.get("name") or "(untitled)"
    date = (to_date(obj.get("update_time")) or to_date(obj.get("create_time"))
            or to_date(obj.get("updated_at")) or to_date(obj.get("created_at")))
    cid = obj.get("conversation_id") or obj.get("id") or obj.get("uuid") or fallback_id
    mapping = obj.get("mapping") if isinstance(obj.get("mapping"), dict) else {}
    chat_messages = obj.get("chat_messages") if isinstance(obj.get("chat_messages"), list) else []
    node_count = len(mapping) if mapping else len(chat_messages)

    model, first_user, earliest = "", "", None
    for node in mapping.values():  # ChatGPT mapping tree
        msg = (node or {}).get("message") or {}
        meta = msg.get("metadata") or {}
        if not model and meta.get("model_slug"):
            model = meta["model_slug"]
        if (msg.get("author") or {}).get("role") == "user":
            ct = msg.get("create_time") or 0
            parts = ((msg.get("content") or {}).get("parts")) or []
            text = " ".join(p for p in parts if isinstance(p, str)).strip()
            if text and (earliest is None or ct < earliest):
                earliest, first_user = ct, text
    if not first_user and chat_messages:  # Claude / agent chat_messages
        for m in chat_messages:
            if m.get("sender") == "human":
                t = (m.get("text") or "").strip()
                if t:
                    first_user = t
                    break
    attachments = obj.get("attachments") if isinstance(obj.get("attachments"), list) else []
    return {
        "conversation_id": cid, "title": title, "date": date, "source": source,
        "model": model, "node_count": node_count, "first_user": first_user[:200],
        "attachments": attachments,
    }


def user_turns(obj):
    turns = []
    mapping = obj.get("mapping") if isinstance(obj.get("mapping"), dict) else {}
    for node in mapping.values():  # ChatGPT mapping tree
        msg = (node or {}).get("message") or {}
        if (msg.get("author") or {}).get("role") != "user":
            continue
        parts = ((msg.get("content") or {}).get("parts")) or []
        text = " ".join(p for p in parts if isinstance(p, str)).strip()
        if text and not text.startswith("Original custom instructions"):
            turns.append((msg.get("create_time") or 0, text))
    if isinstance(obj.get("chat_messages"), list):  # Claude / agents
        for m in obj["chat_messages"]:
            if m.get("sender") == "human":
                text = (m.get("text") or "").strip()
                if text:
                    turns.append((m.get("created_at") or 0, text))
    turns.sort(key=lambda x: str(x[0]))  # str key: safe for mixed int/str timestamps (L1)
    return [t[1] for t in turns]
