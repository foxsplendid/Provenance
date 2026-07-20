# -*- coding: utf-8 -*-
"""
Append-only progress-log primitive (sync-layer.md §3).

The safety-critical net-new piece of the event/sync layer. It appends a dated
session-summary block inside a project note's *frozen* marker region and **never
touches a byte outside that region** (the hand-authored section, master = user).

This is NOT a splice-replace like Steward's `portfolio.apply_snapshot`; it is a
true append that preserves every existing entry. It only borrows the *shape* of
Steward `apply.py`'s safety model (journal-before-write, reversible) — not its
code, which is Zotero-REST-specific.

Frozen markers (sync-layer.md §3):

    <!-- scriptorium:progress-log:begin -->
    ... newest entry block on top ...
    <!-- scriptorium:progress-log:end -->

Each block is prefixed by `<!-- entry:<summary_id> -->`; `<summary_id>` is the
idempotency key (re-applying the same id is a no-op).

Invariants (sync-layer.md §2):
  - append-only: new blocks insert immediately after `:begin` (newest-first);
    existing blocks are preserved.
  - never overwrite human content: bytes outside the marker region are identical
    before/after, to the byte (BOM, newline style, content).
  - refuse on missing marker pair: route to a draft instead; never inject markers
    into a pre-existing hand-authored note. A NEW note may be created with an
    empty marker pair (create_if_missing).
  - idempotent: skip if `<!-- entry:<summary_id> -->` already present.
  - encoding-safe: BOM-tolerant read (utf-8-sig), preserve newline style.
  - atomic: temp-file + os.replace; a failed write leaves no half-file.
  - journal-before-write: pre-state captured to sync-state/journal/<stamp>.json
    before the content write; reversible via rollback().

Stdlib only, no network.
"""
import base64
import hashlib
import json
import os
import re
import tempfile
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from provenance import common as C

BEGIN_MARKER = "<!-- scriptorium:progress-log:begin -->"
END_MARKER = "<!-- scriptorium:progress-log:end -->"
UTF8_BOM = b"\xef\xbb\xbf"

# append_entry() outcomes
APPENDED = "appended"                 # new block inserted after :begin
SKIPPED_DUPLICATE = "skipped_duplicate"  # entry:<id> already present -> no-op
CREATED = "created"                   # new note created with marker pair + block
REFUSED_NO_MARKER = "refused_no_marker"  # file exists but lacks the marker pair
REFUSED_NO_FILE = "refused_no_file"   # file missing and create_if_missing=False

Result = namedtuple("Result", ["status", "path", "journal", "summary_id"])


def entry_marker(summary_id):
    return f"<!-- entry:{summary_id} -->"


def default_journal_dir():
    return C.data_root() / "sync-state" / "journal"


# --- low-level helpers ------------------------------------------------------

def _read(path):
    """Return (raw_bytes, had_bom, text). BOM-tolerant (utf-8-sig)."""
    raw = path.read_bytes()
    had_bom = raw.startswith(UTF8_BOM)
    text = (raw[len(UTF8_BOM):] if had_bom else raw).decode("utf-8")
    return raw, had_bom, text


def _detect_newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _find_region(text):
    """(begin_idx, end_idx) of the marker pair, or None if absent/malformed.
    begin_idx points at BEGIN_MARKER; end_idx at END_MARKER (after begin)."""
    b = text.find(BEGIN_MARKER)
    if b == -1:
        return None
    e = text.find(END_MARKER, b + len(BEGIN_MARKER))
    if e == -1:
        return None
    return b, e


def _norm_block(block, nl):
    """Trim surrounding blank lines and normalize the block's own newlines to the
    file's style. Only ever applied to inserted content, never to existing bytes."""
    s = block.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return s.replace("\n", nl) if nl != "\n" else s


def _atomic_write(path, data):
    """Write `data` (bytes) to `path` atomically: temp in same dir + os.replace.
    On any failure the temp file is removed, so a crash never leaves a half-file
    and never corrupts the existing `path`."""
    path = Path(path)
    fd, tmpname = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_journal(journal_dir, path, raw_before, had_bom, existed, summary_id, status):
    """Capture the touched file's pre-state BEFORE the content write so the append
    is reversible (rollback()). Borrows Steward apply.py's *shape* only."""
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    jpath = journal_dir / f"{stamp}.json"
    doc = {
        "created": stamp,
        "kind": "progress-log-append",
        "file": str(Path(path).resolve()),
        "summary_id": summary_id,
        "status": status,
        "existed": existed,
        "had_bom": had_bom,
        "pre_sha256": hashlib.sha256(raw_before).hexdigest() if existed else None,
        "pre_b64": base64.b64encode(raw_before).decode("ascii") if existed else None,
    }
    _atomic_write(jpath, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    return jpath


# --- public API -------------------------------------------------------------

def append_entry(path, summary_id, block, *, journal_dir=None, create_if_missing=False):
    """Append a dated `block` for `summary_id` into the project note at `path`.

    Returns a Result(status, path, journal, summary_id). Never touches bytes
    outside the marker region. Journals pre-state before writing.
    """
    path = Path(path)
    jdir = Path(journal_dir) if journal_dir is not None else default_journal_dir()

    # (a) missing file
    if not path.exists():
        if not create_if_missing:
            return Result(REFUSED_NO_FILE, path, None, summary_id)
        nl = "\n"  # fresh tool-created notes use LF
        new_text = (BEGIN_MARKER + nl
                    + entry_marker(summary_id) + nl + _norm_block(block, nl) + nl
                    + END_MARKER + nl)
        jpath = _write_journal(jdir, path, b"", had_bom=False, existed=False,
                               summary_id=summary_id, status=CREATED)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, new_text.encode("utf-8"))
        return Result(CREATED, path, jpath, summary_id)

    # (b) existing file
    raw, had_bom, text = _read(path)
    region = _find_region(text)
    if region is None:
        # never grow / inject markers into a hand-authored note: refuse, write nothing
        return Result(REFUSED_NO_MARKER, path, None, summary_id)

    b, e = region
    if entry_marker(summary_id) in text[b:e]:
        return Result(SKIPPED_DUPLICATE, path, None, summary_id)  # idempotent no-op

    nl = _detect_newline(text)
    after = b + len(BEGIN_MARKER)
    if text[after:after + 2] == "\r\n":
        ins, lead = after + 2, ""
    elif text[after:after + 1] == "\n":
        ins, lead = after + 1, ""
    else:
        # degenerate note with no newline after the begin marker: keep its bytes,
        # start the block on a fresh line.
        ins, lead = after, nl

    unit = lead + entry_marker(summary_id) + nl + _norm_block(block, nl) + nl
    new_text = text[:ins] + unit + text[ins:]
    new_bytes = (UTF8_BOM if had_bom else b"") + new_text.encode("utf-8")

    jpath = _write_journal(jdir, path, raw, had_bom, existed=True,
                           summary_id=summary_id, status=APPENDED)
    _atomic_write(path, new_bytes)
    return Result(APPENDED, path, jpath, summary_id)


# --- frontmatter field updater (sync-layer.md §5) ---------------------------
# Surgical updater for the LEADING `--- ... ---` YAML frontmatter block. Unlike
# append_entry() (which only ever touches the progress-log marker region), this
# writes a small, named set of top-level frontmatter keys so an approved
# status/stage/next_actions/blocked_by shows on the Dataview board (which reads
# frontmatter). It NEVER touches the note body, the progress-log region, prose, or
# any frontmatter key it was not asked to set — those bytes are preserved exactly.
# Same safety model as append_entry: BOM-tolerant read, newline-preserving, journal
# pre-state, atomic temp+rename, reversible via rollback().

FM_UPDATED = "updated"            # fields-changed (and/or `updated` bumped)
FM_ADDED = "added"                # at least one requested key was absent and ADDED
FM_NO_FRONTMATTER = "no_frontmatter"  # note has no leading `--- ... ---` block: skipped
FM_NOOP = "noop"                  # nothing to change (no fields, already equal)

FMResult = namedtuple("FMResult", ["status", "path", "journal", "changed", "added"])


def _fm_bounds(lines):
    """(start, end) line indices of the leading frontmatter block: `lines[start]`
    is the opening `---`, `lines[end]` the closing `---`/`...`. None if absent.
    The block must START on the very first line (a leading `---`)."""
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return 0, i
    return None


def _fm_key(line):
    """The top-level key of a frontmatter line (indent 0, `key:` form), else None."""
    if not line or line[:1] in (" ", "\t") or line.lstrip().startswith("#"):
        return None
    m = re.match(r"([^:#]+):(?:\s.*|)$", line)
    return m.group(1).strip() if m else None


def _key_span(fm_lines, idx):
    """Number of lines the key at `fm_lines[idx]` occupies, INCLUDING any indented
    block children (block list / block map continued on following indented lines)."""
    span = 1
    j = idx + 1
    while j < len(fm_lines):
        nxt = fm_lines[j]
        if not nxt.strip():                       # blank line still belongs to the key
            span += 1
            j += 1
            continue
        if nxt[:1] in (" ", "\t"):                # indented child line
            span += 1
            j += 1
            continue
        break
    # trailing blank lines belong to the gap, not the key: trim them back
    while span > 1 and not fm_lines[idx + span - 1].strip():
        span -= 1
    return span


def _is_block_list(fm_lines, idx):
    """True if the key at `idx` is rendered as a BLOCK list (`key:` then indented
    `- item` lines) rather than a flow list (`key: [a, b]`) or a scalar."""
    head = fm_lines[idx]
    after_colon = head.split(":", 1)[1].strip() if ":" in head else ""
    if after_colon:                               # has an inline value -> not a block list
        return False
    j = idx + 1
    while j < len(fm_lines) and not fm_lines[j].strip():
        j += 1
    return j < len(fm_lines) and fm_lines[j][:1] in (" ", "\t") and fm_lines[j].lstrip().startswith("-")


def _child_indent(fm_lines, idx):
    """The indent string of the first block-list child under the key at `idx`."""
    j = idx + 1
    while j < len(fm_lines):
        if fm_lines[j].strip():
            return fm_lines[j][:len(fm_lines[j]) - len(fm_lines[j].lstrip())]
        j += 1
    return "  "


def _yaml_scalar(v):
    """Render a scalar value. Quote only when needed to stay parseable by the
    stdlib subset parser (leading/trailing space, or a leading list/map char)."""
    s = str(v)
    if s == "":
        return '""'
    if s != s.strip() or s[:1] in ("[", "{", "#", "-", "'", '"', "&", "*", "!", "|", ">", "@", "`"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _render_list(key, items, *, block, indent, nl):
    """Render `key` + `items` either as a flow list (`key: [a, b]`) or a block list
    (`key:` then indented `- item` lines). Returns the list of physical lines."""
    if block:
        out = [f"{key}:"]
        out += [f"{indent}- {_yaml_scalar(x)}" for x in items]
        return out
    return [f"{key}: [" + ", ".join(_yaml_scalar(x) for x in items) + "]"]


def _fm_field_lines(key, value, *, block_list, indent, nl):
    """Render the physical line(s) for one frontmatter field."""
    if isinstance(value, list):
        return _render_list(key, value, block=block_list, indent=indent, nl=nl)
    return [f"{key}: {_yaml_scalar(value)}"]


def update_frontmatter_fields(path, fields, *, journal_dir=None):
    """Update ONLY the named top-level keys inside the leading `--- ... ---`
    frontmatter block of the note at `path`, preserving every other byte: the note
    body, the progress-log region, all OTHER frontmatter keys, key order, comments,
    BOM, and newline style.

    `fields` maps frontmatter key -> value (scalar str or list[str] for sequences).
    A key already present is replaced IN PLACE (its existing flow/block style kept);
    a key absent is ADDED just before the closing `---`. A note with no leading
    frontmatter block is skipped (FM_NO_FRONTMATTER) — the caller still did the
    progress-log append + memory overlay. Journals pre-state before writing; never
    touches a byte outside the frontmatter block; reversible via rollback().
    """
    path = Path(path)
    jdir = Path(journal_dir) if journal_dir is not None else default_journal_dir()
    if not fields:
        return FMResult(FM_NOOP, path, None, [], [])
    if not path.exists():
        return FMResult(FM_NO_FRONTMATTER, path, None, [], [])

    raw, had_bom, text = _read(path)
    nl = _detect_newline(text)
    # split on the file's newline style, KEEPING the structure so we can rejoin 1:1
    lines = text.split(nl)
    bounds = _fm_bounds(lines)
    if bounds is None:
        return FMResult(FM_NO_FRONTMATTER, path, None, [], [])

    start, end = bounds
    fm = lines[start + 1:end]                      # the frontmatter body lines (exclusive)

    # index the present top-level keys -> their first physical line within `fm`
    present = {}
    i = 0
    while i < len(fm):
        k = _fm_key(fm[i])
        if k is not None and k not in present:
            present[k] = i
        i += 1

    changed, added = [], []
    # process replacements from the BOTTOM up so earlier indices stay valid
    for key in sorted(fields, key=lambda k: present.get(k, -1), reverse=True):
        if key not in present:
            continue
        idx = present[key]
        span = _key_span(fm, idx)
        block_list = _is_block_list(fm, idx)
        indent = _child_indent(fm, idx) if block_list else "  "
        new_lines = _fm_field_lines(key, fields[key], block_list=block_list, indent=indent, nl=nl)
        if fm[idx:idx + span] != new_lines:
            fm[idx:idx + span] = new_lines
            changed.append(key)
        # else: identical -> leave the bytes untouched (no spurious change)

    # additions: keys not already present go in just before the closing `---`,
    # in a stable order. New lists default to FLOW style (matches the project/1.0
    # example note `next_actions: [a, b]`).
    for key in fields:
        if key in present:
            continue
        new_lines = _fm_field_lines(key, fields[key], block_list=False, indent="  ", nl=nl)
        fm.extend(new_lines)
        added.append(key)

    if not changed and not added:
        return FMResult(FM_NOOP, path, None, [], [])

    new_text = nl.join(lines[:start + 1] + fm + lines[end:])
    new_bytes = (UTF8_BOM if had_bom else b"") + new_text.encode("utf-8")

    status = FM_ADDED if added else FM_UPDATED
    jpath = _write_journal(jdir, path, raw, had_bom, existed=True,
                           summary_id="frontmatter-update", status=status)
    _atomic_write(path, new_bytes)
    return FMResult(status, path, jpath, changed, added)


def rollback(journal_path):
    """Restore a file to the pre-state captured in `journal_path`. A CREATED entry
    rolls back by deleting the file the append created."""
    doc = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    target = Path(doc["file"])
    if not doc.get("existed"):
        if target.exists():
            target.unlink()
        return
    _atomic_write(target, base64.b64decode(doc["pre_b64"]))
