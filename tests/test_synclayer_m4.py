#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for M4 — Obsidian front-door note ingest (sync-layer.md §1/§6).

Covers the note/1.0 ingester (provenance.ingest.notes):

  - note ingest: writes a `note/1.0` record, forced `private=true`, stored in the
    protected `sync-state/notes/` layer (outside inbox/), secret/PII scrubbed,
    stable id per path, incremental content-hash skip, read-only on the vault;
  - profile exclusion: the profile prep pipeline (reads inbox/ only) never sees a
    private note.

The host-side vault watcher daemon was retired (the pull model ingests on demand
via sync-run.ps1 -Mode once), so its dispatch/debounce tests are gone.

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

from provenance.ingest import notes as N  # noqa: E402
from provenance.synclayer import paths  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.vault = self.root / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)

    def vwrite(self, rel, text):
        """Write a file at <vault>/<rel> (creating parents); return the Path."""
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def notes(self):
        """All stored note records, keyed by note_id."""
        ndir = paths.notes_dir(self.root)
        out = {}
        if ndir.exists():
            for jf in ndir.glob("*.json"):
                out[jf.stem] = json.loads(jf.read_text(encoding="utf-8"))
        return out


# --- note/1.0 ingester -------------------------------------------------------

class NotesIngestTest(_Base):
    def test_ingests_note_v1_private_in_protected_layer(self):
        self.vwrite("Inbox/idea.md", "# Spark\n\nA free-text idea 中文.\n")
        res = N.ingest_zone(self.vault, "Inbox", root=self.root)
        self.assertEqual([r["state"] for r in res], ["ingested"])
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        rec = next(iter(notes.values()))
        self.assertEqual(rec["schema_version"], "note/1.0")
        self.assertEqual(rec["source"], "obsidian")
        self.assertTrue(rec["private"])                       # personal zone -> always private
        self.assertEqual(rec["origin_path"], "Inbox/idea.md")
        self.assertIn("free-text idea", rec["body"])
        self.assertEqual(rec["title"], "Spark")
        # stored in the protected layer, NOT in inbox/
        ndir = paths.notes_dir(self.root)
        self.assertEqual(ndir.parent.name, "sync-state")
        self.assertFalse(str(ndir).startswith(str(self.root / "inbox")))

    def test_stable_note_id_from_path(self):
        self.vwrite("_planning/plan.md", "rough plan")
        N.ingest_zone(self.vault, "_planning", root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertEqual(rec["note_id"], N.note_id_for("_planning/plan.md"))
        self.assertTrue(rec["note_id"].startswith("obsidian-"))

    def test_frontmatter_title_tags_project(self):
        self.vwrite("Inbox/n.md",
                    "---\ntitle: My Note\nproject: synthetic-xq17-calibration\ntags: [idea, draft]\n"
                    "created: 2026-06-01T00:00:00Z\n---\n\nbody here\n")
        N.ingest_zone(self.vault, "Inbox", root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertEqual(rec["title"], "My Note")
        self.assertEqual(rec["project"], "synthetic-xq17-calibration")
        self.assertEqual(rec["tags"], ["idea", "draft"])
        self.assertEqual(rec["created"], "2026-06-01T00:00:00Z")
        self.assertEqual(rec["body"], "body here")

    def test_secrets_are_scrubbed(self):
        token = "sk-ant-" + "A" * 24
        self.vwrite("Inbox/leak.md", f"token here {token} and me@example.com\n")
        N.ingest_zone(self.vault, "Inbox", root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertNotIn("sk-ant-AAAA", rec["body"])
        self.assertIn("[REDACTED:api-key]", rec["body"])
        self.assertIn("[REDACTED:email]", rec["body"])

    def test_incremental_skip_unchanged(self):
        self.vwrite("Inbox/n.md", "stable content")
        r1 = N.ingest_zone(self.vault, "Inbox", root=self.root)
        self.assertEqual(r1[0]["state"], "ingested")
        out = paths.notes_dir(self.root) / f"{r1[0]['note_id']}.json"
        before = out.read_bytes()
        r2 = N.ingest_zone(self.vault, "Inbox", root=self.root)
        self.assertEqual(r2[0]["state"], "skipped")
        self.assertEqual(out.read_bytes(), before)            # untouched (idempotent)

    def test_update_on_change_same_id(self):
        p = self.vwrite("Inbox/n.md", "first version")
        r1 = N.ingest_zone(self.vault, "Inbox", root=self.root)
        p.write_text("second version", encoding="utf-8")
        r2 = N.ingest_zone(self.vault, "Inbox", root=self.root)
        self.assertEqual(r2[0]["state"], "updated")
        self.assertEqual(r1[0]["note_id"], r2[0]["note_id"])  # same record, not a dup
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("second version", next(iter(self.notes().values()))["body"])

    def test_read_only_vault(self):
        p = self.vwrite("Inbox/n.md", "# Keep me byte-identical\n\ntext\n")
        before = p.read_bytes()
        N.ingest_zone(self.vault, "Inbox", root=self.root)
        self.assertEqual(p.read_bytes(), before)              # vault never written

    def test_skips_dotfiles_and_tmp(self):
        self.vwrite("Inbox/real.md", "keep")
        self.vwrite("Inbox/.scratch.md", "hidden")
        self.vwrite("Inbox/half.md.tmp", "half-saved")
        self.vwrite("Inbox/.obsidian/workspace.md", "obsidian internal")
        res = N.ingest_zone(self.vault, "Inbox", root=self.root)
        paths_seen = {r["origin_path"] for r in res}
        self.assertEqual(paths_seen, {"Inbox/real.md"})
        self.assertEqual(len(self.notes()), 1)

    def test_default_zones_via_ingest(self):
        self.vwrite("Inbox/a.md", "inbox note")
        self.vwrite("_planning/b.md", "planning note")
        res = N.ingest(self.vault, ["Inbox", "_planning"], root=self.root)
        self.assertEqual(sorted(r["origin_path"] for r in res),
                         ["Inbox/a.md", "_planning/b.md"])

    def test_created_is_never_empty(self):
        # no frontmatter `created` -> falls back to the file mtime, never "" (required ISO)
        self.vwrite("Inbox/n.md", "no frontmatter here")
        N.ingest_zone(self.vault, "Inbox", root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertTrue(rec["created"])
        self.assertIn("T", rec["created"])

    def test_machine_keys_are_raw_not_sanitized(self):
        # the dedup key + filename are the raw values, independent of the redaction ruleset
        import hashlib
        p = self.vwrite("Inbox/n.md", "body text")
        N.ingest_zone(self.vault, "Inbox", root=self.root)
        rec = next(iter(self.notes().values()))
        self.assertEqual(rec["_content_sha"], hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertEqual(rec["note_id"], N.note_id_for("Inbox/n.md"))

    def test_half_saved_file_is_skipped(self):
        # a file whose signature changes across the read (Obsidian half-save) is not ingested
        self.vwrite("Inbox/n.md", "content")
        calls = []

        def fake_sig(md):
            calls.append(1)
            return (len(calls), float(len(calls)))   # differs each call -> s0 != s1

        orig = N._sig
        N._sig = fake_sig
        try:
            res = N.ingest_zone(self.vault, "Inbox", root=self.root)
        finally:
            N._sig = orig
        self.assertEqual(res[0]["state"], "unstable")
        self.assertEqual(self.notes(), {})           # nothing written for a half-saved file


# --- profile exclusion -------------------------------------------------------

class ProfileExclusionTest(_Base):
    def test_private_note_never_reaches_profile_prep(self):
        # one real inbox conversation (the profile pipeline reads inbox/*/*.json)
        conv = {
            "source": "claude-code", "conversation_id": "claude-code-x", "name": "a chat",
            "created_at": "2026-06-20T10:00:00Z", "updated_at": "2026-06-20T11:00:00Z",
            "chat_messages": [{"sender": "human", "text": "ZZZCONVMARKERZZZ please help",
                               "created_at": "t1"}],
        }
        cdir = self.root / "inbox" / "claude-code"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "x.json").write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")
        # a private note carrying a distinct marker
        self.vwrite("Inbox/secret.md", "ZZZNOTEMARKERZZZ deeply personal\n")
        N.ingest_zone(self.vault, "Inbox", root=self.root)

        # run the profile prep over inbox/ -> _prep digests
        import provenance.organize.prep as prep
        out = self.root / "_prep"
        old_argv = sys.argv
        sys.argv = ["prov-prep", str(self.root / "inbox"), str(out)]
        try:
            prep.main()
        finally:
            sys.argv = old_argv

        digests = "\n".join(f.read_text(encoding="utf-8") for f in out.glob("*.md"))
        self.assertIn("ZZZCONVMARKERZZZ", digests)            # conversation IS profiled
        self.assertNotIn("ZZZNOTEMARKERZZZ", digests)         # private note is NOT
        # and the note never landed under inbox/
        inbox_blob = "\n".join(f.read_text(encoding="utf-8")
                               for f in (self.root / "inbox").rglob("*.json"))
        self.assertNotIn("ZZZNOTEMARKERZZZ", inbox_blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
