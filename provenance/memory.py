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
    memory/evidence-registry.json   derived experiment runs + untrusted claims

Written by the library, vault, research-artifact and evidence ingesters; read by the
read-only MCP tools, Context Capsule builder and search indexing.
Stdlib only, no network — same privacy posture as the rest of the pipeline.

Also home to a minimal YAML-frontmatter parser (a stdlib subset, NOT PyYAML) so
the vault ingester stays zero-dependency.
"""
import hashlib
import json
import re
from pathlib import Path

from provenance import common as C
from provenance.redact import sanitize_json

LIBRARY = "library.json"
PROJECTS = "projects.json"
APPLIED_CLAIMS = "applied_claims.json"   # sync-layer M3 approved high-value claims
RESEARCH_ARTIFACTS = "research-artifacts.json"
EVIDENCE_REGISTRY = "evidence-registry.json"
EVIDENCE_REGISTRY_VERSION = "evidence-registry/1.0"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_RUN_RECORD_FIELDS = frozenset({
    "run_id",
    "project",
    "schema_version",
    "source_fingerprint",
    "content_fingerprint",
    "content",
})
EVIDENCE_CLAIM_RECORD_FIELDS = frozenset({
    "claim_id",
    "project",
    "schema_version",
    "source_fingerprint",
    "content_fingerprint",
    "trust_state",
    "local_review_state",
    "producer_review_state",
    "content",
})

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


class EvidenceRegistryError(RuntimeError):
    """The derived evidence registry is present but unsafe or corrupt."""


def _evidence_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceRegistryError("evidence registry JSON has duplicate keys")
        result[key] = value
    return result


def _reject_evidence_constant(_value):
    raise EvidenceRegistryError("evidence registry JSON has a non-finite number")


def _evidence_fingerprint(value):
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_evidence_record(
    record,
    identity_field,
    contract_format,
    safe_io,
    *,
    claim=False,
):
    expected_fields = (
        EVIDENCE_CLAIM_RECORD_FIELDS if claim else EVIDENCE_RUN_RECORD_FIELDS
    )
    if (
        not isinstance(record, dict)
        or set(record) != expected_fields
        or not isinstance(record.get(identity_field), str)
        or not record[identity_field]
        or not isinstance(record.get("project"), str)
        or not record["project"]
        or not isinstance(record.get("schema_version"), str)
        or not isinstance(record.get("content"), dict)
        or not isinstance(record.get("source_fingerprint"), str)
        or _SHA256_RE.fullmatch(record["source_fingerprint"]) is None
        or not isinstance(record.get("content_fingerprint"), str)
        or _SHA256_RE.fullmatch(record["content_fingerprint"]) is None
    ):
        raise EvidenceRegistryError("evidence registry has an invalid record")
    content = record["content"]
    if _evidence_fingerprint(content) != record["content_fingerprint"]:
        raise EvidenceRegistryError("evidence registry content fingerprint does not match")
    if sanitize_json(safe_io._redact_local_paths(content)) != content:
        raise EvidenceRegistryError("evidence registry violates the privacy boundary")
    if (
        record["schema_version"] != content.get("schema_version")
        or re.fullmatch(
            re.escape(contract_format) + r"/1\.[0-9]+",
            record["schema_version"],
        )
        is None
        or record[identity_field] != content.get(identity_field)
        or record["project"] != content.get("project")
    ):
        raise EvidenceRegistryError("evidence registry record identity is inconsistent")
    if claim and (
        record.get("trust_state") != "untrusted-import"
        or record.get("local_review_state") != "candidate"
        or record.get("producer_review_state") != content.get("review_state")
    ):
        raise EvidenceRegistryError("evidence registry violates the candidate boundary")


def _read_evidence_registry(root=None):
    """Read evidence state strictly; missing is empty, corruption is an error."""
    path = mem_dir(root) / EVIDENCE_REGISTRY
    try:
        from provenance.ingest import research as safe_io

        _, info = safe_io._safe_chain(path, allow_missing_tail=True)
        if info is None:
            return {"runs": [], "candidate_claims": []}
        raw = safe_io._read_safe_bytes(path, expected=safe_io._file_snapshot(info))
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_evidence_object,
            parse_constant=_reject_evidence_constant,
        )
    except EvidenceRegistryError:
        raise
    except Exception as exc:
        raise EvidenceRegistryError("evidence registry is unreadable") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != EVIDENCE_REGISTRY_VERSION
        or not isinstance(document.get("runs"), list)
        or not isinstance(document.get("candidate_claims"), list)
    ):
        raise EvidenceRegistryError("evidence registry has an invalid shape")
    seen_runs = set()
    for record in document["runs"]:
        _validate_evidence_record(record, "run_id", "experiment-run", safe_io)
        if record["run_id"] in seen_runs:
            raise EvidenceRegistryError("evidence registry has duplicate run IDs")
        seen_runs.add(record["run_id"])
    seen_claims = set()
    for record in document["candidate_claims"]:
        _validate_evidence_record(
            record,
            "claim_id",
            "claim-evidence",
            safe_io,
            claim=True,
        )
        if record["claim_id"] in seen_claims:
            raise EvidenceRegistryError("evidence registry has duplicate claim IDs")
        seen_claims.add(record["claim_id"])
    return document


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


def load_experiment_runs(root=None):
    """Return imported experiment observations from the derived evidence registry."""
    return _read_evidence_registry(root)["runs"]


def load_candidate_claims(root=None):
    """Return imported, locally untrusted Claim candidates.

    Producer-declared ``content.review_state`` is retained for audit, but every
    returned record must still pass a separate local human-review workflow before
    it can become an authoritative project fact.
    """
    return _read_evidence_registry(root)["candidate_claims"]



def load_current_evidence_reviews(root=None, workspace=None):
    """Return current human decisions validated against the workspace master."""
    from provenance.evidence_review import load_current_reviews

    return load_current_reviews(root=root, workspace=workspace)


def load_reviewed_claims(root=None, workspace=None):
    """Return only currently accepted reviewed Claim contract objects."""
    return [
        receipt["reviewed_claim"]
        for receipt in load_current_evidence_reviews(root, workspace)
        if receipt["decision"] == "accepted"
    ]
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
