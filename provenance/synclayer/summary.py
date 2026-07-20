# -*- coding: utf-8 -*-
"""Session-summary scaffold / assemble (sync-layer.md §4; Steward review.py shape).

Same `scaffold -> [external LLM/skill fills] -> assemble` philosophy as Steward
`review`: this module NEVER calls an LLM. It only

  (a) build_scaffold: distils ONE session's already-sanitized turns into an
      LLM-facing scaffold — the absolute cwd (`project` field of the inbox record)
      is dropped (§6); and
  (b) assemble: validates a filled draft and builds the authoritative
      `session-summary/1.0`.

The summary's identity fields (summary_id / project / created) come from the
SCAFFOLD, never the fill, so an LLM cannot retarget a writeback to another project
or forge the idempotency key — the same fabrication-resistance Steward gets by
building citations from the KB, not the draft.

The deterministic halves (scaffold + assemble) are fully unit-tested; the LLM fill
between them is a documented seam with a fixture (tests/fixtures/synclayer/).

Stdlib only, no network.
"""
from provenance import common as C

SCAFFOLD_SCHEMA = "summary-scaffold/1.0"   # synclayer-local (scaffold -> fill)
FILL_SCHEMA = "summary-fill/1.0"           # synclayer-local (filled by external LLM/skill/human)
SUMMARY_SCHEMA = "session-summary/1.0"     # the frozen contract (scriptorium-spec)

STATUS_ENUM = {"planned", "active", "paused", "done", "archived"}
CONFIDENCE_ENUM = {"high", "medium", "low"}

PROMPT = """\
# How to fill this session writeback (for an LLM, a skill, or a human)

`scaffold.json` carries ONE Claude Code session's already-sanitized turns
(`turns`: role + text) plus its identity (`summary_id`, `project`, `created`).
The absolute working directory has been stripped — do not reconstruct it.

Produce JSON conforming to the `summary-fill/1.0` contract below, then submit it on
standard input through `prov-sync-fill <summary_id> --provenance-home <home>`.
Do not construct the pending path or write `fill.json` directly. The worker assembles
the accepted fill into the authoritative `session-summary/1.0` and applies it; it takes
`summary_id` / `project` / `created` from the scaffold, NOT from your fill, so you
cannot retarget the writeback.

## What to produce
- `timeline` (REQUIRED — low-risk, auto-applied WITHOUT approval): 2-6 short
  factual bullets of what happened — files/modules touched, commits, decisions,
  tests run. Facts only; each bullet must read on its own (a project-note reader
  sees it without the chat).
- High-value claims (OPTIONAL — gated; staged for one-click approval, NOT
  auto-applied):
  - `status`: one of planned|active|paused|done|archived (project state now)
  - `stage`: FREE-TEXT project phase (no enum), e.g. "drafting", "review" ("" if none)
  - `next_actions`: concrete next steps
  - `conclusion`: one-line takeaway
  - `blocked_by`: what blocks progress ("" if nothing)
  - `confidence`: high|medium|low for the high-value claims

## Rules
- Do not invent facts. If the session did not establish a status/conclusion, omit
  it — an empty high-value field is better than a guessed one.
- Output ONLY valid JSON for `fill.json`.

## summary-fill/1.0 contract
{
  "schema_version": "summary-fill/1.0",
  "timeline": ["...", "..."],
  "status": "active",
  "stage": "...",
  "next_actions": ["..."],
  "conclusion": "...",
  "blocked_by": "",
  "confidence": "medium"
}
"""


class SummaryError(RuntimeError):
    pass


def build_scaffold(record, *, summary_id, project_id, created, session_id=None):
    """Build the LLM-facing scaffold from a sanitized inbox session record.

    Includes only the already-sanitized turns; drops the absolute cwd (the inbox
    record's `project` field) — never handed to the summarizer (§6)."""
    turns = []
    for m in record.get("chat_messages", []):
        text = (m.get("text") or "").strip()
        if text:
            turns.append({
                "role": "user" if m.get("sender") == "human" else "assistant",
                "text": text,
            })
    return {
        "schema_version": SCAFFOLD_SCHEMA,
        "summary_id": summary_id,
        "project": project_id,          # worker supplies a registered project id
        "created": created,
        "session_id": session_id,
        "title": record.get("name", ""),
        "turns": turns,
    }


def validate_fill(fill):
    """Return a list of human-readable errors ([] if the fill is valid)."""
    if not isinstance(fill, dict):
        return ["fill: not an object"]
    errors = []
    if fill.get("schema_version") != FILL_SCHEMA:
        errors.append(f"schema_version must be {FILL_SCHEMA} (unfilled draft?)")

    tl = fill.get("timeline")
    if not isinstance(tl, list) or not all(isinstance(x, str) for x in tl):
        errors.append("timeline: must be a list of strings")
    elif not any(x.strip() for x in tl):
        errors.append("timeline: empty (an auto-applied writeback needs >=1 bullet)")

    st = fill.get("status")
    if st is not None and st not in STATUS_ENUM:
        errors.append(f"status: must be one of {sorted(STATUS_ENUM)} or omitted")

    na = fill.get("next_actions", [])
    if not isinstance(na, list) or not all(isinstance(x, str) for x in na):
        errors.append("next_actions: must be a list of strings")

    # stage is a FREE-TEXT project phase (no enum) — only a string-type check, never
    # an enum check like status. Grouped with the other free-text claims.
    for field in ("stage", "conclusion", "blocked_by"):
        v = fill.get(field)
        if v is not None and not isinstance(v, str):
            errors.append(f"{field}: must be a string")

    cf = fill.get("confidence")
    if cf is not None and cf not in CONFIDENCE_ENUM:
        errors.append(f"confidence: must be one of {sorted(CONFIDENCE_ENUM)} or omitted")
    return errors


def assemble(scaffold, fill):
    """Validate `fill` and build the authoritative session-summary/1.0.

    Identity (summary_id / project / created / session_id) is copied from the
    SCAFFOLD; only the content (timeline + gated high-value claims) comes from the
    fill. Raises SummaryError on an invalid fill."""
    project = scaffold.get("project") if isinstance(scaffold, dict) else None
    if not isinstance(project, str) or not project.strip():
        raise SummaryError("project: must be a resolved non-empty project_id")

    errs = validate_fill(fill)
    if errs:
        raise SummaryError("; ".join(errs))

    summary = {
        "schema_version": SUMMARY_SCHEMA,
        "summary_id": scaffold["summary_id"],   # identity from scaffold, not fill
        "project": project,
        "created": scaffold["created"],
        "approval_state": "draft",
        "session_id": scaffold.get("session_id"),
        "timeline": [x for x in fill.get("timeline", []) if x.strip()],
        "generated_by": "provenance synclayer worker",
    }
    # gated high-value claims, copied verbatim only when present
    if fill.get("status"):
        summary["status"] = fill["status"]
    if (fill.get("stage") or "").strip():          # free-text project phase (no enum)
        summary["stage"] = fill["stage"].strip()
    na = [x for x in fill.get("next_actions", []) if x.strip()]
    if na:
        summary["next_actions"] = na
    if (fill.get("conclusion") or "").strip():
        summary["conclusion"] = fill["conclusion"].strip()
    if (fill.get("blocked_by") or "").strip():
        summary["blocked_by"] = fill["blocked_by"].strip()
    if fill.get("confidence"):
        summary["confidence"] = fill["confidence"]
    return summary


def render_timeline_block(summary):
    """The auto, low-risk block appended to the project note (timeline ONLY; the
    gated high-value claims stay in the draft until approved). append_entry() adds
    the `<!-- entry:<summary_id> -->` marker and normalizes newlines."""
    date = C.to_date(summary.get("created")) or (summary.get("created") or "")[:10]
    lines = [f"**{date}** · session writeback"]
    for bullet in summary.get("timeline", []):
        lines.append(f"- {bullet}")
    return "\n".join(lines)
