#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the surgical frontmatter field-updater (sync-layer.md §5).

`progress_log.update_frontmatter_fields` writes ONLY a named set of top-level keys
inside a note's leading `--- ... ---` block, preserving every other byte: the note
body, the progress-log marker region, all OTHER frontmatter keys, key order,
comments, BOM, and newline style. Same safety model as append_entry (journal
pre-state, atomic write, reversible).

Run from repo root:
    python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance import memory as M  # noqa: E402
from provenance.synclayer import progress_log as PL  # noqa: E402

BEGIN, END = PL.BEGIN_MARKER, PL.END_MARKER

# a note with frontmatter (status scalar, a flow-list next_actions, a CJK stage, a
# stray comment) + prose + a progress-log region + trailing prose.
NOTE = (
    "---\n"
    "schema_version: project/1.0\n"
    "project_id: synthetic-xq17-calibration\n"
    "title: Synthetic XQ-17 Calibration\n"
    "status: planned\n"
    "stage: 调研\n"
    "next_actions: [old one, old two]\n"
    "# a stray comment\n"
    "linked_repo: C:/ScriptoriumDemo/projects/synthetic-xq17-calibration\n"
    "---\n\n"
    "# Synthetic XQ-17 Calibration\n\nhand-authored plan 中文.\n\n"
    + BEGIN + "\n<!-- entry:x -->\n**2026-06-20** · session writeback\n- did a thing\n" + END + "\n"
    "\n## Notes (hand-authored)\ntrailing human text\n"
)


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.jdir = self.dir / "journal"

    def write_note(self, text=NOTE, *, bom=False, crlf=False, name="n.md"):
        if crlf:
            text = text.replace("\n", "\r\n")
        data = text.encode("utf-8")
        if bom:
            data = PL.UTF8_BOM + data
        p = self.dir / name
        p.write_bytes(data)
        return p

    def update(self, p, fields):
        return PL.update_frontmatter_fields(p, fields, journal_dir=str(self.jdir))

    # the byte span from BEGIN onward = body tail (progress-log region + trailing prose)
    def tail(self, raw):
        return raw[raw.find(BEGIN.encode("utf-8")):]

    # the byte span up to (and incl.) the `# Synthetic XQ-17 Calibration` heading = body head
    def body_head(self, raw):
        return raw[raw.find(b"# Synthetic XQ-17 Calibration"):raw.find(BEGIN.encode("utf-8"))]


class UpdateInPlaceTest(_Base):
    def test_replaces_named_scalar_only(self):
        p = self.write_note()
        before = p.read_bytes()
        r = self.update(p, {"status": "done"})
        self.assertIn(r.status, (PL.FM_UPDATED, PL.FM_ADDED))
        self.assertIn("status", r.changed)
        after = p.read_text(encoding="utf-8")
        self.assertIn("status: done", after)
        self.assertNotIn("status: planned", after)
        # every OTHER frontmatter key is byte-identical
        self.assertIn("schema_version: project/1.0", after)
        self.assertIn("stage: 调研", after)
        self.assertIn("next_actions: [old one, old two]", after)
        self.assertIn("# a stray comment", after)
        self.assertIn("linked_repo: C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", after)
        # body head + progress-log region + trailing prose untouched (byte-for-byte)
        out = p.read_bytes()
        self.assertEqual(self.body_head(out), self.body_head(before))
        self.assertEqual(self.tail(out), self.tail(before))

    def test_replaces_flow_list_in_place(self):
        p = self.write_note()
        self.update(p, {"next_actions": ["wire hook", "build queue"]})
        after = p.read_text(encoding="utf-8")
        # flow style preserved (single line), old items gone, others intact
        self.assertIn("next_actions: [wire hook, build queue]", after)
        self.assertNotIn("old one", after)
        self.assertIn("status: planned", after)        # unrelated key untouched
        fm, _ = M.parse_frontmatter(after)
        self.assertEqual(fm["next_actions"], ["wire hook", "build queue"])

    def test_replaces_block_list_in_place(self):
        text = NOTE.replace("next_actions: [old one, old two]\n",
                            "next_actions:\n  - old one\n  - old two\n")
        p = self.write_note(text)
        self.update(p, {"next_actions": ["new a", "new b", "new c"]})
        after = p.read_text(encoding="utf-8")
        # block style preserved (indented `- item` lines), 2 -> 3 items, next key intact
        self.assertIn("next_actions:\n  - new a\n  - new b\n  - new c\n", after)
        self.assertIn("linked_repo: C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", after)
        fm, _ = M.parse_frontmatter(after)
        self.assertEqual(fm["next_actions"], ["new a", "new b", "new c"])

    def test_adds_absent_key_before_closing_fence(self):
        p = self.write_note()
        before = p.read_bytes()
        r = self.update(p, {"blocked_by": "waiting on data", "updated": "2026-06-21"})
        self.assertEqual(r.status, PL.FM_ADDED)
        self.assertEqual(sorted(r.added), ["blocked_by", "updated"])
        after = p.read_text(encoding="utf-8")
        self.assertIn("blocked_by: waiting on data", after)
        self.assertIn("updated: 2026-06-21", after)
        fm, _ = M.parse_frontmatter(after)
        self.assertEqual(fm["blocked_by"], "waiting on data")
        self.assertEqual(fm["updated"], "2026-06-21")
        # body + progress-log region untouched
        out = p.read_bytes()
        self.assertEqual(self.tail(out), self.tail(before))

    def test_identical_value_is_a_noop(self):
        p = self.write_note()
        before = p.read_bytes()
        r = self.update(p, {"status": "planned"})   # already 'planned'
        self.assertEqual(r.status, PL.FM_NOOP)
        self.assertIsNone(r.journal)
        self.assertEqual(p.read_bytes(), before)     # byte-identical, nothing written

    def test_empty_fields_is_a_noop(self):
        p = self.write_note()
        before = p.read_bytes()
        r = self.update(p, {})
        self.assertEqual(r.status, PL.FM_NOOP)
        self.assertEqual(p.read_bytes(), before)


class PreservationTest(_Base):
    def test_no_frontmatter_is_skipped(self):
        p = self.dir / "nofm.md"
        raw = b"# just a body\nno frontmatter here\n"
        p.write_bytes(raw)
        r = PL.update_frontmatter_fields(p, {"status": "done"}, journal_dir=str(self.jdir))
        self.assertEqual(r.status, PL.FM_NO_FRONTMATTER)
        self.assertIsNone(r.journal)
        self.assertEqual(p.read_bytes(), raw)         # untouched

    def test_missing_file_is_skipped(self):
        r = PL.update_frontmatter_fields(self.dir / "absent.md", {"status": "done"},
                                         journal_dir=str(self.jdir))
        self.assertEqual(r.status, PL.FM_NO_FRONTMATTER)

    def test_bom_preserved(self):
        p = self.write_note(bom=True)
        self.update(p, {"status": "done"})
        self.assertTrue(p.read_bytes().startswith(PL.UTF8_BOM))
        fm, _ = M.parse_frontmatter(p.read_text(encoding="utf-8-sig"))
        self.assertEqual(fm["status"], "done")

    def test_crlf_preserved(self):
        p = self.write_note(crlf=True)
        self.update(p, {"status": "done", "updated": "2026-06-21"})
        raw = p.read_bytes()
        # every LF is part of a CRLF: no bare LF was introduced
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertIn(b"status: done\r\n", raw)

    def test_rollback_restores_byte_identical(self):
        p = self.write_note()
        before = p.read_bytes()
        r = self.update(p, {"status": "done", "next_actions": ["a", "b"], "updated": "2026-06-21"})
        self.assertNotEqual(p.read_bytes(), before)
        PL.rollback(r.journal)
        self.assertEqual(p.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
