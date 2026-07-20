# -*- coding: utf-8 -*-
"""cwd -> project_id reverse resolver (sync-layer.md §7).

Builds a reverse index from memory/projects.json keyed by normalized `linked_repo`
(absolute path) AND by `project_id`. A session/openclaw event's cwd resolves to the
owning project: an exact repo match wins, else the nearest ANCESTOR repo (so a
worktree / subdir cwd still resolves to its project — a worktree cwd like
`<repo>/.claude/worktrees/<name>` never equals `linked_repo` exactly).

Fallback when nothing matches: return None. The caller keeps the event pending for
explicit project resolution and NEVER authors a summary or project spine implicitly.

Stdlib only, no network, read-only on projects.json.
"""
from pathlib import PurePosixPath

from provenance import memory as M


_WINDOWS_USER_ALIAS_PREFIX = "\0windows-user:"
_MISSING = object()
_AMBIGUOUS = object()


def _norm(p):
    """Deterministic, platform-independent path key: forward slashes, case-folded,
    no trailing slash. Repo paths in projects.json are logical Windows paths used as
    keys; they must compare case- and separator-insensitively on ANY host running
    Provenance (e.g. a Linux CI runner). os.path.normcase/normpath/abspath only fold
    case and convert separators on Windows (and pull in the CWD on POSIX), so we
    normalize explicitly with pure string ops + PurePosixPath instead."""
    if not p:
        return None
    s = PurePosixPath(str(p).replace("\\", "/")).as_posix()
    return s.casefold().rstrip("/") or "/"


def _windows_user_alias(normalized):
    """Alias one Windows profile segment without retaining or recovering its value."""
    if not normalized:
        return None
    parts = normalized.split("/")
    if (
        len(parts) < 3
        or len(parts[0]) != 2
        or not parts[0][0].isalpha()
        or parts[0][1] != ":"
        or parts[1] != "users"
        or not parts[2]
    ):
        return None
    parts[2] = "<user>"
    return "/".join(parts)


def _alias_key(normalized):
    alias = _windows_user_alias(normalized)
    return _WINDOWS_USER_ALIAS_PREFIX + alias if alias else None


def _path_candidates(target):
    return [target] + [_norm(parent) for parent in PurePosixPath(target).parents]


def _register_index_key(repo_index, key, project_id):
    """Register one key, preserving duplicates and poisoning cross-project clashes."""
    current = repo_index.get(key, _MISSING)
    if current is _MISSING:
        repo_index[key] = project_id
    elif current is not _AMBIGUOUS and current != project_id:
        repo_index[key] = _AMBIGUOUS


def _lookup_index_key(repo_index, key):
    value = repo_index.get(key, _MISSING)
    if value is _AMBIGUOUS:
        return None, True
    if value is _MISSING:
        return None, False
    return value, True


def build_index(projects):
    """Build exact and privacy-alias repo keys without writing either back to memory.

    Exact keys remain authoritative. Windows profile aliases only provide a fallback
    for public snapshots whose username was already redacted to ``<user>``. A key
    claimed by different projects becomes ambiguous and therefore cannot resolve.
    """
    repo_index, ids = {}, set()
    for pid, rec in (projects or {}).items():
        ids.add(str(pid))
        repo = rec.get("linked_repo") if isinstance(rec, dict) else None
        key = _norm(repo) if repo else None
        if key:
            project_id = str(pid)
            _register_index_key(repo_index, key, project_id)
            alias_key = _alias_key(key)
            if alias_key:
                _register_index_key(repo_index, alias_key, project_id)
    return repo_index, ids


def resolve(cwd, repo_index, ids):
    """Resolve a cwd (or an explicit project_id hint) to a project_id, or None."""
    if not cwd:
        return None
    s = str(cwd)
    if s in ids:                      # an explicit project_id hint (e.g. openclaw)
        return s
    target = _norm(s)
    if not target:
        return None
    candidates = _path_candidates(target)
    # Preserve existing behavior and disambiguation: exact paths always win.
    for cand in candidates:
        if not cand:
            continue
        project_id, matched = _lookup_index_key(repo_index, cand)
        if matched:
            return project_id
    # A real Windows profile path can now match a public `<user>` snapshot without
    # recovering or persisting the username.
    for cand in candidates:
        alias_key = _alias_key(cand)
        if not alias_key:
            continue
        project_id, matched = _lookup_index_key(repo_index, alias_key)
        if matched:
            return project_id
    return None


def resolve_project(cwd, root=None):
    """Convenience: load projects.json under `root` and resolve in one call."""
    repo_index, ids = build_index(M.load_projects(root))
    return resolve(cwd, repo_index, ids)
