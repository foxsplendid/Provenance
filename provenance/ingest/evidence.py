#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingest local experiment and claim-evidence contracts as untrusted records.

Only explicitly selected JSON files or directories are read. Source files remain
the local masters and are never changed. The derived registry can be rebuilt by
re-ingesting those files.

Supported contracts:
    experiment-run/1.x
    claim-evidence/1.x

``execution.code_ref`` is data. This module never imports it, opens it, or
interprets it as a command.

Usage:
    prov-ingest-evidence Evidence/Runs Evidence/Claims/pending
    prov-ingest-evidence Evidence/Runs Evidence/Claims/pending --run --plan-id ID
    prov-ingest-evidence RUN.json CLAIM.json --memory-dir MEMORY_DIR --json
"""
import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

from provenance import __version__, common as C, memory as M
from provenance.ingest import research as _safe_io
from provenance.redact import sanitize_json
from provenance.synclayer.worker import WorkerBusy, WorkerStateError


RUN_FORMAT = "experiment-run"
CLAIM_FORMAT = "claim-evidence"
STORE_VERSION = "evidence-registry/1.0"

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
RUN_STATUSES = {"running"} | TERMINAL_RUN_STATUSES
ARTIFACT_ROLES = {"input", "output", "log"}
METRIC_DIRECTIONS = {"minimize", "maximize", "target", "none"}
CLAIM_KINDS = {
    "observation",
    "hypothesis",
    "interpretation",
    "limitation",
    "decision",
}
EPISTEMIC_STATUSES = {
    "speculative",
    "partially-supported",
    "supported",
    "contradicted",
    "mixed",
    "unresolved",
}
PRODUCER_REVIEW_STATES = {"draft", "accepted", "rejected", "superseded"}
EVIDENCE_RELATIONS = {"supports", "contradicts", "qualifies", "context"}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SEED_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA_VERSION_RE = re.compile(
    r"^(?P<format>experiment-run|claim-evidence)/(?P<major>[0-9]+)\.(?P<minor>[0-9]+)$"
)
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
_RUN_LOCATOR_RE = re.compile(
    r"^(?P<kind>metrics|artifacts)\[(?P<name>[a-z0-9][a-z0-9-]*)\]$"
)

# Aliases keep the filesystem policy in one place and make fault injection local
# to this module's tests.
_atomic_write_json = _safe_io._atomic_write_json
_input_files = _safe_io._input_files
_read_safe_bytes = _safe_io._read_safe_bytes
_store_lock = _safe_io._store_lock
_optional_safe_file = _safe_io._optional_safe_file
_file_snapshot = _safe_io._file_snapshot


class EvidenceError(ValueError):
    """A validation or storage error safe to show without a local source path."""


class PlanMismatch(EvidenceError):
    """A write authorization no longer matches the strict current plan."""


def _nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be a non-empty string")


def _optional_string(obj, field, where, *, nonempty=False):
    if field not in obj:
        return
    value = obj[field]
    if not isinstance(value, str) or (nonempty and not value.strip()):
        suffix = "a non-empty string" if nonempty else "a string"
        raise EvidenceError(f"{where}.{field} must be {suffix}")


def _object(value, field):
    if not isinstance(value, dict):
        raise EvidenceError(f"{field} must be an object")
    return value


def _object_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise EvidenceError(f"{field} must be an array of objects")
    return value


def _slug(value, field):
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise EvidenceError(f"{field} must be a lowercase stable ID")


def _date_time(value, field):
    _nonempty_string(value, field)
    if _RFC3339_RE.fullmatch(value) is None:
        raise EvidenceError(f"{field} must be an RFC 3339 date-time")
    normalized = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    normalized = normalized.replace("t", "T", 1)
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError(f"{field} must be an RFC 3339 date-time") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite_constant(_value):
    raise EvidenceError("JSON must not contain NaN or Infinity")


def _parse_json_bytes(raw):
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_nonfinite_constant,
    )


def _semantic_fingerprint(value):
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contract_type(schema_version):
    if not isinstance(schema_version, str):
        raise EvidenceError("schema_version is missing or malformed")
    match = _SCHEMA_VERSION_RE.fullmatch(schema_version)
    if match is None:
        raise EvidenceError("schema_version is missing or malformed")
    if match.group("major") != "1":
        raise EvidenceError("schema_version has an unsupported major version")
    return "run" if match.group("format") == RUN_FORMAT else "claim"


def _json_value(value, field):
    """Reject Python's non-standard NaN/Infinity extension and non-JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceError(f"{field} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{field} object keys must be strings")
            _json_value(item, f"{field}.{key}")
        return
    raise EvidenceError(f"{field} must contain only JSON values")


def _validate_experiment_run(doc):
    _slug(doc.get("run_id"), "run_id")
    _slug(doc.get("project"), "project")
    _date_time(doc.get("created"), "created")
    if doc.get("status") not in RUN_STATUSES:
        raise EvidenceError("status is not supported")
    _nonempty_string(doc.get("objective"), "objective")

    execution = _object(doc.get("execution"), "execution")
    runner = execution.get("runner")
    if not isinstance(runner, str) or _SOURCE_TYPE_RE.fullmatch(runner) is None:
        raise EvidenceError("execution.runner must be a lowercase runner class")
    _nonempty_string(execution.get("code_ref"), "execution.code_ref")
    _nonempty_string(execution.get("environment_ref"), "execution.environment_ref")
    parameters = _object(execution.get("parameters"), "execution.parameters")
    _json_value(parameters, "execution.parameters")
    random_seeds = _object(execution.get("random_seeds"), "execution.random_seeds")
    for name, seed in random_seeds.items():
        if not isinstance(name, str) or _SEED_NAME_RE.fullmatch(name) is None:
            raise EvidenceError("execution.random_seeds contains an invalid name")
        if (
            not isinstance(seed, (int, str))
            or isinstance(seed, bool)
            or (isinstance(seed, str) and not seed)
        ):
            raise EvidenceError("execution.random_seeds values must be integers or strings")

    artifacts = doc.get("artifacts", [])
    artifact_ids = set()
    for index, artifact in enumerate(_object_list(artifacts, "artifacts")):
        where = f"artifacts[{index}]"
        artifact_id = artifact.get("artifact_id")
        _slug(artifact_id, f"{where}.artifact_id")
        if artifact_id in artifact_ids:
            raise EvidenceError("artifacts contains a duplicate artifact_id")
        artifact_ids.add(artifact_id)
        if artifact.get("role") not in ARTIFACT_ROLES:
            raise EvidenceError(f"{where}.role is not supported")
        _optional_string(artifact, "locator", where, nonempty=True)
        _optional_string(artifact, "media_type", where)
        if "sha256" in artifact:
            digest = artifact["sha256"]
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise EvidenceError(f"{where}.sha256 must be a lowercase SHA-256 digest")

    metrics = doc.get("metrics", [])
    metric_names = set()
    for index, metric in enumerate(_object_list(metrics, "metrics")):
        where = f"metrics[{index}]"
        name = metric.get("name")
        _slug(name, f"{where}.name")
        if name in metric_names:
            raise EvidenceError("metrics contains a duplicate name")
        metric_names.add(name)
        value = metric.get("value")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise EvidenceError(f"{where}.value must be a finite number")
        _optional_string(metric, "unit", where)
        if "direction" in metric and metric["direction"] not in METRIC_DIRECTIONS:
            raise EvidenceError(f"{where}.direction is not supported")

    if doc["status"] == "failed":
        _nonempty_string(doc.get("failure_reason"), "failure_reason")
    else:
        _optional_string(doc, "failure_reason", "experiment-run", nonempty=True)
    _optional_string(doc, "source_session_id", "experiment-run")
    _nonempty_string(doc.get("generated_by"), "generated_by")
    _json_value(doc, "experiment-run")


def _validate_claim_evidence(doc):
    _slug(doc.get("claim_id"), "claim_id")
    _slug(doc.get("project"), "project")
    _date_time(doc.get("created"), "created")
    _nonempty_string(doc.get("statement"), "statement")
    if "kind" in doc and doc["kind"] not in CLAIM_KINDS:
        raise EvidenceError("kind is not supported")
    if doc.get("epistemic_status") not in EPISTEMIC_STATUSES:
        raise EvidenceError("epistemic_status is not supported")
    review_state = doc.get("review_state")
    if review_state not in PRODUCER_REVIEW_STATES:
        raise EvidenceError("review_state is not supported")

    evidence = _object_list(doc.get("evidence"), "evidence")
    seen = set()
    for index, item in enumerate(evidence):
        where = f"evidence[{index}]"
        if item.get("relation") not in EVIDENCE_RELATIONS:
            raise EvidenceError(f"{where}.relation is not supported")
        source_type = item.get("source_type")
        if (
            not isinstance(source_type, str)
            or _SOURCE_TYPE_RE.fullmatch(source_type) is None
        ):
            raise EvidenceError(f"{where}.source_type is invalid")
        _nonempty_string(item.get("source_id"), f"{where}.source_id")
        _nonempty_string(item.get("locator"), f"{where}.locator")
        if "artifact_id" in item:
            _slug(item["artifact_id"], f"{where}.artifact_id")
        if "sha256" in item:
            digest = item["sha256"]
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise EvidenceError(f"{where}.sha256 must be a lowercase SHA-256 digest")
        _optional_string(item, "note", where)
        if source_type == "experiment-run":
            _slug(item.get("artifact_id"), f"{where}.artifact_id")
            digest = item.get("sha256")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise EvidenceError(
                    f"{where}.sha256 is required for experiment-run evidence"
                )
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            raise EvidenceError("evidence contains a duplicate entry")
        seen.add(identity)

    if review_state in {"accepted", "rejected", "superseded"}:
        _date_time(doc.get("reviewed_at"), "reviewed_at")
    elif "reviewed_at" in doc:
        _date_time(doc["reviewed_at"], "reviewed_at")
    if review_state == "accepted":
        if not evidence:
            raise EvidenceError("accepted claims require at least one evidence link")
        if doc["epistemic_status"] == "speculative":
            raise EvidenceError("accepted claims cannot remain speculative")
    if review_state == "superseded":
        _slug(doc.get("superseded_by"), "superseded_by")
    elif "superseded_by" in doc:
        _slug(doc["superseded_by"], "superseded_by")
    _nonempty_string(doc.get("generated_by"), "generated_by")
    _json_value(doc, "claim-evidence")


def validate_document(doc):
    """Validate a supported v1-major contract and return ``run`` or ``claim``."""
    if not isinstance(doc, dict):
        raise EvidenceError("input must be a JSON object")
    record_type = _contract_type(doc.get("schema_version"))
    if record_type == "run":
        _validate_experiment_run(doc)
        return "run"
    _validate_claim_evidence(doc)
    return "claim"


def _clean_document(doc):
    return sanitize_json(_safe_io._redact_local_paths(doc))


def normalize_document(doc):
    """Return a path-free derived record with an explicit local trust boundary."""
    record_type = validate_document(doc)
    clean = _clean_document(doc)
    source_fingerprint = _semantic_fingerprint(doc)
    content_fingerprint = _semantic_fingerprint(clean)
    if record_type == "run":
        return record_type, {
            "run_id": doc["run_id"],
            "project": doc["project"],
            "schema_version": doc["schema_version"],
            "source_fingerprint": source_fingerprint,
            "content_fingerprint": content_fingerprint,
            "content": clean,
        }
    return record_type, {
        "claim_id": doc["claim_id"],
        "project": doc["project"],
        "schema_version": doc["schema_version"],
        "source_fingerprint": source_fingerprint,
        "content_fingerprint": content_fingerprint,
        "trust_state": "untrusted-import",
        "local_review_state": "candidate",
        "producer_review_state": doc["review_state"],
        "content": clean,
    }


def _validate_persisted_content(record, where):
    fingerprint = record.get("content_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        raise EvidenceError(f"{where} has an invalid content fingerprint")
    if _semantic_fingerprint(record["content"]) != fingerprint:
        raise EvidenceError(f"{where} content fingerprint does not match")
    if _clean_document(record["content"]) != record["content"]:
        raise EvidenceError(f"{where} content violates the local privacy boundary")


def _validate_run_record(record, where):
    if (
        not isinstance(record, dict)
        or set(record) != M.EVIDENCE_RUN_RECORD_FIELDS
        or not isinstance(record.get("content"), dict)
    ):
        raise EvidenceError(f"{where} has an invalid record shape")
    if validate_document(record["content"]) != "run":
        raise EvidenceError(f"{where} has an unsupported schema")
    if record.get("schema_version") != record["content"]["schema_version"]:
        raise EvidenceError(f"{where} schema does not match its content")
    fingerprint = record.get("source_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        raise EvidenceError(f"{where} has an invalid source fingerprint")
    _validate_persisted_content(record, where)
    if (
        record.get("run_id") != record["content"]["run_id"]
        or record.get("project") != record["content"]["project"]
    ):
        raise EvidenceError(f"{where} identity does not match its content")


def _validate_claim_record(record, where):
    if (
        not isinstance(record, dict)
        or set(record) != M.EVIDENCE_CLAIM_RECORD_FIELDS
        or not isinstance(record.get("content"), dict)
    ):
        raise EvidenceError(f"{where} has an invalid record shape")
    if validate_document(record["content"]) != "claim":
        raise EvidenceError(f"{where} has an unsupported schema")
    if record.get("schema_version") != record["content"]["schema_version"]:
        raise EvidenceError(f"{where} schema does not match its content")
    fingerprint = record.get("source_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        raise EvidenceError(f"{where} has an invalid source fingerprint")
    _validate_persisted_content(record, where)
    if (
        record.get("claim_id") != record["content"]["claim_id"]
        or record.get("project") != record["content"]["project"]
    ):
        raise EvidenceError(f"{where} identity does not match its content")
    if (
        record.get("trust_state") != "untrusted-import"
        or record.get("local_review_state") != "candidate"
        or record.get("producer_review_state") != record["content"]["review_state"]
    ):
        raise EvidenceError(f"{where} bypasses the local candidate trust boundary")


def _empty_registry_state():
    empty = {
        "schema_version": STORE_VERSION,
        "runs": [],
        "candidate_claims": [],
    }
    return [], [], _semantic_fingerprint(
        {"present": False, "document": empty}
    )


def _load_existing(path):
    """Load the derived registry strictly; corruption never becomes empty state."""
    path = Path(path)
    try:
        _, info = _optional_safe_file(path)
    except _safe_io.ArtifactError as exc:
        raise EvidenceError("existing evidence registry is unreadable") from exc
    if info is None:
        return _empty_registry_state()
    try:
        raw = _read_safe_bytes(path, expected=_file_snapshot(info))
        document = _parse_json_bytes(raw)
    except Exception as exc:
        raise EvidenceError("existing evidence registry is unreadable") from exc
    if not isinstance(document, dict) or document.get("schema_version") != STORE_VERSION:
        raise EvidenceError("existing evidence registry has an unsupported schema")
    runs = document.get("runs")
    claims = document.get("candidate_claims")
    if not isinstance(runs, list) or not isinstance(claims, list):
        raise EvidenceError("existing evidence registry has an invalid shape")
    seen_runs = set()
    for index, record in enumerate(runs):
        _validate_run_record(record, f"runs[{index}]")
        if record["run_id"] in seen_runs:
            raise EvidenceError("existing evidence registry has duplicate run IDs")
        seen_runs.add(record["run_id"])
    seen_claims = set()
    for index, record in enumerate(claims):
        _validate_claim_record(record, f"candidate_claims[{index}]")
        if record["claim_id"] in seen_claims:
            raise EvidenceError("existing evidence registry has duplicate claim IDs")
        seen_claims.add(record["claim_id"])
    return runs, claims, _semantic_fingerprint(
        {"present": True, "document": document}
    )


def _validate_run_link(claim, item, runs):
    if item["source_type"] != "experiment-run":
        raise EvidenceError(
            "evidence source_type is not yet resolvable by the local evidence registry"
        )
    run = runs.get(item["source_id"])
    if run is None:
        raise EvidenceError("evidence references a missing experiment run")
    run_content = run["content"]
    if run["project"] != claim["project"]:
        raise EvidenceError("claim and experiment run must belong to the same project")

    artifacts = {
        artifact["artifact_id"]: artifact
        for artifact in run_content.get("artifacts", [])
    }
    artifact = artifacts.get(item["artifact_id"])
    if artifact is None:
        raise EvidenceError("evidence references a missing run artifact")
    if artifact.get("sha256") != item["sha256"]:
        raise EvidenceError("evidence artifact digest does not match the experiment run")

    match = _RUN_LOCATOR_RE.fullmatch(item["locator"])
    if match is None:
        raise EvidenceError("experiment-run evidence has an unparseable locator")
    name = match.group("name")
    if match.group("kind") == "metrics":
        metric_names = {metric["name"] for metric in run_content.get("metrics", [])}
        if name not in metric_names:
            raise EvidenceError("evidence locator references a missing run metric")
    else:
        if name not in artifacts:
            raise EvidenceError("evidence locator references a missing run artifact")
        if name != item["artifact_id"]:
            raise EvidenceError("artifact locator and artifact_id must identify the same artifact")


def _validate_relations(runs, claims):
    for claim in claims.values():
        for item in claim["content"]["evidence"]:
            _validate_run_link(claim, item, runs)


def _merge_runs(existing, incoming):
    merged = dict(existing)
    for run_id, record in incoming.items():
        prior = merged.get(run_id)
        if prior is not None:
            if prior["source_fingerprint"] == record["source_fingerprint"]:
                continue
            if prior["project"] != record["project"]:
                raise EvidenceError("an existing run_id cannot move between projects")
            if prior["content"]["created"] != record["content"]["created"]:
                raise EvidenceError("an existing run_id cannot change its creation time")
            if prior["content"]["status"] in TERMINAL_RUN_STATUSES:
                raise EvidenceError("a terminal experiment run is immutable")
        merged[run_id] = record
    return merged


def _merge_claims(existing, incoming):
    merged = dict(existing)
    for claim_id, record in incoming.items():
        prior = merged.get(claim_id)
        if prior is not None:
            if prior["source_fingerprint"] == record["source_fingerprint"]:
                continue
            if prior["project"] != record["project"]:
                raise EvidenceError("an existing claim_id cannot move between projects")
            if prior["content"]["created"] != record["content"]["created"]:
                raise EvidenceError("an existing claim_id cannot change its creation time")
        merged[claim_id] = record
    return merged


def _source_changed(existing, key, record):
    prior = existing.get(key)
    return (
        prior is not None
        and prior["source_fingerprint"] != record["source_fingerprint"]
    )


def _plan_refs(records, identity_field):
    if isinstance(records, dict):
        records = records.values()
    return sorted(
        (
            {
                identity_field: record[identity_field],
                "schema_version": record["schema_version"],
                "source_fingerprint": record["source_fingerprint"],
                "content_fingerprint": record["content_fingerprint"],
            }
            for record in records
        ),
        key=lambda item: item[identity_field],
    )


def _plan_id(
    existing_state_fingerprint,
    incoming_runs,
    incoming_claims,
    run_records,
    claim_records,
    added,
    updated,
):
    descriptor = {
        "schema_version": "evidence-ingest-plan/1.0",
        "existing_registry": existing_state_fingerprint,
        "inputs": {
            "runs": _plan_refs(incoming_runs, "run_id"),
            "claims": _plan_refs(incoming_claims, "claim_id"),
        },
        "result": {
            "runs": _plan_refs(run_records, "run_id"),
            "claims": _plan_refs(claim_records, "claim_id"),
            "added": added,
            "updated": updated,
            "total": len(run_records) + len(claim_records),
        },
    }
    return _semantic_fingerprint(descriptor)


def _read_inputs(inputs, target):
    try:
        files = _input_files(inputs, target)
    except _safe_io.ArtifactError as exc:
        raise EvidenceError(str(exc)) from exc
    incoming_runs = {}
    incoming_claims = {}
    for index, (source, snapshot) in enumerate(files, 1):
        try:
            raw = _read_safe_bytes(source, expected=snapshot)
            document = _parse_json_bytes(raw)
            record_type, record = normalize_document(document)
        except (OSError, UnicodeError) as exc:
            raise EvidenceError(
                f"invalid evidence input #{index}: input could not be read as UTF-8 JSON"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"invalid evidence input #{index}: invalid JSON") from exc
        except (_safe_io.ArtifactError, EvidenceError) as exc:
            raise EvidenceError(f"invalid evidence input #{index}: {exc}") from exc

        if record_type == "run":
            key = record["run_id"]
            prior = incoming_runs.get(key)
            if (
                prior is not None
                and prior["source_fingerprint"] != record["source_fingerprint"]
            ):
                raise EvidenceError("conflicting inputs share one run_id")
            incoming_runs.setdefault(key, record)
        else:
            key = record["claim_id"]
            prior = incoming_claims.get(key)
            if (
                prior is not None
                and prior["source_fingerprint"] != record["source_fingerprint"]
            ):
                raise EvidenceError("conflicting inputs share one claim_id")
            incoming_claims.setdefault(key, record)
    return incoming_runs, incoming_claims


def _build_plan(
    existing_runs_list,
    existing_claims_list,
    existing_state_fingerprint,
    incoming_runs,
    incoming_claims,
):
    existing_runs = {record["run_id"]: record for record in existing_runs_list}
    existing_claims = {
        record["claim_id"]: record for record in existing_claims_list
    }
    runs = _merge_runs(existing_runs, incoming_runs)
    claims = _merge_claims(existing_claims, incoming_claims)
    _validate_relations(runs, claims)

    added = sum(key not in existing_runs for key in incoming_runs)
    added += sum(key not in existing_claims for key in incoming_claims)
    updated = sum(
        _source_changed(existing_runs, key, record)
        for key, record in incoming_runs.items()
    )
    updated += sum(
        _source_changed(existing_claims, key, record)
        for key, record in incoming_claims.items()
    )
    run_records = [runs[key] for key in sorted(runs)]
    claim_records = [claims[key] for key in sorted(claims)]
    plan_id = _plan_id(
        existing_state_fingerprint,
        incoming_runs,
        incoming_claims,
        run_records,
        claim_records,
        added,
        updated,
    )
    return {
        "added": added,
        "updated": updated,
        "total": len(run_records) + len(claim_records),
        "run_records": run_records,
        "claim_records": claim_records,
        "plan_id": plan_id,
        "changed": (
            run_records != existing_runs_list
            or claim_records != existing_claims_list
        ),
    }


def _current_plan(target, incoming_runs, incoming_claims):
    try:
        _, info = _safe_io._safe_chain(target, allow_missing_tail=True)
        existing_runs_list, existing_claims_list, existing_state_fingerprint = (
            _empty_registry_state() if info is None else _load_existing(target)
        )
        return _build_plan(
            existing_runs_list,
            existing_claims_list,
            existing_state_fingerprint,
            incoming_runs,
            incoming_claims,
        )
    except (OSError, _safe_io.ArtifactError) as exc:
        raise EvidenceError("evidence registry could not be read safely") from exc


def preview_plan(inputs, memory_dir=None):
    """Return one strict, content-free write plan without changing any file."""
    memory_dir = Path(memory_dir) if memory_dir is not None else M.mem_dir(C.data_root())
    target = memory_dir / M.EVIDENCE_REGISTRY
    incoming_runs, incoming_claims = _read_inputs(inputs, target)
    return _current_plan(target, incoming_runs, incoming_claims)


def preview(inputs, memory_dir=None):
    """Validate inputs and return counts without creating or changing any file."""
    plan = preview_plan(inputs, memory_dir)
    return plan["added"], plan["updated"], plan["total"]


def _execute_plan(inputs, memory_dir=None, expected_plan_id=None):
    memory_dir = Path(memory_dir) if memory_dir is not None else M.mem_dir(C.data_root())
    target = memory_dir / M.EVIDENCE_REGISTRY
    incoming_runs, incoming_claims = _read_inputs(inputs, target)
    if expected_plan_id is not None:
        if (
            not isinstance(expected_plan_id, str)
            or _SHA256_RE.fullmatch(expected_plan_id) is None
        ):
            raise PlanMismatch("plan_id is malformed or no longer current")
        preflight = _current_plan(target, incoming_runs, incoming_claims)
        if preflight["plan_id"] != expected_plan_id:
            raise PlanMismatch("plan_id no longer matches the strict current plan")
    try:
        with _store_lock(target) as store_lock:
            store_lock.validate()
            (
                existing_runs_list,
                existing_claims_list,
                existing_state_fingerprint,
            ) = _load_existing(target)
            store_lock.validate()
            plan = _build_plan(
                existing_runs_list,
                existing_claims_list,
                existing_state_fingerprint,
                incoming_runs,
                incoming_claims,
            )
            if (
                expected_plan_id is not None
                and plan["plan_id"] != expected_plan_id
            ):
                raise PlanMismatch("plan_id changed before the write lock was applied")
            if plan["changed"]:
                store_lock.validate()
                _atomic_write_json(
                    target,
                    {
                        "schema_version": STORE_VERSION,
                        "runs": plan["run_records"],
                        "candidate_claims": plan["claim_records"],
                    },
                )
                store_lock.validate()
            return plan
    except (WorkerBusy, WorkerStateError) as exc:
        raise EvidenceError(
            "evidence registry is busy or its lock is unsafe"
        ) from exc
    except (OSError, _safe_io.ArtifactError) as exc:
        raise EvidenceError("evidence registry could not be read or updated") from exc


def ingest(inputs, memory_dir=None, expected_plan_id=None):
    """Validate and atomically merge explicit files.

    Returns ``(added, updated, total)`` across both run and candidate-claim
    records. The optional expected plan binds an external authorization; direct
    callers that omit it retain the explicit API behavior. No producer-declared
    review state is promoted locally.
    """
    plan = _execute_plan(inputs, memory_dir, expected_plan_id)
    return plan["added"], plan["updated"], plan["total"]


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Explicit experiment-run/claim-evidence JSON file(s) or directories",
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        help="Override the derived memory directory",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Write a previously previewed plan; requires --plan-id",
    )
    parser.add_argument(
        "--plan-id",
        help="SHA-256 plan id printed by the matching zero-write preview",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a content-free JSON report",
    )
    return parser


def _report(mode, plan):
    return {
        "schema_version": "evidence-ingest-report/1.0",
        "status": "ok",
        "mode": mode,
        "plan_id": plan["plan_id"],
        "added": plan["added"],
        "updated": plan["updated"],
        "total": plan["total"],
    }


def _print_error(json_mode, code, message):
    if json_mode:
        print(
            json.dumps(
                {
                    "schema_version": "evidence-ingest-report/1.0",
                    "status": "error",
                    "error_code": code,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"ERROR: {code}: {message}", file=sys.stderr)


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.inputs:
        _print_error(args.json, "no-inputs", "at least one explicit input is required")
        return 2
    if args.run and not args.plan_id:
        _print_error(
            args.json,
            "plan-required",
            "--run requires the plan_id from a matching preview",
        )
        return 2
    if not args.run and args.plan_id:
        _print_error(
            args.json,
            "plan-not-applicable",
            "--plan-id is accepted only with --run",
        )
        return 2
    mode = "run" if args.run else "preview"
    try:
        if args.run:
            plan = _execute_plan(args.inputs, args.memory_dir, args.plan_id)
        else:
            plan = preview_plan(args.inputs, args.memory_dir)
    except PlanMismatch as exc:
        _print_error(args.json, "plan-mismatch", str(exc))
        return 2
    except EvidenceError as exc:
        _print_error(args.json, "invalid-evidence", str(exc))
        return 2
    report = _report(mode, plan)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"evidence {mode}: added={plan['added']} updated={plan['updated']} "
            f"total={plan['total']} plan_id={plan['plan_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
