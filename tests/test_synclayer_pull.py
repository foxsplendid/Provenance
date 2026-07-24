#!/usr/bin/env python3
"""Tests for the public, deterministic sync pull entrypoint."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provenance import memory as M  # noqa: E402
from provenance.synclayer import approvals, enqueue, paths, progress_log, pull  # noqa: E402
from provenance.synclayer import scan_agents, worker  # noqa: E402


FILL = Path(__file__).resolve().parent / "fixtures" / "synclayer" / "fill.json"


class PullTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = Path(self._td.name)
        self.root = base / "provenance home"
        self.workspace = base / "workspace"
        self.repo = base / "linked repo"
        self.repo_path = "C:/ScriptoriumDemo/projects/alpha"
        self.root.mkdir()
        self.workspace.mkdir()
        self.repo.mkdir()

    def make_project(self):
        projects = self.workspace / "Projects"
        projects.mkdir(parents=True, exist_ok=True)
        note = projects / "alpha.md"
        note.write_text(
            "---\n"
            "schema_version: project/1.0\n"
            "project_id: alpha\n"
            "status: planned\n"
            f"linked_repo: {self.repo_path}\n"
            "---\n\n"
            "# Alpha\n\n"
            "human text\n\n"
            f"{progress_log.BEGIN_MARKER}\n"
            f"{progress_log.END_MARKER}\n",
            encoding="utf-8",
        )
        return note

    def make_event(self, session_id="session-private-id"):
        return {
            "source": "claude-code",
            "session_id": session_id,
            "transcript_path": "C:/private/transcripts/session.jsonl",
            "cwd": self.repo_path,
            "project_id": "alpha",
            "hook_event_name": "SessionEnd",
            "reason": "exit",
            "enqueued_at": "2026-07-15T00:00:00+00:00",
        }

    def write_event_and_inbox(self, event):
        worker._write_jsonl(paths.queue_file(self.root), [event])
        stem = worker._stem_for(event)
        inbox = self.root / "inbox" / "claude-code"
        inbox.mkdir(parents=True, exist_ok=True)
        record = {
            "source": "claude-code",
            "conversation_id": f"claude-code-{stem}",
            "name": "Private session",
            "created_at": "2026-07-15T00:00:00+00:00",
            "updated_at": "2026-07-15T00:01:00+00:00",
            "project": self.repo_path,
            "chat_messages": [
                {"sender": "human", "text": "TOKEN-SHOULD-NOT-LEAK"},
                {"sender": "assistant", "text": "work complete"},
            ],
        }
        (inbox / f"{stem}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        return worker.summary_id_for(event)

    @staticmethod
    def snapshot(*roots):
        out = {}
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    out[str(path.relative_to(root))] = path.read_bytes()
        return out

    def test_capabilities_json_is_stable_and_versioned(self):
        expected = {
            "dry_run_default": True,
            "explicit_run": True,
            "structured_report": True,
            "pull_lock": True,
            "codex_scan": True,
            "applies_checked_approvals": True,
            "model_calls": False,
            "installs_hooks": False,
            "network_requests": False,
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = pull.main(["--capabilities", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["format_version"], 1)
        self.assertEqual(report["operation"], "pull.capabilities")
        self.assertEqual(
            report["generated_by"], {"name": "provenance", "version": "0.18.0"}
        )
        self.assertEqual(report["action_required"], [])
        self.assertEqual(
            report["egress"],
            {
                "suite_managed": "not-requested",
                "host_managed": "not-invoked",
                "optional_connectors": "not-invoked",
            },
        )
        self.assertEqual(report["capabilities"], expected)

    def test_missing_paths_never_fall_back_to_cwd(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            report = pull.run_pull()
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["status"], "error")
        self.assertFalse((Path.cwd() / "sync-state" / "pull.lock").exists())

    def test_pull_json_stdout_is_one_structured_object(self):
        before = self.snapshot(self.root, self.workspace)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = pull.main(
                [
                    "--workspace",
                    str(self.workspace),
                    "--provenance-home",
                    str(self.root),
                    "--json",
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["operation"], "pull")
        self.assertEqual(report["status"], "planned")
        self.assertEqual(
            report["action_required"], [{"type": "run-confirmation"}]
        )
        self.assertEqual(self.snapshot(self.root, self.workspace), before)
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["portfolio"]["status"], "planned")
        self.assertEqual(stages["approvals_apply"]["status"], "planned")
        self.assertEqual(stages["approvals_refresh"]["status"], "planned")
        self.assertEqual(
            report["egress"],
            {
                "suite_managed": "not-requested",
                "host_managed": "not-invoked",
                "optional_connectors": "not-invoked",
            },
        )
        self.assertEqual(
            report["generated_by"], {"name": "provenance", "version": "0.18.0"}
        )

    def test_preview_uses_env_fallback_and_writes_no_authoritative_data(self):
        self.make_project()
        event = self.make_event()
        event["project_id"] = None
        self.write_event_and_inbox(event)
        (self.workspace / "Inbox").mkdir()
        (self.workspace / "Inbox" / "private.md").write_text(
            "# Private\n\nSECRET-NOTE-VALUE", encoding="utf-8"
        )
        before = self.snapshot(self.root, self.workspace)

        with mock.patch.dict(
            os.environ,
            {
                "PROVENANCE_HOME": str(self.root),
                "PROVENANCE_VAULT": str(self.workspace),
            },
            clear=True,
        ):
            report = pull.run_pull()

        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["summary"]["pending_fill"], 1)
        self.assertEqual(
            {item["type"] for item in report["action_required"]},
            {"run-confirmation", "agent-fill"},
        )
        self.assertEqual(self.snapshot(self.root, self.workspace), before)
        rendered = json.dumps(report, ensure_ascii=False)
        for private_value in (
            event["session_id"],
            event["transcript_path"],
            event["cwd"],
            "TOKEN-SHOULD-NOT-LEAK",
            "SECRET-NOTE-VALUE",
        ):
            self.assertNotIn(private_value, rendered)

    def test_unresolved_event_requires_project_resolution_without_summary(self):
        self.make_project()
        event = self.make_event(session_id="unresolved-private-session")
        event["project_id"] = None
        event["cwd"] = "D:/private/unmapped-research"
        summary_id = self.write_event_and_inbox(event)
        before = self.snapshot(self.root, self.workspace)

        preview = pull.run_pull(
            workspace=self.workspace,
            provenance_home=self.root,
        )

        self.assertEqual(preview["exit_code"], 0)
        self.assertEqual(preview["status"], "planned")
        self.assertEqual(preview["summary"]["unresolved_events"], 1)
        self.assertEqual(preview["summary"]["pending_fill"], 0)
        self.assertEqual(
            {item["type"] for item in preview["action_required"]},
            {"run-confirmation", "project-resolution"},
        )
        self.assertEqual(self.snapshot(self.root, self.workspace), before)

        applied = pull.run_pull(
            workspace=self.workspace,
            provenance_home=self.root,
            run=True,
        )

        self.assertEqual(applied["exit_code"], 0)
        self.assertEqual(applied["status"], "action-required")
        self.assertEqual(
            applied["action_required"],
            [{"type": "project-resolution", "count": 1}],
        )
        self.assertEqual(applied["summary"]["pending_fill"], 0)
        self.assertTrue(paths.inflight_file(self.root).exists())
        self.assertFalse((paths.pending_dir(self.root) / summary_id).exists())
        self.assertFalse((paths.drafts_dir(self.root) / f"{summary_id}.json").exists())
        rendered = json.dumps(applied, ensure_ascii=False)
        self.assertNotIn(event["session_id"], rendered)
        self.assertNotIn(event["cwd"], rendered)

    def test_partial_worker_batch_preserves_project_resolution_action(self):
        self.make_project()
        unresolved = self.make_event(session_id="unresolved-private-session")
        unresolved["project_id"] = None
        unresolved["cwd"] = "D:/private/unmapped-research"
        self.write_event_and_inbox(unresolved)
        missing_transcript = self.make_event(session_id="missing-transcript-session")
        worker._write_jsonl(
            paths.queue_file(self.root), [unresolved, missing_transcript]
        )

        report = pull.run_pull(
            workspace=self.workspace,
            provenance_home=self.root,
            run=True,
        )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["unresolved_events"], 1)
        self.assertIn(
            {"type": "project-resolution", "count": 1},
            report["action_required"],
        )

    def test_run_applies_existing_fill_then_only_checked_approval(self):
        note = self.make_project()
        event = self.make_event()
        summary_id = self.write_event_and_inbox(event)
        pending = paths.pending_dir(self.root) / summary_id
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "fill.json").write_bytes(FILL.read_bytes())

        first = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(first["status"], "action-required")
        self.assertEqual(first["summary"]["applied"], 1)
        self.assertEqual(first["summary"]["approved"], 0)
        self.assertEqual(first["summary"]["pending_approval"], 1)
        self.assertEqual(
            first["action_required"], [{"type": "human-approval", "count": 1}]
        )
        queue_path = approvals.approvals_path(self.workspace)
        queue_text = queue_path.read_text(encoding="utf-8")
        self.assertIn("- [ ] **alpha**", queue_text)
        self.assertEqual(
            note.read_text(encoding="utf-8").count(progress_log.entry_marker(summary_id)),
            1,
        )
        self.assertIn(
            "implemented the enqueue hook + cwd->project_id resolver",
            M.load_projects(self.root)["alpha"]["body"],
        )

        second = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )
        self.assertEqual(second["summary"]["approved"], 0)
        self.assertEqual(
            note.read_text(encoding="utf-8").count(progress_log.entry_marker(summary_id)),
            1,
        )

        queue_path.write_text(
            queue_text.replace("- [ ] **alpha**", "- [x] **alpha**"), encoding="utf-8"
        )
        third = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )
        self.assertEqual(third["exit_code"], 0)
        self.assertEqual(third["status"], "completed")
        self.assertEqual(third["action_required"], [])
        self.assertEqual(third["summary"]["approved"], 1)
        self.assertEqual(third["summary"]["pending_approval"], 0)
        self.assertIn("status: active", note.read_text(encoding="utf-8"))
        self.assertIn(
            progress_log.entry_marker(f"{summary_id}#approved"),
            M.load_projects(self.root)["alpha"]["body"],
        )
        self.assertNotIn(summary_id, json.dumps(third, ensure_ascii=False))

    def test_malformed_jsonl_blocks_before_any_authoritative_write(self):
        self.make_project()
        queue = paths.queue_file(self.root)
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text('{"valid": true}\nnot-json\n', encoding="utf-8")
        before = self.snapshot(self.root, self.workspace)

        report = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "blocked")
        after = self.snapshot(self.root, self.workspace)
        after.pop(str(paths.pull_lock_file(self.root).relative_to(self.root)), None)
        self.assertEqual(after, before)
        self.assertNotIn("not-json", json.dumps(report))
        self.assertFalse((self.root / "memory" / "projects.json").exists())

    def test_full_pull_lock_reports_busy_and_does_not_write(self):
        self.make_project()
        lock = paths.pull_lock_file(self.root)
        with worker._Lock(lock):
            report = pull.run_pull(
                workspace=self.workspace, provenance_home=self.root, run=True
            )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["errors"][0]["code"], "pull_busy")
        self.assertFalse((self.root / "memory" / "projects.json").exists())

    def test_pull_lock_release_failure_is_reported_and_left_for_review(self):
        self.make_project()
        lock = paths.pull_lock_file(self.root)
        with mock.patch.object(
            worker,
            "_release_file_lock",
            side_effect=PermissionError("private sharing violation"),
        ):
            report = pull.run_pull(
                workspace=self.workspace, provenance_home=self.root, run=True
            )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["errors"][-1]["code"], "lock_release_failed")
        self.assertTrue(lock.exists())
        self.assertNotIn("private sharing violation", json.dumps(report))

    def test_pull_lock_acquire_failure_is_not_misreported_as_release_failure(self):
        self.make_project()
        with mock.patch.object(
            worker,
            "_acquire_file_lock",
            side_effect=worker.WorkerStateError("private acquire failure"),
        ):
            report = pull.run_pull(
                workspace=self.workspace, provenance_home=self.root, run=True
            )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["errors"][-1]["code"], "worker_state_failed")
        self.assertNotIn("private acquire failure", json.dumps(report))

    def test_worker_event_error_makes_pull_partial_without_identifier_leak(self):
        self.make_project()
        with mock.patch.object(
            worker,
            "drain",
            return_value=[{"summary_id": "private-session", "state": "error"}],
        ):
            report = pull.run_pull(
                workspace=self.workspace, provenance_home=self.root, run=False
            )
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "partial")
        self.assertNotIn("private-session", json.dumps(report))

    def test_worker_state_failure_blocks_without_claiming_completion(self):
        self.make_project()
        with mock.patch.object(
            worker,
            "drain",
            side_effect=worker.WorkerStateError("private sharing violation"),
        ):
            report = pull.run_pull(
                workspace=self.workspace, provenance_home=self.root, run=True
            )

        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["errors"][-1]["code"], "worker_state_failed")
        self.assertNotIn("private sharing violation", json.dumps(report))

    def test_pull_scans_codex_and_reports_only_counts_even_with_hook_guard(self):
        self.make_project()
        codex_home = self.root.parent / "codex home"
        rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "07"
            / "15"
            / "rollout-public.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "session_meta",
                "timestamp": "2026-07-15T00:00:00Z",
                "payload": {
                    "session_id": "codex-private-session",
                    "cwd": self.repo_path,
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-15T00:00:01Z",
                "payload": {"type": "user_message", "message": "private request"},
            },
        ]
        rollout.write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
        )
        stable_time = time.time() - 600
        os.utime(rollout, (stable_time, stable_time))
        event = scan_agents.codex_event(
            rollout, now_iso="2026-07-15T00:02:00+00:00"
        )
        stem = worker._stem_for(event)
        inbox = self.root / "inbox" / "codex"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{stem}.json").write_text(
            json.dumps(
                {
                    "source": "codex",
                    "conversation_id": f"codex-{stem}",
                    "name": "Codex session",
                    "created_at": "2026-07-15T00:00:00+00:00",
                    "updated_at": "2026-07-15T00:01:00+00:00",
                    "project": self.repo_path,
                    "chat_messages": [
                        {"sender": "human", "text": "private request"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {enqueue.NO_ENQUEUE_ENV: "1"}, clear=False):
            report = pull.run_pull(
                workspace=self.workspace,
                provenance_home=self.root,
                run=True,
                scan_codex=True,
                codex_home=codex_home,
                project="alpha",
            )

        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "action-required")
        self.assertEqual(report["summary"]["codex_found"], 1)
        self.assertEqual(report["summary"]["codex_enqueued"], 1)
        self.assertEqual(report["summary"]["pending_fill"], 1)
        self.assertEqual(
            report["action_required"], [{"type": "agent-fill", "count": 1}]
        )
        rendered = json.dumps(report)
        self.assertNotIn("codex-private-session", rendered)
        self.assertNotIn(self.repo_path, rendered)

    def test_project_filter_only_limits_codex_discovery(self):
        self.make_project()
        self.write_event_and_inbox(self.make_event())
        (self.workspace / "Inbox").mkdir()
        (self.workspace / "Inbox" / "context.md").write_text(
            "# Context\n\nworkspace-wide note", encoding="utf-8"
        )
        codex_home = self.root.parent / "codex home"
        rollout = (
            codex_home
            / "sessions"
            / "2026"
            / "07"
            / "15"
            / "rollout-filtered.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-15T00:00:00Z",
                        "payload": {
                            "session_id": "codex-filtered-session",
                            "cwd": self.repo_path,
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-15T00:00:01Z",
                        "payload": {"type": "user_message", "message": "private"},
                    },
                )
            ),
            encoding="utf-8",
        )
        stable_time = time.time() - 600
        os.utime(rollout, (stable_time, stable_time))

        report = pull.run_pull(
            workspace=self.workspace,
            provenance_home=self.root,
            scan_codex=True,
            codex_home=codex_home,
            project="different-project",
        )

        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["summary"]["codex_found"], 0)
        self.assertEqual(report["summary"]["projects"], 1)
        self.assertEqual(report["summary"]["notes_planned"], 1)
        self.assertEqual(report["summary"]["worker_events"], 1)
        self.assertEqual(report["summary"]["pending_fill"], 1)

    def test_provenance_home_must_be_outside_workspace(self):
        nested = self.workspace / "private provenance"
        nested.mkdir()
        for unsafe_home in (self.workspace, nested):
            with self.subTest(unsafe_home=unsafe_home.name):
                report = pull.run_pull(
                    workspace=self.workspace,
                    provenance_home=unsafe_home,
                    run=True,
                )
                self.assertEqual(report["exit_code"], 2)
                self.assertEqual(report["status"], "error")
                self.assertEqual(
                    report["errors"],
                    [
                        {
                            "code": "unsafe_data_root",
                            "message": "Provenance home must be outside the workspace.",
                        }
                    ],
                )
                self.assertFalse(paths.pull_lock_file(unsafe_home).exists())
                rendered = json.dumps(report, ensure_ascii=False)
                self.assertNotIn(str(unsafe_home), rendered)

    def test_exit_zero_status_is_mode_specific(self):
        empty_preview = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root
        )
        run_completed = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )

        self.make_project()
        self.write_event_and_inbox(self.make_event())
        preview_planned = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root
        )
        run_action_required = pull.run_pull(
            workspace=self.workspace, provenance_home=self.root, run=True
        )

        reports = (
            empty_preview,
            run_completed,
            preview_planned,
            run_action_required,
        )
        for report in reports:
            self.assertEqual(report["exit_code"], 0)
            self.assertIsInstance(report["action_required"], list)
            self.assertEqual(
                set(report["egress"]),
                {"suite_managed", "host_managed", "optional_connectors"},
            )
        self.assertEqual(empty_preview["status"], "planned")
        self.assertEqual(
            empty_preview["action_required"], [{"type": "run-confirmation"}]
        )
        self.assertEqual(preview_planned["status"], "planned")
        self.assertEqual(run_completed["status"], "completed")
        self.assertEqual(run_action_required["status"], "action-required")


class GuardAndWorkerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "root"
        self.home = Path(self._td.name) / "home"
        self.repo = Path(self._td.name) / "repo"
        self.root.mkdir()
        self.repo.mkdir()

    def write_rollout(self):
        rollout = self.home / ".codex" / "sessions" / "2026" / "07" / "15" / "rollout-x.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "session_meta",
                "timestamp": "2026-07-15T00:00:00Z",
                "payload": {"session_id": "codex-session", "cwd": self.repo.as_posix()},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-15T00:00:01Z",
                "payload": {"type": "user_message", "message": "refresh"},
            },
        ]
        rollout.write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
        )
        os.utime(rollout, (1000, 1000))
        return rollout

    def test_hook_guard_does_not_suppress_explicit_codex_scan(self):
        self.write_rollout()
        projects = {
            "alpha": {"project_id": "alpha", "linked_repo": self.repo.as_posix()}
        }
        with mock.patch.dict(os.environ, {enqueue.NO_ENQUEUE_ENV: "1"}, clear=False):
            enqueue.enqueue(
                {"session_id": "hook", "cwd": self.repo.as_posix()}, root=self.root
            )
            self.assertFalse(paths.queue_file(self.root).exists())
            scanned = scan_agents.scan_codex(
                root=self.root,
                home=self.home,
                stable_seconds=60,
                now_ts=2000,
                run=True,
                projects=projects,
            )
        self.assertEqual(scanned["enqueued"], 1)
        self.assertEqual(len(worker._read_jsonl(paths.queue_file(self.root))), 1)

    def test_actual_codex_home_reads_sessions_directly(self):
        rollout = (
            self.home
            / "sessions"
            / "2026"
            / "07"
            / "15"
            / "rollout-codex-home.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-15T00:00:00Z",
                        "payload": {
                            "session_id": "codex-home-session",
                            "cwd": self.repo.as_posix(),
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-15T00:00:01Z",
                        "payload": {"type": "user_message", "message": "refresh"},
                    },
                )
            ),
            encoding="utf-8",
        )
        os.utime(rollout, (1000, 1000))
        projects = {
            "alpha": {"project_id": "alpha", "linked_repo": self.repo.as_posix()}
        }

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.home / "wrong")}, clear=False
        ):
            scanned = scan_agents.scan_codex(
                root=self.root,
                codex_home=self.home,
                stable_seconds=60,
                now_ts=2000,
                projects=projects,
            )

        self.assertEqual(scanned["found"], 1)

    def test_legacy_profile_home_still_derives_dot_codex(self):
        self.write_rollout()
        projects = {
            "alpha": {"project_id": "alpha", "linked_repo": self.repo.as_posix()}
        }

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.home / "wrong")}, clear=False
        ):
            scanned = scan_agents.scan_codex(
                root=self.root,
                home=self.home,
                stable_seconds=60,
                now_ts=2000,
                projects=projects,
            )

        self.assertEqual(scanned["found"], 1)

    def test_codex_home_environment_precedes_profile_fallback(self):
        rollout = (
            self.home
            / "sessions"
            / "2026"
            / "07"
            / "15"
            / "rollout-env.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-15T00:00:00Z",
                        "payload": {
                            "session_id": "codex-env-session",
                            "cwd": self.repo.as_posix(),
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-15T00:00:01Z",
                        "payload": {"type": "user_message", "message": "refresh"},
                    },
                )
            ),
            encoding="utf-8",
        )
        os.utime(rollout, (1000, 1000))
        projects = {
            "alpha": {"project_id": "alpha", "linked_repo": self.repo.as_posix()}
        }

        with mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.home), "USERPROFILE": str(self.home / "wrong")},
            clear=False,
        ):
            scanned = scan_agents.scan_codex(
                root=self.root,
                stable_seconds=60,
                now_ts=2000,
                projects=projects,
            )

        self.assertEqual(scanned["found"], 1)

    def test_default_profile_derives_dot_codex_without_codex_home(self):
        self.write_rollout()
        projects = {
            "alpha": {"project_id": "alpha", "linked_repo": self.repo.as_posix()}
        }

        with mock.patch.dict(
            os.environ, {"USERPROFILE": str(self.home)}, clear=True
        ):
            scanned = scan_agents.scan_codex(
                root=self.root,
                stable_seconds=60,
                now_ts=2000,
                projects=projects,
            )

        self.assertEqual(scanned["found"], 1)

    def test_default_ingest_raises_on_subprocess_failure(self):
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=7)):
            with self.assertRaisesRegex(RuntimeError, "exit code 7"):
                worker._default_ingest(self.root)

    def test_lock_release_failure_is_not_silently_ignored(self):
        lock = paths.lock_file(self.root)
        with mock.patch.object(
            worker,
            "_release_file_lock",
            side_effect=PermissionError("sharing violation"),
        ):
            with self.assertRaisesRegex(worker.WorkerStateError, "could not be released"):
                with worker._Lock(lock):
                    pass

        self.assertTrue(lock.exists())

    def test_legacy_lock_file_fails_closed_without_being_deleted(self):
        lock = paths.lock_file(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"123")

        with self.assertRaises(worker.WorkerBusy):
            with worker._Lock(lock):
                pass

        self.assertEqual(lock.read_bytes(), b"123")

    def test_partial_protocol_marker_fails_closed_without_being_changed(self):
        lock = paths.lock_file(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        partial = worker.LOCK_MAGIC[:7]
        lock.write_bytes(partial)

        with self.assertRaises(worker.WorkerBusy):
            with worker._Lock(lock):
                pass

        self.assertEqual(lock.read_bytes(), partial)

    def test_interrupted_first_publication_requires_explicit_recovery(self):
        lock = paths.lock_file(self.root)
        partial = worker.LOCK_MAGIC[:7]

        def interrupt(fd):
            os.write(fd, partial)
            os.fsync(fd)
            raise OSError("simulated initialization interruption")

        with mock.patch.object(worker, "_initialize_lock_file", side_effect=interrupt):
            with self.assertRaisesRegex(worker.WorkerStateError, "initialized"):
                with worker._Lock(lock):
                    pass

        self.assertEqual(lock.read_bytes(), partial)
        with self.assertRaises(worker.WorkerBusy):
            with worker._Lock(lock):
                pass

        lock.unlink()
        with worker._Lock(lock):
            pass
        self.assertEqual(lock.read_bytes(), worker.LOCK_MAGIC)

    def test_existing_empty_legacy_lock_fails_closed_without_adoption(self):
        lock = paths.lock_file(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(lock),
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        try:
            with self.assertRaises(worker.WorkerBusy):
                with worker._Lock(lock):
                    pass
        finally:
            os.close(fd)

        self.assertEqual(lock.read_bytes(), b"")

    def test_creator_publishes_magic_before_attempting_kernel_lock(self):
        lock = paths.lock_file(self.root)

        def lose_first_race(fd):
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(os.read(fd, len(worker.LOCK_MAGIC) + 1), worker.LOCK_MAGIC)
            raise worker.WorkerBusy("simulated contender")

        with mock.patch.object(worker, "_acquire_file_lock", side_effect=lose_first_race):
            with self.assertRaises(worker.WorkerBusy):
                with worker._Lock(lock):
                    pass

        self.assertEqual(lock.read_bytes(), worker.LOCK_MAGIC)
        with worker._Lock(lock):
            pass

    def test_sentinel_lock_does_not_block_creator_marker_write(self):
        lock = paths.lock_file(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        creator_fd = os.open(
            str(lock),
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        contender_fd = os.open(
            str(lock), os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
        try:
            worker._acquire_file_lock(contender_fd)
            worker._initialize_lock_file(creator_fd)
            os.lseek(creator_fd, 0, os.SEEK_SET)
            self.assertEqual(
                os.read(creator_fd, len(worker.LOCK_MAGIC)), worker.LOCK_MAGIC
            )
        finally:
            worker._release_file_lock(contender_fd)
            os.close(contender_fd)
            os.close(creator_fd)

        with worker._Lock(lock):
            pass
        self.assertEqual(lock.read_bytes(), worker.LOCK_MAGIC)

    def test_lock_initialization_completes_short_writes(self):
        lock = paths.lock_file(self.root)
        original_write = os.write

        def short_write(fd, data):
            return original_write(fd, data[:3])

        with mock.patch("os.write", side_effect=short_write):
            with worker._Lock(lock):
                pass

        self.assertEqual(lock.read_bytes(), worker.LOCK_MAGIC)

    def test_kernel_lock_recovers_after_holder_is_killed(self):
        lock = paths.lock_file(self.root)
        ready = self.root / "holder-ready"
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from provenance.synclayer.worker import _Lock\n"
            "with _Lock(Path(sys.argv[1])):\n"
            "    Path(sys.argv[2]).write_text('ready', encoding='ascii')\n"
            "    time.sleep(30)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock), str(ready)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())

        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            self.fail("lock holder did not start")

        with self.assertRaises(worker.WorkerBusy):
            with worker._Lock(lock):
                pass

        process.kill()
        process.wait(timeout=5)
        acquired = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with worker._Lock(lock):
                    acquired = True
                break
            except worker.WorkerBusy:
                time.sleep(0.05)

        self.assertTrue(acquired)
        self.assertEqual(lock.read_bytes(), worker.LOCK_MAGIC)

    def test_release_failure_is_secondary_to_the_body_error(self):
        lock = paths.lock_file(self.root)
        with self.assertRaisesRegex(ValueError, "primary failure") as caught:
            with mock.patch.object(
                worker,
                "_release_file_lock",
                side_effect=PermissionError("sharing violation"),
            ):
                with worker._Lock(lock):
                    raise ValueError("primary failure")

        self.assertTrue(
            any("failed to release" in note for note in getattr(caught.exception, "__notes__", []))
        )

    def test_worker_main_returns_nonzero_for_busy_and_event_error(self):
        output = io.StringIO()
        with mock.patch.object(worker, "drain", side_effect=worker.WorkerBusy("busy")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(worker.main(), 1)
        with mock.patch.object(
            worker, "drain", return_value=[{"summary_id": "private", "state": "error"}]
        ):
            with contextlib.redirect_stdout(output):
                self.assertEqual(worker.main(), 1)
        with mock.patch.object(
            worker,
            "drain",
            side_effect=worker.WorkerStateError("private sharing violation"),
        ):
            with contextlib.redirect_stdout(output):
                self.assertEqual(worker.main(), 1)
        self.assertNotIn("private sharing violation", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
