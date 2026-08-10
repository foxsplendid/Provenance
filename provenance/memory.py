#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integrated memory hub store (Phase A) — structured research context beyond chat.

Provenance started as a conversation archive; Phase A turns it into a memory hub
that also holds the user's *reference library* (Steward `library-kb/1.x`,
including 1.0 and 1.1; 1.1 adds per-item `citekey`) and *project portfolio*
(Obsidian Projects frontmatter). Those normalized,
sanitized snapshots live under `$PROVENANCE_HOME/memory/`:

    memory/library.json      reference library snapshot (literature)
    memory/projects.json     project portfolio (project/1.0 frontmatter)
    memory/research-artifacts.json  parsed papers, reading notes, reviews, lineage

Written by the library, vault and research-artifact ingesters; read by the
read-only MCP tools, Context Capsule builder and search indexing.
Stdlib only, no network — same privacy posture as the rest of the pipeline.

Also home to a minimal YAML-frontmatter parser (a stdlib subset, NOT PyYAML) so
the vault ingester stays zero-dependency.
"""
import json
import re
from pathlib import Path

from provenance import common as C

LIBRARY = "library.json"
PROJECTS = "projects.json"
APPLIED_CLAIMS = "applied_claims.json"   # sync-layer M3 approved high-value claims
RESEARCH_ARTIFACTS = "research-artifacts.json"

# high-value claim fields an approved session-summary overlays onto a project record
APPLIED_FIELDS = ("status", "stage", "next_actions", "conclusion", "blocked_by")


def mem_dir(root=None):
    """The memory store directory, beside inbox/ under the data root."""
    return (root or C.data_root()) / "memory"


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_library(root=None):
    """Return the list of reference-library items (empty if not ingested)."""
    items = _read(mem_dir(root) / LIBRARY).get("items")
    return items if isinstance(items, list) else []


def load_research_artifacts(root=None):
    """Return sanitized literature-research artifact records, or an empty list.

    Each record contains ``artifact_id``, ``artifact_type``, ``schema_version``
    and the original contract object under ``content``. The research ingester
    guarantees stable IDs, path removal and privacy sanitization before write.
    """
    artifacts = _read(mem_dir(root) / RESEARCH_ARTIFACTS).get("artifacts")
    return artifacts if isinstance(artifacts, list) else []


def load_applied_claims(root=None):
    """Return {project_id: approved high-value claims} (sync-layer M3 overlay).

    projects.json is a vault snapshot that prov-ingest-vault rewrites wholesale, so
    a one-click-approved claim (status/next_actions/...) is kept here, beside the
    snapshot, and merged on read by load_projects() — surviving vault re-ingest."""
    d = _read(mem_dir(root) / APPLIED_CLAIMS).get("claims")
    return d if isinstance(d, dict) else {}


def load_projects(root=None):
    """Return {project_id: project record} (empty if not ingested).

    The approved-claims overlay (load_applied_claims) wins for the high-value fields
    on a matching project, so the MCP portfolio/context reflect one-click approvals
    even after a vault snapshot rewrite. Never fabricates a project not in the
    snapshot; the resolver's keys (linked_repo/project_id) are untouched."""
    d = _read(mem_dir(root) / PROJECTS).get("projects")
    projects = d if isinstance(d, dict) else {}
    for pid, claims in load_applied_claims(root).items():
        rec = projects.get(pid)
        if isinstance(rec, dict) and isinstance(claims, dict):
            for k in APPLIED_FIELDS:
                if k in claims:
                    rec[k] = claims[k]
    return projects


def schema_ok(value, prefix):
    """True for a numeric minor in the supported v1 major line."""
    return isinstance(value, str) and re.fullmatch(
        re.escape(prefix) + r"/1\.[0-9]+", value) is not None


# --- minimal YAML frontmatter parser (stdlib subset) ----------------------
# Handles exactly what the project/1.0 contract uses:
# scalar values, flow sequences [a, b], flow mappings {k: v}, block sequences
# (- item) and one level of block mapping. Not a general YAML implementation.

def _coerce(s):
    s = s.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _split_top_commas(s):
    """Split on commas not inside quotes or nested [] / {}."""
    out, buf, q, depth = [], [], None, 0
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def _parse_value(val):
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        return [_coerce(x) for x in _split_top_commas(val[1:-1]) if x.strip()]
    if val.startswith("{") and val.endswith("}"):
        out = {}
        for part in _split_top_commas(val[1:-1]):
            if ":" in part:
                k, v = part.split(":", 1)
                out[_coerce(k)] = _coerce(v)
        return out
    return _coerce(val)


def _collect_block(lines, start, parent_indent):
    """Gather lines more indented than parent_indent into a block list or map.
    Returns (value_or_None, lines_consumed)."""
    sub = []
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= parent_indent:
            break
        sub.append(raw.strip())
        i += 1
    consumed = i - start
    if not sub:
        return None, 0
    if all(s == "-" or s.startswith("- ") for s in sub):
        return [_parse_value(s[1:].strip()) for s in sub if s != "-"], consumed
    out = {}
    for s in sub:
        m = re.match(r"([^:]+):\s*(.*)$", s)
        if m:
            out[_coerce(m.group(1))] = _parse_value(m.group(2))
    return out, consumed


def parse_frontmatter(text):
    """Parse a Markdown note's leading `--- ... ---` YAML frontmatter.
    Returns (frontmatter_dict, body_text). Empty dict if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    fm_lines, body = lines[1:end], "\n".join(lines[end + 1:])
    result, i = {}, 0
    while i < len(fm_lines):
        raw = fm_lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or (len(raw) - len(raw.lstrip())) > 0:
            i += 1  # blank / comment / stray child line (children are consumed below)
            continue
        m = re.match(r"([^:]+):\s*(.*)$", stripped)
        if not m:
            i += 1
            continue
        key, val = _coerce(m.group(1)), m.group(2).strip()
        if val == "":
            block, consumed = _collect_block(fm_lines, i + 1, 0)
            result[key] = "" if block is None else block
            i += 1 + consumed
        else:
            result[key] = _parse_value(val)
            i += 1
    return result, body
