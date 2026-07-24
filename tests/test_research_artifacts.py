#!/usr/bin/env python3
"""Tests for explicit, local-only research-artifact ingestion."""
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
from provenance.ingest import research as R  # noqa: E402
from provenance.search import core as S  # noqa: E402


def _remove_directory_link(path):
    try:
        if os.name == "nt":
            os.rmdir(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _make_directory_link(testcase, link, target):
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            testcase.skipTest("directory junctions are unavailable")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            testcase.skipTest(f"directory symlinks are unavailable: {exc}")
    testcase.addCleanup(_remove_directory_link, link)


def _fixtures():
    secret = "-".join(("sk", "ant", "api03", "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"))
    return [
        {
            "schema_version": "parsed-paper/1.0",
            "id": "demo2026Calibration",
            "created": "2026-07-22T10:00:00Z",
            "parser": "synthetic",
            "source_pdf": r"C:\Users\demo\Private Research\papers\demo.pdf",
            "metadata": {"title": "Synthetic microthermometry calibration",
                         "authors": ["Example Author"],
                         "abstract": "Private cache: /srv/research/private/demo.txt",
                         "local_uri": "file:///srv/private/hidden-paper.pdf",
                         r"C:\Private Lab\metadata.json": "path-shaped object key",
                         secret: "credential-shaped object key"},
            "sections": [{"heading": "Method", "level": 1,
                          "text": f"Run the synthetic assay; token {secret}; "
                                  r"log C:\Private Lab\runs\demo.log after execution"}],
            "references": [],
        },
        {
            "schema_version": "reading-note/1.0",
            "id": "demo2026Calibration",
            "created": "2026-07-22T10:10:00Z",
            "read_status": "In Progress",
            "stages": {"glance": {"tldr": "A synthetic geothermometry orientation",
                                    "tags": ["petrology"],
                                    "key_findings": ["Calibration is deterministic"]}},
            "sources": {"parsed_file": r"D:\private\parsed\demo.json"},
        },
        {
            "schema_version": "review/1.0",
            "direction": {"query": "Synthetic thermobarometry", "created": "2026-07-22T10:20:00Z"},
            "sections": [{"heading": "Comparison", "prose": "Bibliometric synthesis of synthetic evidence."}],
            "gaps": ["Independent validation is missing"],
            "priority_reads": ["demo2026Calibration"],
            "lineage_ref": r"C:\Users\demo\Private Research\lineage.json",
        },
        {
            "schema_version": "lineage-graph/1.0",
            "direction": {"query": "Synthetic thermobarometry",
                          "scope_method": "synthetic own-library citation match",
                          "created": "2026-07-22T10:30:00Z"},
            "nodes": [{"citekey": "demo2026Calibration", "title": "Synthetic lineage anchor"}],
            "edges": [{"from": "demo2026Calibration", "to": "baseline2018",
                       "relation": "cites", "evidence": "Synthetic reference"}],
        },
    ]


class TestResearchArtifactIngest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "explicit-inputs"
        self.inputs.mkdir()
        for index, document in enumerate(_fixtures()):
            (self.inputs / f"artifact-{index}.json").write_text(
                json.dumps(document, ensure_ascii=False), encoding="utf-8")
        self.memdir = self.root / "memory"

    def test_ingests_all_four_contracts_and_loads_them(self):
        self.assertEqual(R.ingest([self.inputs], self.memdir), (4, 0, 4))
        artifacts = M.load_research_artifacts(self.root)
        self.assertEqual(len(artifacts), 4)
        self.assertEqual({item["artifact_type"] for item in artifacts},
                         {"parsed-paper", "reading-note", "review", "lineage-graph"})
        self.assertEqual(len({item["artifact_id"] for item in artifacts}), 4)
        self.assertTrue(all(item["content"]["schema_version"] == item["schema_version"]
                            for item in artifacts))

    def test_repeated_ingest_is_byte_stable_and_has_no_duplicates(self):
        self.assertEqual(R.ingest([self.inputs], self.memdir), (4, 0, 4))
        target = self.memdir / M.RESEARCH_ARTIFACTS
        first = target.read_bytes()
        self.assertEqual(R.ingest([self.inputs], self.memdir), (0, 0, 4))
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual(len(M.load_research_artifacts(self.root)), 4)

    def test_case_variant_citekeys_remain_distinct_across_ingest_runs(self):
        upper = self.root / "upper.json"
        lower = self.root / "lower.json"
        first = {
            "schema_version": "parsed-paper/1.0",
            "id": "Example2100CaseKey",
            "created": "2100-01-01T00:00:00Z",
        }
        second = dict(first, id="example2100casekey")
        upper.write_text(json.dumps(first), encoding="utf-8")
        lower.write_text(json.dumps(second), encoding="utf-8")

        self.assertEqual(R.ingest([upper], self.memdir), (1, 0, 1))
        self.assertEqual(R.ingest([lower], self.memdir), (1, 0, 2))
        artifacts = M.load_research_artifacts(self.root)
        self.assertEqual(
            {record["content"]["id"] for record in artifacts},
            {"Example2100CaseKey", "example2100casekey"},
        )
        self.assertEqual(len({record["artifact_id"] for record in artifacts}), 2)

    def test_case_variant_lineage_citekeys_remain_distinct_without_query(self):
        upper = _fixtures()[3]
        upper["direction"]["query"] = ""
        upper["nodes"][0]["citekey"] = "Example2100CaseKey"
        lower = json.loads(json.dumps(upper))
        lower["nodes"][0]["citekey"] = "example2100casekey"
        upper_path = self.root / "lineage-upper.json"
        lower_path = self.root / "lineage-lower.json"
        upper_path.write_text(json.dumps(upper), encoding="utf-8")
        lower_path.write_text(json.dumps(lower), encoding="utf-8")

        self.assertEqual(R.ingest([upper_path, lower_path], self.memdir), (2, 0, 2))
        artifacts = M.load_research_artifacts(self.root)
        self.assertEqual(len({record["artifact_id"] for record in artifacts}), 2)

    def test_review_and_lineage_identity_include_created(self):
        review_a = _fixtures()[2]
        review_b = json.loads(json.dumps(review_a))
        review_b["direction"]["created"] = "2100-02-01T00:00:00Z"
        review_b["sections"][0]["prose"] = "A later synthetic synthesis."
        lineage_a = _fixtures()[3]
        lineage_b = json.loads(json.dumps(lineage_a))
        lineage_b["direction"]["created"] = "2100-02-01T00:00:00Z"
        paths = []
        for index, document in enumerate((review_a, review_b, lineage_a, lineage_b)):
            path = self.root / f"versioned-{index}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths.append(path)

        self.assertEqual(R.ingest(paths, self.memdir), (4, 0, 4))
        artifacts = M.load_research_artifacts(self.root)
        self.assertEqual(len({record["artifact_id"] for record in artifacts}), 4)

        review_b["sections"][0]["prose"] = "Updated content at the same timestamp."
        paths[1].write_text(json.dumps(review_b), encoding="utf-8")
        self.assertEqual(R.ingest([paths[1]], self.memdir), (0, 1, 4))

    def test_invalid_schema_never_replaces_existing_store(self):
        R.ingest([self.inputs], self.memdir)
        target = self.memdir / M.RESEARCH_ARTIFACTS
        before = target.read_bytes()
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"schema_version": "parsed-paper/2.0",
                                   "id": "bad", "created": "2026-07-22"}), encoding="utf-8")
        with self.assertRaises(R.ArtifactError):
            R.ingest([bad], self.memdir)
        self.assertEqual(target.read_bytes(), before)

    def test_persisted_store_has_no_source_path_absolute_path_or_credential(self):
        R.ingest([self.inputs], self.memdir)
        text = (self.memdir / M.RESEARCH_ARTIFACTS).read_text(encoding="utf-8")
        self.assertNotIn(str(self.inputs), text)
        self.assertNotIn("Private Research", text)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("D:\\", text)
        self.assertNotIn("/srv/research", text)
        self.assertNotIn("Private Lab", text)
        self.assertNotIn("sk-" + "ant-api03", text)
        self.assertIn("[REDACTED", text)

    def test_concurrent_store_lock_fails_closed(self):
        target = self.memdir / M.RESEARCH_ARTIFACTS
        with R._store_lock(target):
            with self.assertRaisesRegex(R.ArtifactError, "store is busy"):
                R.ingest([self.inputs], self.memdir)
        self.assertFalse(target.exists())

    def test_independent_process_writers_do_not_lose_updates(self):
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "artifact.json").write_text(
            json.dumps(_fixtures()[0], ensure_ascii=False), encoding="utf-8"
        )
        (right / "artifact.json").write_text(
            json.dumps(_fixtures()[2], ensure_ascii=False), encoding="utf-8"
        )
        worker = """
import sys
import time
from pathlib import Path
from provenance.ingest import research

source = Path(sys.argv[1])
memory = Path(sys.argv[2])
ready = Path(sys.argv[3])
start = Path(sys.argv[4])
original_load_existing = research._load_existing

def slow_load_existing(path):
    time.sleep(0.2)
    return original_load_existing(path)

research._load_existing = slow_load_existing
ready.write_text("ready", encoding="utf-8")
while not start.exists():
    time.sleep(0.005)
busy = 0
for attempt in range(100):
    try:
        research.ingest([source], memory)
        print(busy)
        raise SystemExit(0)
    except research.ArtifactError as exc:
        if "store is busy" not in str(exc) or attempt == 99:
            raise
        busy += 1
        time.sleep(0.01)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        ready_paths = [self.root / "ready-left", self.root / "ready-right"]
        start = self.root / "start"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(source),
                    str(self.memdir),
                    str(ready),
                    str(start),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for source, ready in zip((left, right), ready_paths)
        ]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(path.exists() for path in ready_paths):
            if any(process.poll() is not None for process in processes):
                break
            time.sleep(0.01)
        both_ready = all(path.exists() for path in ready_paths)
        start.write_text("start", encoding="utf-8")
        failures = []
        busy_counts = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
            elif stdout.strip():
                busy_counts.append(int(stdout.strip().splitlines()[-1]))
        self.assertTrue(both_ready, failures)
        self.assertEqual(failures, [])
        self.assertGreaterEqual(sum(busy_counts), 1)
        artifacts = M.load_research_artifacts(self.root)
        self.assertEqual(
            {item["artifact_type"] for item in artifacts},
            {"parsed-paper", "review"},
        )
        self.assertEqual(len(artifacts), 2)

    def test_invalid_nested_shape_never_replaces_existing_store(self):
        R.ingest([self.inputs], self.memdir)
        target = self.memdir / M.RESEARCH_ARTIFACTS
        before = target.read_bytes()
        bad = self.root / "bad-review.json"
        bad.write_text(json.dumps({
            "schema_version": "review/1.0",
            "direction": {"query": "bad", "created": "2026-07-22"},
            "sections": [{"heading": "Missing prose"}],
        }), encoding="utf-8")
        with self.assertRaises(R.ArtifactError):
            R.ingest([bad], self.memdir)
        self.assertEqual(target.read_bytes(), before)

    def test_explicit_file_symlink_is_rejected_without_reading_target(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(_fixtures()[0]), encoding="utf-8")
        linked = self.inputs / "linked.json"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(R.ArtifactError, "links or reparse points"):
            R.ingest([linked], self.memdir)
        self.assertFalse((self.memdir / M.RESEARCH_ARTIFACTS).exists())

    def test_nested_symlink_fails_closed_before_store_write(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(_fixtures()[0]), encoding="utf-8")
        linked = self.inputs / "linked.json"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(R.ArtifactError, "links or reparse points"):
            R.ingest([self.inputs], self.memdir)
        self.assertFalse((self.memdir / M.RESEARCH_ARTIFACTS).exists())

    def test_ancestor_directory_link_is_rejected_before_read(self):
        outside = self.root / "outside-tree"
        child = outside / "child"
        child.mkdir(parents=True)
        (child / "artifact.json").write_text(
            json.dumps(_fixtures()[0]), encoding="utf-8"
        )
        linked = self.root / "selected-tree"
        _make_directory_link(self, linked, outside)

        with self.assertRaisesRegex(R.ArtifactError, "links or reparse points"):
            R.ingest([linked / "child"], self.memdir)
        self.assertFalse((self.memdir / M.RESEARCH_ARTIFACTS).exists())

    def test_regular_file_replacement_after_discovery_fails_closed(self):
        source = self.inputs / "artifact-0.json"
        replacement = self.root / "replacement.json"
        replacement.write_text(json.dumps(_fixtures()[1]), encoding="utf-8")
        original_input_files = R._input_files

        def replace_after_discovery(inputs, target):
            selected = original_input_files(inputs, target)
            backup = self.root / "original.json"
            source.replace(backup)
            replacement.replace(source)
            return selected

        with mock.patch.object(R, "_input_files", side_effect=replace_after_discovery):
            with self.assertRaisesRegex(R.ArtifactError, "changed before"):
                R.ingest([source], self.memdir)
        self.assertFalse((self.memdir / M.RESEARCH_ARTIFACTS).exists())

    def test_destination_directory_link_is_rejected_without_external_write(self):
        outside = self.root / "outside-memory"
        outside.mkdir()
        linked_memory = self.root / "selected-memory"
        _make_directory_link(self, linked_memory, outside)

        with self.assertRaisesRegex(R.ArtifactError, "busy or its lock is unsafe"):
            R.ingest([self.inputs / "artifact-0.json"], linked_memory)
        self.assertFalse((outside / M.RESEARCH_ARTIFACTS).exists())
        self.assertFalse((outside / f".{M.RESEARCH_ARTIFACTS}.lock").exists())

    def test_existing_target_and_lock_links_are_rejected_without_external_write(self):
        self.memdir.mkdir()
        source = self.inputs / "artifact-0.json"
        for leaf in (M.RESEARCH_ARTIFACTS, f".{M.RESEARCH_ARTIFACTS}.lock"):
            with self.subTest(leaf=leaf):
                outside = self.root / f"outside-{leaf.replace('.', '-')}"
                outside.mkdir()
                linked = self.memdir / leaf
                _make_directory_link(self, linked, outside)
                try:
                    with self.assertRaises(R.ArtifactError):
                        R.ingest([source], self.memdir)
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    _remove_directory_link(linked)

    def test_target_replacement_before_publish_fails_closed(self):
        R.ingest([self.inputs / "artifact-0.json"], self.memdir)
        target = self.memdir / M.RESEARCH_ARTIFACTS
        original_load = R._load_existing

        def replace_target(path):
            records = original_load(path)
            target.unlink()
            target.mkdir()
            return records

        with mock.patch.object(R, "_load_existing", side_effect=replace_target):
            with self.assertRaises(R.ArtifactError):
                R.ingest([self.inputs / "artifact-1.json"], self.memdir)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_windows_reparse_attribute_is_treated_as_unsafe(self):
        reparse = getattr(R.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        info = SimpleNamespace(st_mode=R.stat.S_IFREG, st_file_attributes=reparse)
        with mock.patch.object(R.stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse, create=True):
            self.assertTrue(R._is_link_or_reparse(info))

    def test_read_error_does_not_echo_private_source_path(self):
        source = self.inputs / "artifact-0.json"
        private_path = "\\".join(("C:", "Users", "Researcher", "private.json"))
        private_detail = f"access denied: {private_path}"
        with mock.patch.object(R, "_read_safe_bytes", side_effect=OSError(private_detail)):
            with self.assertRaises(R.ArtifactError) as raised:
                R.ingest([source], self.memdir)
        self.assertNotIn("C:\\Users", str(raised.exception))
        self.assertNotIn("private.json", str(raised.exception))
        self.assertFalse((self.memdir / M.RESEARCH_ARTIFACTS).exists())

    def test_file_uri_is_redacted_before_persistence_and_search(self):
        R.ingest([self.inputs], self.memdir)
        text = (self.memdir / M.RESEARCH_ARTIFACTS).read_text(encoding="utf-8")
        self.assertNotIn("file://", text)
        self.assertNotIn("hidden-paper.pdf", text)
        self.assertIn("[REDACTED:local-path]", text)

        (self.root / "inbox").mkdir()
        database = self.root / "index.db"
        S.build(self.root / "inbox", db=database)
        self.assertEqual(S.query("hidden-paper.pdf", db=database, exact=True), [])

    def test_embedded_file_uri_with_spaces_is_fully_redacted(self):
        document = _fixtures()[0]
        document["metadata"]["local_uri"] = (
            "See (file:///C:/Users/Demo/Research and Development/"
            "Data with Spaces/plan.md) and continue\n"
            "Also see file:///srv/private/hidden paper.pdf\nNext line"
        )
        document["metadata"]["table_uri"] = (
            "| file:///srv/private/table plan.md | note |"
        )
        document["metadata"]["public_url"] = "https://example.org/C:/docs/readme"

        record = R.normalize_artifact(document)
        text = json.dumps(record, ensure_ascii=False)

        self.assertNotIn("file:", text.casefold())
        self.assertNotIn("Research and Development", text)
        self.assertNotIn("Data with Spaces", text)
        self.assertNotIn("plan.md", text)
        self.assertNotIn("hidden paper.pdf", text)
        self.assertNotIn("table plan.md", text)
        self.assertIn("[REDACTED:local-path]", text)
        self.assertIn("https://example.org/C:/docs/readme", text)

    def test_store_read_error_does_not_echo_private_destination_path(self):
        target = self.memdir / M.RESEARCH_ARTIFACTS
        target.mkdir(parents=True)
        with self.assertRaises(R.ArtifactError) as raised:
            R.ingest([self.inputs / "artifact-0.json"], self.memdir)
        self.assertEqual(str(raised.exception), "existing memory store is unreadable")
        self.assertNotIn(str(self.memdir), str(raised.exception))

    def test_store_write_error_does_not_echo_private_destination_path(self):
        private_detail = rf"access denied: {self.memdir}\research-artifacts.json"
        with mock.patch.object(R, "_atomic_write_json", side_effect=OSError(private_detail)):
            with self.assertRaises(R.ArtifactError) as raised:
                R.ingest([self.inputs / "artifact-0.json"], self.memdir)
        self.assertEqual(
            str(raised.exception),
            "research artifact store could not be read or updated",
        )
        self.assertNotIn(str(self.memdir), str(raised.exception))


class TestResearchArtifactSearch(unittest.TestCase):
    def test_search_indexes_all_research_artifact_types(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        inputs = root / "inputs"
        inputs.mkdir()
        for index, document in enumerate(_fixtures()):
            (inputs / f"{index}.json").write_text(json.dumps(document), encoding="utf-8")
        R.ingest([inputs], root / "memory")
        (root / "inbox").mkdir()
        db = root / "index.db"
        S.build(root / "inbox", db=db)

        expected = {
            "microthermometry": "parsed-paper",
            "geothermometry": "reading-note",
            "Bibliometric": "review",
            "lineage anchor": "lineage-graph",
        }
        for query, artifact_type in expected.items():
            with self.subTest(query=query):
                hits = S.query(query, db=db, exact=True)
                self.assertTrue(any(hit["source"] == artifact_type for hit in hits), hits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
