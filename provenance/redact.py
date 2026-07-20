#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic secret/PII redaction for ingested conversation text.

Applied when writing inbox JSON so secrets/PII never persist in the archive
(coding-agent logs in particular can contain API keys, OAuth codes, tokens,
proxy configs, emails, IPs, and home paths). Conservative, high-confidence
patterns only. Operates on the parsed object's string VALUES (see redact_json),
not the serialized text, so JSON escape sequences (\\n, \\t) are never corrupted.

Caveat: pattern-based, so it can over-redact (e.g. academic emails, 4-octet
version-like numbers) or miss exotic secret formats. Tune _RULES as needed.
"""
import json
import re
from pathlib import Path

# (name, regex, replacement). Specific patterns first.
_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED:private-key]"),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:api-key]"),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "[REDACTED:api-key]"),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED:api-key]"),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-key]"),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:token]"),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:token]"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED:jwt]"),
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{10,}"), r"\1 [REDACTED:token]"),
    # key:value / key=value secrets (incl OAuth code, AWS secret, password). Keeps the
    # key name, redacts the value; tolerates spaces and quotes around the separator.
    ("secret-kv", re.compile(r"(?i)\b(aws_secret_access_key|secret_access_key|client_secret|access_token|refresh_token|api[_-]?key|secret|password|passwd|token|code_challenge|code)\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-/+]{6,})"), r"\1=[REDACTED]"),
    ("url-cred", re.compile(r"://[^/:@\s]+:[^/@\s]+@"), "://[REDACTED:creds]@"),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED:email]"),
    # IPv4 but not version numbers: reject if bordered by a word char or dot (so
    # v1.2.3.4 / 1.2.3.4.5 are left alone).
    ("ipv4", re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])"), "[REDACTED:ip]"),
    ("posixhome", re.compile(r"(/home/)[^/\\\"\s]+"), r"\1<user>"),
    ("winuser", re.compile(r"(Users(?:[\\/])+)[^\\/\"\s]+"), r"\1<user>"),
]


def redact(text):
    if not text:
        return text
    for _name, rx, repl in _RULES:
        text = rx.sub(repl, text)
    return text


def redact_json(obj):
    """Recursively redact string VALUES of a parsed JSON object, then the caller
    serializes. Operating on values (not the serialized text) avoids corrupting
    JSON escape sequences like \\n / \\t (a text-level pass could eat the escape
    letter and orphan the backslash)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    return obj


# --- Pseudonymization (privacy layer) -------------------------------------
# Replaces real identities (names / institution / third parties) with stable
# codes, preserving semantics. The alias map (aliases.json) is the re-id key —
# keep it local & git-ignored (see docs/PRIVACY.md).
_ALIAS_PATH = Path(__file__).resolve().parent / "aliases.json"
_alias_cache = None


def load_aliases(path=None):
    p = Path(path) if path else _ALIAS_PATH
    if not p.exists():
        return {}  # absent is expected (pseudonymization optional)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("aliases", {})
    except Exception as e:
        # fail LOUD, not open: a corrupt alias map must not silently disable
        # pseudonymization and let real identities pass into the "safe" inbox.
        import sys
        print(f"WARNING: alias map {p} is unreadable ({e}); pseudonymization is "
              f"DISABLED this run — real identities may reach inbox. Fix/restore it.",
              file=sys.stderr)
        return {}


def pseudonymize(text, amap):
    if not text or not amap:
        return text
    for real in sorted(amap, key=len, reverse=True):  # longest first
        code = amap[real]
        if real.isascii():
            # ASCII alias: bound by non-[A-Za-z0-9_] on both sides. Python's \b is
            # Unicode-aware (treats CJK as word chars), so '我是Tom的' would NOT match;
            # explicit ASCII-class lookarounds fix CJK-adjacent / multiword aliases.
            text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(real) + r"(?![A-Za-z0-9_])", code, text)
        else:
            text = text.replace(real, code)
    return text


def sanitize_json(obj, amap=None):
    """Privacy-layer transform applied at ingest: secret/PII redaction +
    pseudonymization, on string VALUES of a parsed JSON object."""
    global _alias_cache
    if amap is None:
        if _alias_cache is None:
            _alias_cache = load_aliases()
        amap = _alias_cache

    def _walk(o):
        if isinstance(o, str):
            return pseudonymize(redact(o), amap)
        if isinstance(o, list):
            return [_walk(x) for x in o]
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        return o

    return _walk(obj)
