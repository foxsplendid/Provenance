#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the append-only progress-log primitive (sync-layer.md §3).

Safety-critical: every invariant in §2 gets a test. Stdlib unittest, no real data.

Run from repo root:
    python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance.synclayer import progress_log as PL  # noqa: E402

BEGIN = PL.BEGIN_MARKER
END = PL.END_MARKER

# A note with hand-authored content OUTSIDE the marker region, both before and
# after the (initially empty) progress-log pair.
HEADER = "# Synthetic XQ-17 Calibration\n\nHand-authored plan with **markdown** and 中文.\n\n"
FOOTER = "\n## Notes (hand-authored)\ntrailing human text 結尾\n"


def make_note(newline="\n", bom=False):
    """Bytes for a note with an empty marker pair surrounded by hand content."""
    text = HEADER + BEGIN + "\n" + END + "\n" + FOOTER
    if newline != "\n":
        text = text.replace("\n", newline)
    raw = text.encode("utf-8")
    return (PL.UTF8_BOM if bom else b"") + raw


def outside_bytes(raw):
    """(prefix, suffix) = the hand-authored bytes outside the insertion zone:
    everything before BEGIN, and from END to EOF. These must never change."""
    bm, em = BEGIN.encode("utf-8"), END.encode("utf-8")
    b = raw.find(bm)
    e = raw.find(em, b + len(bm))
    return raw[:b], raw[e:]


def order_of(text):
    """summary_ids in the order their entry markers appear (top -> bottom)."""
    ids, i = [], 0
    needle = "<!-- entry:"
    while True:
        j = text.find(needle, i)
        if j == -1:
            break
        k = text.find(" -->", j)
        ids.append(text[j + len(needle):k])
        i = k
    return ids


class ProgressLogTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.jdir = self.root / "sync-state" / "journal"
        self.note = self.root / "note.md"

    def write(self, raw):
        self.note.write_bytes(raw)

    def append(self, sid, block, **kw):
        kw.setdefault("journal_dir", self.jdir)
        return PL.append_entry(self.note, sid, block, **kw)

    def read_text(self):
        return self.note.read_bytes().decode("utf-8-sig")

    # 1. empty-region insert
    def test_insert_into_empty_region(self):
        self.write(make_note())
        r = self.append("s1", "ran tests, all green")
        self.assertEqual(r.status, PL.APPENDED)
        t = self.read_text()
        self.assertIn(PL.entry_marker("s1"), t)
        self.assertIn("ran tests, all green", t)
        # entry sits inside the marker region, after begin, before end
        self.assertLess(t.find(BEGIN), t.find(PL.entry_marker("s1")))
        self.assertLess(t.find(PL.entry_marker("s1")), t.find(END))

    # 2. multiple inserts: newest-first order, old blocks preserved
    def test_multiple_inserts_newest_first(self):
        self.write(make_note())
        self.append("s1", "first block")
        self.append("s2", "second block")
        self.append("s3", "third block")
        t = self.read_text()
        self.assertEqual(order_of(t), ["s3", "s2", "s1"])  # newest on top
        for body in ("first block", "second block", "third block"):
            self.assertIn(body, t)  # all preserved

    # 3. same summary_id twice -> one block (idempotent)
    def test_idempotent_same_id(self):
        self.write(make_note())
        r1 = self.append("dup", "body one")
        r2 = self.append("dup", "body two (should be ignored)")
        self.assertEqual(r1.status, PL.APPENDED)
        self.assertEqual(r2.status, PL.SKIPPED_DUPLICATE)
        t = self.read_text()
        self.assertEqual(order_of(t), ["dup"])             # exactly one block
        self.assertEqual(t.count(PL.entry_marker("dup")), 1)
        self.assertNotIn("body two", t)                    # second call wrote nothing
        self.assertIsNone(r2.journal)                      # no-op did not journal

    # 4. no marker pair -> refuse, write nothing
    def test_refuse_without_marker(self):
        raw = (HEADER + FOOTER).encode("utf-8")            # hand note, NO markers
        self.write(raw)
        r = self.append("s1", "should not be written")
        self.assertEqual(r.status, PL.REFUSED_NO_MARKER)
        self.assertEqual(self.note.read_bytes(), raw)      # byte-for-byte unchanged
        self.assertNotIn("should not be written", self.read_text())
        self.assertFalse(self.jdir.exists() and list(self.jdir.glob("*.json")),
                         "refused write must not journal")

    # 5. content OUTSIDE the region is byte-for-byte unchanged, including a human
    #    edit made between two inserts
    def test_outside_region_byte_identical(self):
        self.write(make_note())
        pre_prefix, pre_suffix = outside_bytes(self.note.read_bytes())
        self.append("s1", "auto block 1")
        p1, s1 = outside_bytes(self.note.read_bytes())
        self.assertEqual((p1, s1), (pre_prefix, pre_suffix))

        # simulate a human editing the hand-authored footer in Obsidian
        raw = self.note.read_bytes()
        edited = raw.replace(b"trailing human text", b"EDITED BY HUMAN text")
        self.note.write_bytes(edited)
        edited_prefix, edited_suffix = outside_bytes(edited)

        # a later auto-append must preserve that human edit verbatim
        self.append("s2", "auto block 2")
        p2, s2 = outside_bytes(self.note.read_bytes())
        self.assertEqual((p2, s2), (edited_prefix, edited_suffix))
        self.assertIn("EDITED BY HUMAN text", self.read_text())
        self.assertEqual(order_of(self.read_text()), ["s2", "s1"])

    # 6. BOM + CRLF file handled correctly
    def test_bom_and_crlf(self):
        self.write(make_note(newline="\r\n", bom=True))
        pre_prefix, pre_suffix = outside_bytes(self.note.read_bytes())
        r = self.append("s1", "win line one\nwin line two")
        self.assertEqual(r.status, PL.APPENDED)
        raw = self.note.read_bytes()
        self.assertTrue(raw.startswith(PL.UTF8_BOM))        # BOM preserved
        # inserted region uses CRLF, not a bare LF
        self.assertIn(b"<!-- entry:s1 -->\r\n", raw)
        self.assertIn(b"win line one\r\nwin line two\r\n", raw)
        body = raw[len(PL.UTF8_BOM):]
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))  # no lone LF anywhere
        self.assertEqual(outside_bytes(raw), (pre_prefix, pre_suffix))

    # 7. atomic write: a failing content write leaves no half-file, original intact
    def test_atomic_write_failure(self):
        self.write(make_note())
        original = self.note.read_bytes()
        real_replace = os.replace

        def boom(src, dst, *a, **k):
            if str(dst).endswith("note.md"):
                raise OSError("simulated disk failure")
            return real_replace(src, dst, *a, **k)

        PL.os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.append("s1", "never lands")
        finally:
            PL.os.replace = real_replace
        self.assertEqual(self.note.read_bytes(), original)  # untouched
        leftovers = list(self.note.parent.glob("*.tmp")) + list(self.note.parent.glob("note.md.*"))
        self.assertEqual(leftovers, [], "no half-written temp file left behind")
        # journal WAS written before the content write was attempted
        self.assertTrue(list(self.jdir.glob("*.json")), "pre-state must be journaled first")

    # 8a. journal-before-write + rollback restores an existing file's pre-state
    def test_rollback_existing(self):
        self.write(make_note())
        original = self.note.read_bytes()
        r = self.append("s1", "auto block")
        self.assertEqual(r.status, PL.APPENDED)
        self.assertNotEqual(self.note.read_bytes(), original)  # changed
        # journal recorded the pre-state before writing
        jdoc = json.loads(Path(r.journal).read_text(encoding="utf-8"))
        self.assertTrue(jdoc["existed"])
        self.assertEqual(jdoc["summary_id"], "s1")
        PL.rollback(r.journal)
        self.assertEqual(self.note.read_bytes(), original)     # restored byte-exact

    # 8b. rollback of a created note deletes it
    def test_rollback_created(self):
        fresh = self.root / "new_project.md"
        r = PL.append_entry(fresh, "s1", "first session", journal_dir=self.jdir,
                            create_if_missing=True)
        self.assertEqual(r.status, PL.CREATED)
        self.assertTrue(fresh.exists())
        t = fresh.read_text(encoding="utf-8")
        self.assertIn(BEGIN, t)
        self.assertIn(END, t)
        self.assertIn(PL.entry_marker("s1"), t)
        PL.rollback(r.journal)
        self.assertFalse(fresh.exists())                       # back to non-existence

    # 9. missing file without create_if_missing -> refuse
    def test_missing_file_refused(self):
        r = PL.append_entry(self.root / "absent.md", "s1", "x", journal_dir=self.jdir)
        self.assertEqual(r.status, PL.REFUSED_NO_FILE)
        self.assertFalse((self.root / "absent.md").exists())
        self.assertFalse(self.jdir.exists() and list(self.jdir.glob("*.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
