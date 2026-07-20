#!/usr/bin/env python3
"""Fail when the public source tree contains private or release-blocked artifacts."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist"}
FORBIDDEN_DIRS = {
    ".agents",
    ".claude",
    ".codex",
    "_attachments",
    "_prep",
    "_raw",
    "captured",
    "drop",
    "inbox",
    "memory",
    "output",
    "profile",
    "sync-state",
    "vault",
}
FORBIDDEN_NAMES = {
    ".env",
    "agents.md",
    "aliases.json",
    "claude.md",
    "credentials.json",
    "secrets.json",
}
FORBIDDEN_SUFFIXES = {
    ".bib",
    ".db",
    ".docx",
    ".jsonl",
    ".key",
    ".p12",
    ".pdf",
    ".pem",
    ".pfx",
    ".pptx",
    ".pyc",
    ".rdf",
    ".ris",
    ".sqlite",
    ".sqlite3",
    ".token",
    ".xls",
    ".xlsx",
    ".zip",
}
EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
WINDOWS_USER = re.compile(r"(?i)[A-Z]:[\\/]+Users[\\/]+([^\\/\s\"']+)")
POSIX_HOME = re.compile(r"/home/([A-Za-z0-9_<][A-Za-z0-9_.<>-]*)")
LOCAL_DEV_PATH = re.compile(r"(?i)\b[A-Z]:[\\/]+(?:Code|Repos|Projects)[\\/]+")
CREDENTIALS = (
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{16,}"),
)

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org"}
ALLOWED_WINDOWS_USERS = {"<user>", "demo", "demo-a", "demo-b", "demo-c", "demo-user"}
ALLOWED_POSIX_USERS = {"<user>", "demo", "demo-a", "demo-b", "demo-c"}


def iter_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        parts = {part.casefold() for part in rel.parts}
        if parts & SKIP_DIRS:
            continue
        yield path, rel, parts


def check_tree():
    errors = []
    for path, rel, parts in iter_files():
        rel_text = rel.as_posix()
        if parts & FORBIDDEN_DIRS:
            errors.append(f"forbidden directory: {rel_text}")
            continue
        if path.is_dir():
            continue
        name = path.name.casefold()
        if name in FORBIDDEN_NAMES or name.startswith("audit") or "hook-findings" in name:
            errors.append(f"forbidden file: {rel_text}")
            continue
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden file type: {rel_text}")
            continue
        data = path.read_bytes()
        if b"\0" in data:
            errors.append(f"binary file: {rel_text}")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"non-UTF-8 file: {rel_text}")
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        for match in EMAIL.finditer(text):
            if match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS:
                errors.append(f"non-example email: {rel_text}")
        for match in WINDOWS_USER.finditer(text):
            if match.group(1).casefold() not in ALLOWED_WINDOWS_USERS:
                errors.append(f"non-demo Windows user path: {rel_text}")
        for match in POSIX_HOME.finditer(text):
            if match.group(1).casefold() not in ALLOWED_POSIX_USERS:
                errors.append(f"non-demo POSIX home path: {rel_text}")
        if LOCAL_DEV_PATH.search(text):
            errors.append(f"local development path: {rel_text}")
        for pattern in CREDENTIALS:
            if pattern.search(text):
                errors.append(f"credential-shaped literal: {rel_text}")
                break
    return sorted(set(errors))


def main():
    errors = check_tree()
    if errors:
        print("Public-tree validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public-tree validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
