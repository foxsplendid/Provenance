#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingest Scriptorium literature-research artifacts into Provenance memory.

The command accepts only explicit files/directories. It never discovers user
data, modifies sources, or persists source paths. Every input is validated and
sanitized before one atomic, idempotent merge into
``memory/research-artifacts.json``.

Supported contracts:
    parsed-paper/1.0
    reading-note/1.0
    review/1.0
    lineage-graph/1.0

Usage:
    prov-ingest-research ARTIFACT.json [ARTIFACT.json ...]
    prov-ingest-research OUTPUT_DIR [OUTPUT_DIR ...]
    prov-ingest-research INPUT [INPUT ...] --memory-dir MEMORY_DIR
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path

from provenance import common as C, memory as M
from provenance.redact import sanitize_json
from provenance.synclayer.worker import (
    WorkerBusy,
    WorkerStateError,
    _Lock as _KernelLock,
)


SUPPORTED = {
    "parsed-paper/1.0": "parsed-paper",
    "reading-note/1.0": "reading-note",
    "review/1.0": "review",
    "lineage-graph/1.0": "lineage-graph",
}
STORE_VERSION = "research-artifacts/1.0"
RELATIONS = {"cites", "extends", "supersedes", "method-of", "contrasts"}
READ_STATUSES = {"New", "To Read", "In Progress", "Read", "Not Reading"}

_WINDOWS_ABSOLUTE = re.compile(
    r"(?i)(?<![A-Za-z0-9/])[A-Z]:[\\/][^\r\n<>\"'`|]+"
)
_UNC_ABSOLUTE = re.compile(r"(?<![\\])\\\\[^\\/\s]+[\\/][^\r\n<>\"'`|]+")
_POSIX_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9:/])/(?!/)[^\r\n<>\"'`|]+")
_FILE_URI = re.compile(
    r"(?ix)\bfile:(?://)?[^\r\n<>\"'`|)\]]+?"
    r"(?=\s+https?://|[)\]}>\"'`]|[<|]|[\r\n]|$)"
)


class ArtifactError(ValueError):
    """A validation/read error safe to show without an absolute source path."""


def _nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{field} must be a non-empty string")


def _optional_string(obj, field, where):
    if field in obj and not isinstance(obj[field], str):
        raise ArtifactError(f"{where}.{field} must be a string")


def _string_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ArtifactError(f"{field} must be an array of strings")


def _object(value, field):
    if not isinstance(value, dict):
        raise ArtifactError(f"{field} must be an object")
    return value


def _object_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ArtifactError(f"{field} must be an array of objects")
    return value


def _validate_parsed_paper(doc):
    _nonempty_string(doc.get("id"), "id")
    _nonempty_string(doc.get("created"), "created")
    for field in ("parser", "source_pdf"):
        _optional_string(doc, field, "parsed-paper")
    if "metadata" in doc:
        metadata = _object(doc["metadata"], "metadata")
        for field in ("title", "year", "doi", "abstract"):
            _optional_string(metadata, field, "metadata")
        if "authors" in metadata:
            _string_list(metadata["authors"], "metadata.authors")
    for field in ("sections", "references", "figures", "tables"):
        if field not in doc:
            continue
        items = _object_list(doc[field], field)
        for index, item in enumerate(items):
            where = f"{field}[{index}]"
            if field == "sections":
                for key in ("heading", "text"):
                    _optional_string(item, key, where)
                if "level" in item and type(item["level"]) is not int:
                    raise ArtifactError(f"{where}.level must be an integer")
            elif field == "references":
                for key in ("raw", "title", "year", "doi"):
                    _optional_string(item, key, where)
                if "authors" in item:
                    _string_list(item["authors"], f"{where}.authors")
            elif field == "figures":
                for key in ("id", "caption"):
                    _optional_string(item, key, where)
                if "page" in item and type(item["page"]) is not int:
                    raise ArtifactError(f"{where}.page must be an integer")
            else:
                for key in ("id", "caption"):
                    _optional_string(item, key, where)
                if "n_rows" in item and type(item["n_rows"]) is not int:
                    raise ArtifactError(f"{where}.n_rows must be an integer")


def _validate_reading_note(doc):
    _nonempty_string(doc.get("id"), "id")
    _nonempty_string(doc.get("created"), "created")
    for field in ("doi", "generated_by"):
        _optional_string(doc, field, "reading-note")
    if "zotero_key" in doc:
        value = doc["zotero_key"]
        if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9]{8}", value) is None:
            raise ArtifactError("zotero_key must contain exactly eight uppercase letters/digits")
    if "read_status" in doc:
        if not isinstance(doc["read_status"], str) or doc["read_status"] not in READ_STATUSES:
            raise ArtifactError("read_status is not a supported reading state")
    if "stages" in doc:
        stages = _object(doc["stages"], "stages")
        stage_fields = {
            "glance": {"tldr": "string", "tags": "strings", "key_findings": "strings"},
            "close_read": {"question": "string", "method": "string", "data": "string",
                           "results": "string", "figures": "strings"},
            "deep_read": {"critique": "string", "reproducibility": "string",
                          "limits": "string", "relation_to_my_work": "string"},
            "situate": {"direction": "string", "lineage_refs": "strings"},
        }
        for stage, fields in stage_fields.items():
            if stage not in stages:
                continue
            value = _object(stages[stage], f"stages.{stage}")
            for field, expected in fields.items():
                if field not in value:
                    continue
                if expected == "string":
                    _optional_string(value, field, f"stages.{stage}")
                else:
                    _string_list(value[field], f"stages.{stage}.{field}")
    if "sources" in doc:
        sources = _object(doc["sources"], "sources")
        if "annotations" in sources:
            _string_list(sources["annotations"], "sources.annotations")
        for field in ("parsed_file", "zotero_uri"):
            _optional_string(sources, field, "sources")


def _validate_review(doc):
    direction = _object(doc.get("direction"), "direction")
    _nonempty_string(direction.get("query"), "direction.query")
    _nonempty_string(direction.get("created"), "direction.created")
    sections = _object_list(doc.get("sections"), "sections")
    for index, section in enumerate(sections):
        _nonempty_string(section.get("heading"), f"sections[{index}].heading")
        if not isinstance(section.get("prose"), str):
            raise ArtifactError(f"sections[{index}].prose must be a string")
    if "comparison_table" in doc:
        table = _object(doc["comparison_table"], "comparison_table")
        if "columns" in table:
            _string_list(table["columns"], "comparison_table.columns")
        if "rows" in table:
            rows = _object_list(table["rows"], "comparison_table.rows")
            for index, row in enumerate(rows):
                _nonempty_string(row.get("citekey"), f"comparison_table.rows[{index}].citekey")
                _string_list(row.get("cells"), f"comparison_table.rows[{index}].cells")
    for field in ("gaps", "priority_reads"):
        if field in doc:
            _string_list(doc[field], field)
    _optional_string(doc, "lineage_ref", "review")


def _validate_lineage(doc):
    direction = _object(doc.get("direction"), "direction")
    for field in ("query", "scope_method", "created"):
        if not isinstance(direction.get(field), str):
            raise ArtifactError(f"direction.{field} must be a string")
    _nonempty_string(direction.get("created"), "direction.created")
    nodes = _object_list(doc.get("nodes"), "nodes")
    for index, node in enumerate(nodes):
        _nonempty_string(node.get("citekey"), f"nodes[{index}].citekey")
        for field in ("title", "year", "cluster"):
            _optional_string(node, field, f"nodes[{index}]")
    edges = _object_list(doc.get("edges"), "edges")
    for index, edge in enumerate(edges):
        for field in ("from", "to", "relation"):
            _nonempty_string(edge.get(field), f"edges[{index}].{field}")
        if edge["relation"] not in RELATIONS:
            raise ArtifactError(f"edges[{index}].relation is not supported")
        _optional_string(edge, "evidence", f"edges[{index}]")
    if "clusters" in doc:
        clusters = _object_list(doc["clusters"], "clusters")
        for index, cluster in enumerate(clusters):
            _nonempty_string(cluster.get("id"), f"clusters[{index}].id")
            _nonempty_string(cluster.get("label"), f"clusters[{index}].label")
    if "timeline" in doc:
        _string_list(doc["timeline"], "timeline")
    _optional_string(doc, "generated_by", "lineage-graph")


VALIDATORS = {
    "parsed-paper": _validate_parsed_paper,
    "reading-note": _validate_reading_note,
    "review": _validate_review,
    "lineage-graph": _validate_lineage,
}


def validate_artifact(doc):
    """Validate the strict minimum needed for safe identity and search indexing."""
    if not isinstance(doc, dict):
        raise ArtifactError("artifact must be a JSON object")
    version = doc.get("schema_version")
    artifact_type = SUPPORTED.get(version)
    if artifact_type is None:
        raise ArtifactError("unsupported schema_version")
    VALIDATORS[artifact_type](doc)
    return artifact_type


def _redact_local_paths(value):
    """Remove absolute local paths while retaining safe relative contract refs."""
    if isinstance(value, str):
        stripped = value.strip()
        if (re.match(r"(?i)^file:", stripped)
                or re.match(r"(?i)^[A-Z]:[\\/]", stripped) or stripped.startswith("\\\\")
                or stripped.startswith("/")):
            return "[REDACTED:local-path]"
        value = _FILE_URI.sub("[REDACTED:local-path]", value)
        value = _WINDOWS_ABSOLUTE.sub("[REDACTED:local-path]", value)
        value = _UNC_ABSOLUTE.sub("[REDACTED:local-path]", value)
        return _POSIX_ABSOLUTE.sub("[REDACTED:local-path]", value)
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            safe_key = (
                sanitize_json(_redact_local_paths(key))
                if isinstance(key, str)
                else key
            )
            if safe_key in clean:
                base = str(safe_key)
                index = 2
                while f"{base}#{index}" in clean:
                    index += 1
                safe_key = f"{base}#{index}"
            clean[safe_key] = _redact_local_paths(item)
        return clean
    return value


def _identity(artifact_type, doc):
    if artifact_type in ("parsed-paper", "reading-note"):
        return doc["id"]
    direction = doc["direction"]
    query = doc["direction"]["query"]
    if query.strip() or artifact_type == "review":
        base = query
    else:
        # The lineage contract explicitly permits an empty ad-hoc query. The paper
        # set is its durable semantic identity in that case; input filenames are
        # intentionally never used because they may move or contain private paths.
        citekeys = sorted(node["citekey"] for node in doc["nodes"])
        base = "\0".join(citekeys) if citekeys else direction["scope_method"]
    return f"{base}\0{direction['created']}"


def _artifact_id(artifact_type, doc):
    identity = unicodedata.normalize("NFKC", _identity(artifact_type, doc)).strip()
    query = str(doc.get("direction", {}).get("query", ""))
    if (
        artifact_type not in ("parsed-paper", "reading-note")
        and (artifact_type == "review" or query.strip())
    ):
        identity = identity.casefold()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{artifact_type}:{digest}"


def normalize_artifact(doc):
    """Return one stable, path-free and privacy-sanitized memory record."""
    artifact_type = validate_artifact(doc)
    clean = sanitize_json(_redact_local_paths(doc))
    return {
        "artifact_id": _artifact_id(artifact_type, doc),
        "artifact_type": artifact_type,
        "schema_version": doc["schema_version"],
        "content": clean,
    }


def _atomic_write_json(path, document):
    path = Path(path)
    _prepare_safe_directory(path.parent)
    _optional_safe_file(path)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _require_safe_directory(path.parent)
        _optional_safe_file(path)
        os.replace(temp, path)
        _require_safe_directory(path.parent)
        _, stored = _optional_safe_file(path)
        if stored is None:
            raise ArtifactError("the research artifact store was not published safely")
    except Exception:
        try:
            _, temp_info = _optional_safe_file(temp)
            if temp_info is not None:
                temp.unlink()
        except (ArtifactError, OSError):
            pass
        raise


def _load_existing(path):
    path = Path(path)
    try:
        _, info = _optional_safe_file(path)
    except ArtifactError as exc:
        raise ArtifactError("existing memory store is unreadable") from exc
    if info is None:
        return []
    try:
        document = json.loads(_read_safe_bytes(path, expected=_file_snapshot(info)).decode("utf-8"))
    except Exception as exc:
        raise ArtifactError("existing memory store is unreadable") from exc
    if not isinstance(document, dict) or document.get("schema_version") != STORE_VERSION:
        raise ArtifactError("existing memory store has an unsupported schema")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        raise ArtifactError("existing memory store artifacts must be an array")
    seen = set()
    for index, record in enumerate(artifacts):
        if not isinstance(record, dict):
            raise ArtifactError(f"existing artifacts[{index}] must be an object")
        artifact_id = record.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id or artifact_id in seen:
            raise ArtifactError("existing memory store has invalid or duplicate artifact IDs")
        if record.get("artifact_type") not in VALIDATORS or not isinstance(record.get("content"), dict):
            raise ArtifactError(f"existing artifacts[{index}] has an invalid record shape")
        seen.add(artifact_id)
    return artifacts


class _ValidatedStoreLock:
    """Kernel lock whose path and fd remain bound to the selected safe store."""

    def __init__(self, target):
        self.target = Path(target)
        self.lock_path = self.target.with_name(f".{self.target.name}.lock")
        self.kernel_lock = _KernelLock(self.lock_path)
        self.identity = None

    def _validate_open_lock(self):
        _require_safe_directory(self.lock_path.parent)
        _, current = _optional_safe_file(self.lock_path)
        if current is None or self.kernel_lock.fd is None:
            raise WorkerStateError("research store lock identity is unavailable")
        try:
            opened = os.fstat(self.kernel_lock.fd)
        except OSError as exc:
            raise WorkerStateError("research store lock identity is unavailable") from exc
        if _file_identity(current) != _file_identity(opened):
            raise WorkerStateError("research store lock identity changed")
        if self.identity is not None and _file_identity(current) != self.identity:
            raise WorkerStateError("research store lock identity changed")
        self.identity = _file_identity(current)

    def validate(self):
        try:
            self._validate_open_lock()
            _reject_unsafe_existing(self.target)
        except ArtifactError as exc:
            raise WorkerStateError("research store filesystem boundary changed") from exc

    def __enter__(self):
        try:
            _prepare_safe_directory(self.lock_path.parent)
            _reject_unsafe_existing(self.target)
            _optional_safe_file(self.lock_path)
        except ArtifactError as exc:
            raise WorkerStateError("research store filesystem boundary is unsafe") from exc
        self.kernel_lock.__enter__()
        try:
            self.validate()
        except Exception:
            self.kernel_lock.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc_info):
        validation_error = None
        try:
            self.validate()
        except (ArtifactError, WorkerStateError) as exc:
            validation_error = exc
        result = self.kernel_lock.__exit__(*exc_info)
        if validation_error is not None and exc_info[1] is None:
            raise WorkerStateError("research store filesystem boundary changed") from validation_error
        return result


def _store_lock(target):
    """Serialize read-merge-write and bind the lock to the safe store path."""
    return _ValidatedStoreLock(target)


def _path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_link_or_reparse(info):
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _path_is_link_or_reparse(path, info):
    if _is_link_or_reparse(info):
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        return bool(is_junction and is_junction())
    except OSError as exc:
        raise ArtifactError("a filesystem boundary could not be verified safely") from exc


def _absolute_components(path):
    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor = Path(absolute.anchor)
    components = [anchor]
    current = anchor
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)
    return absolute, components


def _safe_chain(path, *, allow_missing_leaf=False, allow_missing_tail=False):
    """Return ``(absolute_path, final_lstat)`` after checking every component."""
    absolute, components = _absolute_components(path)
    final = None
    last_index = len(components) - 1
    for index, component in enumerate(components):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing_tail or (allow_missing_leaf and index == last_index):
                return absolute, None
            raise ArtifactError("a filesystem boundary is unavailable")
        except OSError as exc:
            raise ArtifactError("a filesystem boundary could not be verified safely") from exc
        if _path_is_link_or_reparse(component, info):
            raise ArtifactError("filesystem boundaries cannot contain links or reparse points")
        final = info
    return absolute, final


def _file_identity(info):
    return info.st_dev, info.st_ino


def _file_snapshot(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
    )


def _require_safe_directory(path):
    absolute, info = _safe_chain(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise ArtifactError("a filesystem boundary is not a directory")
    return absolute, info


def _prepare_safe_directory(path):
    absolute, _ = _safe_chain(path, allow_missing_tail=True)
    try:
        absolute.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactError("the research artifact store directory is unavailable") from exc
    return _require_safe_directory(absolute)


def _optional_safe_file(path):
    absolute, info = _safe_chain(path, allow_missing_leaf=True)
    if info is None:
        return absolute, None
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactError("a filesystem boundary is not a regular file")
    return absolute, info


def _reject_unsafe_existing(path):
    """Reject link-like components while leaving type errors to the caller."""
    return _safe_chain(path, allow_missing_leaf=True)


def _read_safe_bytes(path, *, expected=None):
    """Read one regular file through one fd and reject identity changes."""
    absolute, before = _safe_chain(path)
    if before is None or not stat.S_ISREG(before.st_mode):
        raise ArtifactError("an explicit input has an unsupported file type")
    if expected is not None and _file_snapshot(before) != expected:
        raise ArtifactError("an explicit input changed before it could be read")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(absolute), flags)
    except OSError as exc:
        raise ArtifactError("an explicit input could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
            raise ArtifactError("an explicit input changed before it could be read")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        after_read = os.fstat(fd)
        if _file_snapshot(after_read) != _file_snapshot(opened):
            raise ArtifactError("an explicit input changed while it was being read")
    finally:
        os.close(fd)

    _, after_path = _safe_chain(absolute)
    if after_path is None or _file_snapshot(after_path) != _file_snapshot(opened):
        raise ArtifactError("an explicit input changed while it was being read")
    return b"".join(chunks)


def _input_kind(path):
    _, info = _safe_chain(path)
    if stat.S_ISREG(info.st_mode):
        return "file", info
    if stat.S_ISDIR(info.st_mode):
        return "directory", info
    raise ArtifactError("an explicit input has an unsupported file type")


def _directory_json_files(root, root_info):
    files = []
    pending = [(Path(root), _file_identity(root_info))]
    while pending:
        directory, expected_identity = pending.pop()
        _, before = _require_safe_directory(directory)
        if _file_identity(before) != expected_identity:
            raise ArtifactError("an explicit input directory changed during traversal")
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ArtifactError("an explicit input directory is unreadable") from exc
        _, after = _require_safe_directory(directory)
        if _file_identity(after) != expected_identity:
            raise ArtifactError("an explicit input directory changed during traversal")
        directories = []
        for entry in children:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArtifactError("an explicit input entry is unavailable") from exc
            path = Path(entry.path)
            if _path_is_link_or_reparse(path, info):
                raise ArtifactError("explicit inputs cannot contain links or reparse points")
            _, bound_info = _safe_chain(path)
            if stat.S_ISDIR(bound_info.st_mode):
                directories.append((path, _file_identity(bound_info)))
            elif stat.S_ISREG(bound_info.st_mode) and entry.name.casefold().endswith(".json"):
                files.append((path, _file_snapshot(bound_info)))
        pending.extend(reversed(directories))
    return files


def _input_files(inputs, target):
    files = []
    seen = set()
    target_key = _path_key(target)
    for supplied in inputs:
        path = Path(supplied)
        kind, info = _input_kind(path)
        candidates = (
            [(path, _file_snapshot(info))]
            if kind == "file"
            else _directory_json_files(path, info)
        )
        for candidate, snapshot in candidates:
            key = _path_key(candidate)
            if key == target_key or key in seen:
                continue
            seen.add(key)
            files.append((candidate, snapshot))
    if not files:
        raise ArtifactError("no JSON artifact files found in the explicit inputs")
    return files


def ingest(inputs, memory_dir=None):
    """Validate all inputs, then atomically merge them. Return (added, updated, total)."""
    memdir = Path(memory_dir) if memory_dir is not None else M.mem_dir(C.data_root())
    target = memdir / M.RESEARCH_ARTIFACTS
    files = _input_files(inputs, target)

    incoming = {}
    for index, (source, snapshot) in enumerate(files, 1):
        try:
            document = json.loads(_read_safe_bytes(source, expected=snapshot).decode("utf-8"))
            record = normalize_artifact(document)
        except (OSError, UnicodeError) as exc:
            raise ArtifactError(
                f"invalid artifact input #{index}: input could not be read as UTF-8 JSON"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"invalid artifact input #{index}: invalid JSON") from exc
        except ArtifactError as exc:
            raise ArtifactError(f"invalid artifact input #{index}: {exc}") from exc
        artifact_id = record["artifact_id"]
        if artifact_id in incoming and incoming[artifact_id] != record:
            raise ArtifactError(f"conflicting artifacts share stable ID {artifact_id}")
        incoming[artifact_id] = record

    try:
        with _store_lock(target) as store_lock:
            store_lock.validate()
            existing = _load_existing(target)
            store_lock.validate()
            merged = {record["artifact_id"]: record for record in existing}
            added = sum(artifact_id not in merged for artifact_id in incoming)
            updated = sum(artifact_id in merged and merged[artifact_id] != record
                          for artifact_id, record in incoming.items())
            merged.update(incoming)
            artifacts = [merged[key] for key in sorted(merged)]
            if artifacts != existing:
                store_lock.validate()
                _atomic_write_json(
                    target,
                    {"schema_version": STORE_VERSION, "artifacts": artifacts},
                )
                store_lock.validate()
            return added, updated, len(artifacts)
    except (WorkerBusy, WorkerStateError) as exc:
        raise ArtifactError("research artifact store is busy or its lock is unsafe") from exc
    except OSError as exc:
        raise ArtifactError("research artifact store could not be read or updated") from exc


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Explicit JSON artifact file(s) or directory/directories")
    parser.add_argument("--memory-dir", type=Path, help="Override the destination memory directory")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        added, updated, total = ingest(args.inputs, args.memory_dir)
    except ArtifactError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"research artifacts ingested: added={added} updated={updated} total={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
