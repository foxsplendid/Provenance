#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the M2 SessionEnd path: resolver + enqueue + summary + worker.

Covers the deterministic halves of the event/sync layer (sync-layer.md §1/§4/§7):
enqueue, cwd->project_id resolve, scaffold (no cwd leak), assemble/validate, worker
drain (scaffold -> apply), auto-timeline via the M1 primitive, draft staging,
idempotency, dry-run, lockfile, Inbox fallback. The LLM fill is stubbed by a
fixture (tests/fixtures/synclayer/fill.json) — the worker never calls an LLM.

Run from repo root:
    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance.synclayer import paths, resolver, enqueue, summary, worker  # noqa: E402
from provenance.synclayer import scan_agents  # noqa: E402
from provenance.synclayer import progress_log as PL  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synclayer" / "fill.json"

BEGIN, END = PL.BEGIN_MARKER, PL.END_MARKER


def projects_doc():
    return {
        "ingested_at": "2026-06-20T00:00:00+00:00",
        "count": 1,
        "projects": {
            "synthetic-xq17-calibration": {
                "schema_version": "project/1.0",
                "project_id": "synthetic-xq17-calibration",
                "linked_repo": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                "_source_file": "synthetic-xq17-calibration.md",
            }
        },
    }


def inbox_record(session_id, name="A session"):
    return {
        "source": "claude-code",
        "conversation_id": f"claude-code-{session_id}",
        "name": name,
        "created_at": "2026-06-20T10:00:00.000Z",
        "updated_at": "2026-06-20T11:00:00.000Z",
        "project": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",  # absolute cwd -> must be dropped
        "chat_messages": [
            {"sender": "human", "text": "please add the synthetic calibration figure", "created_at": "t1"},
            {"sender": "assistant", "text": "done, regenerated fig 3", "created_at": "t2"},
        ],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        # memory/projects.json under the tmp root
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(projects_doc(), ensure_ascii=False), encoding="utf-8")

    def write_inbox(self, event):
        """Drop a sanitized inbox file at the exact stem the worker computes."""
        stem = worker._stem_for(event)
        d = self.root / "inbox" / "claude-code"
        d.mkdir(parents=True, exist_ok=True)
        rec = inbox_record(event["session_id"])
        (d / f"{stem}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return rec

    def make_event(self, sid="11111111-2222-3333-4444-555555555555",
                   cwd="C:/ScriptoriumDemo/projects/synthetic-xq17-calibration"):
        tp = f"C:/ScriptoriumDemo/agent-sessions/{sid}.jsonl"
        return {"session_id": sid, "transcript_path": tp, "cwd": cwd,
                "hook_event_name": "SessionEnd", "reason": "exit", "enqueued_at": "2026-06-20T11:00:01+00:00"}

    def make_note(self, project_file="synthetic-xq17-calibration.md"):
        vault = self.root / "vault"
        pdir = vault / "Projects"
        pdir.mkdir(parents=True, exist_ok=True)
        note = pdir / project_file
        note.write_text("# Synthetic XQ-17 Calibration\n\nhand plan\n\n" + BEGIN + "\n" + END + "\n", encoding="utf-8")
        return vault, note


# --- resolver ----------------------------------------------------------------

class ResolverTest(_Base):
    def test_exact_match(self):
        idx, ids = resolver.build_index(projects_doc()["projects"])
        self.assertEqual(resolver.resolve("C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", idx, ids),
                         "synthetic-xq17-calibration")

    def test_ancestor_match_for_worktree(self):
        idx, ids = resolver.build_index(projects_doc()["projects"])
        # a worktree / subdir cwd resolves up to its repo
        self.assertEqual(
            resolver.resolve("C:/ScriptoriumDemo/projects/synthetic-xq17-calibration/.claude/worktrees/x", idx, ids),
            "synthetic-xq17-calibration")

    def test_case_and_separator_insensitive(self):
        idx, ids = resolver.build_index(projects_doc()["projects"])
        self.assertEqual(resolver.resolve(r"c:\ScriptoriumDemo\projects\Synthetic-XQ17-Calibration", idx, ids),
                         "synthetic-xq17-calibration")

    def test_redacted_windows_user_snapshot_matches_real_cwd(self):
        projects = {
            "synthetic-private-project": {
                "project_id": "synthetic-private-project",
                "linked_repo": "C:/Users/<user>/Code/synthetic-private-project",
            }
        }
        snapshot_before = json.dumps(projects, sort_keys=True)
        idx, ids = resolver.build_index(projects)

        self.assertEqual(
            resolver.resolve("C:/Users/demo-a/Code/synthetic-private-project", idx, ids),
            "synthetic-private-project",
        )
        self.assertEqual(
            resolver.resolve(
                "C:/Users/demo-a/Code/synthetic-private-project/.codex/worktrees/review",
                idx,
                ids,
            ),
            "synthetic-private-project",
        )
        self.assertEqual(json.dumps(projects, sort_keys=True), snapshot_before)
        self.assertNotIn("alice", repr(idx).casefold())

    def test_unredacted_windows_exact_matches_but_alias_collision_fails_closed(self):
        projects = {
            "demo-a-project": {
                "project_id": "demo-a-project",
                "linked_repo": "C:/Users/demo-a/Code/shared-name",
            },
            "demo-b-project": {
                "project_id": "demo-b-project",
                "linked_repo": "C:/Users/demo-b/Code/shared-name",
            },
        }
        idx, ids = resolver.build_index(projects)

        self.assertEqual(
            resolver.resolve("C:/Users/demo-a/Code/shared-name", idx, ids),
            "demo-a-project",
        )
        self.assertEqual(
            resolver.resolve("C:/Users/demo-b/Code/shared-name", idx, ids),
            "demo-b-project",
        )
        self.assertIsNone(
            resolver.resolve("C:/Users/demo-c/Code/shared-name", idx, ids)
        )

    def test_two_sanitized_same_name_repos_are_ambiguous(self):
        projects = {
            "parent-research": {
                "project_id": "parent-research",
                "linked_repo": "C:/Users/<user>/Code",
            },
            "first-research": {
                "project_id": "first-research",
                "linked_repo": "C:/Users/<user>/Code/shared-name",
            },
            "second-research": {
                "project_id": "second-research",
                "linked_repo": "C:/Users/<user>/Code/shared-name",
            },
        }
        idx, ids = resolver.build_index(projects)

        self.assertIsNone(
            resolver.resolve("C:/Users/demo-a/Code/shared-name", idx, ids)
        )
        self.assertIsNone(
            resolver.resolve("C:/Users/<user>/Code/shared-name", idx, ids)
        )

    def test_repeated_same_project_alias_is_not_ambiguous(self):
        class RepeatedProjects:
            @staticmethod
            def items():
                return [
                    (
                        "same-research",
                        {"linked_repo": "C:/Users/demo-a/Code/shared-name"},
                    ),
                    (
                        "same-research",
                        {"linked_repo": "C:/Users/demo-b/Code/shared-name"},
                    ),
                ]

        idx, ids = resolver.build_index(RepeatedProjects())

        self.assertEqual(
            resolver.resolve("C:/Users/demo-c/Code/shared-name", idx, ids),
            "same-research",
        )

    def test_non_windows_paths_keep_exact_behavior(self):
        projects = {
            "linux-research": {
                "project_id": "linux-research",
                "linked_repo": "/srv/users/demo-a/synthetic-private-project",
            }
        }
        idx, ids = resolver.build_index(projects)

        self.assertEqual(
            resolver.resolve("/srv/users/demo-a/synthetic-private-project", idx, ids),
            "linux-research",
        )
        self.assertIsNone(
            resolver.resolve("/srv/users/demo-b/synthetic-private-project", idx, ids)
        )

    def test_project_id_hint(self):
        idx, ids = resolver.build_index(projects_doc()["projects"])
        self.assertEqual(resolver.resolve("synthetic-xq17-calibration", idx, ids), "synthetic-xq17-calibration")

    def test_miss_returns_none(self):
        idx, ids = resolver.build_index(projects_doc()["projects"])
        self.assertIsNone(resolver.resolve("C:/ScriptoriumDemo/projects/unrelated", idx, ids))

    def test_resolve_project_reads_root(self):
        self.assertEqual(
            resolver.resolve_project("C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", root=self.root),
            "synthetic-xq17-calibration")


# --- enqueue -----------------------------------------------------------------

class EnqueueTest(_Base):
    def test_read_event_snake_case(self):
        payload = json.dumps({"session_id": "s", "transcript_path": "t", "cwd": "c",
                              "hook_event_name": "SessionEnd", "reason": "exit"})
        ev = enqueue.read_event(payload)
        self.assertEqual(ev["session_id"], "s")
        self.assertEqual(ev["hook_event_name"], "SessionEnd")
        self.assertEqual(ev["source"], "claude-code")
        self.assertTrue(ev["stdin_received"])

    def test_read_event_empty_stdin(self):
        ev = enqueue.read_event("")
        self.assertFalse(ev["stdin_received"])
        self.assertIsNone(ev["session_id"])

    def test_enqueue_appends_and_resolves(self):
        ev = enqueue.read_event(json.dumps({"session_id": "s", "transcript_path": "t",
                                            "cwd": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                                            "hook_event_name": "SessionEnd"}))
        enqueue.enqueue(ev, root=self.root)
        lines = worker._read_jsonl(paths.queue_file(self.root))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["project_id"], "synthetic-xq17-calibration")
        self.assertEqual(lines[0]["cwd"], "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration")

    def test_enqueue_unresolved_project_is_null(self):
        ev = enqueue.read_event(json.dumps({"session_id": "s", "cwd": "D:/nope",
                                            "hook_event_name": "SessionEnd"}))
        enqueue.enqueue(ev, root=self.root)
        lines = worker._read_jsonl(paths.queue_file(self.root))
        self.assertIsNone(lines[0]["project_id"])

    def test_enqueue_two_events_both_present(self):
        for sid in ("a", "b"):
            enqueue.enqueue(enqueue.read_event(json.dumps({"session_id": sid})), root=self.root)
        self.assertEqual(len(worker._read_jsonl(paths.queue_file(self.root))), 2)

    def test_no_enqueue_env_suppresses_write(self):
        # self-feed guard: a sync-layer-spawned session sets the env -> no enqueue
        ev = enqueue.read_event(json.dumps({"session_id": "s",
                                            "cwd": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                                            "hook_event_name": "SessionEnd"}))
        with mock.patch.dict(os.environ, {enqueue.NO_ENQUEUE_ENV: "1"}):
            enqueue.enqueue(ev, root=self.root)
        self.assertFalse(paths.queue_file(self.root).exists())  # nothing written

    def test_falsey_env_still_enqueues(self):
        # a present-but-falsey value must NOT suppress (only truthy does)
        ev = enqueue.read_event(json.dumps({"session_id": "s",
                                            "cwd": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                                            "hook_event_name": "SessionEnd"}))
        with mock.patch.dict(os.environ, {enqueue.NO_ENQUEUE_ENV: "0"}):
            enqueue.enqueue(ev, root=self.root)
        self.assertEqual(len(worker._read_jsonl(paths.queue_file(self.root))), 1)


# --- summary: scaffold / validate / assemble --------------------------------

class SummaryTest(_Base):
    def test_scaffold_drops_cwd_keeps_turns(self):
        rec = inbox_record("sid")
        sc = summary.build_scaffold(rec, summary_id="claude-code-sid",
                                    project_id="synthetic-xq17-calibration", created="2026-06-20")
        blob = json.dumps(sc, ensure_ascii=False)
        self.assertNotIn("C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", blob)  # absolute cwd gone
        self.assertNotIn("project\": \"D:", blob)
        self.assertEqual(len(sc["turns"]), 2)
        self.assertEqual(sc["turns"][0]["role"], "user")
        self.assertEqual(sc["project"], "synthetic-xq17-calibration")

    def test_validate_rejects_unfilled_and_bad_enum(self):
        self.assertTrue(summary.validate_fill({"timeline": ["x"]}))  # missing schema_version
        self.assertTrue(summary.validate_fill(
            {"schema_version": "summary-fill/1.0", "timeline": ["x"], "status": "bogus"}))
        self.assertTrue(summary.validate_fill(
            {"schema_version": "summary-fill/1.0", "timeline": []}))  # empty timeline

    def test_validate_accepts_good_fill(self):
        self.assertEqual(summary.validate_fill(json.loads(FIXTURE.read_text(encoding="utf-8"))), [])

    def test_assemble_identity_from_scaffold_not_fill(self):
        sc = summary.build_scaffold(inbox_record("sid"), summary_id="claude-code-sid",
                                    project_id="synthetic-xq17-calibration", created="2026-06-20")
        fill = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fill["project"] = "ATTACKER-PROJECT"      # fill cannot retarget
        fill["summary_id"] = "forged"
        summ = summary.assemble(sc, fill)
        self.assertEqual(summ["project"], "synthetic-xq17-calibration")
        self.assertEqual(summ["summary_id"], "claude-code-sid")
        self.assertEqual(summ["approval_state"], "draft")
        self.assertEqual(summ["schema_version"], "session-summary/1.0")
        self.assertIn("status", summ)             # high-value carried through

    def test_assemble_raises_on_bad_fill(self):
        sc = summary.build_scaffold(inbox_record("sid"), summary_id="x",
                                    project_id=None, created="2026-06-20")
        with self.assertRaises(summary.SummaryError):
            summary.assemble(sc, {"schema_version": "summary-fill/1.0", "timeline": []})

    def test_assemble_rejects_an_unresolved_project(self):
        scaffold = summary.build_scaffold(
            inbox_record("sid"),
            summary_id="claude-code-sid",
            project_id=None,
            created="2026-06-20",
        )

        with self.assertRaisesRegex(summary.SummaryError, "resolved"):
            summary.assemble(
                scaffold, json.loads(FIXTURE.read_text(encoding="utf-8"))
            )

    def test_freetext_stage_validated_as_string_no_enum(self):
        base = {"schema_version": "summary-fill/1.0", "timeline": ["x"]}
        # any free-text stage is accepted (no enum) ...
        self.assertEqual(summary.validate_fill({**base, "stage": "off-enum phase"}), [])
        # ... but a non-string stage is rejected by the type check
        self.assertTrue(summary.validate_fill({**base, "stage": 7}))

    def test_assemble_carries_freetext_stage(self):
        sc = summary.build_scaffold(inbox_record("sid"), summary_id="claude-code-sid",
                                    project_id="synthetic-xq17-calibration", created="2026-06-20")
        fill = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fill["stage"] = "manuscript drafting"   # free-text, not a status enum value
        summ = summary.assemble(sc, fill)
        self.assertEqual(summ["stage"], "manuscript drafting")

    def test_render_timeline_block_excludes_high_value(self):
        summ = {"summary_id": "x", "created": "2026-06-20T11:00:00Z",
                "timeline": ["did a thing", "did another"], "status": "active",
                "conclusion": "secret takeaway", "next_actions": ["secret next"]}
        block = summary.render_timeline_block(summ)
        self.assertIn("did a thing", block)
        self.assertIn("2026-06-20", block)
        self.assertNotIn("secret takeaway", block)   # high-value stays gated
        self.assertNotIn("secret next", block)


# --- apply_summary: M1 primitive + draft staging ----------------------------

class ApplySummaryTest(_Base):
    def _summ(self, sid="claude-code-sid"):
        return {"schema_version": "session-summary/1.0", "summary_id": sid,
                "project": "synthetic-xq17-calibration", "created": "2026-06-20T11:00:00Z",
                "session_id": "11111111-2222-3333-4444-555555555555",
                "approval_state": "draft", "timeline": ["ran tests, all green"],
                "status": "active", "conclusion": "spine in place"}

    def test_auto_timeline_appends_via_m1(self):
        _vault, note = self.make_note()
        summ = self._summ()
        plan = worker.apply_summary(summ, root=self.root, run=True, note_path=note)
        self.assertEqual(plan["state"], "applied")
        self.assertEqual(plan["append_status"], PL.APPENDED)
        t = note.read_text(encoding="utf-8")
        self.assertIn(PL.entry_marker("claude-code-sid"), t)
        self.assertIn("ran tests, all green", t)
        self.assertLess(t.find(BEGIN), t.find(PL.entry_marker("claude-code-sid")))
        # high-value claim NOT written to the note (gated), but IS in the draft
        self.assertNotIn("spine in place", t)
        draft = json.loads((paths.drafts_dir(self.root) / "claude-code-sid.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["approval_state"], "draft")
        self.assertEqual(draft["conclusion"], "spine in place")

    def test_journal_written_for_note_append(self):
        _vault, note = self.make_note()
        worker.apply_summary(self._summ(), root=self.root, run=True, note_path=note)
        self.assertTrue(list(paths.journal_dir(self.root).glob("*.json")))

    def test_dry_run_writes_nothing(self):
        _vault, note = self.make_note()
        before = note.read_bytes()
        plan = worker.apply_summary(self._summ(), root=self.root, run=False, note_path=note)
        self.assertEqual(plan["state"], "would_apply")
        self.assertEqual(note.read_bytes(), before)
        self.assertFalse((paths.drafts_dir(self.root) / "claude-code-sid.json").exists())

    def test_idempotent_double_apply(self):
        _vault, note = self.make_note()
        worker.apply_summary(self._summ(), root=self.root, run=True, note_path=note)
        plan2 = worker.apply_summary(self._summ(), root=self.root, run=True, note_path=note)
        self.assertEqual(plan2["append_status"], PL.SKIPPED_DUPLICATE)
        t = note.read_text(encoding="utf-8")
        self.assertEqual(t.count(PL.entry_marker("claude-code-sid")), 1)

    def test_apply_summary_rejects_an_unresolved_project(self):
        summ = self._summ()
        summ["project"] = None

        with self.assertRaisesRegex(worker.WorkerStateError, "resolved project"):
            worker.apply_summary(summ, root=self.root, run=True, note_path=None)

        self.assertFalse((paths.drafts_dir(self.root) / "claude-code-sid.json").exists())

    def test_missing_marker_routes_to_draft_not_grow(self):
        vault = self.root / "vault" / "Projects"
        vault.mkdir(parents=True, exist_ok=True)
        note = vault / "synthetic-xq17-calibration.md"
        raw = "# hand note, no markers\n".encode("utf-8")
        note.write_bytes(raw)
        plan = worker.apply_summary(self._summ(), root=self.root, run=True, note_path=note)
        self.assertEqual(plan["append_status"], PL.REFUSED_NO_MARKER)
        self.assertEqual(plan["routed"], "inbox-draft")
        self.assertEqual(note.read_bytes(), raw)   # never grew the human file

    def test_timeline_failure_does_not_create_a_false_completion_draft(self):
        _vault, note = self.make_note()
        draft = paths.drafts_dir(self.root) / "claude-code-sid.json"

        with mock.patch.object(
            PL, "append_entry", side_effect=OSError("private timeline failure")
        ):
            with self.assertRaises(OSError):
                worker.apply_summary(
                    self._summ(), root=self.root, run=True, note_path=note
                )

        self.assertFalse(draft.exists())
        recovered = worker.apply_summary(
            self._summ(), root=self.root, run=True, note_path=note
        )
        self.assertEqual(recovered["append_status"], PL.APPENDED)
        self.assertTrue(draft.exists())

    def test_existing_draft_repairs_a_missing_timeline_before_retiring(self):
        vault, note = self.make_note()
        event = self.make_event()
        self.write_inbox(event)
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        completion = self._summ(sid)
        completion["created"] = "2026-06-20T11:00:00.000Z"
        draft.write_text(
            json.dumps(completion, ensure_ascii=False), encoding="utf-8"
        )

        result = worker._advance(
            event,
            self.root,
            run=True,
            ingest_fn=lambda root: None,
            vault=vault,
        )

        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["append_status"], PL.APPENDED)
        self.assertIn(PL.entry_marker(sid), note.read_text(encoding="utf-8"))

    def test_existing_draft_project_mismatch_fails_closed(self):
        vault, alpha_note = self.make_note()
        _vault, beta_note = self.make_note(project_file="beta.md")
        portfolio = projects_doc()
        portfolio["projects"]["beta"] = {
            "schema_version": "project/1.0",
            "project_id": "beta",
            "linked_repo": "C:/ScriptoriumDemo/projects/beta",
            "_source_file": "beta.md",
        }
        (self.root / "memory" / "projects.json").write_text(
            json.dumps(portfolio), encoding="utf-8"
        )
        event = self.make_event()
        event["project_id"] = "beta"
        self.write_inbox(event)
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        completion = self._summ(sid)
        completion["created"] = "2026-06-20T11:00:00.000Z"
        draft.write_text(json.dumps(completion, ensure_ascii=False), encoding="utf-8")
        alpha_before = alpha_note.read_bytes()
        beta_before = beta_note.read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "project identity"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=lambda root: None,
                vault=vault,
            )

        self.assertEqual(alpha_note.read_bytes(), alpha_before)
        self.assertEqual(beta_note.read_bytes(), beta_before)

    def test_invalid_existing_draft_is_not_a_completion_credential(self):
        event = self.make_event()
        self.write_inbox(event)
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        invalid = self._summ(sid)
        invalid["schema_version"] = "session-summary/0.0"
        draft.write_text(json.dumps(invalid), encoding="utf-8")

        with self.assertRaisesRegex(worker.WorkerStateError, "credential is invalid"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=lambda root: None,
            )

    def test_incomplete_existing_draft_cannot_retire_event(self):
        event = self.make_event()
        self.write_inbox(event)
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)

        for field, value in (
            ("created", ""),
            ("approval_state", "forged"),
            ("session_id", "another-session"),
            ("project", None),
            ("summary_id", "forged"),
        ):
            with self.subTest(field=field):
                invalid = self._summ(sid)
                invalid["created"] = "2026-06-20T11:00:00.000Z"
                invalid[field] = value
                draft.write_text(json.dumps(invalid), encoding="utf-8")

                with self.assertRaisesRegex(worker.WorkerStateError, "credential is invalid"):
                    worker._advance(
                        event,
                        self.root,
                        run=True,
                        ingest_fn=lambda root: None,
                    )

    def test_draft_repair_rejects_nonempty_forged_created_identity(self):
        event = self.make_event()
        self.write_inbox(event)
        vault, note = self.make_note()
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        forged = self._summ(sid)
        forged["created"] = "2099-01-01T00:00:00Z"
        draft.write_text(json.dumps(forged), encoding="utf-8")
        note_before = note.read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "credential is invalid"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=lambda root: None,
                vault=vault,
            )

        self.assertEqual(note.read_bytes(), note_before)

    def test_forged_created_cannot_retire_when_timeline_already_exists(self):
        event = self.make_event()
        self.write_inbox(event)
        vault, note = self.make_note()
        sid = worker.summary_id_for(event)
        completion = self._summ(sid)
        completion["created"] = "2026-06-20T11:00:00.000Z"
        worker.apply_summary(completion, root=self.root, run=True, note_path=note)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        forged = json.loads(draft.read_text(encoding="utf-8"))
        forged["created"] = "2099-01-01T00:00:00Z"
        draft.write_text(json.dumps(forged), encoding="utf-8")
        note_before = note.read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "credential is invalid"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=lambda root: None,
                vault=vault,
            )

        self.assertEqual(note.read_bytes(), note_before)

    def test_existing_draft_without_inbox_source_fails_closed(self):
        event = self.make_event()
        sid = worker.summary_id_for(event)
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        completion = self._summ(sid)
        completion["created"] = "2026-06-20T11:00:00.000Z"
        draft.write_text(json.dumps(completion), encoding="utf-8")

        with self.assertRaisesRegex(worker.WorkerStateError, "source is unavailable"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=lambda root: None,
            )


# --- worker drain: scaffold phase, apply phase, idempotency, lock ------------

class DrainTest(_Base):
    def _noop_ingest(self, root):
        pass

    def test_phase_a_scaffold_then_phase_b_apply(self):
        ev = self.make_event()
        self.write_inbox(ev)
        vault, note = self.make_note()
        enqueue.enqueue(ev, root=self.root)

        # phase A: drain produces a scaffold + PROMPT, event kept in inflight
        r1 = worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault)
        self.assertEqual(r1[0]["state"], "scaffolded")
        sid = worker.summary_id_for(ev)
        pdir = paths.pending_dir(self.root) / sid
        sc = json.loads((pdir / "scaffold.json").read_text(encoding="utf-8"))
        self.assertNotIn("C:/ScriptoriumDemo/projects/synthetic-xq17-calibration", json.dumps(sc, ensure_ascii=False))
        self.assertTrue((pdir / "PROMPT.md").exists())
        self.assertEqual(len(worker._read_jsonl(paths.inflight_file(self.root))), 1)

        # external fill arrives (the LLM seam) -> next drain applies
        (pdir / "fill.json").write_bytes(FIXTURE.read_bytes())
        r2 = worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault)
        self.assertEqual(r2[0]["state"], "applied")
        t = note.read_text(encoding="utf-8")
        self.assertIn(PL.entry_marker(sid), t)
        self.assertIn("ran the synclayer test suite, all green", t)
        # applied event dropped from the queue/inflight
        self.assertFalse(paths.inflight_file(self.root).exists())

    def test_draft_atomic_failure_retries_without_duplicate_timeline(self):
        event = self.make_event()
        self.write_inbox(event)
        vault, note = self.make_note()
        sid = worker.summary_id_for(event)
        enqueue.enqueue(event, root=self.root)
        worker.drain(
            root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault
        )
        pending = paths.pending_dir(self.root) / sid
        (pending / "fill.json").write_bytes(FIXTURE.read_bytes())
        draft = paths.drafts_dir(self.root) / f"{sid}.json"
        original_atomic_write = PL._atomic_write

        def fail_only_draft(path, data):
            if Path(path) == draft:
                raise OSError("private simulated draft failure")
            return original_atomic_write(path, data)

        with mock.patch.object(PL, "_atomic_write", side_effect=fail_only_draft):
            failed = worker.drain(
                root=self.root,
                run=True,
                ingest_fn=self._noop_ingest,
                vault=vault,
            )

        self.assertEqual(failed[0]["state"], "error")
        self.assertFalse(draft.exists())
        self.assertTrue(paths.inflight_file(self.root).exists())
        self.assertEqual(note.read_text(encoding="utf-8").count(PL.entry_marker(sid)), 1)

        recovered = worker.drain(
            root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault
        )
        self.assertEqual(recovered[0]["state"], "applied")
        self.assertTrue(draft.exists())
        self.assertFalse(paths.inflight_file(self.root).exists())
        self.assertEqual(note.read_text(encoding="utf-8").count(PL.entry_marker(sid)), 1)

    def test_forged_scaffold_project_fails_before_any_authoritative_write(self):
        event = self.make_event()
        self.write_inbox(event)
        vault, alpha_note = self.make_note()
        _vault, beta_note = self.make_note(project_file="beta.md")
        sid = worker.summary_id_for(event)
        pending = paths.pending_dir(self.root) / sid
        pending.mkdir(parents=True, exist_ok=True)
        scaffold = summary.build_scaffold(
            inbox_record(event["session_id"]),
            summary_id=sid,
            project_id="synthetic-xq17-calibration",
            created="2026-06-20T11:00:00Z",
            session_id=event["session_id"],
        )
        scaffold["project"] = "beta"
        (pending / "scaffold.json").write_text(json.dumps(scaffold), encoding="utf-8")
        (pending / "fill.json").write_bytes(FIXTURE.read_bytes())
        alpha_before = alpha_note.read_bytes()
        beta_before = beta_note.read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "scaffold identity"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=self._noop_ingest,
                vault=vault,
            )

        self.assertEqual(alpha_note.read_bytes(), alpha_before)
        self.assertEqual(beta_note.read_bytes(), beta_before)
        self.assertFalse((paths.drafts_dir(self.root) / f"{sid}.json").exists())

    def test_forged_scaffold_schema_or_summary_id_fails_closed(self):
        event = self.make_event()
        self.write_inbox(event)
        sid = worker.summary_id_for(event)

        for field, value in (
            ("schema_version", "summary-scaffold/0.0"),
            ("summary_id", "forged"),
            ("created", "2099-01-01T00:00:00Z"),
            ("session_id", "another-session"),
        ):
            with self.subTest(field=field):
                pending = paths.pending_dir(self.root) / sid
                pending.mkdir(parents=True, exist_ok=True)
                scaffold = summary.build_scaffold(
                    inbox_record(event["session_id"]),
                    summary_id=sid,
                    project_id="synthetic-xq17-calibration",
                    created="2026-06-20T11:00:00Z",
                    session_id=event["session_id"],
                )
                scaffold[field] = value
                (pending / "scaffold.json").write_text(
                    json.dumps(scaffold), encoding="utf-8"
                )
                (pending / "fill.json").write_bytes(FIXTURE.read_bytes())

                with self.assertRaisesRegex(worker.WorkerStateError, "scaffold identity"):
                    worker._advance(
                        event,
                        self.root,
                        run=True,
                        ingest_fn=self._noop_ingest,
                    )

                self.assertFalse((paths.drafts_dir(self.root) / f"{sid}.json").exists())
                self.assertEqual(list(paths.drafts_dir(self.root).glob("*.json")), [])

    def test_legacy_null_project_resolution_drift_fails_closed(self):
        event = self.make_event()
        event["project_id"] = None
        self.write_inbox(event)
        vault, alpha_note = self.make_note()
        _vault, beta_note = self.make_note(project_file="beta.md")
        sid = worker.summary_id_for(event)
        first = worker._advance(
            event,
            self.root,
            run=True,
            ingest_fn=self._noop_ingest,
            vault=vault,
        )
        self.assertEqual(first["state"], "scaffolded")
        projects = {
            "beta": {
                "project_id": "beta",
                "linked_repo": "C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                "_source_file": "beta.md",
            }
        }
        (self.root / "memory" / "projects.json").write_text(
            json.dumps({"projects": projects}), encoding="utf-8"
        )
        pending = paths.pending_dir(self.root) / sid
        (pending / "fill.json").write_bytes(FIXTURE.read_bytes())
        alpha_before = alpha_note.read_bytes()
        beta_before = beta_note.read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "scaffold identity"):
            worker._advance(
                event,
                self.root,
                run=True,
                ingest_fn=self._noop_ingest,
                vault=vault,
            )

        self.assertEqual(alpha_note.read_bytes(), alpha_before)
        self.assertEqual(beta_note.read_bytes(), beta_before)
        self.assertFalse((paths.drafts_dir(self.root) / f"{sid}.json").exists())

    def test_missing_scaffold_rebuild_preserves_current_binding(self):
        event = self.make_event()
        self.write_inbox(event)
        vault, note = self.make_note()
        sid = worker.summary_id_for(event)
        first = worker._advance(
            event,
            self.root,
            run=True,
            ingest_fn=self._noop_ingest,
            vault=vault,
        )
        self.assertEqual(first["state"], "scaffolded")
        pending = paths.pending_dir(self.root) / sid
        (pending / "scaffold.json").unlink()
        (pending / "fill.json").write_bytes(FIXTURE.read_bytes())

        applied = worker._advance(
            event,
            self.root,
            run=True,
            ingest_fn=self._noop_ingest,
            vault=vault,
        )

        self.assertEqual(applied["state"], "applied")
        draft = json.loads(
            (paths.drafts_dir(self.root) / f"{sid}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(draft["summary_id"], sid)
        self.assertEqual(draft["project"], "synthetic-xq17-calibration")
        self.assertIn(PL.entry_marker(sid), note.read_text(encoding="utf-8"))

    def test_ingest_seam_called_when_transcript_absent(self):
        ev = self.make_event(sid="absent-session")
        enqueue.enqueue(ev, root=self.root)
        called = []

        def fake_ingest(root):
            called.append(root)
            self.write_inbox(ev)  # the "ingest" captures the transcript

        r = worker.drain(root=self.root, run=True, ingest_fn=fake_ingest)
        self.assertEqual(called, [self.root])
        self.assertEqual(r[0]["state"], "scaffolded")

    def test_no_transcript_keeps_event(self):
        ev = self.make_event(sid="never-captured")
        enqueue.enqueue(ev, root=self.root)
        r = worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest)
        self.assertEqual(r[0]["state"], "no_transcript")
        self.assertEqual(len(worker._read_jsonl(paths.inflight_file(self.root))), 1)

    def test_dry_run_does_not_consume_queue(self):
        ev = self.make_event()
        self.write_inbox(ev)
        enqueue.enqueue(ev, root=self.root)
        r = worker.drain(root=self.root, run=False, ingest_fn=self._noop_ingest)
        self.assertEqual(r[0]["state"], "would_scaffold")
        # queue intact, nothing scaffolded
        self.assertEqual(len(worker._read_jsonl(paths.queue_file(self.root))), 1)
        self.assertFalse((paths.pending_dir(self.root)).exists())

    def test_queue_claim_failure_is_explicit_and_preserves_queue(self):
        ev = self.make_event()
        self.write_inbox(ev)
        enqueue.enqueue(ev, root=self.root)
        queued_before = worker._read_jsonl(paths.queue_file(self.root))

        with mock.patch.object(
            worker.os, "replace", side_effect=PermissionError("sharing violation")
        ):
            with self.assertRaisesRegex(worker.WorkerStateError, "could not be claimed"):
                worker.drain(
                    root=self.root,
                    run=True,
                    ingest_fn=self._noop_ingest,
                )

        self.assertEqual(
            worker._read_jsonl(paths.queue_file(self.root)), queued_before
        )
        self.assertFalse(paths.inflight_file(self.root).exists())
        self.assertFalse(paths.pending_dir(self.root).exists())

    def test_malformed_queue_snapshot_is_preserved_as_a_claim(self):
        queue = paths.queue_file(self.root)
        queue.parent.mkdir(parents=True, exist_ok=True)
        raw = b'{"session_id":"safe"}\nnot-json\n'
        queue.write_bytes(raw)

        with self.assertRaisesRegex(worker.WorkerStateError, "invalid record"):
            worker.drain(
                root=self.root,
                run=True,
                ingest_fn=self._noop_ingest,
            )

        claims = list(paths.sync_state(self.root).glob("queue.claim.*.jsonl"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].read_bytes(), raw)
        self.assertFalse(paths.inflight_file(self.root).exists())
        self.assertFalse(paths.pending_dir(self.root).exists())

    def test_existing_malformed_claim_blocks_before_claiming_new_queue(self):
        sync_state = paths.sync_state(self.root)
        sync_state.mkdir(parents=True, exist_ok=True)
        claim = sync_state / "queue.claim.legacy.jsonl"
        claim.write_bytes(b"not-json\n")
        event = self.make_event(sid="new-session")
        worker._write_jsonl(paths.queue_file(self.root), [event])
        queue_before = paths.queue_file(self.root).read_bytes()

        with self.assertRaisesRegex(worker.WorkerStateError, "invalid record"):
            worker._take_queue(self.root)

        self.assertEqual(claim.read_bytes(), b"not-json\n")
        self.assertEqual(paths.queue_file(self.root).read_bytes(), queue_before)

    def test_preview_includes_orphan_claims_without_changing_them(self):
        event = self.make_event(sid="orphan-session")
        self.write_inbox(event)
        claim = paths.sync_state(self.root) / "queue.claim.orphan.jsonl"
        worker._write_jsonl(claim, [event])
        before = claim.read_bytes()

        result = worker.drain(
            root=self.root,
            run=False,
            ingest_fn=self._noop_ingest,
        )

        self.assertEqual(result[0]["state"], "would_scaffold")
        self.assertEqual(claim.read_bytes(), before)
        self.assertFalse(paths.lock_file(self.root).exists())
        self.assertFalse(paths.pending_dir(self.root).exists())

    def test_unique_claim_never_overwrites_a_same_pid_orphan(self):
        sync_state = paths.sync_state(self.root)
        sync_state.mkdir(parents=True, exist_ok=True)
        orphan = sync_state / f"queue.claim.{os.getpid()}.jsonl"
        old_event = self.make_event(sid="old-session")
        new_event = self.make_event(sid="new-session")
        worker._write_jsonl(orphan, [old_event])
        orphan_before = orphan.read_bytes()
        worker._write_jsonl(paths.queue_file(self.root), [new_event])

        with mock.patch.object(
            worker,
            "_write_jsonl",
            side_effect=worker.WorkerStateError("simulated crash before inflight"),
        ):
            with self.assertRaises(worker.WorkerStateError):
                worker._take_queue(self.root)

        claims = list(sync_state.glob("queue.claim.*.jsonl"))
        self.assertEqual(len(claims), 2)
        self.assertEqual(orphan.read_bytes(), orphan_before)
        recovered = [item for claim in claims for item in worker._read_jsonl(claim)]
        self.assertEqual(
            {item["session_id"] for item in recovered},
            {"old-session", "new-session"},
        )

    def test_legacy_null_project_id_resolves_at_worker_time(self):
        ev = self.make_event()
        ev["project_id"] = None
        self.write_inbox(ev)
        worker._write_jsonl(paths.queue_file(self.root), [ev])

        r = worker.drain(root=self.root, run=False, ingest_fn=self._noop_ingest)

        self.assertEqual(r[0]["state"], "would_scaffold")
        self.assertEqual(r[0]["project"], "synthetic-xq17-calibration")
        self.assertIsNone(worker._read_jsonl(paths.queue_file(self.root))[0]["project_id"])

    def test_legacy_null_project_id_stays_unresolved_for_unmapped_cwd(self):
        ev = self.make_event(cwd="C:/ScriptoriumDemo/projects/synthetic-notebook")
        ev["project_id"] = None
        self.write_inbox(ev)
        worker._write_jsonl(paths.queue_file(self.root), [ev])

        r = worker.drain(root=self.root, run=False, ingest_fn=self._noop_ingest)

        self.assertEqual(r[0]["state"], "unresolved_project")
        self.assertFalse(paths.pending_dir(self.root).exists())

    def test_unregistered_explicit_project_id_stays_unresolved(self):
        event = self.make_event(cwd="C:/ScriptoriumDemo/projects/unrelated")
        event["project_id"] = "not-registered"
        self.write_inbox(event)
        worker._write_jsonl(paths.queue_file(self.root), [event])

        result = worker.drain(
            root=self.root, run=False, ingest_fn=self._noop_ingest
        )

        self.assertEqual(result[0]["state"], "unresolved_project")
        self.assertFalse(paths.pending_dir(self.root).exists())

    def test_crash_resume_is_idempotent(self):
        ev = self.make_event()
        self.write_inbox(ev)
        vault, note = self.make_note()
        sid = worker.summary_id_for(ev)
        enqueue.enqueue(ev, root=self.root)
        worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault)
        (paths.pending_dir(self.root) / sid / "fill.json").write_bytes(FIXTURE.read_bytes())
        worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault)
        # re-enqueue the SAME session (hook re-fire) and drain again
        enqueue.enqueue(ev, root=self.root)
        r = worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest, vault=vault)
        self.assertEqual(r[0]["state"], "applied")  # recognized as already applied
        t = note.read_text(encoding="utf-8")
        self.assertEqual(t.count(PL.entry_marker(sid)), 1)  # no double-logging

    def test_lockfile_serializes(self):
        with worker._Lock(paths.lock_file(self.root)):
            with self.assertRaises(worker.WorkerBusy):
                with worker._Lock(paths.lock_file(self.root)):
                    pass

    def test_unresolved_project_stays_inflight_until_mapping_exists(self):
        ev = self.make_event(cwd="C:/ScriptoriumDemo/projects/unrelated")
        self.write_inbox(ev)
        sid = worker.summary_id_for(ev)
        enqueue.enqueue(ev, root=self.root)
        unresolved = worker.drain(
            root=self.root, run=True, ingest_fn=self._noop_ingest
        )

        self.assertEqual(unresolved[0]["state"], "unresolved_project")
        self.assertTrue(paths.inflight_file(self.root).exists())
        self.assertFalse(paths.pending_dir(self.root).exists())
        self.assertFalse((paths.drafts_dir(self.root) / f"{sid}.json").exists())

        projects = {
            "recovered": {
                "schema_version": "project/1.0",
                "project_id": "recovered",
                "linked_repo": "C:/ScriptoriumDemo/projects/unrelated",
                "_source_file": "recovered.md",
            }
        }
        (self.root / "memory" / "projects.json").write_text(
            json.dumps({"projects": projects}), encoding="utf-8"
        )
        recovered = worker.drain(
            root=self.root, run=True, ingest_fn=self._noop_ingest
        )

        self.assertEqual(recovered[0]["state"], "scaffolded")
        scaffold = json.loads(
            (paths.pending_dir(self.root) / sid / "scaffold.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(scaffold["project"], "recovered")
        self.assertFalse((paths.drafts_dir(self.root) / f"{sid}.json").exists())

    def test_codex_event_scaffolds_from_codex_inbox(self):
        ev = self.make_event(sid="codex-session", cwd="C:/ScriptoriumDemo/projects/synthetic-xq17-calibration")
        ev["source"] = "codex"
        stem = worker._stem_for(ev)
        d = self.root / "inbox" / "codex"
        d.mkdir(parents=True, exist_ok=True)
        rec = inbox_record(ev["session_id"])
        rec["source"] = "codex"
        rec["conversation_id"] = f"codex-{stem}"
        (d / f"{stem}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        enqueue.enqueue(ev, root=self.root)
        r = worker.drain(root=self.root, run=True, ingest_fn=self._noop_ingest)
        self.assertEqual(r[0]["state"], "scaffolded")
        self.assertTrue((paths.pending_dir(self.root) / worker.summary_id_for(ev) / "scaffold.json").exists())


class ScanAgentsTest(_Base):
    def _rollout(self, home, name="rollout-x.jsonl", cwd="C:/ScriptoriumDemo/projects/synthetic-xq17-calibration",
                 session_id="cx-scan"):
        p = home / ".codex" / "sessions" / "2026" / "07" / "02" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "timestamp": "2026-07-02T00:00:00Z",
             "payload": {"session_id": session_id, "cwd": cwd}},
            {"type": "event_msg", "timestamp": "2026-07-02T00:00:01Z",
             "payload": {"type": "user_message", "message": "refresh board"}},
            {"type": "response_item", "timestamp": "2026-07-02T00:00:02Z",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}},
        ]
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows), encoding="utf-8")
        os.utime(p, (1000, 1000))
        return p

    def test_codex_scan_previews_then_enqueues_once(self):
        home = self.root / "home"
        self._rollout(home)
        preview = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=False)
        self.assertEqual(preview["found"], 1)
        self.assertEqual(preview["enqueued"], 0)
        self.assertFalse(paths.queue_file(self.root).exists())

        written = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=True)
        self.assertEqual(written["enqueued"], 1)
        q = worker._read_jsonl(paths.queue_file(self.root))
        self.assertEqual(q[0]["source"], "codex")
        self.assertEqual(q[0]["project_id"], "synthetic-xq17-calibration")

        again = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                       now_ts=2000, run=True)
        self.assertEqual(again["enqueued"], 0)

    def test_codex_scan_skips_unresolved_by_default(self):
        home = self.root / "home"
        self._rollout(home, cwd="C:/ScriptoriumDemo/projects/synthetic-notebook")

        preview = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=False)

        self.assertEqual(preview["found"], 0)

    def test_codex_scan_can_include_unresolved_when_requested(self):
        home = self.root / "home"
        self._rollout(home, cwd="C:/ScriptoriumDemo/projects/synthetic-notebook")

        preview = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=False, include_unresolved=True)

        self.assertEqual(preview["found"], 1)
        self.assertIsNone(preview["events"][0]["project_id"])

    def test_codex_scan_skips_desktop_by_default(self):
        home = self.root / "home"
        self._rollout(home, cwd="C:/Users/demo/Desktop/synthetic-xq17")

        preview = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=False, include_unresolved=True)

        self.assertEqual(preview["found"], 0)

    def test_codex_scan_filters_project(self):
        home = self.root / "home"
        self._rollout(home, name="rollout-a.jsonl", session_id="cx-a",
                      cwd="C:/ScriptoriumDemo/projects/synthetic-xq17-calibration")
        self._rollout(home, name="rollout-b.jsonl", session_id="cx-b",
                      cwd="C:/ScriptoriumDemo/projects/synthetic-notebook")

        preview = scan_agents.scan_codex(root=self.root, home=home, stable_seconds=60,
                                         now_ts=2000, run=False, project="synthetic-xq17-calibration",
                                         include_unresolved=True)

        self.assertEqual(preview["found"], 1)
        self.assertEqual(preview["events"][0]["project_id"], "synthetic-xq17-calibration")

    def test_codex_scan_resolves_redacted_windows_user_snapshot(self):
        home = self.root / "home"
        self._rollout(home, cwd="C:/Users/demo-a/Code/synthetic-private-project")
        projects = {
            "synthetic-private-project": {
                "project_id": "synthetic-private-project",
                "linked_repo": "C:/Users/<user>/Code/synthetic-private-project",
            }
        }

        preview = scan_agents.scan_codex(
            root=self.root,
            home=home,
            stable_seconds=60,
            now_ts=2000,
            run=False,
            projects=projects,
        )

        self.assertEqual(preview["found"], 1)
        self.assertEqual(preview["events"][0]["project_id"], "synthetic-private-project")


if __name__ == "__main__":
    unittest.main(verbosity=2)
