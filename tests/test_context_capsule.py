#!/usr/bin/env python3
"""Context Capsule contract tests; synthetic files only, no private data."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provenance import context  # noqa: E402
from provenance import mcp_server  # noqa: E402


PROJECT_ID = "synthetic-alpha"
CITEKEY = "example2100SyntheticAlpha"


class TestContextCapsule(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        memory = self.root / "memory"
        memory.mkdir()

        project = {
            "schema_version": "project/1.0",
            "project_id": PROJECT_ID,
            "title": "[SYNTHETIC] Alpha project",
            "status": "planned",
            "stage": "intake",
            "priority": "medium",
            "next_actions": ["Human-authored fallback action"],
            "blocked_by": "",
            "linked_literature": ["DEMO0001", "Synthetic/Alpha"],
            "linked_repo": r"C:\Private\SyntheticAlpha",
            "updated": "2100-01-01",
            "body": (
                "Research goal: Verify a generated alpha workflow using "
                r"C:\Private\SyntheticAlpha\inputs.json and /private/synthetic/input.json"
                "\n\nHuman-authored project note.\n"
                "<!-- scriptorium:progress-log:begin -->\n"
                "<!-- entry:session-new#approved -->\n"
                "**2100-01-03** · approved\n"
                "- conclusion: duplicated approved log text\n"
                "<!-- entry:session-new -->\n"
                "**2100-01-03** · session writeback\n"
                "- Ran the generated benchmark\n"
                "- Recorded the low-risk fixture count\n"
                "<!-- scriptorium:progress-log:end -->\n"
            ),
        }
        (memory / "projects.json").write_text(
            json.dumps({"projects": {PROJECT_ID: project}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (memory / "applied_claims.json").write_text(
            json.dumps({
                "claims": {
                    PROJECT_ID: {
                        "status": "active",
                        "stage": "approved validation",
                        "next_actions": ["Run the approved synthetic check"],
                        "conclusion": "Approved synthetic conclusion",
                        "blocked_by": "Awaiting generated fixture",
                    }
                }
            }),
            encoding="utf-8",
        )
        (memory / "library.json").write_text(
            json.dumps({
                "items": [{
                    "key": "DEMO0001",
                    "citekey": CITEKEY,
                    "title": "[SYNTHETIC] Alpha reference",
                    "year": "2100",
                    "readStatus": "In Progress",
                    "folders": ["Synthetic/Alpha/Baselines"],
                    "tldr": r"Reference fixture at C:\Private Lab\papers\alpha.pdf after path",
                }]
            }),
            encoding="utf-8",
        )
        (memory / "research-artifacts.json").write_text(
            json.dumps({
                "schema_version": "research-artifacts/1.0",
                "artifacts": [
                    {
                        "artifact_id": "parsed:" + CITEKEY,
                        "artifact_type": "parsed-paper",
                        "schema_version": "parsed-paper/1.0",
                        "content": {
                            "schema_version": "parsed-paper/1.0",
                            "id": CITEKEY,
                            "created": "2100-01-02",
                            "parser": "synthetic",
                            "source_pdf": r"C:\Private\papers\alpha.pdf",
                            "metadata": {"title": "[SYNTHETIC] Parsed alpha paper"},
                            "sections": [{"heading": "Generated"}],
                            "references": [],
                            "figures": [],
                        },
                    },
                    {
                        "artifact_id": "reading:" + CITEKEY,
                        "artifact_type": "reading-note",
                        "schema_version": "reading-note/1.0",
                        "content": {
                            "schema_version": "reading-note/1.0",
                            "id": CITEKEY,
                            "created": "2100-01-02",
                            "read_status": "In Progress",
                            "stages": {"glance": {"tldr": "Candidate reading interpretation"}},
                            "sources": {"parsed_file": r"C:\Private\parsed\alpha.json"},
                        },
                    },
                    {
                        "artifact_id": "review:synthetic-alpha",
                        "artifact_type": "review",
                        "schema_version": "review/1.0",
                        "content": {
                            "schema_version": "review/1.0",
                            "direction": {"query": "Synthetic alpha direction", "created": "2100-01-03"},
                            "sections": [{"heading": "Overview", "prose": "Candidate prose"}],
                            "comparison_table": {
                                "rows": [{"citekey": CITEKEY, "cells": ["candidate"]}]
                            },
                            "gaps": ["Candidate gap from the literature review"],
                            "priority_reads": [CITEKEY],
                            "lineage_ref": r"C:\Private\lineage.json",
                        },
                    },
                    {
                        "artifact_id": "lineage:synthetic-alpha",
                        "artifact_type": "lineage-graph",
                        "schema_version": "lineage-graph/1.0",
                        "content": {
                            "schema_version": "lineage-graph/1.0",
                            "direction": {"query": "Synthetic alpha direction", "created": "2100-01-03"},
                            "nodes": [{"citekey": CITEKEY, "title": "Alpha"}],
                            "edges": [],
                        },
                    },
                    {
                        "artifact_id": "lineage:related-only-by-seventh-node",
                        "artifact_type": "lineage-graph",
                        "schema_version": "lineage-graph/1.0",
                        "content": {
                            "schema_version": "lineage-graph/1.0",
                            "direction": {"query": "Seventh-node direction", "created": "2100-01-03"},
                            "nodes": [
                                {"citekey": f"unrelated-{index}"} for index in range(6)
                            ] + [{"citekey": CITEKEY}],
                            "edges": [],
                        },
                    },
                    {
                        "artifact_id": "parsed:unrelated",
                        "artifact_type": "parsed-paper",
                        "schema_version": "parsed-paper/1.0",
                        "content": {
                            "schema_version": "parsed-paper/1.0",
                            "id": "unrelated2100Paper",
                            "created": "2100-01-02",
                            "metadata": {"title": "UNRELATED ARTIFACT"},
                        },
                    },
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        drafts = self.root / "sync-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "draft.json").write_text(json.dumps({
            "approval_state": "draft",
            "project": PROJECT_ID,
            "conclusion": "DRAFT MUST NOT APPEAR",
            "next_actions": ["DRAFT ACTION MUST NOT APPEAR"],
        }), encoding="utf-8")
        (drafts / "rejected.json").write_text(json.dumps({
            "approval_state": "rejected",
            "project": PROJECT_ID,
            "conclusion": "REJECTED MUST NOT APPEAR",
        }), encoding="utf-8")
        inbox = self.root / "inbox" / "codex"
        inbox.mkdir(parents=True)
        (inbox / "raw.json").write_text(json.dumps({
            "chat_messages": [{"text": "RAW CHAT MUST NOT APPEAR"}],
        }), encoding="utf-8")

    def _snapshot(self):
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(self.root.rglob("*")) if path.is_file()
        }

    def _mcp_call(self, name, arguments=None):
        old = os.environ.get("PROVENANCE_HOME")
        os.environ["PROVENANCE_HOME"] = str(self.root)
        try:
            response = mcp_server.handle({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            })
            return response["result"]["content"][0]["text"]
        finally:
            if old is None:
                os.environ.pop("PROVENANCE_HOME", None)
            else:
                os.environ["PROVENANCE_HOME"] = old

    def test_case_insensitive_id_and_approved_fields(self):
        capsule = context.build_capsule("SYNTHETIC-ALPHA", root=self.root)
        self.assertEqual(capsule["project"]["project_id"], PROJECT_ID)
        self.assertEqual(capsule["project"]["status"], "active")
        self.assertEqual(capsule["project"]["stage"], "approved validation")
        self.assertEqual(capsule["project"]["conclusion"], "Approved synthetic conclusion")
        self.assertEqual(capsule["next_actions"], ["Run the approved synthetic check"])
        self.assertEqual(capsule["trust"]["project_state"], "human_or_approved")
        self.assertEqual(
            capsule["trust"]["recent_progress"],
            "auto_applied_low_risk_not_approved_claims",
        )

    def test_canonical_init_note_uses_research_intuition_not_title(self):
        projects_path = self.root / "memory" / "projects.json"
        document = json.loads(projects_path.read_text(encoding="utf-8"))
        project = document["projects"][PROJECT_ID]
        project["body"] = (
            "# [SYNTHETIC] Alpha project\n\n"
            "## Research intuition\n\n"
            "Test a synthetic catalyst hypothesis.\n\n"
            "## Research question\n\n"
            "<!-- Define the research question after review. -->\n\n"
            "## Progress log\n\n"
            "<!-- scriptorium:progress-log:begin -->\n"
            "<!-- scriptorium:progress-log:end -->\n"
        )
        projects_path.write_text(json.dumps(document), encoding="utf-8")

        capsule = context.build_capsule(PROJECT_ID, root=self.root)

        self.assertEqual(
            capsule["project"]["goal"],
            "Test a synthetic catalyst hypothesis.",
        )

    def test_unknown_project(self):
        with self.assertRaises(context.ProjectNotFoundError) as raised:
            context.build_capsule("missing", root=self.root)
        self.assertEqual(str(raised.exception), "Project not found.")

    def test_unknown_project_error_is_fixed_and_bounded(self):
        requested = "private-" + ("x" * 50000)
        projects = {
            f"private-project-{index}-" + ("y" * 500): {}
            for index in range(100)
        }

        with self.assertRaises(context.ProjectNotFoundError) as raised:
            context._find_project(requested, projects)

        message = str(raised.exception)
        self.assertEqual(message, "Project not found.")
        self.assertNotIn("private-", message)
        self.assertNotIn("Known:", message)

    def test_drafts_and_rejected_claims_are_not_read(self):
        text = json.dumps(context.build_capsule(PROJECT_ID, root=self.root), ensure_ascii=False)
        self.assertNotIn("DRAFT MUST NOT APPEAR", text)
        self.assertNotIn("DRAFT ACTION MUST NOT APPEAR", text)
        self.assertNotIn("REJECTED MUST NOT APPEAR", text)
        self.assertNotIn("RAW CHAT MUST NOT APPEAR", text)
        self.assertIn("Ran the generated benchmark", text)  # low-risk timeline is allowed
        self.assertNotIn("duplicated approved log text", text)  # approved log block is not re-read

    def test_paths_are_not_exposed(self):
        capsule = context.build_capsule(PROJECT_ID, root=self.root)
        text = json.dumps(capsule, ensure_ascii=False)
        self.assertNotIn(r"C:\Private", text)
        self.assertNotIn(r"Lab\papers\alpha.pdf", text)
        self.assertNotIn("/private/synthetic", text)
        self.assertNotIn("source_pdf", text)
        self.assertNotIn("parsed_file", text)
        self.assertNotIn("linked_repo", text)
        self.assertIn("[local path omitted]", text)

    def test_file_uris_are_omitted_without_redacting_public_urls(self):
        public_url = "https://example.org/public-paper"
        drive_like_public_url = "https://example.org/C:/docs/readme"
        projects_path = self.root / "memory" / "projects.json"
        projects = json.loads(projects_path.read_text(encoding="utf-8"))
        project = projects["projects"][PROJECT_ID]
        project["goal"] = (
            "Review file:///srv/private/hidden.pdf and "
            + public_url
            + " with "
            + drive_like_public_url
        )
        project["body"] = (
            "<!-- scriptorium:progress-log:begin -->\n"
            "<!-- entry:session-file-uri -->\n"
            "**2100-01-04** - session writeback\n"
            "- Compared file://lab-host/share/Private%20Research/result.pdf "
            "with " + public_url + "\n"
            "<!-- scriptorium:progress-log:end -->\n"
        )
        projects_path.write_text(
            json.dumps(projects, ensure_ascii=False), encoding="utf-8"
        )
        claims_path = self.root / "memory" / "applied_claims.json"
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        claims["claims"][PROJECT_ID]["next_actions"] = [
            "Open FILE:///C:/Users/Demo/Private%20Research/plan.md",
            "Open file:///C:/Users/Demo/Private Research/plan.md and continue",
            "Consult " + public_url,
        ]
        claims_path.write_text(
            json.dumps(claims, ensure_ascii=False), encoding="utf-8"
        )

        capsule = context.build_capsule(PROJECT_ID, root=self.root)
        text = json.dumps(capsule, ensure_ascii=False)

        self.assertNotIn("file:", text.casefold())
        self.assertNotIn("hidden.pdf", text)
        self.assertNotIn("lab-host", text)
        self.assertNotIn("Private%20Research", text)
        self.assertNotIn("Private Research", text)
        self.assertNotIn(r"\plan.md", text)
        self.assertGreaterEqual(text.count("[local path omitted]"), 4)
        self.assertIn(public_url, capsule["project"]["goal"])
        self.assertIn(drive_like_public_url, capsule["project"]["goal"])
        self.assertTrue(
            any(public_url in item for item in capsule["next_actions"])
        )
        self.assertIn(public_url, capsule["recent_progress"][0]["items"][0])

    def test_windows_user_path_is_removed_before_pii_redaction(self):
        value = r"Inspect C:\Users\Demo\Private Research\result.json and continue"

        bounded = context._bounded(value)

        self.assertEqual(bounded, "Inspect [local path omitted]")

    def test_parenthesized_file_uri_does_not_leak_its_tail(self):
        value = "See (file:///C:/Users/Demo/Private Research/plan.md) and continue"

        bounded = context._bounded(value)

        self.assertEqual(bounded, "See ([local path omitted]) and continue")
        self.assertNotIn("Private Research", bounded)
        self.assertNotIn("plan.md", bounded)

    def test_file_uri_path_words_are_not_treated_as_prose_boundaries(self):
        value = (
            "Inspect file:///C:/Users/Demo/Research and Development/"
            "Data with Spaces/plan.md"
        )

        bounded = context._bounded(value)

        self.assertEqual(bounded, "Inspect [local path omitted]")
        self.assertNotIn("Development", bounded)
        self.assertNotIn("plan.md", bounded)

    def test_posix_path_with_spaces_does_not_leak_its_tail(self):
        value = "Inspect /home/demo/Private Research/plan.md and continue"

        bounded = context._bounded(value)

        self.assertEqual(bounded, "Inspect [local path omitted]")
        self.assertNotIn("Research", bounded)
        self.assertNotIn("plan.md", bounded)

    def test_multiline_local_paths_do_not_leak_their_first_line(self):
        value = (
            "See file:///srv/private/hidden paper.pdf\n"
            "Then inspect /home/demo/Private Research/plan.md\n"
            "| file:///srv/private/table plan.md | note |\n"
            "Next line"
        )

        bounded = context._bounded(value)

        self.assertNotIn("file:", bounded.casefold())
        self.assertNotIn("hidden paper.pdf", bounded)
        self.assertNotIn("Private Research", bounded)
        self.assertNotIn("plan.md", bounded)
        self.assertNotIn("table plan.md", bounded)
        self.assertNotIn("Demo", bounded)
        self.assertNotIn("Private Research", bounded)
        self.assertNotIn("result.json", bounded)

    def test_related_artifacts_are_reference_only(self):
        capsule = context.build_capsule(PROJECT_ID, root=self.root)
        kinds = {item["kind"] for item in capsule["research_artifacts"]}
        self.assertEqual(kinds, {"parsed-paper", "reading-note", "review", "lineage-graph"})
        self.assertTrue(any(
            item["id"] == "lineage:related-only-by-seventh-node"
            for item in capsule["research_artifacts"]
        ))
        self.assertTrue(all(item["trust"] == "reference_only"
                            for item in capsule["research_artifacts"]))
        self.assertNotIn("UNRELATED ARTIFACT", json.dumps(capsule, ensure_ascii=False))
        self.assertEqual(capsule["reference_leads"]["gaps"],
                         ["Candidate gap from the literature review"])
        rendered = context.render_markdown(capsule)
        self.assertIn("not approved claims", rendered)

    def test_idempotent_bounded_and_read_only(self):
        before = self._snapshot()
        first = context.build_capsule(PROJECT_ID, root=self.root)
        first_md = context.render_markdown(first)
        second = context.build_capsule(PROJECT_ID, root=self.root)
        after = self._snapshot()
        self.assertEqual(first, second)
        self.assertEqual(first_md, context.render_markdown(second))
        self.assertLessEqual(len(first_md), context.MAX_MARKDOWN_CHARS)
        self.assertLessEqual(
            len(json.dumps(first, ensure_ascii=False, indent=2).encode("utf-8")),
            context.MAX_JSON_BYTES,
        )
        oversized = json.loads(json.dumps(first))
        oversized["next_actions"] = ["x" * 5000] * 10
        rendered = context.render_markdown(oversized)
        self.assertLessEqual(len(rendered), context.MAX_MARKDOWN_CHARS)
        self.assertIn("Capsule truncated", rendered)
        self.assertEqual(before, after)

    def test_mcp_and_json_cli(self):
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertIn("get_context_capsule", names)
        text = self._mcp_call("get_context_capsule", {"project_id": PROJECT_ID})
        self.assertIn("Context Capsule", text)
        self.assertIn("reference only", text)

        stdout = io.StringIO()
        old = os.environ.get("PROVENANCE_HOME")
        os.environ["PROVENANCE_HOME"] = str(self.root)
        try:
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(context.main(["--project", PROJECT_ID, "--json"]), 0)
        finally:
            if old is None:
                os.environ.pop("PROVENANCE_HOME", None)
            else:
                os.environ["PROVENANCE_HOME"] = old
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["capsule_version"], context.CAPSULE_VERSION)
        self.assertEqual(payload["project"]["project_id"], PROJECT_ID)
        self.assertLessEqual(
            len(stdout.getvalue().rstrip("\n").encode("utf-8")),
            context.MAX_JSON_BYTES,
        )

    def test_json_budget_counts_pretty_printed_utf8_bytes(self):
        capsule = context.build_capsule(PROJECT_ID, root=self.root)
        template = capsule["research_artifacts"][0]
        capsule["research_artifacts"] = [
            {
                **template,
                "id": f"artifact-{index}",
                "summary": "界" * 300,
            }
            for index in range(20)
        ]

        context._fit_json(capsule)
        rendered = json.dumps(capsule, ensure_ascii=False, indent=2)

        self.assertLessEqual(
            len(rendered.encode("utf-8")),
            context.MAX_JSON_BYTES,
        )
        self.assertTrue(capsule["limits"]["truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
