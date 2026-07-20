#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the openclaw proposal -> approvable-draft bridge (sync-layer.md §5.1).

A structured `kind: proposal` openclaw drop must STAGE a session-summary/1.0 DRAFT
(approval_state=draft) under sync-state/drafts/ so it surfaces in the vault Approvals
queue — and never become a private note, never auto-apply, never invent a project.

Covers:
  - proposal envelope (markdown+YAML and the equivalent JSON) -> draft with the
    correct host-forced fields, AND approvals._pending_drafts / generate surface it;
  - a non-proposal drop still becomes a note (existing behaviour unchanged);
  - an unknown project is REJECTED (NOT applied, NOT a note that could leak to memory
    as an approved change);
  - an invalid `status` is omitted (not trusted), and a proposal with no high-value
    field falls back to the note route;
  - idempotent by content sha (re-drop = same draft id, overwrite-stable);
  - the LEAST-TRUST property: a proposal never reaches a project note / memory without
    passing through a draft + an explicit approve tick.

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

from provenance import memory as M                       # noqa: E402
from provenance.ingest import openclaw as OC             # noqa: E402
from provenance.synclayer import approvals as A, paths   # noqa: E402
from provenance.synclayer import progress_log as PL      # noqa: E402


def projects_doc():
    return {
        "ingested_at": "2026-06-21T00:00:00+00:00",
        "count": 1,
        "projects": {
            "synthetic-xq17-calibration": {
                "schema_version": "project/1.0",
                "project_id": "synthetic-xq17-calibration",
                "status": "planned",
                "linked_repo": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                "_source_file": "synthetic-xq17-calibration.md",
            }
        },
    }


# a project note with hand-authored frontmatter + prose OUTSIDE an empty marker pair
NOTE_HEAD = ("---\n"
             "schema_version: project/1.0\n"
             "project_id: synthetic-xq17-calibration\n"
             "status: planned\n"
             "---\n\n"
             "# Synthetic XQ-17 Calibration\n\nhand-authored plan.\n\n")
NOTE_TAIL = "\n## Notes (hand-authored)\ntrailing human text\n"

PROPOSAL_MD = (
    "---\n"
    "kind: proposal\n"
    "project: synthetic-xq17-calibration\n"
    "status: active\n"
    "stage: bridge wiring\n"
    "next_actions: [wire the bridge, run the suite]\n"
    "conclusion: proposal route in place\n"
    "confidence: high\n"
    "---\n"
    "Free-text rationale from the phone.\n"
)


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.inbox = self.root / "_openclaw-inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(projects_doc(), ensure_ascii=False), encoding="utf-8")
        self.vault = self.root / "vault"

    def drop(self, name, content):
        p = self.inbox / name
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def drafts(self):
        ddir = paths.drafts_dir(self.root)
        out = {}
        if ddir.exists():
            for jf in ddir.glob("*.json"):
                out[jf.stem] = json.loads(jf.read_text(encoding="utf-8"))
        return out

    def notes(self):
        ndir = paths.notes_dir(self.root)
        out = {}
        if ndir.exists():
            for jf in ndir.glob("*.json"):
                out[jf.stem] = json.loads(jf.read_text(encoding="utf-8"))
        return out

    def rejected(self):
        d = paths.rejected_dir(self.root)
        if not d.is_dir():
            return {}
        return {p.name: p.with_name(p.name + ".reason").read_text(encoding="utf-8").strip()
                for p in d.iterdir() if not p.name.endswith(".reason")}

    def make_note(self):
        pdir = self.vault / "Projects"
        pdir.mkdir(parents=True, exist_ok=True)
        note = pdir / "synthetic-xq17-calibration.md"
        note.write_bytes((NOTE_HEAD + PL.BEGIN_MARKER + "\n" + PL.END_MARKER
                          + "\n" + NOTE_TAIL).encode("utf-8"))
        return note


# --- proposal -> draft -------------------------------------------------------

class ProposalDraftTest(_Base):
    def test_markdown_proposal_becomes_draft_not_note(self):
        self.drop("p.md", PROPOSAL_MD)
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual([r["state"] for r in res], ["drafted"])
        self.assertEqual(self.notes(), {})                  # NOT a note
        drafts = self.drafts()
        self.assertEqual(len(drafts), 1)
        sid, d = next(iter(drafts.items()))
        self.assertTrue(sid.startswith("openclaw-"))
        self.assertEqual(d["schema_version"], "session-summary/1.0")
        self.assertEqual(d["approval_state"], "draft")      # human-gated, never applied
        self.assertEqual(d["project"], "synthetic-xq17-calibration")
        self.assertEqual(d["status"], "active")
        self.assertEqual(d["stage"], "bridge wiring")       # FREE-TEXT phase carried through
        self.assertEqual(d["next_actions"], ["wire the bridge", "run the suite"])
        self.assertEqual(d["conclusion"], "proposal route in place")
        self.assertEqual(d["confidence"], "high")
        self.assertEqual(d["_source"], "openclaw-proposal")
        self.assertNotIn("timeline", d)                     # NO auto timeline
        # raw drop relocated out of the inbox
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_json_proposal_object_equivalent(self):
        payload = json.dumps({
            "kind": "proposal", "project": "synthetic-xq17-calibration",
            "status": "paused", "blocked_by": "awaiting data",
            "rationale": "from the JSON drop",
        })
        self.drop("p.json", payload)
        OC.ingest_inbox(self.inbox, root=self.root)
        d = next(iter(self.drafts().values()))
        self.assertEqual(d["status"], "paused")
        self.assertEqual(d["blocked_by"], "awaiting data")
        self.assertEqual(d["project"], "synthetic-xq17-calibration")
        self.assertEqual(self.notes(), {})

    def test_draft_surfaces_in_pending_and_generate(self):
        self.drop("p.md", PROPOSAL_MD)
        OC.ingest_inbox(self.inbox, root=self.root)
        sid = next(iter(self.drafts()))
        # approvals picks it up as an approvable pending draft
        pending = A._pending_drafts(self.root)
        self.assertIn(sid, pending)
        # and `generate` renders it into the Approvals queue note
        A.regenerate(self.vault, root=self.root, run=True)
        t = A.approvals_path(self.vault).read_text(encoding="utf-8")
        self.assertIn(A.draft_marker(sid), t)
        self.assertIn("**synthetic-xq17-calibration**", t)
        self.assertIn("- status: active", t)
        self.assertIn("- stage: bridge wiring", t)          # free-text stage in the queue

    def test_host_forces_id_ignoring_vm_declared_id(self):
        payload = json.dumps({
            "kind": "proposal", "project": "synthetic-xq17-calibration",
            "summary_id": "vm-supplied-evil", "status": "done",
        })
        self.drop("p.json", payload)
        OC.ingest_inbox(self.inbox, root=self.root)
        sid = next(iter(self.drafts()))
        self.assertNotEqual(sid, "vm-supplied-evil")
        self.assertTrue(sid.startswith("openclaw-"))

    def test_secrets_scrubbed_in_draft(self):
        token = "sk-ant-" + "A" * 24
        md = ("---\nkind: proposal\nproject: synthetic-xq17-calibration\n"
              f"conclusion: token {token} leaked\n---\n")
        self.drop("p.md", md)
        OC.ingest_inbox(self.inbox, root=self.root)
        d = next(iter(self.drafts().values()))
        self.assertNotIn("sk-ant-AAAA", d["conclusion"])
        self.assertIn("[REDACTED:api-key]", d["conclusion"])


# --- non-proposal unchanged --------------------------------------------------

class NonProposalUnchangedTest(_Base):
    def test_plain_markdown_still_a_note(self):
        self.drop("idea.md", "# Spark\n\nA mobile idea, no kind field.\n")
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual([r["state"] for r in res], ["ingested"])
        self.assertEqual(self.drafts(), {})                 # NOT a draft
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        rec = next(iter(notes.values()))
        self.assertEqual(rec["schema_version"], "note/1.0")
        self.assertEqual(rec["source"], "openclaw")
        self.assertTrue(rec["private"])

    def test_note_envelope_json_still_a_note(self):
        payload = json.dumps({
            "schema_version": "note/1.0", "body": "from the envelope", "title": "T",
        })
        self.drop("e.json", payload)
        OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(self.drafts(), {})
        self.assertEqual(len(self.notes()), 1)

    def test_proposal_without_high_value_falls_back_to_note(self):
        # a proposal that proposes nothing approvable is not a draft -> note route
        md = ("---\nkind: proposal\nproject: synthetic-xq17-calibration\n---\n"
              "just a rationale, no status/next/conclusion/blocked.\n")
        self.drop("p.md", md)
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual([r["state"] for r in res], ["ingested"])
        self.assertEqual(self.drafts(), {})
        self.assertEqual(len(self.notes()), 1)


# --- rejection / validation --------------------------------------------------

class ProposalRejectTest(_Base):
    def test_unknown_project_rejected_not_applied(self):
        md = "---\nkind: proposal\nproject: ghost-project\nstatus: active\n---\n"
        self.drop("p.md", md)
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "rejected")
        self.assertEqual(res[0]["reason"], "unknown-project")
        # NOT applied: no draft, no note (cannot leak to memory as an approved change)
        self.assertEqual(self.drafts(), {})
        self.assertEqual(self.notes(), {})
        # quarantined with a reason
        rej = self.rejected()
        self.assertIn("p.md", rej)
        self.assertIn("unknown project", rej["p.md"])
        # never invents a project in memory
        self.assertNotIn("ghost-project", M.load_projects(self.root))

    def test_invalid_status_omitted(self):
        md = ("---\nkind: proposal\nproject: synthetic-xq17-calibration\n"
              "status: bogus\nconclusion: keep me\n---\n")
        self.drop("p.md", md)
        OC.ingest_inbox(self.inbox, root=self.root)
        d = next(iter(self.drafts().values()))
        self.assertNotIn("status", d)                       # invalid status dropped
        self.assertEqual(d["conclusion"], "keep me")        # other claim survives

    def test_freetext_stage_is_not_enum_rejected(self):
        # stage is FREE-TEXT: a value that is NOT a valid status enum must survive,
        # AND a stage-only proposal (no status/next/conclusion) is still approvable.
        md = ("---\nkind: proposal\nproject: synthetic-xq17-calibration\n"
              "stage: somewhere off-enum entirely\n---\n")
        self.drop("p.md", md)
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual([r["state"] for r in res], ["drafted"])  # NOT rejected, NOT a note
        d = next(iter(self.drafts().values()))
        self.assertEqual(d["stage"], "somewhere off-enum entirely")
        self.assertNotIn("status", d)

    def test_missing_project_rejected(self):
        md = "---\nkind: proposal\nstatus: active\n---\n"
        self.drop("p.md", md)
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "rejected")
        self.assertEqual(self.drafts(), {})
        self.assertEqual(self.notes(), {})

    def test_default_confidence_medium(self):
        md = "---\nkind: proposal\nproject: synthetic-xq17-calibration\nstatus: active\n---\n"
        self.drop("p.md", md)
        OC.ingest_inbox(self.inbox, root=self.root)
        d = next(iter(self.drafts().values()))
        self.assertEqual(d["confidence"], "medium")


# --- idempotency -------------------------------------------------------------

class ProposalIdempotentTest(_Base):
    def test_redrop_same_content_is_duplicate(self):
        self.drop("first.md", PROPOSAL_MD)
        OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(len(self.drafts()), 1)
        self.drop("second.md", PROPOSAL_MD)                 # different name, same bytes
        res = OC.ingest_inbox(self.inbox, root=self.root)
        self.assertEqual(res[0]["state"], "duplicate")
        self.assertEqual(len(self.drafts()), 1)             # one draft, not a dup
        self.assertEqual(list(self.inbox.iterdir()), [])    # both drops archived


# --- least-trust end-to-end --------------------------------------------------

class LeastTrustTest(_Base):
    def test_proposal_never_reaches_note_or_memory_without_approval(self):
        note = self.make_note()
        note_before = note.read_bytes()
        self.drop("p.md", PROPOSAL_MD)
        OC.ingest_inbox(self.inbox, root=self.root)
        sid = next(iter(self.drafts()))
        # BEFORE approval: project note untouched, memory has no applied claim
        self.assertEqual(note.read_bytes(), note_before)
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "planned")
        self.assertFalse((self.root / "memory" / M.APPLIED_CLAIMS).exists())
        # the human surfaces + ticks the queue (the ONLY path in)
        A.regenerate(self.vault, root=self.root, run=True)
        t = A.approvals_path(self.vault).read_text(encoding="utf-8")
        t = t.replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        A.approvals_path(self.vault).write_text(t, encoding="utf-8")
        plan = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan["count"], 1)
        # AFTER the explicit tick: claim is applied via the existing M1 path
        note_text = note.read_text(encoding="utf-8")
        self.assertIn(PL.entry_marker(sid + "#approved"), note_text)
        self.assertIn("- stage: bridge wiring", note_text)   # free-text stage in the note log
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "active")
        # the free-text stage rides the SAME applied_claims overlay path as status
        self.assertEqual(M.load_applied_claims(self.root)["synthetic-xq17-calibration"]["stage"],
                         "bridge wiring")
        d = self.drafts()[sid]
        self.assertEqual(d["approval_state"], "applied")
        self.assertEqual(d["applied_claims"]["stage"], "bridge wiring")


if __name__ == "__main__":
    unittest.main(verbosity=2)
