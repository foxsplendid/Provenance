# -*- coding: utf-8 -*-
"""Safely accept one in-session summary fill through a public local CLI.

The caller supplies a ``summary_id`` and a ``summary-fill/1.0`` JSON object on
stdin. Provenance owns path construction, identity validation, serialization,
and the atomic write. No path or scaffold content is emitted.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from provenance import common as C
from provenance.synclayer import paths, summary, worker


MAX_FILL_BYTES = 1024 * 1024
_SUMMARY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_IDENTITY_FIELDS = {
    "summary_id",
    "project",
    "created",
    "session_id",
    "approval_state",
    "generated_by",
}
_FILL_FIELDS = {
    "schema_version",
    "timeline",
    "status",
    "stage",
    "next_actions",
    "conclusion",
    "blocked_by",
    "confidence",
}


class PendingFillError(RuntimeError):
    """A pending fill could not be accepted safely."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _PendingBinding:
    """One pending item held by stable OS directory/file identities."""

    def __init__(self, *, root, pending_item, fill_path, scaffold):
        self.root = root
        self.pending_item = pending_item
        self.fill_path = fill_path
        self.scaffold = scaffold
        self.item_fd = None
        self.item_handle = None
        self.posix_fds = []
        self.windows_handles = []


class _PendingRootBinding:
    """The pending collection held by a stable OS directory identity."""

    def __init__(self, *, pending_root):
        self.pending_root = pending_root
        self.pending_fd = None
        self.posix_fds = []
        self.windows_handles = []


class _WindowsTemp:
    def __init__(self, handle):
        self.handle = handle
        self.published = False


def _metadata_is_link_like(path, metadata):
    """Detect POSIX links and Windows junction/reparse-point paths."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    is_junction = getattr(path, "is_junction", None)
    try:
        return (
            stat.S_ISLNK(metadata.st_mode)
            or bool(reparse_flag and attributes & reparse_flag)
            or bool(is_junction and is_junction())
        )
    except OSError as exc:
        raise PendingFillError("unsafe_path") from exc


def _lstat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PendingFillError("unsafe_path") from exc


def _require_directory(path):
    metadata = _lstat(path)
    if metadata is None or _metadata_is_link_like(path, metadata):
        raise PendingFillError("unsafe_path")
    if not stat.S_ISDIR(metadata.st_mode):
        raise PendingFillError("invalid_pending_state")


def _require_regular_file(path):
    metadata = _lstat(path)
    if metadata is None:
        raise PendingFillError("invalid_scaffold")
    if _metadata_is_link_like(path, metadata):
        raise PendingFillError("unsafe_path")
    if not stat.S_ISREG(metadata.st_mode):
        raise PendingFillError("invalid_scaffold")


def _require_absent(path):
    metadata = _lstat(path)
    if metadata is None:
        return
    if _metadata_is_link_like(path, metadata):
        raise PendingFillError("unsafe_path")
    raise PendingFillError("fill_exists")


def _safe_root(root):
    requested = Path(root or C.data_root()).expanduser()
    try:
        # Normalize dots without resolving links. The platform binding opens this
        # lexical root with no-follow semantics and becomes the identity anchor.
        lexical = Path(os.path.abspath(os.fspath(requested)))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PendingFillError("unsafe_root") from exc
    metadata = _lstat(lexical)
    if metadata is None or _metadata_is_link_like(lexical, metadata):
        raise PendingFillError("unsafe_root")
    if not stat.S_ISDIR(metadata.st_mode):
        raise PendingFillError("unsafe_root")
    return lexical


def _safe_summary_id(summary_id):
    if not isinstance(summary_id, str) or not _SUMMARY_ID.fullmatch(summary_id):
        raise PendingFillError("invalid_summary_id")
    if summary_id in {".", ".."}:
        raise PendingFillError("invalid_summary_id")
    return summary_id


def _parse_scaffold_bytes(raw, *, summary_id):
    try:
        scaffold = json.loads(raw.decode("utf-8-sig"))
    except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
        raise PendingFillError("invalid_scaffold") from exc
    project = scaffold.get("project") if isinstance(scaffold, dict) else None
    created = scaffold.get("created") if isinstance(scaffold, dict) else None
    session_id = scaffold.get("session_id") if isinstance(scaffold, dict) else None
    title = scaffold.get("title") if isinstance(scaffold, dict) else None
    turns = scaffold.get("turns") if isinstance(scaffold, dict) else None
    if (
        not isinstance(scaffold, dict)
        or scaffold.get("schema_version") != summary.SCAFFOLD_SCHEMA
        or scaffold.get("summary_id") != summary_id
        or not isinstance(project, str)
        or not project.strip()
        or not isinstance(created, str)
        or not created.strip()
        or (session_id is not None and not isinstance(session_id, str))
        or not isinstance(title, str)
        or not isinstance(turns, list)
        or not all(
            isinstance(turn, dict)
            and turn.get("role") in {"user", "assistant"}
            and isinstance(turn.get("text"), str)
            for turn in turns
        )
    ):
        raise PendingFillError("invalid_scaffold")
    return {
        "schema_version": summary.SCAFFOLD_SCHEMA,
        "summary_id": summary_id,
        "project": project,
        "created": created,
        "session_id": session_id,
        "title": title,
        "turns": [
            {"role": turn["role"], "text": turn["text"]}
            for turn in turns
        ],
    }


def _load_scaffold(scaffold_path, *, summary_id):
    """Legacy path helper; public operations use an identity-bound file handle."""
    _require_regular_file(scaffold_path)
    try:
        raw = scaffold_path.read_bytes()
    except OSError as exc:
        raise PendingFillError("invalid_scaffold") from exc
    return _parse_scaffold_bytes(raw, summary_id=summary_id)


def _validated_paths(root, summary_id):
    """Return safe canonical paths for one pending item, without creating them."""
    resolved_root = _safe_root(root)
    safe_id = _safe_summary_id(summary_id)
    sync_state = paths.sync_state(resolved_root)
    pending_root = paths.pending_dir(resolved_root)
    pending_item = pending_root / safe_id
    scaffold_path = pending_item / "scaffold.json"
    fill_path = pending_item / "fill.json"

    for directory in (sync_state, pending_root, pending_item):
        _require_directory(directory)
    try:
        pending_item.resolve(strict=True).relative_to(pending_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PendingFillError("unsafe_path") from exc
    _require_regular_file(scaffold_path)
    _require_absent(fill_path)

    lock_path = paths.lock_file(resolved_root)
    lock_metadata = _lstat(lock_path)
    if lock_metadata is not None and _metadata_is_link_like(lock_path, lock_metadata):
        raise PendingFillError("unsafe_path")
    return resolved_root, pending_item, fill_path


def _same_file_identity(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _posix_open_entry(parent_fd, name_or_path, *, directory, error_code):
    """Open one path component without following links and bind its inode."""
    dir_fd_support = getattr(os, "supports_dir_fd", set())
    follow_support = getattr(os, "supports_follow_symlinks", set())
    if (
        os.open not in dir_fd_support
        or os.stat not in dir_fd_support
        or os.stat not in follow_support
    ):
        raise PendingFillError("unsafe_path")
    try:
        if parent_fd is None:
            before = os.stat(name_or_path, follow_symlinks=False)
        else:
            before = os.stat(
                name_or_path, dir_fd=parent_fd, follow_symlinks=False
            )
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingFillError(error_code) from exc
    except OSError as exc:
        raise PendingFillError("unsafe_path") from exc
    if stat.S_ISLNK(before.st_mode):
        raise PendingFillError("unsafe_path")
    if (directory and not stat.S_ISDIR(before.st_mode)) or (
        not directory and not stat.S_ISREG(before.st_mode)
    ):
        raise PendingFillError(error_code)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        if parent_fd is None:
            fd = os.open(name_or_path, flags)
        else:
            fd = os.open(name_or_path, flags, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingFillError(error_code) from exc
    except OSError as exc:
        raise PendingFillError("unsafe_path") from exc
    try:
        after = os.fstat(fd)
        wrong_type = (
            directory and not stat.S_ISDIR(after.st_mode)
        ) or (not directory and not stat.S_ISREG(after.st_mode))
        if wrong_type or not _same_file_identity(before, after):
            raise PendingFillError("unsafe_path")
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_posix_fd(fd):
    chunks = []
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError as exc:
        raise PendingFillError("invalid_scaffold") from exc


def _posix_require_fill_absent(item_fd):
    try:
        metadata = os.stat("fill.json", dir_fd=item_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PendingFillError("unsafe_path") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PendingFillError("unsafe_path")
    raise PendingFillError("fill_exists")


def _open_posix_pending_root(resolved_root):
    fds = []
    try:
        root_fd = _posix_open_entry(
            None, str(resolved_root), directory=True, error_code="unsafe_root"
        )
        fds.append(root_fd)
        sync_fd = _posix_open_entry(
            root_fd, "sync-state", directory=True, error_code="invalid_pending_state"
        )
        fds.append(sync_fd)
        pending_fd = _posix_open_entry(
            sync_fd, "pending", directory=True, error_code="invalid_pending_state"
        )
        fds.append(pending_fd)
        binding = _PendingRootBinding(
            pending_root=paths.pending_dir(resolved_root)
        )
        binding.pending_fd = pending_fd
        binding.posix_fds = fds
        return binding
    except Exception:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _open_posix_item_from_pending(root_binding, resolved_root, safe_id):
    fds = []
    try:
        item_fd = _posix_open_entry(
            root_binding.pending_fd,
            safe_id,
            directory=True,
            error_code="invalid_pending_state",
        )
        fds.append(item_fd)
        scaffold_fd = _posix_open_entry(
            item_fd, "scaffold.json", directory=False, error_code="invalid_scaffold"
        )
        fds.append(scaffold_fd)
        scaffold = _parse_scaffold_bytes(
            _read_posix_fd(scaffold_fd), summary_id=safe_id
        )
        _posix_require_fill_absent(item_fd)
        pending_item = paths.pending_dir(resolved_root) / safe_id
        binding = _PendingBinding(
            root=resolved_root,
            pending_item=pending_item,
            fill_path=pending_item / "fill.json",
            scaffold=scaffold,
        )
        binding.item_fd = item_fd
        binding.posix_fds = fds
        return binding
    except Exception:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _open_posix_binding(resolved_root, safe_id):
    dir_fd_support = getattr(os, "supports_dir_fd", set())
    follow_support = getattr(os, "supports_follow_symlinks", set())
    if (
        os.link not in dir_fd_support
        or os.unlink not in dir_fd_support
        or os.link not in follow_support
    ):
        raise PendingFillError("unsafe_path")
    fds = []
    try:
        root_fd = _posix_open_entry(
            None, str(resolved_root), directory=True, error_code="unsafe_root"
        )
        fds.append(root_fd)
        sync_fd = _posix_open_entry(
            root_fd, "sync-state", directory=True, error_code="invalid_pending_state"
        )
        fds.append(sync_fd)
        pending_fd = _posix_open_entry(
            sync_fd, "pending", directory=True, error_code="invalid_pending_state"
        )
        fds.append(pending_fd)
        item_fd = _posix_open_entry(
            pending_fd, safe_id, directory=True, error_code="invalid_pending_state"
        )
        fds.append(item_fd)
        scaffold_fd = _posix_open_entry(
            item_fd, "scaffold.json", directory=False, error_code="invalid_scaffold"
        )
        fds.append(scaffold_fd)
        scaffold = _parse_scaffold_bytes(
            _read_posix_fd(scaffold_fd), summary_id=safe_id
        )
        _posix_require_fill_absent(item_fd)
        pending_item = paths.pending_dir(resolved_root) / safe_id
        binding = _PendingBinding(
            root=resolved_root,
            pending_item=pending_item,
            fill_path=pending_item / "fill.json",
            scaffold=scaffold,
        )
        binding.item_fd = item_fd
        binding.posix_fds = fds
        return binding
    except Exception:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_WRITE_DATA = 0x00000002
    _FILE_WRITE_ATTRIBUTES = 0x00000100
    _FILE_TRAVERSE = 0x00000020
    _FILE_CREATE = 2
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OBJ_DONT_REPARSE = 0x00001000
    _OPEN_EXISTING = 3
    _FILE_RENAME_INFORMATION_CLASS = 10
    _FILE_DISPOSITION_INFORMATION_CLASS = 13
    _STATUS_OBJECT_NAME_COLLISION = 0xC0000035
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IO_STATUS_VALUE(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("value", _IO_STATUS_VALUE), ("Information", ctypes.c_size_t)]

    class _RENAME_UNION(ctypes.Union):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BYTE),
            ("Flags", wintypes.DWORD),
        ]

    class _FILE_RENAME_INFORMATION(ctypes.Structure):
        _anonymous_ = ("choice",)
        _fields_ = [
            ("choice", _RENAME_UNION),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.ULONG),
            ("FileName", wintypes.WCHAR * 1),
        ]

    class _FILE_DISPOSITION_INFORMATION(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _KERNEL32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _KERNEL32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _GetFileInformationByHandle = _KERNEL32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _GetFileInformationByHandle.restype = wintypes.BOOL
    _ReadFile = _KERNEL32.ReadFile
    _ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _ReadFile.restype = wintypes.BOOL
    _WriteFile = _KERNEL32.WriteFile
    _WriteFile.argtypes = _ReadFile.argtypes
    _WriteFile.restype = wintypes.BOOL
    _FlushFileBuffers = _KERNEL32.FlushFileBuffers
    _FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _FlushFileBuffers.restype = wintypes.BOOL
    _NTDLL = ctypes.WinDLL("ntdll")
    _NtCreateFile = _NTDLL.NtCreateFile
    _NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    _NtCreateFile.restype = wintypes.LONG
    _NtSetInformationFile = _NTDLL.NtSetInformationFile
    _NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    _NtSetInformationFile.restype = wintypes.LONG


def _win_extended_path(path):
    # The root was canonicalized before binding. Keep descendants lexical here:
    # resolving a just-created ``fill.json`` symlink would retarget the operation.
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _nt_u32(status):
    return status & 0xFFFFFFFF


def _nt_object_attributes(root_handle, leaf):
    buffer = ctypes.create_unicode_buffer(leaf)
    byte_length = len(leaf.encode("utf-16-le"))
    name = _UNICODE_STRING(
        byte_length,
        byte_length + 2,
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES),
        root_handle,
        ctypes.pointer(name),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        None,
        None,
    )
    return attributes, name, buffer


def _nt_create_temp(item_handle, leaf):
    attributes, name, buffer = _nt_object_attributes(item_handle, leaf)
    handle = wintypes.HANDLE()
    iosb = _IO_STATUS_BLOCK()
    status = _NtCreateFile(
        ctypes.byref(handle),
        _FILE_WRITE_DATA
        | _FILE_WRITE_ATTRIBUTES
        | _DELETE
        | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(iosb),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        0,
        _FILE_CREATE,
        _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )
    # Keep the object-attribute backing values alive until the native call returns.
    _ = (name, buffer)
    return status, handle.value


def _nt_publish_no_replace(temp_handle, item_handle):
    encoded = "fill.json".encode("utf-16-le")
    size = ctypes.sizeof(_FILE_RENAME_INFORMATION) + len(encoded)
    raw = ctypes.create_string_buffer(size)
    info = ctypes.cast(raw, ctypes.POINTER(_FILE_RENAME_INFORMATION)).contents
    info.Flags = 0
    info.RootDirectory = item_handle
    info.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(raw) + _FILE_RENAME_INFORMATION.FileName.offset,
        encoded,
        len(encoded),
    )
    iosb = _IO_STATUS_BLOCK()
    return _NtSetInformationFile(
        temp_handle,
        ctypes.byref(iosb),
        raw,
        size,
        _FILE_RENAME_INFORMATION_CLASS,
    )


def _nt_delete_by_handle(temp_handle):
    info = _FILE_DISPOSITION_INFORMATION(1)
    iosb = _IO_STATUS_BLOCK()
    return _NtSetInformationFile(
        temp_handle,
        ctypes.byref(iosb),
        ctypes.byref(info),
        ctypes.sizeof(info),
        _FILE_DISPOSITION_INFORMATION_CLASS,
    )


def _win_close(handle):
    if os.name == "nt" and handle not in (None, _INVALID_HANDLE_VALUE):
        _CloseHandle(handle)


def _win_info(handle, *, error_code):
    info = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise PendingFillError(error_code)
    return info


def _win_validate_info(info, *, directory, error_code):
    attributes = info.dwFileAttributes
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise PendingFillError("unsafe_path")
    if bool(attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise PendingFillError(error_code)


def _win_open(path, *, directory, access, share, disposition, error_code):
    handle = _CreateFileW(
        _win_extended_path(path),
        access,
        share,
        None,
        disposition,
        (
            _FILE_FLAG_OPEN_REPARSE_POINT
            | (_FILE_FLAG_BACKUP_SEMANTICS if directory else _FILE_ATTRIBUTE_NORMAL)
        ),
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise PendingFillError(error_code)
    try:
        _win_validate_info(
            _win_info(handle, error_code=error_code),
            directory=directory,
            error_code=error_code,
        )
    except Exception:
        _win_close(handle)
        raise
    return handle


def _read_windows_handle(handle):
    chunks = []
    while True:
        buffer = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD()
        if not _ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
            raise PendingFillError("invalid_scaffold")
        if not read.value:
            return b"".join(chunks)
        chunks.append(buffer.raw[: read.value])


def _open_windows_pending_root(resolved_root):
    handles = []
    try:
        current = resolved_root
        for component in (None, "sync-state", "pending"):
            if component is not None:
                current = current / component
            handle = _win_open(
                current,
                directory=True,
                access=_FILE_READ_ATTRIBUTES | _FILE_TRAVERSE | _SYNCHRONIZE,
                share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                disposition=_OPEN_EXISTING,
                error_code=("unsafe_root" if component is None else "invalid_pending_state"),
            )
            handles.append(handle)
        binding = _PendingRootBinding(pending_root=current)
        binding.windows_handles = handles
        return binding
    except Exception:
        for handle in reversed(handles):
            _win_close(handle)
        raise


def _open_windows_binding(resolved_root, safe_id):
    handles = []
    try:
        current = resolved_root
        for component in (None, "sync-state", "pending", safe_id):
            if component is not None:
                current = current / component
            handle = _win_open(
                current,
                directory=True,
                access=_FILE_READ_ATTRIBUTES | _FILE_TRAVERSE | _SYNCHRONIZE,
                share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                disposition=_OPEN_EXISTING,
                error_code=("unsafe_root" if component is None else "invalid_pending_state"),
            )
            handles.append(handle)
        scaffold_path = current / "scaffold.json"
        scaffold_handle = _win_open(
            scaffold_path,
            directory=False,
            access=_GENERIC_READ,
            share=_FILE_SHARE_READ,
            disposition=_OPEN_EXISTING,
            error_code="invalid_scaffold",
        )
        handles.append(scaffold_handle)
        scaffold = _parse_scaffold_bytes(
            _read_windows_handle(scaffold_handle), summary_id=safe_id
        )
        fill_path = current / "fill.json"
        _require_absent(fill_path)
        binding = _PendingBinding(
            root=resolved_root,
            pending_item=current,
            fill_path=fill_path,
            scaffold=scaffold,
        )
        binding.item_handle = handles[3]
        binding.windows_handles = handles
        return binding
    except Exception:
        for handle in reversed(handles):
            _win_close(handle)
        raise


@contextlib.contextmanager
def _bound_pending(root, summary_id):
    resolved_root = _safe_root(root)
    safe_id = _safe_summary_id(summary_id)
    binding = (
        _open_windows_binding(resolved_root, safe_id)
        if os.name == "nt"
        else _open_posix_binding(resolved_root, safe_id)
    )
    try:
        yield binding
    finally:
        for fd in reversed(binding.posix_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        for handle in reversed(binding.windows_handles):
            _win_close(handle)


@contextlib.contextmanager
def _bound_pending_root(root):
    resolved_root = _safe_root(root)
    binding = (
        _open_windows_pending_root(resolved_root)
        if os.name == "nt"
        else _open_posix_pending_root(resolved_root)
    )
    try:
        yield binding
    finally:
        for fd in reversed(binding.posix_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        for handle in reversed(binding.windows_handles):
            _win_close(handle)


def _bound_pending_names(binding):
    try:
        if os.name == "nt":
            return sorted(item.name for item in binding.pending_root.iterdir())
        if os.listdir not in getattr(os, "supports_fd", set()):
            raise PendingFillError("unsafe_path")
        return sorted(os.listdir(binding.pending_fd))
    except PendingFillError:
        raise
    except OSError as exc:
        raise PendingFillError("invalid_pending_state") from exc


@contextlib.contextmanager
def _bound_pending_candidate(root_binding, resolved_root, summary_id):
    safe_id = _safe_summary_id(summary_id)
    if os.name == "nt":
        context = _bound_pending(resolved_root, safe_id)
    else:
        binding = _open_posix_item_from_pending(
            root_binding, resolved_root, safe_id
        )

        @contextlib.contextmanager
        def held():
            try:
                yield binding
            finally:
                for fd in reversed(binding.posix_fds):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        context = held()
    with context as binding:
        yield binding


def _write_all(fd, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if not written:
            raise OSError(errno.EIO, "short write")
        offset += written


def _create_posix_temp(binding, serialized):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(128):
        name = f".fill-{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=binding.item_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PendingFillError("write_failed") from exc
        try:
            _write_all(fd, serialized)
            os.fsync(fd)
        except OSError as exc:
            try:
                os.unlink(name, dir_fd=binding.item_fd)
            except OSError:
                pass
            raise PendingFillError("write_failed") from exc
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return name
    raise PendingFillError("write_failed")


def _write_windows_handle(handle, payload):
    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + 65536]
        buffer = ctypes.create_string_buffer(chunk, len(chunk))
        written = wintypes.DWORD()
        if not _WriteFile(
            handle, buffer, len(chunk), ctypes.byref(written), None
        ) or not written.value:
            raise PendingFillError("write_failed")
        offset += written.value
    if not _FlushFileBuffers(handle):
        raise PendingFillError("write_failed")


def _create_windows_temp(binding, serialized):
    for _attempt in range(128):
        leaf = f".fill-{secrets.token_hex(16)}.tmp"
        status, handle = _nt_create_temp(binding.item_handle, leaf)
        if status < 0:
            if _nt_u32(status) == _STATUS_OBJECT_NAME_COLLISION:
                continue
            raise PendingFillError("write_failed")
        temp = _WindowsTemp(handle)
        try:
            _write_windows_handle(handle, serialized)
        except PendingFillError:
            _nt_delete_by_handle(handle)
            _win_close(handle)
            temp.handle = None
            raise
        return temp
    raise PendingFillError("write_failed")


def _create_temp(binding, serialized):
    return (
        _create_windows_temp(binding, serialized)
        if os.name == "nt"
        else _create_posix_temp(binding, serialized)
    )


def _publish_no_replace(binding, temp):
    """Atomically publish ``fill.json`` and fail if any destination exists."""
    if os.name == "nt":
        status = _nt_publish_no_replace(temp.handle, binding.item_handle)
        if status >= 0:
            temp.published = True
            return
        if _nt_u32(status) == _STATUS_OBJECT_NAME_COLLISION:
            raise PendingFillError("fill_exists")
        raise PendingFillError("write_failed")
    try:
        os.link(
            temp,
            "fill.json",
            src_dir_fd=binding.item_fd,
            dst_dir_fd=binding.item_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise PendingFillError("fill_exists") from exc
    except OSError as exc:
        raise PendingFillError("write_failed") from exc


def _cleanup_temp(binding, temp):
    if temp is None:
        return
    try:
        if os.name == "nt":
            if temp.handle is not None:
                if not temp.published:
                    _nt_delete_by_handle(temp.handle)
                _win_close(temp.handle)
                temp.handle = None
        else:
            os.unlink(temp, dir_fd=binding.item_fd)
    except OSError:
        pass


def list_pending_ids(*, root=None):
    """Return only safe summary IDs; never expose protected filesystem paths."""
    resolved_root = _safe_root(root)
    sync_state = paths.sync_state(resolved_root)
    pending_root = paths.pending_dir(resolved_root)
    if _lstat(sync_state) is None or _lstat(pending_root) is None:
        return []
    _require_directory(sync_state)
    _require_directory(pending_root)
    summary_ids = []
    with _bound_pending_root(resolved_root) as root_binding:
        candidate_names = _bound_pending_names(root_binding)
        for candidate_name in candidate_names:
            try:
                safe_id = _safe_summary_id(candidate_name)
                with _bound_pending_candidate(
                    root_binding, resolved_root, safe_id
                ):
                    pass
            except PendingFillError as exc:
                if exc.code in {
                    "invalid_summary_id",
                    "invalid_pending_state",
                    "invalid_scaffold",
                    "fill_exists",
                }:
                    continue
                raise
            summary_ids.append(safe_id)
    return summary_ids


def read_pending_scaffold(summary_id, *, root=None):
    """Read one allowlisted, already-sanitized scaffold without exposing its path."""
    resolved_root, _pending_item, _fill_path = _validated_paths(root, summary_id)
    with _bound_pending(resolved_root, summary_id) as binding:
        return binding.scaffold


def validate_fill(fill):
    if not isinstance(fill, dict):
        raise PendingFillError("invalid_fill")
    if _IDENTITY_FIELDS.intersection(fill):
        raise PendingFillError("invalid_fill")
    if set(fill).difference(_FILL_FIELDS):
        raise PendingFillError("invalid_fill")
    if summary.validate_fill(fill):
        raise PendingFillError("invalid_fill")


def parse_fill(text):
    try:
        payload = text.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise PendingFillError("invalid_fill") from exc
    if not payload or len(payload) > MAX_FILL_BYTES:
        raise PendingFillError("invalid_fill")
    try:
        fill = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PendingFillError("invalid_fill") from exc
    validate_fill(fill)
    return fill


def submit_fill(summary_id, fill, *, root=None):
    """Validate and atomically write one fill under the worker lock."""
    validate_fill(fill)
    resolved_root, _pending_item, _fill_path = _validated_paths(root, summary_id)
    serialized = (json.dumps(fill, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with worker._Lock(paths.lock_file(resolved_root)):
            resolved_root, _pending_item, _fill_path = _validated_paths(
                resolved_root, summary_id
            )
            with _bound_pending(resolved_root, summary_id) as binding:
                temp = None
                try:
                    temp = _create_temp(binding, serialized)
                    _publish_no_replace(binding, temp)
                finally:
                    _cleanup_temp(binding, temp)
    except worker.WorkerBusy as exc:
        raise PendingFillError("worker_busy") from exc
    except worker.WorkerStateError as exc:
        raise PendingFillError("worker_state_failed") from exc
    return {"state": "accepted"}


def _report(*, status, exit_code, code=None):
    report = {
        "format_version": 1,
        "operation": "pending.fill",
        "status": status,
        "exit_code": exit_code,
    }
    if code is not None:
        report["code"] = code
    return report


def build_parser():
    parser = argparse.ArgumentParser(
        description="Accept one validated pending session fill from stdin."
    )
    parser.add_argument("summary_id")
    parser.add_argument("--provenance-home", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        text = sys.stdin.read(MAX_FILL_BYTES + 1)
        fill = parse_fill(text)
        submit_fill(args.summary_id, fill, root=args.provenance_home)
    except PendingFillError as exc:
        report = _report(status="error", exit_code=1, code=exc.code)
        if args.json_output:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"pending fill refused: {exc.code}", file=sys.stderr)
        return 1
    report = _report(status="accepted", exit_code=0)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("pending fill accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
