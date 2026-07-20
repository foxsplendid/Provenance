#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for safe pending-scaffold inspection (summarize_driver.py).

The headless `claude -p` summarizer was retired: the per-session fill.json is now
produced IN-SESSION and submitted through the public safe writer. This module is
read-only: it lists safe IDs and returns one allowlisted scaffold without exposing a
protected path. These tests cover discovery, idempotent skip after fill, and invalid
pending entries.

Run from repo root:
    python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance.synclayer import paths, enqueue, worker  # noqa: E402
from provenance.synclayer import summarize_driver as drv  # noqa: E402


def projects_doc():
    return {
        "ingested_at": "2026-06-20T00:00:00+00:00", "count": 1,
        "projects": {"synthetic-xq17-calibration": {
            "schema_version": "project/1.0", "project_id": "synthetic-xq17-calibration",
            "linked_repo": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
            "_source_file": "synthetic-xq17-calibration.md"}},
    }


def inbox_record(session_id, name="A session"):
    return {
        "source": "claude-code", "conversation_id": f"claude-code-{session_id}",
        "name": name, "created_at": "2026-06-20T10:00:00.000Z",
        "updated_at": "2026-06-20T11:00:00.000Z",
        "project": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",  # absolute cwd -> must be dropped
        "chat_messages": [
            {"sender": "human", "text": "add the synthetic calibration figure", "created_at": "t1"},
            {"sender": "assistant", "text": "done, regenerated fig 3", "created_at": "t2"}],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(projects_doc(), ensure_ascii=False), encoding="utf-8")

    def make_event(self, sid="11111111-2222-3333-4444-555555555555",
                   cwd="C:/ScriptoriumDemo/projects/synthetic-xq17-calibration"):
        tp = f"C:/ScriptoriumDemo/agent-sessions/{sid}.jsonl"
        return {"session_id": sid, "transcript_path": tp, "cwd": cwd,
                "hook_event_name": "SessionEnd", "reason": "exit",
                "enqueued_at": "2026-06-20T11:00:01+00:00"}

    def write_inbox(self, event):
        stem = worker._stem_for(event)
        d = self.root / "inbox" / "claude-code"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.json").write_text(
            json.dumps(inbox_record(event["session_id"]), ensure_ascii=False), encoding="utf-8")

    def scaffold_one(self, ev=None):
        """Drive the worker to phase A so a real scaffold (no fill) exists."""
        ev = ev or self.make_event()
        self.write_inbox(ev)
        enqueue.enqueue(ev, root=self.root)
        worker.drain(root=self.root, run=True, ingest_fn=lambda r: None)
        return ev, paths.pending_dir(self.root) / worker.summary_id_for(ev)


# --- discovery ---------------------------------------------------------------

class FindPendingTest(_Base):
    def test_discovers_scaffold_without_fill(self):
        _ev, pdir = self.scaffold_one()
        self.assertEqual(drv.find_pending(self.root), [pdir])

    def test_skips_when_fill_present(self):
        _ev, pdir = self.scaffold_one()
        (pdir / "fill.json").write_text("{}", encoding="utf-8")
        self.assertEqual(drv.find_pending(self.root), [])

    def test_skips_dir_without_scaffold(self):
        d = paths.pending_dir(self.root) / "stray"
        d.mkdir(parents=True)
        self.assertEqual(drv.find_pending(self.root), [])

    def test_empty_when_no_pending(self):
        self.assertEqual(drv.find_pending(self.root), [])

    def test_lists_only_does_not_write_fill(self):
        # Inspection never produces a fill; prov-sync-fill owns that write boundary.
        _ev, pdir = self.scaffold_one()
        self.assertEqual(drv.find_pending(self.root), [pdir])
        self.assertFalse((pdir / "fill.json").exists())

    def test_skips_legacy_unresolved_scaffold(self):
        pending = paths.pending_dir(self.root) / "legacy-unresolved"
        pending.mkdir(parents=True)
        (pending / "scaffold.json").write_text(
            json.dumps(
                {
                    "schema_version": "summary-scaffold/1.0",
                    "summary_id": "legacy-unresolved",
                    "project": None,
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(drv.find_pending(self.root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
