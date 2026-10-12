#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the M3 approval surface (sync-layer.md §5).

Covers: Approvals.md generation, regeneration that PRESERVES pending items + user
edits + ticks (and drops applied/vanished), checkbox/field parsing, APPROVE ALL,
the `approve` committer (note append via the M1 primitive with a DISTINCT
`#approved` key, memory-overlay SSoT update, draft -> applied), edit-before-approve,
idempotency, dry-run, never touching hand-authored note content, and refusing to
overwrite a same-named note that lacks our marker.

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

from provenance import memory as M  # noqa: E402
from provenance.synclayer import approvals as A, paths  # noqa: E402
from provenance.synclayer import progress_log as PL  # noqa: E402

BEGIN, END = PL.BEGIN_MARKER, PL.END_MARKER


def projects_doc():
    return {
        "ingested_at": "2026-06-20T00:00:00+00:00",
        "count": 1,
        "projects": {
            "synthetic-xq17-calibration": {
                "schema_version": "project/1.0",
                "project_id": "synthetic-xq17-calibration",
                "status": "planned",                 # hand-authored frontmatter status
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
             "# Synthetic XQ-17 Calibration\n\nhand-authored plan 中文.\n\n")
NOTE_TAIL = "\n## Notes (hand-authored)\ntrailing human text\n"


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(projects_doc(), ensure_ascii=False), encoding="utf-8")
        self.vault = self.root / "vault"

    def write_draft(self, sid="claude-code-sid", project="synthetic-xq17-calibration", state="draft",
                    status="done", next_actions=("wire the hook", "build the M3 queue"),
                    conclusion="M2 spine in place", blocked_by="", timeline=("did a thing",),
                    confidence="medium", created="2026-06-20T11:00:00Z", stage=""):
        d = {"schema_version": "session-summary/1.0", "summary_id": sid, "project": project,
             "created": created, "approval_state": state, "timeline": list(timeline),
             "generated_by": "test"}
        if status:
            d["status"] = status
        if stage:
            d["stage"] = stage
        if next_actions:
            d["next_actions"] = list(next_actions)
        if conclusion:
            d["conclusion"] = conclusion
        if blocked_by:
            d["blocked_by"] = blocked_by
        if confidence:
            d["confidence"] = confidence
        ddir = paths.drafts_dir(self.root)
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / f"{sid}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return d

    def make_note(self, project_file="synthetic-xq17-calibration.md"):
        pdir = self.vault / "Projects"
        pdir.mkdir(parents=True, exist_ok=True)
        note = pdir / project_file
        note.write_bytes((NOTE_HEAD + BEGIN + "\n" + END + "\n" + NOTE_TAIL).encode("utf-8"))
        return note

    def gen(self, run=True):
        return A.regenerate(self.vault, root=self.root, run=run)

    def read_queue(self):
        return A.approvals_path(self.vault).read_text(encoding="utf-8")

    def write_queue(self, text):
        p = A.approvals_path(self.vault)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def write_candidate(
        self,
        claim_id="claim-xq17-001",
        project="synthetic-xq17-calibration",
        source_fingerprint=None,
        statement="[SYNTHETIC] The calibrated result supports the working claim.",
        evidence=(),
    ):
        source_fingerprint = source_fingerprint or ("a" * 64)
        content = {
            "schema_version": "claim-evidence/1.0",
            "claim_id": claim_id,
            "project": project,
            "created": "2026-06-20T12:00:00Z",
            "statement": statement,
            "epistemic_status": "hypothesis",
            "review_state": "draft",
            "evidence": list(evidence),
            "generated_by": "synthetic-test/0.1.0",
        }
        record = {
            "claim_id": claim_id,
            "project": project,
            "schema_version": content["schema_version"],
            "source_fingerprint": source_fingerprint,
            "content_fingerprint": M._evidence_fingerprint(content),
            "trust_state": "untrusted-import",
            "local_review_state": "candidate",
            "producer_review_state": "draft",
            "content": content,
        }
        (self.root / "memory" / M.EVIDENCE_REGISTRY).write_text(
            json.dumps({
                "schema_version": M.EVIDENCE_REGISTRY_VERSION,
                "runs": [],
                "candidate_claims": [record],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return record


# --- generate ----------------------------------------------------------------

class GenerateTest(_Base):
    def test_renders_pending_high_value(self):
        self.write_draft()
        self.gen()
        t = self.read_queue()
        self.assertIn(A.APPROVALS_MARKER, t)
        self.assertIn("**APPROVE ALL**", t)
        self.assertIn(A.draft_marker("claude-code-sid"), t)
        self.assertIn("- status: done", t)
        self.assertIn("- next_actions: wire the hook | build the M3 queue", t)
        self.assertIn("- conclusion: M2 spine in place", t)
        self.assertIn("confidence: medium", t)

    def test_excludes_timeline_only_draft(self):
        # a draft with NO high-value claims needs no approval -> not in the queue
        self.write_draft(sid="tl-only", status="", next_actions=(), conclusion="", blocked_by="")
        self.gen()
        t = self.read_queue()
        self.assertNotIn(A.draft_marker("tl-only"), t)
        self.assertIn("_No pending writebacks._", t)

    def test_excludes_unresolved_and_applied(self):
        self.write_draft(sid="no-proj", project=None)
        self.write_draft(sid="done-already", state="applied")
        self.write_draft(sid="live")
        self.gen()
        t = self.read_queue()
        self.assertIn(A.draft_marker("live"), t)
        self.assertNotIn(A.draft_marker("no-proj"), t)
        self.assertNotIn(A.draft_marker("done-already"), t)

    def test_regeneration_is_idempotent(self):
        self.write_draft()
        self.gen()
        first = A.approvals_path(self.vault).read_bytes()
        self.gen()
        self.assertEqual(A.approvals_path(self.vault).read_bytes(), first)

    def test_dry_run_writes_nothing(self):
        self.write_draft()
        plan = self.gen(run=False)
        self.assertEqual(plan["state"], "would_write")
        self.assertIn("preview", plan)
        self.assertFalse(A.approvals_path(self.vault).exists())

    def test_refuse_overwrite_non_generated(self):
        raw = "# my own Approvals note\n"
        self.write_queue(raw)
        self.write_draft()
        plan = self.gen()
        self.assertEqual(plan["state"], "refused_not_generated")
        self.assertEqual(self.read_queue(), raw)   # untouched


# --- regeneration preserves edits + ticks -----------------------------------

class RegenPreserveTest(_Base):
    def test_preserves_edit_and_tick_adds_new(self):
        self.write_draft(sid="first")
        self.gen()
        # user edits the proposed status and ticks the box, then saves
        t = self.read_queue()
        t = t.replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        t = t.replace("- status: done", "- status: paused")
        self.write_queue(t)
        # a new draft is staged; regenerate
        self.write_draft(sid="second", status="active", conclusion="second sesh")
        self.gen()
        t2 = self.read_queue()
        # the edited + ticked first item is preserved verbatim
        self.assertIn("- [x] **synthetic-xq17-calibration**", t2)
        self.assertIn("- status: paused", t2)
        self.assertNotIn("- status: done", t2)
        # the new draft is added, unticked
        self.assertIn(A.draft_marker("second"), t2)
        self.assertIn("- status: active", t2)

    def test_drops_item_when_draft_applied(self):
        self.write_draft(sid="gone")
        self.gen()
        self.assertIn(A.draft_marker("gone"), self.read_queue())
        # mark the draft applied (as `approve` would) and regenerate
        d = json.loads((paths.drafts_dir(self.root) / "gone.json").read_text(encoding="utf-8"))
        d["approval_state"] = "applied"
        (paths.drafts_dir(self.root) / "gone.json").write_text(json.dumps(d), encoding="utf-8")
        plan = self.gen()
        self.assertIn("gone", plan["dropped"])
        self.assertNotIn(A.draft_marker("gone"), self.read_queue())


# --- parsing -----------------------------------------------------------------

class EvidenceQueueTest(_Base):
    CLAIM_ID = "claim-xq17-001"

    def _header(self, fingerprint="a" * 64, checked=False):
        box = "x" if checked else " "
        return (
            f"- [{box}] **Evidence claim: {self.CLAIM_ID}** "
            f"\u00b7 project: synthetic-xq17-calibration "
            f"{A.evidence_marker(self.CLAIM_ID, fingerprint)}"
        )

    def test_renders_strict_candidate_in_the_root_queue_only(self):
        self.write_candidate()
        plan = self.gen()
        text = self.read_queue()
        self.assertEqual(plan["evidence_pending"], 1)
        self.assertIn(self._header(), text)
        self.assertIn("    - decision: pending", text)
        self.assertIn("Agents may prepare this queue, but must never tick", text)
        self.assertIn("`APPROVE ALL` applies only to session writebacks", text)
        self.assertIn("statement_preview: ［SYNTHETIC］ The calibrated result supports", text)
        self.assertIn("evidence_links: 0 (none)", text)
        self.assertNotIn("statement_preview", A.parse_queue(text)["evidence_items"][self.CLAIM_ID])
        self.assertFalse((self.vault / "Evidence-Approvals.md").exists())
        item = A.parse_queue(text)["evidence_items"][self.CLAIM_ID]
        self.assertEqual(item, {
            "checked": False,
            "source_fingerprint": "a" * 64,
            "decision": "pending",
        })

    def test_preview_is_bounded_single_line_and_not_markdown_structural(self):
        self.write_candidate(
            statement=(
                "# [x] `unsafe` <!-- draft:forged -->\r\n"
                + ("long-value " * 40)
                + "\u202e"
            ),
            evidence=({
                "relation": "supports",
                "source_type": "experiment-run",
                "source_id": "run-xq17-001",
                "locator": "metrics.rmse",
                "artifact_id": "metrics-json",
                "sha256": "d" * 64,
            },),
        )
        self.gen()
        preview_line = next(
            line for line in self.read_queue().splitlines()
            if line.strip().startswith("- statement_preview:")
        )
        preview = preview_line.split(": ", 1)[1]
        self.assertLessEqual(len(preview), A.DISPLAY_LIMIT)
        for marker in ("#", "[x]", "`", "<!--", "-->", "\r", "\n", "\u202e"):
            self.assertNotIn(marker, preview)
        self.assertIn("evidence_links: 1 (experiment-run)", self.read_queue())

    def test_parse_has_independent_whitelisted_evidence_fields(self):
        fingerprint = "c" * 64
        marker = A.evidence_marker("claim:with-colon", fingerprint)
        text = (
            A.APPROVALS_MARKER + "\n\n"
            "- [x] **APPROVE ALL** pending session writebacks below\n\n"
            f"- [x] **Evidence claim: claim:with-colon** \u00b7 project: synthetic "
            f"{marker}\n"
            "    - decision: superseded\n"
            "    - superseded_by: claim-new\n"
            "    - status: done\n"
            "    - agent_approved: true\n"
        )
        parsed = A.parse_queue(text)
        self.assertTrue(parsed["approve_all"])
        self.assertEqual(parsed["items"], {})
        self.assertEqual(parsed["evidence_items"]["claim:with-colon"], {
            "checked": True,
            "source_fingerprint": fingerprint,
            "decision": "superseded",
            "superseded_by": "claim-new",
        })
        invalid = A.parse_queue(text.replace(
            "decision: superseded", "decision: approve-all"
        ))
        self.assertEqual(
            invalid["evidence_items"]["claim:with-colon"]["decision"],
            "pending",
        )

    def test_regenerate_preserves_only_the_same_claim_and_fingerprint(self):
        self.write_candidate()
        self.gen()
        text = self.read_queue()
        text = text.replace(self._header(), self._header(checked=True))
        text = text.replace("    - decision: pending", "    - decision: accepted")
        self.write_queue(text)

        self.gen()
        same = A.parse_queue(self.read_queue())["evidence_items"][self.CLAIM_ID]
        self.assertTrue(same["checked"])
        self.assertEqual(same["decision"], "accepted")

        self.write_candidate(
            source_fingerprint="b" * 64,
            statement="[SYNTHETIC] Revised candidate content.",
        )
        self.gen()
        changed_text = self.read_queue()
        self.assertNotIn(A.evidence_marker(self.CLAIM_ID, "a" * 64), changed_text)
        self.assertIn(self._header("b" * 64, checked=False), changed_text)
        changed = A.parse_queue(changed_text)["evidence_items"][self.CLAIM_ID]
        self.assertFalse(changed["checked"])
        self.assertEqual(changed["decision"], "pending")
        self.assertNotIn("superseded_by", changed)

    def test_session_approve_all_never_commits_an_evidence_item(self):
        self.write_draft()
        self.make_note()
        self.write_candidate()
        self.gen()
        text = self.read_queue()
        text = text.replace(
            "- [ ] **APPROVE ALL** pending session writebacks below",
            "- [x] **APPROVE ALL** pending session writebacks below",
        )
        text = text.replace(self._header(), self._header(checked=True))
        text = text.replace("    - decision: pending", "    - decision: accepted")
        self.write_queue(text)
        registry = self.root / "memory" / M.EVIDENCE_REGISTRY
        registry_before = registry.read_bytes()

        plan = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["items"][0]["summary_id"], "claude-code-sid")
        self.assertEqual(registry.read_bytes(), registry_before)
        candidates = M.load_candidate_claims(self.root)
        self.assertEqual(candidates[0]["local_review_state"], "candidate")
        still_pending = A.parse_queue(self.read_queue())["evidence_items"][self.CLAIM_ID]
        self.assertTrue(still_pending["checked"])
        self.assertEqual(still_pending["decision"], "accepted")


class ParseTest(_Base):
    def test_parse_checkbox_fields_and_approve_all(self):
        text = (A.APPROVALS_MARKER + "\n\n"
                "- [x] **APPROVE ALL** pending items below\n\n"
                "- [x] **synthetic-xq17-calibration** · 2026-06-20 " + A.draft_marker("s1") + "\n"
                "    - status: active\n"
                "    - next_actions: a | b | c\n"
                "    - conclusion: hi\n\n"
                "- [ ] **other** " + A.draft_marker("s2") + "\n"
                "    - status: done\n")
        parsed = A.parse_queue(text)
        self.assertTrue(parsed["approve_all"])
        self.assertTrue(parsed["items"]["s1"]["checked"])
        self.assertFalse(parsed["items"]["s2"]["checked"])
        self.assertEqual(parsed["items"]["s1"]["fields"]["status"], "active")
        self.assertEqual(parsed["items"]["s1"]["fields"]["next_actions"], "a | b | c")
        self.assertEqual(parsed["items"]["s1"]["fields"]["conclusion"], "hi")


# --- approve: commit via M1 + memory overlay --------------------------------

class ApproveTest(_Base):
    def _gen_and_tick(self, sid="claude-code-sid"):
        self.gen()
        t = self.read_queue().replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        self.write_queue(t)

    def test_approve_commits_via_m1_and_memory(self):
        self.write_draft()
        note = self.make_note()
        self._gen_and_tick()
        plan = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan["state"], "approved")
        self.assertEqual(plan["count"], 1)
        # the approved block landed in the note progress-log under a DISTINCT key
        t = note.read_text(encoding="utf-8")
        self.assertIn(PL.entry_marker("claude-code-sid#approved"), t)
        self.assertNotIn(PL.entry_marker("claude-code-sid") + "\n", t)  # not the timeline key
        self.assertIn("status: done", t)
        self.assertIn("conclusion: M2 spine in place", t)
        # journaled
        self.assertTrue(list(paths.journal_dir(self.root).glob("*.json")))
        # draft flipped to applied (audit kept)
        d = json.loads((paths.drafts_dir(self.root) / "claude-code-sid.json").read_text(encoding="utf-8"))
        self.assertEqual(d["approval_state"], "applied")
        self.assertEqual(d["applied_claims"]["status"], "done")
        # memory overlay (SSoT) updated and visible through load_projects
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "done")
        # the queue regenerated -> the applied item is gone
        self.assertNotIn(A.draft_marker("claude-code-sid"), self.read_queue())

    def test_does_not_touch_handwritten_content(self):
        # approve now updates the named frontmatter fields (status/...), but the
        # hand-authored PROSE and the progress-log region (END onward) stay
        # byte-identical. The leading frontmatter block is the only part that may
        # change, and only for the named project/1.0 keys.
        self.write_draft()
        note = self.make_note()
        raw = note.read_bytes()
        fence = raw.index(b"\n---\n") + len(b"\n---\n")     # end of leading frontmatter
        prose = raw[fence:raw.find(BEGIN.encode())]         # hand prose before the log region
        suffix = raw[raw.find(END.encode()):]              # END + trailing hand prose
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        out = note.read_bytes()
        # hand-authored prose between the frontmatter and the log region: untouched
        self.assertEqual(out[out.index(b"\n---\n") + len(b"\n---\n"):out.find(BEGIN.encode())], prose)
        # progress-log region + trailing prose from END onward: untouched
        self.assertEqual(out[out.find(END.encode()):], suffix)
        # unrelated frontmatter keys untouched; only the named field changed
        fm, _ = M.parse_frontmatter(out.decode("utf-8"))
        self.assertEqual(fm["schema_version"], "project/1.0")
        self.assertEqual(fm["project_id"], "synthetic-xq17-calibration")
        self.assertEqual(fm["status"], "done")             # the approved value

    def test_approve_all_commits_every_pending(self):
        self.write_draft(sid="a", conclusion="c-a")
        self.write_draft(sid="b", conclusion="c-b")
        self.make_note()
        self.gen()
        # tick only APPROVE ALL (leave both items unticked)
        t = self.read_queue().replace("- [ ] **APPROVE ALL**", "- [x] **APPROVE ALL**")
        self.write_queue(t)
        plan = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan["count"], 2)
        for sid in ("a", "b"):
            d = json.loads((paths.drafts_dir(self.root) / f"{sid}.json").read_text(encoding="utf-8"))
            self.assertEqual(d["approval_state"], "applied")

    def test_dry_run_writes_nothing(self):
        self.write_draft()
        note = self.make_note()
        before = note.read_bytes()
        self._gen_and_tick()
        queue_before = self.read_queue()
        plan = A.approve(self.vault, root=self.root, run=False)
        self.assertEqual(plan["state"], "would_approve")
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["items"][0]["state"], "would_approve")
        self.assertEqual(note.read_bytes(), before)                       # note untouched
        self.assertEqual(self.read_queue(), queue_before)                 # queue untouched
        d = json.loads((paths.drafts_dir(self.root) / "claude-code-sid.json").read_text(encoding="utf-8"))
        self.assertEqual(d["approval_state"], "draft")                    # not flipped
        self.assertFalse((self.root / "memory" / M.APPLIED_CLAIMS).exists())

    def test_idempotent_double_approve(self):
        self.write_draft()
        note = self.make_note()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        # the queue was regenerated (item gone); a second approve is a no-op
        plan2 = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan2["count"], 0)
        self.assertEqual(note.read_text(encoding="utf-8").count(
            PL.entry_marker("claude-code-sid#approved")), 1)

    def test_edit_before_approve_commits_edited_value(self):
        self.write_draft(status="done")
        note = self.make_note()
        self.gen()
        # user edits status done->paused AND ticks
        t = self.read_queue()
        t = t.replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        t = t.replace("- status: done", "- status: paused")
        self.write_queue(t)
        A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "paused")
        self.assertIn("status: paused", note.read_text(encoding="utf-8"))
        d = json.loads((paths.drafts_dir(self.root) / "claude-code-sid.json").read_text(encoding="utf-8"))
        self.assertEqual(d["applied_claims"]["status"], "paused")
        self.assertEqual(d["status"], "done")   # original proposal kept as audit

    def test_freetext_stage_flows_to_note_and_memory(self):
        # a CC-session draft carrying a FREE-TEXT stage: generate shows it, approve
        # commits it to the note progress-log AND the memory overlay (same path as
        # status), and it is NEVER enum-rejected like status would be.
        self.write_draft(status="", conclusion="", next_actions=(),
                         stage="off-enum drafting phase")
        note = self.make_note()
        self.gen()
        self.assertIn("- stage: off-enum drafting phase", self.read_queue())
        self._gen_and_tick()
        plan = A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["items"][0]["warnings"], [])   # stage not enum-rejected
        self.assertIn("- stage: off-enum drafting phase", note.read_text(encoding="utf-8"))
        self.assertEqual(
            M.load_applied_claims(self.root)["synthetic-xq17-calibration"]["stage"],
            "off-enum drafting phase")
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["stage"],
                         "off-enum drafting phase")

    def test_edit_before_approve_commits_edited_stage(self):
        self.write_draft(stage="initial phase")
        note = self.make_note()
        self.gen()
        t = self.read_queue()
        t = t.replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        t = t.replace("- stage: initial phase", "- stage: edited phase")
        self.write_queue(t)
        A.approve(self.vault, root=self.root, run=True)
        self.assertEqual(M.load_applied_claims(self.root)["synthetic-xq17-calibration"]["stage"],
                         "edited phase")
        self.assertIn("- stage: edited phase", note.read_text(encoding="utf-8"))

    def test_memory_overlay_survives_vault_reingest(self):
        self.write_draft(status="done")
        self.make_note()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        # simulate prov-ingest-vault rewriting projects.json from the (unchanged)
        # hand-authored frontmatter (status still 'planned')
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(projects_doc(), ensure_ascii=False), encoding="utf-8")
        # the approved claim still wins through the overlay
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "done")

    def test_no_queue_and_refuse_non_generated(self):
        self.write_draft()
        self.assertEqual(A.approve(self.vault, root=self.root, run=True)["state"], "no_queue")
        self.write_queue("# not ours\n")
        self.assertEqual(A.approve(self.vault, root=self.root, run=True)["state"],
                         "refused_not_generated")


# --- approve: writes approved fields back into the note frontmatter ----------
# (so the Dataview board reflects an approval). ONLY the named project/1.0 fields
# (status/stage/next_actions/blocked_by + a bumped `updated`); the note body, the
# progress-log region, and unrelated frontmatter keys stay byte-identical;
# conclusion has no frontmatter field and stays log-only.

class ApproveFrontmatterTest(_Base):
    def _gen_and_tick(self):
        self.gen()
        t = self.read_queue().replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        self.write_queue(t)

    @staticmethod
    def _frontmatter(note):
        fm, _ = M.parse_frontmatter(note.read_text(encoding="utf-8"))
        return fm

    @staticmethod
    def _split(raw):
        """(frontmatter_bytes_excl_fence, body_bytes_from_BEGIN_on)."""
        end_fence = raw.index(b"\n---\n")                 # close of the leading block
        begin = raw.find(BEGIN.encode("utf-8"))
        return raw[:end_fence], raw[begin:]

    def test_approve_updates_named_frontmatter_fields(self):
        self.write_draft(status="done", stage="modeling",
                         next_actions=("wire the hook", "build the M3 queue"),
                         conclusion="M2 spine in place")
        note = self.make_note()
        before = note.read_bytes()
        self._gen_and_tick()
        plan = A.approve(self.vault, root=self.root, run=True)
        item = plan["items"][0]
        self.assertIn(item["frontmatter_status"], ("updated", "added"))
        fm = self._frontmatter(note)
        # the named project/1.0 fields now reflect the approval
        self.assertEqual(fm["status"], "done")
        self.assertEqual(fm["stage"], "modeling")
        self.assertEqual(fm["next_actions"], ["wire the hook", "build the M3 queue"])
        # `updated` bumped to the commit date
        self.assertEqual(fm["updated"], "2026-06-20")
        # conclusion is NOT a frontmatter field -> stays log-only
        self.assertNotIn("conclusion", fm)
        # ... but it IS in the appended progress-log block
        self.assertIn("conclusion: M2 spine in place", note.read_text(encoding="utf-8"))
        # unrelated hand-authored frontmatter keys untouched
        self.assertEqual(fm["schema_version"], "project/1.0")
        self.assertEqual(fm["project_id"], "synthetic-xq17-calibration")
        # the body + progress-log region after BEGIN is byte-identical EXCEPT for the
        # one appended #approved entry (the append is the M1 path, separately tested);
        # the frontmatter-only check: bytes BEFORE the body are the only place changed.
        _, body_after = self._split(note.read_bytes())
        # the body still carries the original hand prose verbatim
        self.assertIn("hand-authored plan 中文.".encode("utf-8"), note.read_bytes())
        self.assertIn("trailing human text".encode("utf-8"), body_after)

    def test_blocked_by_flows_to_frontmatter(self):
        self.write_draft(status="paused", next_actions=(), conclusion="",
                         blocked_by="waiting on R2 reviews")
        note = self.make_note()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        fm = self._frontmatter(note)
        self.assertEqual(fm["status"], "paused")
        self.assertEqual(fm["blocked_by"], "waiting on R2 reviews")

    def test_note_without_frontmatter_skips_gracefully(self):
        # a project note that has the progress-log marker pair but NO frontmatter:
        # the log append + memory overlay still happen; frontmatter is skipped.
        self.write_draft(status="done", conclusion="c")
        pdir = self.vault / "Projects"
        pdir.mkdir(parents=True, exist_ok=True)
        note = pdir / "synthetic-xq17-calibration.md"
        note.write_bytes(("# Synthetic XQ-17 Calibration (no frontmatter)\n\n"
                          + BEGIN + "\n" + END + "\n").encode("utf-8"))
        self._gen_and_tick()
        plan = A.approve(self.vault, root=self.root, run=True)
        item = plan["items"][0]
        self.assertEqual(item["frontmatter_status"], "no_frontmatter")
        # progress-log append still landed
        self.assertEqual(item["append_status"], PL.APPENDED)
        self.assertIn(PL.entry_marker("claude-code-sid#approved"),
                      note.read_text(encoding="utf-8"))
        # memory overlay still updated
        self.assertEqual(M.load_projects(self.root)["synthetic-xq17-calibration"]["status"], "done")

    def test_only_committed_fields_touched_never_blanks_omitted(self):
        # a draft proposing ONLY status: stage/next_actions/blocked_by are absent in
        # the proposal -> they must NOT be written/blanked in the frontmatter.
        self.write_draft(status="active", stage="", next_actions=(), conclusion="",
                         blocked_by="")
        note = self.make_note()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        text = note.read_text(encoding="utf-8")
        fm = self._frontmatter(note)
        self.assertEqual(fm["status"], "active")
        # the proposal carried no stage/next_actions/blocked_by -> none added
        self.assertNotIn("stage", fm)
        self.assertNotIn("next_actions", fm)
        self.assertNotIn("blocked_by", fm)

    def test_edit_before_approve_carries_edited_value_into_frontmatter(self):
        self.write_draft(status="done", stage="initial phase")
        note = self.make_note()
        self.gen()
        t = self.read_queue()
        t = t.replace("- [ ] **synthetic-xq17-calibration**", "- [x] **synthetic-xq17-calibration**")
        t = t.replace("- status: done", "- status: paused")
        t = t.replace("- stage: initial phase", "- stage: edited phase")
        self.write_queue(t)
        A.approve(self.vault, root=self.root, run=True)
        fm = self._frontmatter(note)
        self.assertEqual(fm["status"], "paused")       # edited value, not the proposal
        self.assertEqual(fm["stage"], "edited phase")

    def test_dry_run_writes_no_frontmatter(self):
        self.write_draft(status="done")
        note = self.make_note()
        before = note.read_bytes()
        self._gen_and_tick()
        plan = A.approve(self.vault, root=self.root, run=False)
        self.assertEqual(plan["items"][0]["state"], "would_approve")
        self.assertNotIn("frontmatter_status", plan["items"][0])  # not committed in dry-run
        self.assertEqual(note.read_bytes(), before)              # note byte-identical

    def test_frontmatter_writeback_is_journaled_and_reversible(self):
        self.write_draft(status="done", next_actions=("a", "b"))
        note = self.make_note()
        before = note.read_bytes()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        after = note.read_bytes()
        self.assertNotEqual(after, before)
        # roll back EVERY journal entry, newest first -> the note returns to pre-state
        journals = sorted(paths.journal_dir(self.root).glob("*.json"), reverse=True)
        self.assertTrue(journals)
        for j in journals:
            PL.rollback(j)
        self.assertEqual(note.read_bytes(), before)

    def test_reingest_reflects_new_status_and_stage(self):
        # after approve writes the frontmatter, prov-ingest-vault re-reads the note and
        # projects.json reflects the approved status/stage (board source of truth).
        self.write_draft(status="done", stage="modeling",
                         next_actions=("ship it",), conclusion="x")
        self.make_note()
        self._gen_and_tick()
        A.approve(self.vault, root=self.root, run=True)
        # re-ingest the vault into a FRESH memory dir (so the overlay can't mask it)
        from provenance.ingest import vault as V
        memdir = self.root / "memory2"
        old_argv = sys.argv
        try:
            sys.argv = ["prov-ingest-vault", str(self.vault), str(memdir)]
            V.main()
        finally:
            sys.argv = old_argv
        projects = json.loads((memdir / "projects.json").read_text(encoding="utf-8"))["projects"]
        rec = projects["synthetic-xq17-calibration"]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["stage"], "modeling")
        self.assertEqual(rec["next_actions"], ["ship it"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
