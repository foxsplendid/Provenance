#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a bounded, read-only context capsule for one research project.

The capsule is deliberately narrower than ``get_current_context``.  It exposes
only the human-owned project snapshot, approved high-value overlays, low-risk
timeline entries, and explicitly labelled reference leads from literature
artifacts.  It never reads session drafts or raw conversations and it never
writes to the Provenance data root.
"""
import argparse
import json
import re
import sys

from provenance import __version__, evidence_review as ER, memory as M, redact


CAPSULE_VERSION = "context-capsule/0.2"
MAX_MARKDOWN_CHARS = 8000
MAX_JSON_BYTES = 20000
# Kept as a compatibility alias for callers that imported the old name.
MAX_JSON_CHARS = MAX_JSON_BYTES

_MAX_TEXT = 320
_MAX_ID = 160
_MAX_ACTIONS = 5
_MAX_PROGRESS_BLOCKS = 4
_MAX_PROGRESS_ITEMS = 3
_MAX_LITERATURE = 6
_MAX_ARTIFACTS = 8
_MAX_SOURCE_IDS = 6
_MAX_REFERENCE_LEADS = 6
_MAX_EXPERIMENT_RUNS = 6
_MAX_RUN_METRICS = 8
_MAX_REVIEWED_CLAIMS = 6
_MAX_EVIDENCE_REFS = 8

_BEGIN = "<!-- scriptorium:progress-log:begin -->"
_END = "<!-- scriptorium:progress-log:end -->"
_ENTRY_RE = re.compile(r"<!--\s*entry:[^>]+-->")
_DATE_RE = re.compile(r"^\*\*([^*]+)\*\*\s*[·-]\s*session writeback\s*$", re.I)
_FILE_URI_RE = re.compile(
    r"(?ix)\bfile:(?://)?[^\r\n<>\"'`|)\]]+?"
    r"(?=\s+https?://|[)\]}>\"'`]|[<|]|[\r\n]|$)"
)
_WINDOWS_ABS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9/])(?:[A-Z]:[\\/])[^\r\n<>\"'`|]+"
)
_UNC_ABS_RE = re.compile(r"(?<![\\])\\\\[^\\/\s]+[\\/][^\r\n<>\"'`|]+")
_POSIX_ABS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9:/])/(?!/)[^\r\n<>\"'`|)\]]+?"
    r"(?=\s+https?://|[)\]}>\"'`]|[<|]|[\r\n]|$)"
)


class ProjectNotFoundError(LookupError):
    """Raised when a requested project is absent from the ingested portfolio."""

    def __init__(self, project_id, known_projects=()):
        self.project_id = project_id
        self.known_projects = tuple(sorted(str(x) for x in known_projects))
        super().__init__("Project not found.")


def _bounded(value, limit=_MAX_TEXT):
    """Return one safe, single-line value with local absolute paths omitted."""
    if value is None:
        return ""
    text = str(value)
    text = _FILE_URI_RE.sub("[local path omitted]", text)
    text = _WINDOWS_ABS_RE.sub("[local path omitted]", text)
    text = _UNC_ABS_RE.sub("[local path omitted]", text)
    text = _POSIX_ABS_RE.sub("[local path omitted]", text)
    text = redact.redact(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _bounded_list(values, count, *, limit=_MAX_TEXT):
    if not isinstance(values, (list, tuple)):
        values = [values] if values not in (None, "") else []
    out = []
    for value in values:
        item = _bounded(value, limit)
        if item and item not in out:
            out.append(item)
        if len(out) >= count:
            break
    return out


def _find_project(project_id, projects):
    requested = (project_id or "").strip()
    if not requested:
        raise ProjectNotFoundError(requested, projects)
    project = projects.get(requested)
    if project is None:
        folded = requested.casefold()
        project = next(
            (value for key, value in projects.items() if str(key).casefold() == folded),
            None,
        )
    if not isinstance(project, dict):
        raise ProjectNotFoundError(requested, projects)
    return project


def _split_human_and_progress(body):
    text = str(body or "")
    begin = text.find(_BEGIN)
    if begin < 0:
        return text, ""
    end = text.find(_END, begin + len(_BEGIN))
    if end < 0:
        # A malformed generated region is not safe to reinterpret as human prose.
        return text[:begin], ""
    human = text[:begin] + text[end + len(_END):]
    return human, text[begin + len(_BEGIN):end]


def _project_goal(project):
    for key in ("goal", "objective", "research_question", "description", "purpose"):
        value = _bounded(project.get(key))
        if value:
            return value

    human, _ = _split_human_and_progress(project.get("body"))
    lines = []
    preferred = []
    sections = {"question": [], "goal": [], "intuition": []}
    active_section = None
    for raw in human.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            heading_text = heading.group(1).strip().casefold()
            if heading_text in {
                "research question", "question", "研究问题", "科研问题",
            }:
                active_section = "question"
            elif heading_text in {
                "research goal", "goal", "objective", "purpose",
                "研究目标", "科研目标", "项目目标", "目的",
            }:
                active_section = "goal"
            elif heading_text in {
                "research intuition", "intuition", "研究直觉", "科研直觉",
            }:
                active_section = "intuition"
            else:
                active_section = None
            continue
        if not line:
            continue
        match = re.match(
            r"^(?:research\s+)?(?:goal|objective|question|purpose)\s*[:：]\s*(.+)$",
            line,
            re.I,
        ) or re.match(
            r"^(?:研究目标|研究问题|科研目标|科研问题|项目目标|目的)\s*[:：]\s*(.+)$",
            line,
        )
        if match:
            preferred.append(match.group(1))
        elif active_section:
            sections[active_section].append(line)
        else:
            lines.append(line)
    section_values = (
        sections["question"] or sections["goal"] or sections["intuition"]
    )
    return _bounded((preferred or section_values or lines or [""])[0])


def _recent_progress(body):
    _, region = _split_human_and_progress(body)
    if not region:
        return []
    out = []
    for block in _ENTRY_RE.split(region):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        heading = next((line for line in lines if "session writeback" in line.lower()), "")
        match = _DATE_RE.match(heading)
        if not match:
            continue  # skips approved blocks and unknown generated content
        items = _bounded_list(
            [line[2:].strip() for line in lines if line.startswith("- ")],
            _MAX_PROGRESS_ITEMS,
        )
        if items:
            out.append({"date": _bounded(match.group(1), 40), "items": items})
        if len(out) >= _MAX_PROGRESS_BLOCKS:
            break
    return out


def _match_literature(project, library):
    linked = project.get("linked_literature") or []
    if not isinstance(linked, list):
        linked = [linked]
    direct = {str(value).casefold() for value in linked if str(value).strip()}
    rows = []
    for item in library if isinstance(library, list) else []:
        if not isinstance(item, dict):
            continue
        ids = {
            str(item.get(key) or "").casefold()
            for key in ("key", "citekey", "doi")
            if item.get(key)
        }
        folders = [str(value) for value in (item.get("folders") or [])]
        folder_match = any(
            folder.casefold() == ref or folder.casefold().startswith(ref + "/")
            for ref in direct
            for folder in folders
        )
        if not (direct.intersection(ids) or folder_match):
            continue
        rows.append({
            "id": _bounded(item.get("key") or item.get("citekey"), _MAX_ID),
            "citekey": _bounded(item.get("citekey"), _MAX_ID),
            "title": _bounded(item.get("title")),
            "year": _bounded(item.get("year"), 20),
            "read_status": _bounded(item.get("readStatus") or item.get("read_status"), 80),
            "tldr": _bounded(item.get("tldr")),
        })
    rows.sort(key=lambda row: (row["year"], row["title"], row["id"]))
    return rows


def _association_tokens(project, literature):
    linked = project.get("linked_literature") or []
    if not isinstance(linked, list):
        linked = [linked]
    tokens = {
        str(value).casefold()
        for value in linked
        if str(value).strip()
    }
    for item in literature:
        for key in ("id", "citekey"):
            if item.get(key):
                tokens.add(str(item[key]).casefold())
    return tokens


def _raw_artifact_source_ids(kind, content):
    if kind in ("parsed-paper", "reading-note"):
        return [content.get("id")]
    if kind == "review":
        table = content.get("comparison_table") or {}
        rows = table.get("rows") if isinstance(table, dict) else []
        ids = [row.get("citekey") for row in rows if isinstance(row, dict)]
        ids += list(content.get("priority_reads") or [])
        return ids
    if kind == "lineage-graph":
        return [node.get("citekey") for node in (content.get("nodes") or [])
                if isinstance(node, dict)]
    return []


def _artifact_source_ids(kind, content):
    return _bounded_list(
        _raw_artifact_source_ids(kind, content), _MAX_SOURCE_IDS, limit=_MAX_ID
    )


def _artifact_title(kind, content):
    if kind == "parsed-paper":
        metadata = content.get("metadata") or {}
        return _bounded(metadata.get("title") if isinstance(metadata, dict) else "")
    if kind == "reading-note":
        stages = content.get("stages") or {}
        glance = stages.get("glance") if isinstance(stages, dict) else {}
        return _bounded(glance.get("tldr") if isinstance(glance, dict) else content.get("id"))
    direction = content.get("direction") or {}
    return _bounded(direction.get("query") if isinstance(direction, dict) else "")


def _artifact_status(kind, content):
    if kind == "parsed-paper":
        return _bounded(content.get("parser") or "parsed", 80)
    if kind == "reading-note":
        return _bounded(content.get("read_status") or "reading note", 80)
    if kind == "review":
        return "direction synthesis"
    if kind == "lineage-graph":
        return "lineage graph"
    return "reference"


def _artifact_summary(kind, content):
    if kind == "parsed-paper":
        return _bounded(
            "Structured parse available: "
            f"{len(content.get('sections') or [])} section(s), "
            f"{len(content.get('references') or [])} reference(s), "
            f"{len(content.get('figures') or [])} figure(s)."
        )
    if kind == "reading-note":
        stages = content.get("stages") or {}
        names = sorted(stages) if isinstance(stages, dict) else []
        glance = stages.get("glance") if isinstance(stages, dict) else {}
        tldr = glance.get("tldr") if isinstance(glance, dict) else ""
        return _bounded(tldr or ("Reading stages available: " + ", ".join(names)))
    if kind == "review":
        table = content.get("comparison_table") or {}
        rows = table.get("rows") if isinstance(table, dict) else []
        return _bounded(
            f"Direction synthesis available: {len(content.get('sections') or [])} section(s), "
            f"{len(rows or [])} compared paper(s)."
        )
    if kind == "lineage-graph":
        return _bounded(
            f"Lineage source available: {len(content.get('nodes') or [])} paper(s), "
            f"{len(content.get('edges') or [])} relation(s)."
        )
    return "Reference artifact available."


def _load_related_artifacts(project, literature, root):
    loader = getattr(M, "load_research_artifacts", None)
    loaded = loader(root) if callable(loader) else []
    tokens = _association_tokens(project, literature)
    rows = []
    gaps = []
    priority_reads = []
    for record in loaded if isinstance(loaded, list) else []:
        if not isinstance(record, dict):
            continue
        kind = record.get("artifact_type")
        content = record.get("content")
        if kind not in {"parsed-paper", "reading-note", "review", "lineage-graph"}:
            continue
        if not isinstance(content, dict):
            continue
        raw_source_ids = _raw_artifact_source_ids(kind, content)
        if not tokens.intersection(
                str(value).casefold() for value in raw_source_ids if value):
            continue
        source_ids = _bounded_list(raw_source_ids, _MAX_SOURCE_IDS, limit=_MAX_ID)
        direction = content.get("direction") or {}
        query = direction.get("query") if isinstance(direction, dict) else ""
        artifact_id = record.get("artifact_id") or content.get("id") or query
        rows.append({
            "kind": kind,
            "schema_version": _bounded(record.get("schema_version") or content.get("schema_version"), 80),
            "id": _bounded(artifact_id, _MAX_ID),
            "title": _artifact_title(kind, content),
            "status": _artifact_status(kind, content),
            "source_ids": source_ids,
            "summary": _artifact_summary(kind, content),
            "trust": "reference_only",
        })
        if kind == "review":
            gaps.extend(content.get("gaps") or [])
            priority_reads.extend(content.get("priority_reads") or [])

    rows.sort(key=lambda row: (row["kind"], row["id"], row["title"]))
    return (
        rows[:_MAX_ARTIFACTS],
        _bounded_list(gaps, _MAX_REFERENCE_LEADS),
        _bounded_list(priority_reads, _MAX_REFERENCE_LEADS, limit=_MAX_ID),
    )


def _render_unbounded(capsule):
    p = capsule["project"]
    out = [
        f"# Context Capsule: {p['title'] or p['project_id']}",
        "",
        "> Trust boundary: project fields are human-authored or explicitly approved. "
        "Recent progress is auto-applied low-risk context, not an approved claim. "
        "Literature artifacts and leads below are references/candidates, not approved claims.",
        "",
        "## Project state",
        f"- Project ID: {p['project_id']}",
        f"- Status: {p['status'] or '?'}",
    ]
    for label, key in (("Stage", "stage"), ("Priority", "priority"), ("Updated", "updated"),
                       ("Goal", "goal"), ("Approved conclusion", "conclusion")):
        if p[key]:
            out.append(f"- {label}: {p[key]}")

    if capsule["next_actions"] or capsule["blocked_by"]:
        out += ["", "## Gaps and next steps"]
        for action in capsule["next_actions"]:
            out.append(f"- Next: {action}")
        if capsule["blocked_by"]:
            out.append(f"- Blocked by: {capsule['blocked_by']}")

    if capsule["recent_progress"]:
        out += ["", "## Recent low-risk progress"]
        for block in capsule["recent_progress"]:
            for item in block["items"]:
                out.append(f"- {block['date']}: {item}")

    if capsule["literature"]:
        out += ["", "## Linked literature"]
        for item in capsule["literature"]:
            suffix = " · ".join(x for x in (item["year"], item["read_status"]) if x)
            out.append(f"- {item['title'] or item['citekey'] or item['id']}"
                       + (f" ({suffix})" if suffix else ""))
            if item["tldr"]:
                out.append(f"  Reference summary: {item['tldr']}")

    if capsule["research_artifacts"]:
        out += ["", "## Research artifact hints (reference only)"]
        for item in capsule["research_artifacts"]:
            out.append(f"- [{item['kind']}] {item['title'] or item['id']}: {item['summary']}")

    leads = capsule["reference_leads"]
    if leads["gaps"] or leads["priority_reads"]:
        out += ["", "## Candidate leads from literature (not approved claims)"]
        for gap in leads["gaps"]:
            out.append(f"- Candidate gap: {gap}")
        for citekey in leads["priority_reads"]:
            out.append(f"- Suggested reading: {citekey}")
    return "\n".join(out).rstrip() + "\n"


def render_markdown(capsule):
    """Render a deterministic Markdown view, always within the hard limit."""
    text = _render_unbounded(capsule)
    if len(text) <= MAX_MARKDOWN_CHARS:
        return text
    marker = "\n\n[Capsule truncated at the 8000-character safety limit]\n"
    return text[: MAX_MARKDOWN_CHARS - len(marker)].rstrip() + marker


def _fit_json(capsule):
    """Keep the JSON CLI output bounded without changing its public envelope."""
    serialized_size = lambda: len(
        json.dumps(capsule, ensure_ascii=False, indent=2).encode("utf-8")
    )
    for key in ("research_artifacts", "literature", "recent_progress"):
        while capsule[key] and serialized_size() > MAX_JSON_BYTES:
            capsule[key].pop()
            capsule["limits"]["truncated"] = True
    while capsule["reference_leads"]["gaps"] and serialized_size() > MAX_JSON_BYTES:
        capsule["reference_leads"]["gaps"].pop()
        capsule["limits"]["truncated"] = True
    while (
        capsule["reference_leads"]["priority_reads"]
        and serialized_size() > MAX_JSON_BYTES
    ):
        capsule["reference_leads"]["priority_reads"].pop()
        capsule["limits"]["truncated"] = True
    return capsule


def build_capsule(project_id, root=None):
    """Return a deterministic, bounded, read-only context capsule dictionary."""
    project = _find_project(project_id, M.load_projects(root))
    matched_literature = _match_literature(project, M.load_library(root))
    artifacts, gaps, priority_reads = _load_related_artifacts(
        project, matched_literature, root
    )
    literature = matched_literature[:_MAX_LITERATURE]
    canonical_id = _bounded(project.get("project_id") or project_id, _MAX_ID)
    capsule = {
        "capsule_version": CAPSULE_VERSION,
        "project": {
            "project_id": canonical_id,
            "title": _bounded(project.get("title")),
            "status": _bounded(project.get("status"), 80),
            "stage": _bounded(project.get("stage")),
            "priority": _bounded(project.get("priority"), 80),
            "updated": _bounded(project.get("updated"), 80),
            "goal": _project_goal(project),
            "conclusion": _bounded(project.get("conclusion")),
        },
        "next_actions": _bounded_list(project.get("next_actions"), _MAX_ACTIONS),
        "blocked_by": _bounded(project.get("blocked_by")),
        "recent_progress": _recent_progress(
            project.get("_progress_log") or project.get("body")
        ),
        "literature": literature,
        "research_artifacts": artifacts,
        "reference_leads": {"gaps": gaps, "priority_reads": priority_reads},
        "trust": {
            "project_state": "human_or_approved",
            "recent_progress": "auto_applied_low_risk_not_approved_claims",
            "research_artifacts": "reference_only_not_approved_claims",
        },
        "limits": {"max_markdown_chars": MAX_MARKDOWN_CHARS, "truncated": False},
    }
    _fit_json(capsule)
    if len(_render_unbounded(capsule)) > MAX_MARKDOWN_CHARS:
        capsule["limits"]["truncated"] = True
    return capsule


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Print a bounded, read-only research-project context capsule."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project", required=True, help="Stable project ID")
    parser.add_argument("--json", action="store_true", help="Emit the structured JSON envelope")
    args = parser.parse_args(argv)
    try:
        capsule = build_capsule(args.project)
    except ProjectNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(capsule, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(capsule), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
