#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for M5 — openclaw mobile-drop router (sync-layer.md §1/§6/§9).

Covers the least-trust ingest of the VM's quarantine inbox:

  - route_record: markdown/free-text wrapped into note/1.0; a note-envelope JSON's
    fields used; source="openclaw" + private=true + a host-minted note_id forced
    regardless of what the VM declares; secrets scrubbed;
  - ingest_inbox: writes a private note into the protected sync-state/notes/ layer,
    relocates each consumed drop to _openclaw-processed, never touches an authoritative
    store (least-trust); abuse caps (oversize/empty -> _rejected with reason; per-scan
    count throttled, not lost); idempotent re-delivery; quarantine budget pruning.

The host-side openclaw watcher daemon was retired (the pull model ingests the inbox
on demand via sync-run.ps1 -Mode once), so its debounce/dispatch tests are gone.

Run from repo root:
    python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance.ingest import openclaw as OC          # noqa: E402
from provenance.synclayer import paths                 # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.inbox = self.root / "_openclaw-inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)

    def drop(self, name, content):
        """Write a delivery into the inbox; bytes or str. Returns the Path."""
        p = self.inbox / name
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def notes(self):
        """All stored note records, keyed by note_id."""
        ndir = paths.notes_dir(self.root)
        out = {}
        if ndir.exists():
            for jf in ndir.glob("*.json"):
                out[jf.stem] = json.loads(jf.read_text(encoding="utf-8"))
        return out

    def processed(self):
        d = paths.openclaw_processed_dir(self.root)
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def rejected(self):
        d = paths.rejected_dir(self.root)
        if not d.is_dir():
            return {}
        return {p.name: p.with_name(p.name + ".reason").read_text(encoding="utf-8").strip()
                for p in d.iterdir() if not p.name.endswith(".reason")}


# --- route_record ------------------------------------------------------------

class RouteRecordTest(unittest.TestCase):
    def test_markdown_free_text_wrapped(self):
        note_id, rec = OC.route_record("idea.md", b"# Spark\n\nA mobile idea \xe4\xb8\xad\xe6\x96\x87.\n")
        self.assertEqual(rec["schema_version"], "note/1.0")
        self.assertEqual(rec["source"], "openclaw")
        self.assertTrue(rec["private"])
        self.assertEqual(rec["title"], "Spark")
        self.assertIn("A mobile idea", rec["body"])
        self.assertTrue(note_id.startswith("openclaw-"))

    def test_json_envelope_fields_used_source_private_id_forced(self):
        # a hostile envelope: claims a different source, private=false, and its own id
        payload = json.dumps({
            "schema_version": "note/1.0", "note_id": "vm-supplied-evil",
            "source": "agent", "private": False, "created": "2026-06-01T00:00:00Z",
            "title": "Captured", "tags": ["mobile", "idea"], "project": "synthetic-xq17-calibration",
            "body": "envelope body text",
        }).encode("utf-8")
        note_id, rec = OC.route_record("drop.json", payload)
        self.assertEqual(rec["source"], "openclaw")        # host-forced, not "agent"
        self.assertTrue(rec["private"])                     # host-forced, not False
        self.assertNotEqual(rec["note_id"], "vm-supplied-evil")
        self.assertTrue(rec["note_id"].startswith("openclaw-"))
        self.assertEqual(rec["created"], "2026-06-01T00:00:00Z")
        self.assertEqual(rec["title"], "Captured")
        self.assertEqual(rec["tags"], ["mobile", "idea"])
        self.assertEqual(rec["project"], "synthetic-xq17-calibration")
        self.assertEqual(rec["body"], "envelope body text")

    def test_non_envelope_json_is_free_text(self):
        # JSON without a note/1.x schema_version is NOT trusted as an envelope
        payload = json.dumps({"foo": "bar", "body": "should not be lifted"}).encode("utf-8")
        _id, rec = OC.route_record("x.json", payload)
        self.assertIn("should not be lifted", rec["body"])  # whole text is the body
        self.assertIn("foo", rec["body"])

    def test_secrets_scrubbed(self):
        token = "sk-ant-" + "A" * 24
        _id, rec = OC.route_record(
            "leak.md", f"token {token} and me@example.com\n".encode("utf-8"))
        self.assertNotIn("sk-ant-AAAA", rec["body"])
        self.assertIn("[REDACTED:api-key]", rec["body"])
        self.assertIn("[REDACTED:email]", rec["body"])

    def test_empty_payload_returns_none(self):
        self.assertIsNone(OC.route_record("empty.json", b""))
        self.assertIsNone(OC.route_record("blank.md", b"   \n  \n"))

    def test_note_id_is_stable_content_hash(self):
        a1, _ = OC.route_record("a.md", b"same bytes")
        a2, _ = OC.route_record("different-name.md", b"same bytes")
        b1, _ = OC.route_record("a.md", b"other bytes")
        self.assertEqual(a1, a2)                            # content, not filename
        self.assertNotEqual(a1, b1)


# --- ingest_inbox ------------------------------------------------------------

class IngestInboxTest(_Base):
    def test_md_ingested_into_protected_layer_and_archived(self):
        self.drop("n.md", "# Mobile\n\nZZZDROPZZZ a captured thought\n")
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual([r["state"] for r in res], ["ingested"])
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        rec = next(iter(notes.values()))
        self.assertEqual(rec["source"], "openclaw")
        self.assertTrue(rec["private"])
        self.assertIn("ZZZDROPZZZ", rec["body"])
        # protected layer, not inbox/
        ndir = paths.notes_dir(self.root)
        self.assertEqual(ndir.parent.name, "sync-state")
        self.assertFalse(str(ndir).startswith(str(self.root / "inbox")))
        # raw drop relocated out of the inbox into the processed backup
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertEqual(self.processed(), ["n.md"])

    def test_least_trust_no_authoritative_writes(self):
        self.drop("n.md", "anything")
        OC.ingest_inbox(self.inbox, root=self.root)
        # only the protected sync-state tree is written; no inbox/ memory/ project notes
        self.assertFalse((self.root / "inbox").exists())
        self.assertFalse((self.root / "memory").exists())
        written = sorted(p.name for p in (self.root / "sync-state").iterdir())
        self.assertEqual(written, ["_openclaw-processed", "notes"])

    def test_oversize_rejected(self):
        self.drop("big.md", b"x" * (OC.MAX_FILE_BYTES + 1))
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "rejected")
        self.assertEqual(res[0]["reason"], "oversize")
        self.assertEqual(self.notes(), {})
        rej = self.rejected()
        self.assertIn("big.md", rej)
        self.assertIn("oversize", rej["big.md"])

    def test_empty_drop_rejected(self):
        self.drop("empty.json", b"")
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "rejected")
        self.assertEqual(res[0]["reason"], "empty")
        self.assertIn("empty.json", self.rejected())

    def test_per_scan_count_cap_throttles_not_loses(self):
        orig = OC.MAX_PER_SCAN
        OC.MAX_PER_SCAN = 2
        self.addCleanup(setattr, OC, "MAX_PER_SCAN", orig)
        for i in range(5):
            self.drop(f"d{i}.md", f"drop number {i}")
        r1 = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(sum(1 for r in r1 if r["state"] == "ingested"), 2)
        self.assertEqual(sum(1 for r in r1 if r["state"] == "deferred"), 3)
        # deferred drops survive in the inbox and drain on the next passes
        OC.ingest_inbox(self.inbox, root=self.root)
        OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(len(self.notes()), 5)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_idempotent_redelivery_same_content(self):
        self.drop("first.md", "identical body")
        OC.ingest_inbox(self.inbox, root=self.root)
        self.drop("second.md", "identical body")            # different name, same bytes
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "duplicate")
        self.assertEqual(len(self.notes()), 1)              # one record, not a dup
        self.assertEqual(list(self.inbox.iterdir()), [])    # both drops archived

    def test_json_envelope_end_to_end(self):
        payload = json.dumps({
            "schema_version": "note/1.0", "source": "agent", "private": False,
            "body": "from the envelope", "title": "T",
        })
        self.drop("e.json", payload)
        OC.ingest_inbox(self.inbox, root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertEqual(rec["source"], "openclaw")
        self.assertTrue(rec["private"])
        self.assertEqual(rec["body"], "from the envelope")

    def test_budget_prune_bounds_dir(self):
        d = paths.rejected_dir(self.root)
        d.mkdir(parents=True, exist_ok=True)
        now = 1_000_000.0
        # 3 fresh + 1 very old; cap to 2 newest -> old dropped, 2 survive
        for i, age in enumerate([10, 20, 30, 40 * 86400]):
            p = d / f"r{i}.md"
            p.write_text("x", encoding="utf-8")
            p.with_name(p.name + ".reason").write_text("why", encoding="utf-8")
            import os
            os.utime(p, (now - age, now - age))
        OC._prune_budget(d, now=now, max_files=2, max_age_days=30)
        survivors = sorted(p.name for p in d.iterdir() if not p.name.endswith(".reason"))
        self.assertEqual(survivors, ["r0.md", "r1.md"])     # 2 newest within age
        # the .reason sidecar of a pruned file is gone too
        self.assertFalse((d / "r3.md.reason").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
