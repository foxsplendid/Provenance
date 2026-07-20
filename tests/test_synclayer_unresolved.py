import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from provenance.synclayer import paths, unresolved, worker


class UnresolvedInspectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, *, session_id="private-session", cwd="D:/private/new-study"):
        return {
            "source": "claude-code",
            "session_id": session_id,
            "transcript_path": f"D:/private/transcripts/{session_id}.jsonl",
            "cwd": cwd,
            "hook_event_name": "SessionEnd",
            "reason": "exit",
            "project_id": None,
        }

    def snapshot(self):
        out = {}
        for item in sorted(self.root.rglob("*")):
            key = item.relative_to(self.root).as_posix()
            out[key] = item.read_bytes() if item.is_file() else None
        return out

    def test_default_report_is_stable_opaque_and_read_only(self):
        event = self.event()
        paths.sync_state(self.root).mkdir(parents=True)
        worker._write_jsonl(paths.queue_file(self.root), [event])
        before = self.snapshot()

        first = unresolved.report(self.root)
        second = unresolved.report(self.root)

        self.assertEqual(first, second)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["paths"], "suppressed")
        self.assertFalse(first["writes"])
        item = first["unresolved"][0]
        self.assertEqual(set(item), {"resolution_id", "repo_label"})
        self.assertRegex(item["resolution_id"], r"^resolution-[0-9a-f]{16}$")
        self.assertRegex(item["repo_label"], r"^repo-[0-9a-f]{10}$")
        rendered = json.dumps(first)
        for private in (event["session_id"], event["transcript_path"], event["cwd"]):
            self.assertNotIn(private, rendered)
        self.assertEqual(self.snapshot(), before)

    def test_show_paths_is_explicit_and_still_omits_session_and_transcript(self):
        event = self.event()
        paths.sync_state(self.root).mkdir(parents=True)
        worker._write_jsonl(paths.inflight_file(self.root), [event])

        payload = unresolved.report(self.root, show_paths=True)

        self.assertEqual(payload["paths"], "included")
        self.assertEqual(payload["unresolved"][0]["cwd"], event["cwd"])
        rendered = json.dumps(payload)
        self.assertNotIn(event["session_id"], rendered)
        self.assertNotIn(event["transcript_path"], rendered)

    def test_deduplicates_claim_queue_and_inflight_and_excludes_resolved(self):
        unresolved_event = self.event()
        resolved_event = self.event(
            session_id="resolved-session", cwd="C:/ScriptoriumDemo/projects/registered"
        )
        resolved_event["project_id"] = "registered"
        memory = self.root / "memory"
        memory.mkdir()
        (memory / "projects.json").write_text(
            json.dumps(
                {
                    "projects": {
                        "registered": {
                            "project_id": "registered",
                            "linked_repo": "C:/ScriptoriumDemo/projects/registered",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        state = paths.sync_state(self.root)
        state.mkdir(parents=True)
        worker._write_jsonl(paths.inflight_file(self.root), [unresolved_event])
        worker._write_jsonl(
            state / "queue.claim.123.unique.jsonl", [unresolved_event]
        )
        worker._write_jsonl(
            paths.queue_file(self.root), [unresolved_event, resolved_event]
        )

        payload = unresolved.report(self.root)

        self.assertEqual(payload["count"], 1)

    def test_json_cli_failure_is_aggregate_and_does_not_rewrite_bad_state(self):
        state = paths.sync_state(self.root)
        state.mkdir(parents=True)
        queue = paths.queue_file(self.root)
        queue.write_text('{"cwd":"D:/private/broken"}\nnot-json\n', encoding="utf-8")
        before = queue.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = unresolved.main(
                ["--provenance-home", str(self.root), "--json"]
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["errors"], [{"code": "unresolved_state_failed"}])
        self.assertNotIn("D:/private/broken", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(queue.read_bytes(), before)

    def test_text_path_is_quoted_only_after_explicit_opt_in(self):
        event = self.event(cwd="D:/private/new-study\nwith-control")
        paths.sync_state(self.root).mkdir(parents=True)
        worker._write_jsonl(paths.queue_file(self.root), [event])

        safe_text = unresolved._format_text(unresolved.report(self.root))
        path_text = unresolved._format_text(
            unresolved.report(self.root, show_paths=True)
        )

        self.assertNotIn("private", safe_text)
        self.assertIn(r"\n", path_text)
        self.assertNotRegex(path_text, re.compile("new-study\nwith-control"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
