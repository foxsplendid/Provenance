# -*- coding: utf-8 -*-
"""Host-side event/sync layer (scriptorium-spec specs/sync-layer.md).

Currently ships the append-only progress-log primitive (§3) — the safety-critical
net-new piece that appends dated session-summary blocks into a project note's
frozen marker region without ever touching hand-authored content. Distinct from
the legacy one-shot pipeline driver `provenance/sync.py` (prov-sync).
"""
