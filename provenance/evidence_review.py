#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local, single-claim human review for imported research evidence.

The candidate registry is untrusted. A decision becomes current only after an
exact Approvals.md item is checked and a matching zero-write plan is replayed
under the pull lock. Receipts are protected and append-only; reviewed Claim
files are cross-component projections that can be rebuilt from those receipts.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from provenance import __version__, common as C, memory as M
from provenance.ingest import evidence as E
from provenance.ingest import research as _safe_io
from provenance.synclayer import paths, worker


RECEIPT_VERSION = "evidence-review-receipt/1.0"
PLAN_VERSION = "evidence-review-plan/1.0"
REPORT_VERSION = "evidence-review-report/1.0"
STATUS_VERSION = "evidence-status/1.0"
DECISIONS = {"accepted", "rejected", "superseded"}
TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MARKER_RE = re.compile(
    r"<!-- evidence:(?P<claim>[a-z0-9][a-z0-9-]*):"
    r"(?P<fingerprint>[a-f0-9]{64}) -->"
)
_CHECKBOX_RE = re.compile(r"^- \[([ xX])\] ")
_FIELD_RE = re.compile(r"^    - (decision|superseded_by):\s*(.*?)\s*$")
_RECEIPT_FIELDS = frozenset({
    "schema_version", "receipt_id", "claim_id", "project",
    "source_fingerprint", "content_fingerprint", "decision",
    "superseded_by", "reviewed_at", "previous_receipt_id",
    "run_fingerprints", "artifacts", "queue_fingerprint", "plan_id",
    "reviewed_claim",
})


class ReviewError(RuntimeError):
    """Fail-closed review error carrying only a stable public code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class PlanMismatch(ReviewError):
    def __init__(self):
        super().__init__("plan-mismatch")


def _fingerprint(value):
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bytes_fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_safe_file(path, *, missing_code, invalid_code):
    try:
        absolute, info = _safe_io._optional_safe_file(path)
        if info is None:
            raise ReviewError(missing_code)
        if getattr(info, "st_nlink", 1) != 1:
            raise ReviewError(invalid_code)
        return _safe_io._read_safe_bytes(
            absolute, expected=_safe_io._file_snapshot(info)
        )
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError(invalid_code) from exc


def _load_registry(root, *, allow_missing=False):
    target = M.mem_dir(root) / M.EVIDENCE_REGISTRY
    try:
        _, info = _safe_io._optional_safe_file(target)
    except Exception as exc:
        raise ReviewError("registry-invalid") from exc
    if info is None:
        if allow_missing:
            return [], [], _fingerprint({"present": False}), None
        raise ReviewError("candidate-missing")
    if getattr(info, "st_nlink", 1) != 1:
        raise ReviewError("registry-invalid")
    try:
        raw = _safe_io._read_safe_bytes(
            target, expected=_safe_io._file_snapshot(info)
        )
        document = E._parse_json_bytes(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "runs", "candidate_claims"}
            or document.get("schema_version") != E.STORE_VERSION
            or not isinstance(document.get("runs"), list)
            or not isinstance(document.get("candidate_claims"), list)
        ):
            raise ReviewError("registry-invalid")
        seen_runs = set()
        for index, record in enumerate(document["runs"]):
            E._validate_run_record(record, "run")
            if record["run_id"] in seen_runs:
                raise ReviewError("registry-invalid")
            seen_runs.add(record["run_id"])
        seen_claims = set()
        for index, record in enumerate(document["candidate_claims"]):
            E._validate_claim_record(record, "claim")
            if record["claim_id"] in seen_claims:
                raise ReviewError("registry-invalid")
            seen_claims.add(record["claim_id"])
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("registry-invalid") from exc
    return (
        document["runs"], document["candidate_claims"],
        _fingerprint({"present": True, "document": document}),
        _bytes_fingerprint(raw),
    )


def _load_project(root, project_id):
    target = M.mem_dir(root) / M.PROJECTS
    raw = _read_safe_file(
        target, missing_code="project-missing", invalid_code="project-invalid"
    )
    try:
        document = E._parse_json_bytes(raw)
        projects = document.get("projects") if isinstance(document, dict) else None
        if not isinstance(projects, dict) or not isinstance(projects.get(project_id), dict):
            raise ReviewError("project-missing")
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("project-invalid") from exc
    return _bytes_fingerprint(raw), _fingerprint(projects[project_id])


def _parse_queue(workspace, claim_id, source_fingerprint):
    raw = _read_safe_file(
        Path(workspace) / "Approvals.md",
        missing_code="approval-missing",
        invalid_code="approval-invalid",
    )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ReviewError("approval-invalid") from exc

    matches = []
    current = None
    for line in text.splitlines():
        checkbox = _CHECKBOX_RE.match(line)
        marker_matches = list(_MARKER_RE.finditer(line))
        if checkbox:
            current = None
            if marker_matches:
                if len(marker_matches) != 1:
                    raise ReviewError("approval-invalid")
                marker = marker_matches[0]
                current = {
                    "claim_id": marker.group("claim"),
                    "source_fingerprint": marker.group("fingerprint"),
                    "checked": checkbox.group(1).lower() == "x",
                    "fields": {},
                }
                matches.append(current)
            continue
        if marker_matches:
            raise ReviewError("approval-invalid")
        if current is not None:
            field = _FIELD_RE.match(line)
            if field:
                name, value = field.groups()
                if name in current["fields"]:
                    raise ReviewError("approval-invalid")
                current["fields"][name] = value.strip()

    selected = [item for item in matches if item["claim_id"] == claim_id]
    if len(selected) != 1:
        raise ReviewError("approval-missing" if not selected else "approval-invalid")
    item = selected[0]
    if item["source_fingerprint"] != source_fingerprint:
        raise ReviewError("candidate-changed")
    if not item["checked"]:
        raise ReviewError("approval-not-checked")
    decision = item["fields"].get("decision", "")
    if decision == "pending" or not decision:
        raise ReviewError("decision-required")
    if decision not in DECISIONS:
        raise ReviewError("decision-invalid")
    superseded_by = item["fields"].get("superseded_by") or None
    if decision == "superseded":
        if not isinstance(superseded_by, str) or _SLUG_RE.fullmatch(superseded_by) is None:
            raise ReviewError("superseded-by-required")
    elif superseded_by is not None:
        raise ReviewError("approval-invalid")
    return {
        "decision": decision,
        "superseded_by": superseded_by,
        "queue_fingerprint": _bytes_fingerprint(raw),
    }


def _flat_json_files(directory):
    directory = Path(directory)
    try:
        absolute, info = _safe_io._safe_chain(directory, allow_missing_tail=True)
        if info is None:
            return []
        if not stat.S_ISDIR(info.st_mode):
            raise ReviewError("review-store-invalid")
        before_identity = _safe_io._file_identity(info)
        with os.scandir(absolute) as entries:
            children = sorted(list(entries), key=lambda entry: entry.name)
        _, after = _safe_io._require_safe_directory(absolute)
        if _safe_io._file_identity(after) != before_identity:
            raise ReviewError("review-store-invalid")
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("review-store-invalid") from exc

    files = []
    for entry in children:
        try:
            path = absolute / entry.name
            _, info = _safe_io._safe_chain(path)
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
                or re.fullmatch(r"[a-f0-9]{64}\.json", entry.name) is None
            ):
                raise ReviewError("review-store-invalid")
            raw = _safe_io._read_safe_bytes(
                path, expected=_safe_io._file_snapshot(info)
            )
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError("review-store-invalid") from exc
        files.append((entry.name[:-5], raw))
    return files




def _canonical_records(directory, record_type):
    """Strictly read one flat canonical JSON contract directory."""
    directory = Path(directory)
    try:
        absolute, info = _safe_io._safe_chain(directory, allow_missing_tail=True)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ReviewError("canonical-source-missing")
        identity = _safe_io._file_identity(info)
        with os.scandir(absolute) as entries:
            children = sorted(list(entries), key=lambda entry: entry.name)
        _, after = _safe_io._require_safe_directory(absolute)
        if _safe_io._file_identity(after) != identity:
            raise ReviewError("canonical-source-changed")
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("canonical-source-invalid") from exc

    records = {}
    identity_field = "claim_id" if record_type == "claim" else "run_id"
    for entry in children:
        try:
            path = absolute / entry.name
            _, info = _safe_io._safe_chain(path)
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
                or not entry.name.lower().endswith(".json")
            ):
                raise ReviewError("canonical-source-invalid")
            raw = _safe_io._read_safe_bytes(
                path, expected=_safe_io._file_snapshot(info)
            )
            document = E._parse_json_bytes(raw)
            actual_type, normalized = E.normalize_document(document)
            if actual_type != record_type:
                raise ReviewError("canonical-source-invalid")
            identity_value = normalized[identity_field]
            if identity_value in records:
                raise ReviewError("canonical-source-duplicate")
            records[identity_value] = {
                "record": normalized,
                "file_fingerprint": _bytes_fingerprint(raw),
            }
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError("canonical-source-invalid") from exc
    return records
def _validate_receipt(receipt_id, receipt):
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ReviewError("receipt-invalid")
    if receipt.get("schema_version") != RECEIPT_VERSION:
        raise ReviewError("receipt-invalid")
    if receipt.get("receipt_id") != receipt_id or _SHA256_RE.fullmatch(receipt_id) is None:
        raise ReviewError("receipt-invalid")
    payload = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if _fingerprint(payload) != receipt_id:
        raise ReviewError("receipt-invalid")
    if (
        not isinstance(receipt.get("claim_id"), str)
        or _SLUG_RE.fullmatch(receipt["claim_id"]) is None
        or not isinstance(receipt.get("project"), str)
        or _SLUG_RE.fullmatch(receipt["project"]) is None
        or receipt.get("decision") not in DECISIONS
        or _SHA256_RE.fullmatch(str(receipt.get("source_fingerprint", ""))) is None
        or _SHA256_RE.fullmatch(str(receipt.get("content_fingerprint", ""))) is None
        or _SHA256_RE.fullmatch(str(receipt.get("queue_fingerprint", ""))) is None
        or _SHA256_RE.fullmatch(str(receipt.get("plan_id", ""))) is None
    ):
        raise ReviewError("receipt-invalid")
    previous = receipt.get("previous_receipt_id")
    if previous is not None and _SHA256_RE.fullmatch(str(previous)) is None:
        raise ReviewError("receipt-invalid")
    superseded_by = receipt.get("superseded_by")
    if receipt["decision"] == "superseded":
        if not isinstance(superseded_by, str) or _SLUG_RE.fullmatch(superseded_by) is None:
            raise ReviewError("receipt-invalid")
    elif superseded_by is not None:
        raise ReviewError("receipt-invalid")
    try:
        E._date_time(receipt.get("reviewed_at"), "reviewed_at")
        reviewed = receipt.get("reviewed_claim")
        if E.validate_document(reviewed) != "claim":
            raise ReviewError("receipt-invalid")
    except Exception as exc:
        raise ReviewError("receipt-invalid") from exc
    if (
        reviewed.get("claim_id") != receipt["claim_id"]
        or reviewed.get("project") != receipt["project"]
        or reviewed.get("review_state") != receipt["decision"]
        or reviewed.get("reviewed_at") != receipt["reviewed_at"]
        or reviewed.get("superseded_by") != superseded_by
    ):
        raise ReviewError("receipt-invalid")
    run_fingerprints = receipt.get("run_fingerprints")
    artifacts = receipt.get("artifacts")
    if not isinstance(run_fingerprints, list) or not isinstance(artifacts, list):
        raise ReviewError("receipt-invalid")
    seen_runs = set()
    for item in run_fingerprints:
        if (
            not isinstance(item, dict)
            or set(item) != {"run_id", "source_fingerprint", "content_fingerprint"}
            or not isinstance(item.get("run_id"), str)
            or _SLUG_RE.fullmatch(item["run_id"]) is None
            or _SHA256_RE.fullmatch(str(item.get("source_fingerprint", ""))) is None
            or _SHA256_RE.fullmatch(str(item.get("content_fingerprint", ""))) is None
            or item["run_id"] in seen_runs
        ):
            raise ReviewError("receipt-invalid")
        seen_runs.add(item["run_id"])
    for item in artifacts:
        if (
            not isinstance(item, dict)
            or set(item) != {
                "run_id", "artifact_id", "locator", "declared_sha256",
                "observed_sha256",
            }
            or not isinstance(item.get("locator"), str)
            or _SLUG_RE.fullmatch(str(item.get("run_id", ""))) is None
            or _SLUG_RE.fullmatch(str(item.get("artifact_id", ""))) is None
            or _SHA256_RE.fullmatch(str(item.get("declared_sha256", ""))) is None
            or item.get("observed_sha256") != item.get("declared_sha256")
        ):
            raise ReviewError("receipt-invalid")


def _load_receipts(root):
    receipts = {}
    for receipt_id, raw in _flat_json_files(paths.evidence_review_receipts_dir(root)):
        try:
            receipt = E._parse_json_bytes(raw)
            _validate_receipt(receipt_id, receipt)
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError("receipt-invalid") from exc
        receipts[receipt_id] = receipt

    groups = {}
    for receipt in receipts.values():
        groups.setdefault(receipt["claim_id"], []).append(receipt)
    heads = {}
    for claim_id, group in groups.items():
        ids = {item["receipt_id"] for item in group}
        previous_ids = set()
        for item in group:
            previous = item["previous_receipt_id"]
            if previous is not None:
                if previous not in ids or previous in previous_ids:
                    raise ReviewError("receipt-chain-invalid")
                if receipts[previous]["claim_id"] != claim_id:
                    raise ReviewError("receipt-chain-invalid")
                previous_ids.add(previous)
        head_ids = ids - previous_ids
        roots = [item for item in group if item["previous_receipt_id"] is None]
        if len(head_ids) != 1 or len(roots) != 1:
            raise ReviewError("receipt-chain-invalid")
        head = next(iter(head_ids))
        visited = set()
        current = head
        while current is not None:
            if current in visited:
                raise ReviewError("receipt-chain-invalid")
            visited.add(current)
            current = receipts[current]["previous_receipt_id"]
        if visited != ids:
            raise ReviewError("receipt-chain-invalid")
        heads[claim_id] = head
    return receipts, heads


def _projection_dir(workspace):
    return Path(workspace) / "Evidence" / "Claims" / "reviewed"


def _load_projection_state(workspace, receipts):
    present = {}
    for receipt_id, raw in _flat_json_files(_projection_dir(workspace)):
        receipt = receipts.get(receipt_id)
        if receipt is None:
            raise ReviewError("projection-invalid")
        try:
            document = E._parse_json_bytes(raw)
        except Exception as exc:
            raise ReviewError("projection-invalid") from exc
        if document != receipt["reviewed_claim"]:
            raise ReviewError("projection-invalid")
        present[receipt_id] = _bytes_fingerprint(raw)
    return present


def _relative_locator(locator):
    if not isinstance(locator, str) or not locator or "\x00" in locator:
        raise ReviewError("artifact-locator-invalid")
    if (
        locator.startswith("/") or locator.startswith("\\")
        or "\\" in locator or re.match(r"(?i)^file:", locator)
        or re.match(r"(?i)^[a-z]:", locator) or ":" in locator
    ):
        raise ReviewError("artifact-locator-invalid")
    parts = locator.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ReviewError("artifact-locator-invalid")
    return parts


def _safe_artifact_root(workspace, artifact_root):
    selected = Path(artifact_root) if artifact_root is not None else Path(workspace)
    try:
        absolute, info = _safe_io._require_safe_directory(selected)
    except Exception as exc:
        raise ReviewError("artifact-root-invalid") from exc
    return absolute, _fingerprint({"root": os.path.normcase(os.path.abspath(str(absolute)))})


def _hash_artifact(root, locator):
    parts = _relative_locator(locator)
    target = Path(root).joinpath(*parts)
    try:
        absolute, before = _safe_io._safe_chain(target)
        if (
            before is None or not stat.S_ISREG(before.st_mode)
            or getattr(before, "st_nlink", 1) != 1
        ):
            raise ReviewError("artifact-file-invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(absolute), flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_nlink", 1) != 1
                or _safe_io._file_identity(opened) != _safe_io._file_identity(before)
            ):
                raise ReviewError("artifact-file-invalid")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                digest.update(chunk)
            after_read = os.fstat(fd)
            if _safe_io._file_snapshot(after_read) != _safe_io._file_snapshot(opened):
                raise ReviewError("artifact-changed")
        finally:
            os.close(fd)
        _, after_path = _safe_io._safe_chain(absolute)
        if (
            after_path is None
            or getattr(after_path, "st_nlink", 1) != 1
            or _safe_io._file_snapshot(after_path) != _safe_io._file_snapshot(opened)
        ):
            raise ReviewError("artifact-changed")
        return digest.hexdigest()
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("artifact-file-invalid") from exc


def _validate_accepted_evidence(claim, runs, artifact_root):
    runs_by_id = {record["run_id"]: record for record in runs}
    run_refs = {}
    artifacts = {}
    for item in claim["content"].get("evidence", []):
        if item.get("source_type") != "experiment-run":
            continue
        try:
            E._validate_run_link(claim, item, runs_by_id)
        except Exception as exc:
            raise ReviewError("evidence-link-invalid") from exc
        run = runs_by_id[item["source_id"]]
        if run["project"] != claim["project"]:
            raise ReviewError("run-project-mismatch")
        if run["content"].get("status") not in TERMINAL_RUN_STATUSES:
            raise ReviewError("run-not-terminal")
        run_refs[run["run_id"]] = {
            "run_id": run["run_id"],
            "source_fingerprint": run["source_fingerprint"],
            "content_fingerprint": run["content_fingerprint"],
        }
        run_artifacts = {
            artifact["artifact_id"]: artifact
            for artifact in run["content"].get("artifacts", [])
        }
        artifact = run_artifacts.get(item.get("artifact_id"))
        if artifact is None or artifact.get("sha256") != item.get("sha256"):
            raise ReviewError("evidence-link-invalid")
        locator = artifact.get("locator")
        observed = _hash_artifact(artifact_root, locator)
        if observed != item["sha256"]:
            raise ReviewError("artifact-digest-mismatch")
        key = (run["run_id"], item["artifact_id"])
        observation = {
            "run_id": run["run_id"],
            "artifact_id": item["artifact_id"],
            "locator": locator,
            "declared_sha256": item["sha256"],
            "observed_sha256": observed,
        }
        prior = artifacts.get(key)
        if prior is not None and prior != observation:
            raise ReviewError("evidence-link-invalid")
        artifacts[key] = observation
    return (
        [run_refs[key] for key in sorted(run_refs)],
        [artifacts[key] for key in sorted(artifacts)],
    )


def _build_reviewed_claim(candidate, decision, superseded_by, reviewed_at):
    reviewed = json.loads(json.dumps(candidate["content"], ensure_ascii=False))
    reviewed["review_state"] = decision
    reviewed["reviewed_at"] = reviewed_at
    if decision == "superseded":
        reviewed["superseded_by"] = superseded_by
    else:
        reviewed.pop("superseded_by", None)
    try:
        E.validate_document(reviewed)
    except Exception as exc:
        raise ReviewError("reviewed-claim-invalid") from exc
    return reviewed


def _current_plan(workspace, claim_id, artifact_root=None, root=None):
    workspace = Path(workspace)
    try:
        workspace, _ = _safe_io._require_safe_directory(workspace)
    except Exception as exc:
        raise ReviewError("workspace-invalid") from exc
    if not isinstance(claim_id, str) or _SLUG_RE.fullmatch(claim_id) is None:
        raise ReviewError("claim-id-invalid")

    root = Path(root) if root is not None else C.data_root()
    try:
        root, _ = _safe_io._require_safe_directory(root)
    except Exception as exc:
        raise ReviewError("data-root-invalid") from exc

    runs, candidates, registry_fingerprint, registry_file_fingerprint = _load_registry(root)
    matching = [record for record in candidates if record["claim_id"] == claim_id]
    if len(matching) != 1:
        raise ReviewError("candidate-missing" if not matching else "registry-invalid")
    candidate = matching[0]
    canonical_claims = _canonical_records(
        workspace / "Evidence" / "Claims" / "pending", "claim"
    )
    canonical_candidate = canonical_claims.get(claim_id)
    if canonical_candidate is None:
        raise ReviewError("canonical-source-missing")
    if canonical_candidate["record"] != candidate:
        raise ReviewError("candidate-changed")
    candidate_file_fingerprint = canonical_candidate["file_fingerprint"]
    project_file_fingerprint, project_fingerprint = _load_project(
        root, candidate["project"]
    )
    approval = _parse_queue(workspace, claim_id, candidate["source_fingerprint"])
    decision = approval["decision"]
    superseded_by = approval["superseded_by"]

    if decision == "superseded":
        replacements = [
            record for record in candidates
            if record["claim_id"] == superseded_by
        ]
        if (
            superseded_by == claim_id or len(replacements) != 1
            or replacements[0]["project"] != candidate["project"]
        ):
            raise ReviewError("superseded-by-invalid")
        canonical_replacement = canonical_claims.get(superseded_by)
        if (
            canonical_replacement is None
            or canonical_replacement["record"] != replacements[0]
        ):
            raise ReviewError("superseded-by-invalid")
        replacement_ref = {
            "claim_id": replacements[0]["claim_id"],
            "source_fingerprint": replacements[0]["source_fingerprint"],
            "content_fingerprint": replacements[0]["content_fingerprint"],
        }
    else:
        replacement_ref = None

    root_fingerprint = None
    run_fingerprints = []
    artifacts = []
    run_source_files = []
    if decision == "accepted":
        canonical_runs = _canonical_records(workspace / "Evidence" / "Runs", "run")
        registry_runs = {record["run_id"]: record for record in runs}
        referenced_run_ids = sorted({
            item["source_id"]
            for item in candidate["content"].get("evidence", [])
            if item.get("source_type") == "experiment-run"
        })
        source_runs = []
        for run_id in referenced_run_ids:
            canonical_run = canonical_runs.get(run_id)
            registry_run = registry_runs.get(run_id)
            if canonical_run is None or registry_run is None:
                raise ReviewError("canonical-source-missing")
            if canonical_run["record"] != registry_run:
                raise ReviewError("run-source-changed")
            source_runs.append(canonical_run["record"])
            run_source_files.append({
                "run_id": run_id,
                "file_fingerprint": canonical_run["file_fingerprint"],
            })
        selected_root, root_fingerprint = _safe_artifact_root(workspace, artifact_root)
        run_fingerprints, artifacts = _validate_accepted_evidence(
            candidate, source_runs, selected_root
        )

    receipts, heads = _load_receipts(root)
    projection_present = _load_projection_state(workspace, receipts)
    previous_receipt_id = heads.get(claim_id)
    previous = receipts.get(previous_receipt_id)
    exact_previous = bool(
        previous
        and previous["source_fingerprint"] == candidate["source_fingerprint"]
        and previous["content_fingerprint"] == candidate["content_fingerprint"]
        and previous["decision"] == decision
        and previous["superseded_by"] == superseded_by
    )
    if exact_previous:
        target_receipt_id = previous_receipt_id
        action = (
            "noop" if target_receipt_id in projection_present
            else "recover-projection"
        )
    else:
        target_receipt_id = None
        action = "create-receipt"

    claim_receipt_ids = sorted(
        receipt_id for receipt_id, receipt in receipts.items()
        if receipt["claim_id"] == claim_id
    )
    projection_state = [
        {
            "receipt_id": receipt_id,
            "file_fingerprint": projection_present.get(receipt_id),
        }
        for receipt_id in claim_receipt_ids
    ]
    descriptor = {
        "schema_version": PLAN_VERSION,
        "claim_id": claim_id,
        "project": candidate["project"],
        "candidate": {
            "source_fingerprint": candidate["source_fingerprint"],
            "content_fingerprint": candidate["content_fingerprint"],
        },
        "candidate_file_fingerprint": candidate_file_fingerprint,
        "decision": decision,
        "superseded_by": superseded_by,
        "replacement": replacement_ref,
        "queue_fingerprint": approval["queue_fingerprint"],
        "registry_fingerprint": registry_fingerprint,
        "registry_file_fingerprint": registry_file_fingerprint,
        "project_file_fingerprint": project_file_fingerprint,
        "project_fingerprint": project_fingerprint,
        "artifact_root_fingerprint": root_fingerprint,
        "run_fingerprints": run_fingerprints,
        "artifacts": artifacts,
        "run_source_files": run_source_files,
        "previous_receipt_id": previous_receipt_id,
        "target_receipt_id": target_receipt_id,
        "projection_state": projection_state,
        "action": action,
    }
    return {
        "plan_id": _fingerprint(descriptor),
        "descriptor": descriptor,
        "candidate": candidate,
        "run_fingerprints": run_fingerprints,
        "artifacts": artifacts,
        "receipts": receipts,
        "action": action,
        "target_receipt_id": target_receipt_id,
        "changed": action != "noop",
    }


def preview_review(workspace, claim_id, artifact_root=None, root=None):
    """Build a content-free, zero-write single-claim review plan."""
    return _current_plan(workspace, claim_id, artifact_root, root)


def _exclusive_json(path, document, *, invalid_code, prepublish=None):
    path = Path(path)
    created = False
    fd = None
    try:
        _safe_io._prepare_safe_directory(path.parent)
        _, existing = _safe_io._optional_safe_file(path)
        if existing is not None:
            raise ReviewError(invalid_code)
        if prepublish is not None:
            prepublish()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(str(path), flags, 0o600)
        created = True
        raw = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        _, stored = _safe_io._optional_safe_file(path)
        if stored is None or getattr(stored, "st_nlink", 1) != 1:
            raise ReviewError(invalid_code)
        stored_raw = _safe_io._read_safe_bytes(
            path, expected=_safe_io._file_snapshot(stored)
        )
        if E._parse_json_bytes(stored_raw) != document:
            raise ReviewError(invalid_code)
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError(invalid_code) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created and sys.exc_info()[0] is not None:
            try:
                _, info = _safe_io._optional_safe_file(path)
                if info is not None and getattr(info, "st_nlink", 1) == 1:
                    path.unlink()
            except Exception:
                pass


def _publish_projection(workspace, receipt):
    target = _projection_dir(workspace) / f"{receipt['receipt_id']}.json"
    try:
        _, info = _safe_io._safe_chain(target, allow_missing_tail=True)
    except Exception as exc:
        raise ReviewError("projection-invalid") from exc
    if info is not None:
        raw = _read_safe_file(
            target, missing_code="projection-invalid", invalid_code="projection-invalid"
        )
        try:
            if E._parse_json_bytes(raw) != receipt["reviewed_claim"]:
                raise ReviewError("projection-conflict")
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError("projection-invalid") from exc
        return False
    _exclusive_json(target, receipt["reviewed_claim"], invalid_code="projection-conflict")
    return True


def _commit_plan(workspace, root, plan, prepublish=None):
    action = plan["action"]
    if action in {"noop", "recover-projection"}:
        receipt = plan["receipts"][plan["target_receipt_id"]]
        published = _publish_projection(workspace, receipt)
        return receipt["receipt_id"], published

    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    descriptor = plan["descriptor"]
    candidate = plan["candidate"]
    reviewed_claim = _build_reviewed_claim(
        candidate,
        descriptor["decision"],
        descriptor["superseded_by"],
        reviewed_at,
    )
    payload = {
        "schema_version": RECEIPT_VERSION,
        "claim_id": descriptor["claim_id"],
        "project": descriptor["project"],
        "source_fingerprint": candidate["source_fingerprint"],
        "content_fingerprint": candidate["content_fingerprint"],
        "decision": descriptor["decision"],
        "superseded_by": descriptor["superseded_by"],
        "reviewed_at": reviewed_at,
        "previous_receipt_id": descriptor["previous_receipt_id"],
        "run_fingerprints": plan["run_fingerprints"],
        "artifacts": plan["artifacts"],
        "queue_fingerprint": descriptor["queue_fingerprint"],
        "plan_id": plan["plan_id"],
        "reviewed_claim": reviewed_claim,
    }
    receipt_id = _fingerprint(payload)
    receipt = dict(payload)
    receipt["receipt_id"] = receipt_id
    _exclusive_json(
        paths.evidence_review_receipts_dir(root) / f"{receipt_id}.json",
        receipt,
        invalid_code="receipt-conflict",
        prepublish=prepublish,
    )
    published = _publish_projection(workspace, receipt)
    return receipt_id, published


def execute_review(workspace, claim_id, expected_plan_id, artifact_root=None, root=None):
    """Replay one preview under both pull and registry locks."""
    if not isinstance(expected_plan_id, str) or _SHA256_RE.fullmatch(expected_plan_id) is None:
        raise PlanMismatch()
    root = Path(root) if root is not None else C.data_root()
    preflight = _current_plan(workspace, claim_id, artifact_root, root)
    if preflight["plan_id"] != expected_plan_id:
        raise PlanMismatch()

    try:
        _safe_io._prepare_safe_directory(paths.sync_state(root))
        lock_path = paths.pull_lock_file(root)
        _, lock_info = _safe_io._optional_safe_file(lock_path)
        if lock_info is not None and getattr(lock_info, "st_nlink", 1) != 1:
            raise ReviewError("review-lock-invalid")
        registry_path = M.mem_dir(root) / M.EVIDENCE_REGISTRY
        with worker._Lock(lock_path):
            with E._store_lock(registry_path) as registry_lock:
                registry_lock.validate()
                current = _current_plan(workspace, claim_id, artifact_root, root)
                if current["plan_id"] != expected_plan_id:
                    raise PlanMismatch()
                registry_lock.validate()
                final = _current_plan(workspace, claim_id, artifact_root, root)
                if final["plan_id"] != expected_plan_id:
                    raise PlanMismatch()
                def prepublish():
                    registry_lock.validate()
                    refreshed = _current_plan(
                        workspace, claim_id, artifact_root, root
                    )
                    if refreshed["plan_id"] != expected_plan_id:
                        raise PlanMismatch()
                receipt_id, published = _commit_plan(workspace, root, final, prepublish)
                registry_lock.validate()
                return {
                    "plan_id": final["plan_id"],
                    "descriptor": final["descriptor"],
                    "action": final["action"],
                    "changed": final["changed"],
                    "receipt_id": receipt_id,
                    "projection_published": published,
                }
    except (ReviewError, PlanMismatch):
        raise
    except (worker.WorkerBusy, worker.WorkerStateError) as exc:
        raise ReviewError("review-busy") from exc
    except Exception as exc:
        raise ReviewError("review-write-failed") from exc



def _verify_canonical_candidates(workspace, candidates):
    canonical = _canonical_records(
        Path(workspace) / "Evidence" / "Claims" / "pending", "claim"
    )
    by_id = {record["claim_id"]: record for record in candidates}
    if set(canonical) != set(by_id):
        raise ReviewError("candidate-changed")
    for claim_id, record in by_id.items():
        if canonical[claim_id]["record"] != record:
            raise ReviewError("candidate-changed")


def load_current_reviews(root=None, workspace=None):
    """Return current sanitized receipts, or nothing without the workspace master."""
    if workspace is None:
        return []
    root = Path(root) if root is not None else C.data_root()
    try:
        workspace, _ = _safe_io._require_safe_directory(workspace)
        root, _ = _safe_io._require_safe_directory(root)
    except Exception as exc:
        raise ReviewError("workspace-invalid") from exc
    _runs, candidates, _semantic, _raw = _load_registry(root, allow_missing=True)
    _verify_canonical_candidates(workspace, candidates)
    receipts, heads = _load_receipts(root)
    current = []
    for candidate in candidates:
        receipt = receipts.get(heads.get(candidate["claim_id"]))
        if (
            receipt is not None
            and receipt["source_fingerprint"] == candidate["source_fingerprint"]
            and receipt["content_fingerprint"] == candidate["content_fingerprint"]
        ):
            current.append(receipt)
    return sorted(current, key=lambda item: item["claim_id"])


def evidence_status(workspace, root=None):
    """Return content-free counts; stale or un-ingested masters fail closed."""
    root = Path(root) if root is not None else C.data_root()
    try:
        workspace, _ = _safe_io._require_safe_directory(workspace)
        root, _ = _safe_io._require_safe_directory(root)
    except Exception as exc:
        raise ReviewError("workspace-invalid") from exc
    runs, candidates, _semantic, _raw = _load_registry(root, allow_missing=True)
    _verify_canonical_candidates(workspace, candidates)
    receipts, heads = _load_receipts(root)
    _load_projection_state(workspace, receipts)
    counts = {
        "runs": len(runs), "pending": 0, "accepted": 0,
        "rejected": 0, "superseded": 0, "receipts": len(receipts),
    }
    for candidate in candidates:
        receipt = receipts.get(heads.get(candidate["claim_id"]))
        if (
            receipt is None
            or receipt["source_fingerprint"] != candidate["source_fingerprint"]
            or receipt["content_fingerprint"] != candidate["content_fingerprint"]
        ):
            counts["pending"] += 1
        else:
            counts[receipt["decision"]] += 1
    return counts


def current_evidence_status(workspace, root=None):
    """Public content-free status helper for Context and umbrella adapters."""
    return evidence_status(workspace, root)
class _StableParser(argparse.ArgumentParser):
    def error(self, _message):
        print("ERROR: invalid-arguments", file=sys.stderr)
        raise SystemExit(2)


def _review_parser():
    parser = _StableParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--plan-id")
    parser.add_argument("--json", action="store_true")
    return parser


def _status_parser():
    parser = _StableParser(description="Report local evidence review counts.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser


def _error(code, json_mode):
    if json_mode:
        print(json.dumps({
            "schema_version": REPORT_VERSION,
            "status": "error",
            "error_code": code,
        }, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {code}", file=sys.stderr)


def _review_report(mode, plan):
    descriptor = plan["descriptor"]
    report = {
        "schema_version": REPORT_VERSION,
        "status": "ok",
        "mode": mode,
        "claim_id": descriptor["claim_id"],
        "decision": descriptor["decision"],
        "action": plan["action"],
        "changed": plan["changed"],
        "plan_id": plan["plan_id"],
    }
    if plan.get("receipt_id"):
        report["receipt_id"] = plan["receipt_id"]
    return report


def main(argv=None):
    args = _review_parser().parse_args(argv)
    if args.run and not args.plan_id:
        _error("plan-required", args.json)
        return 2
    if not args.run and args.plan_id:
        _error("plan-not-applicable", args.json)
        return 2
    try:
        if args.run:
            plan = execute_review(
                args.workspace, args.claim_id, args.plan_id, args.artifact_root
            )
            mode = "run"
        else:
            plan = preview_review(args.workspace, args.claim_id, args.artifact_root)
            mode = "preview"
    except ReviewError as exc:
        _error(exc.code, args.json)
        return 2
    report = _review_report(mode, plan)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"evidence review {mode}: claim_id={report['claim_id']} "
            f"decision={report['decision']} action={report['action']} "
            f"plan_id={report['plan_id']}"
        )
    return 0


def status_main(argv=None):
    args = _status_parser().parse_args(argv)
    try:
        counts = evidence_status(args.workspace)
    except ReviewError as exc:
        print(json.dumps({
            "schema_version": STATUS_VERSION,
            "status": "error",
            "error_code": exc.code,
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "schema_version": STATUS_VERSION,
        "status": "ok",
        **counts,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
