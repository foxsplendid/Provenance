#!/usr/bin/env python3
"""Tests for local-only experiment-run and Claim candidate ingestion."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provenance import __version__, memory as M  # noqa: E402
from provenance.ingest import evidence as E  # noqa: E402


RUN_ID = "synthetic-xq17-run-0001"
PROJECT_ID = "synthetic-xq17-calibration"
ARTIFACT_ID = "synthetic-xq17-metrics-0001"
ARTIFACT_SHA = "b" * 64


def _run(*, status="succeeded", project=PROJECT_ID):
    document = {
        "schema_version": "experiment-run/1.0",
        "run_id": RUN_ID,
        "project": project,
        "created": "2100-01-04T09:30:00Z",
        "status": status,
        "objective": "[SYNTHETIC] Exercise the local evidence registry.",
        "execution": {
            "runner": "python",
            "code_ref": "git:demo0001#scripts/run_synthetic_xq17.py",
            "environment_ref": "lock:synthetic-environment-0001",
            "parameters": {"generated_rows": 32},
            "random_seeds": {"python": 1701},
        },
        "artifacts": [
            {
                "artifact_id": ARTIFACT_ID,
                "role": "output",
                "locator": "results/generated-xq17-metrics.json",
                "sha256": ARTIFACT_SHA,
                "media_type": "application/json",
            }
        ],
        "metrics": [
            {
                "name": "synthetic-error",
                "value": 0.125,
                "unit": "generated-units",
                "direction": "minimize",
            }
        ],
        "source_session_id": "demo-session-0001",
        "generated_by": "synthetic-executor/0.1.0",
    }
    if status == "failed":
        document["failure_reason"] = "[SYNTHETIC] Evaluator returned no result."
    return document


def _claim(
    *,
    project=PROJECT_ID,
    review_state="accepted",
    locator="metrics[synthetic-error]",
    artifact_id=ARTIFACT_ID,
    sha256=ARTIFACT_SHA,
):
    document = {
        "schema_version": "claim-evidence/1.0",
        "claim_id": "synthetic-xq17-claim-0001",
        "project": project,
        "created": "2100-01-04T10:00:00Z",
        "statement": "[SYNTHETIC] The generated metric is 0.125.",
        "kind": "observation",
        "epistemic_status": "supported",
        "review_state": review_state,
        "evidence": [
            {
                "relation": "supports",
                "source_type": "experiment-run",
                "source_id": RUN_ID,
                "locator": locator,
                "artifact_id": artifact_id,
                "sha256": sha256,
                "note": "[SYNTHETIC] Read from the generated output.",
            }
        ],
        "generated_by": "synthetic-review-workflow/0.1.0",
    }
    if review_state != "draft":
        document["reviewed_at"] = "2100-01-04T10:05:00Z"
    return document


class TestEvidenceIngest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "Evidence"
        self.inputs.mkdir()
        self.run_path = self.inputs / "run.json"
        self.claim_path = self.inputs / "claim.json"
        self.run_path.write_text(json.dumps(_run()), encoding="utf-8")
        self.claim_path.write_text(json.dumps(_claim()), encoding="utf-8")
        self.memdir = self.root / "memory"

    def _write(self, path, document):
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def test_ingests_run_and_forces_producer_accepted_claim_to_local_candidate(self):
        self.assertEqual(E.ingest([self.inputs], self.memdir), (2, 0, 2))
        runs = M.load_experiment_runs(self.root)
        claims = M.load_candidate_claims(self.root)
        self.assertEqual([record["run_id"] for record in runs], [RUN_ID])
        self.assertEqual(len(claims), 1)
        candidate = claims[0]
        self.assertEqual(candidate["producer_review_state"], "accepted")
        self.assertEqual(candidate["trust_state"], "untrusted-import")
        self.assertEqual(candidate["local_review_state"], "candidate")
        self.assertEqual(candidate["content"]["review_state"], "accepted")
        self.assertNotIn("reviewed_claims", json.loads(
            (self.memdir / M.EVIDENCE_REGISTRY).read_text(encoding="utf-8")
        ))
        for record in runs + claims:
            self.assertRegex(record["source_fingerprint"], r"^[a-f0-9]{64}$")
            self.assertRegex(record["content_fingerprint"], r"^[a-f0-9]{64}$")

    def test_repeated_ingest_is_idempotent_and_byte_stable(self):
        self.assertEqual(E.ingest([self.inputs], self.memdir), (2, 0, 2))
        target = self.memdir / M.EVIDENCE_REGISTRY
        before = target.read_bytes()
        self.assertEqual(E.ingest([self.inputs], self.memdir), (0, 0, 2))
        self.assertEqual(target.read_bytes(), before)

    def test_preview_is_zero_write_and_matches_the_run_plan(self):
        self.assertEqual(E.preview([self.inputs], self.memdir), (2, 0, 2))
        self.assertFalse(self.memdir.exists())
        self.assertEqual(E.ingest([self.inputs], self.memdir), (2, 0, 2))

    def test_plan_id_changes_when_statement_changes_but_counts_do_not(self):
        first = E.preview_plan([self.inputs], self.memdir)
        claim = _claim()
        claim["statement"] = "[SYNTHETIC] Revised statement with the same counts."
        self._write(self.claim_path, claim)
        second = E.preview_plan([self.inputs], self.memdir)
        self.assertEqual(
            (first["added"], first["updated"], first["total"]),
            (second["added"], second["updated"], second["total"]),
        )
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        with self.assertRaises(E.PlanMismatch):
            E.ingest(
                [self.inputs],
                self.memdir,
                expected_plan_id=first["plan_id"],
            )
        self.assertFalse(self.memdir.exists())

    def test_same_source_fingerprint_keeps_the_existing_clean_projection(self):
        _, prior = E.normalize_document(_run())
        incoming = copy.deepcopy(prior)
        incoming["content"]["objective"] = "[REDACTED:alternate-policy]"
        incoming["content_fingerprint"] = E._semantic_fingerprint(incoming["content"])
        self.assertEqual(
            prior["source_fingerprint"],
            incoming["source_fingerprint"],
        )
        merged = E._merge_runs({RUN_ID: prior}, {RUN_ID: incoming})
        self.assertEqual(merged[RUN_ID], prior)
        self.assertFalse(E._source_changed({RUN_ID: prior}, RUN_ID, incoming))

    def test_registry_is_rebuildable_and_source_files_are_unchanged(self):
        run_before = self.run_path.read_bytes()
        claim_before = self.claim_path.read_bytes()
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        first = target.read_bytes()
        target.unlink()
        self.assertEqual(E.ingest([self.inputs], self.memdir), (2, 0, 2))
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual(self.run_path.read_bytes(), run_before)
        self.assertEqual(self.claim_path.read_bytes(), claim_before)

    def test_claim_cannot_arrive_without_its_source_run(self):
        with self.assertRaisesRegex(E.EvidenceError, "missing experiment run"):
            E.ingest([self.claim_path], self.memdir)
        self.assertFalse((self.memdir / M.EVIDENCE_REGISTRY).exists())

    def test_claim_and_run_must_share_project(self):
        self._write(self.claim_path, _claim(project="synthetic-other-project"))
        with self.assertRaisesRegex(E.EvidenceError, "same project"):
            E.ingest([self.inputs], self.memdir)
        self.assertFalse((self.memdir / M.EVIDENCE_REGISTRY).exists())

    def test_artifact_digest_must_match_run(self):
        self._write(self.claim_path, _claim(sha256="c" * 64))
        with self.assertRaisesRegex(E.EvidenceError, "digest does not match"):
            E.ingest([self.inputs], self.memdir)

    def test_claim_artifact_must_exist_in_run(self):
        self._write(
            self.claim_path,
            _claim(artifact_id="synthetic-missing-artifact", sha256="c" * 64),
        )
        with self.assertRaisesRegex(E.EvidenceError, "missing run artifact"):
            E.ingest([self.inputs], self.memdir)

    def test_locator_must_parse_and_resolve_metric(self):
        cases = (
            ("metrics.synthetic-error", "unparseable locator"),
            ("metrics[missing-metric]", "missing run metric"),
        )
        for locator, message in cases:
            with self.subTest(locator=locator):
                self._write(self.claim_path, _claim(locator=locator))
                with self.assertRaisesRegex(E.EvidenceError, message):
                    E.ingest([self.inputs], self.memdir)

    def test_artifact_locator_must_match_artifact_id(self):
        run = _run()
        run["artifacts"].append(
            {
                "artifact_id": "synthetic-log-0001",
                "role": "log",
                "locator": "logs/synthetic.log",
                "sha256": "c" * 64,
            }
        )
        self._write(self.run_path, run)
        self._write(
            self.claim_path,
            _claim(locator="artifacts[synthetic-log-0001]"),
        )
        with self.assertRaisesRegex(E.EvidenceError, "identify the same artifact"):
            E.ingest([self.inputs], self.memdir)

    def test_duplicate_run_artifact_and_metric_names_are_rejected(self):
        cases = []
        duplicate_artifact = _run()
        duplicate_artifact["artifacts"].append(copy.deepcopy(duplicate_artifact["artifacts"][0]))
        cases.append((duplicate_artifact, "duplicate artifact_id"))
        duplicate_metric = _run()
        duplicate_metric["metrics"].append(copy.deepcopy(duplicate_metric["metrics"][0]))
        cases.append((duplicate_metric, "duplicate name"))
        for document, message in cases:
            with self.subTest(message=message):
                self._write(self.run_path, document)
                with self.assertRaisesRegex(E.EvidenceError, message):
                    E.ingest([self.run_path], self.memdir)

    def test_unsupported_evidence_source_is_not_silently_unresolved(self):
        claim = _claim(review_state="draft")
        claim["evidence"][0] = {
            "relation": "supports",
            "source_type": "parsed-paper",
            "source_id": "synthetic-paper-0001",
            "locator": "sections[0]",
        }
        self._write(self.claim_path, claim)
        with self.assertRaisesRegex(E.EvidenceError, "not yet resolvable"):
            E.ingest([self.inputs], self.memdir)

    def test_terminal_run_is_immutable(self):
        E.ingest([self.run_path], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        before = target.read_bytes()
        run = _run()
        run["objective"] = "[SYNTHETIC] Mutated terminal observation."
        self._write(self.run_path, run)
        with self.assertRaisesRegex(E.EvidenceError, "terminal experiment run is immutable"):
            E.ingest([self.run_path], self.memdir)
        self.assertEqual(target.read_bytes(), before)

    def test_redacted_source_difference_cannot_bypass_terminal_immutability(self):
        first = _run()
        first["execution"]["code_ref"] = r"C:\Synthetic\First\run.py"
        self._write(self.run_path, first)
        E.ingest([self.run_path], self.memdir)
        before = (self.memdir / M.EVIDENCE_REGISTRY).read_bytes()

        second = _run()
        second["execution"]["code_ref"] = r"C:\Synthetic\Second\run.py"
        self._write(self.run_path, second)
        with self.assertRaisesRegex(E.EvidenceError, "terminal experiment run is immutable"):
            E.ingest([self.run_path], self.memdir)
        self.assertEqual((self.memdir / M.EVIDENCE_REGISTRY).read_bytes(), before)

    def test_redacted_source_difference_is_still_a_batch_conflict(self):
        first = _run()
        first["execution"]["code_ref"] = r"C:\Synthetic\First\run.py"
        second = _run()
        second["execution"]["code_ref"] = r"C:\Synthetic\Second\run.py"
        self._write(self.inputs / "run-a.json", first)
        self._write(self.inputs / "run-b.json", second)
        self.run_path.unlink()
        self.claim_path.unlink()
        with self.assertRaisesRegex(E.EvidenceError, "conflicting inputs"):
            E.ingest([self.inputs], self.memdir)
        self.assertFalse((self.memdir / M.EVIDENCE_REGISTRY).exists())

    def test_running_record_can_be_replaced_by_terminal_observation(self):
        self._write(self.run_path, _run(status="running"))
        self.assertEqual(E.ingest([self.run_path], self.memdir), (1, 0, 1))
        self._write(self.run_path, _run(status="succeeded"))
        self.assertEqual(E.ingest([self.run_path], self.memdir), (0, 1, 1))
        self.assertEqual(
            M.load_experiment_runs(self.root)[0]["content"]["status"],
            "succeeded",
        )

    def test_claim_update_remains_untrusted_candidate(self):
        E.ingest([self.inputs], self.memdir)
        claim = _claim()
        claim["statement"] = "[SYNTHETIC] Revised producer statement."
        self._write(self.claim_path, claim)
        self.assertEqual(E.ingest([self.claim_path], self.memdir), (0, 1, 2))
        candidate = M.load_candidate_claims(self.root)[0]
        self.assertEqual(candidate["local_review_state"], "candidate")
        self.assertEqual(candidate["trust_state"], "untrusted-import")

    def test_invalid_batch_does_not_publish_partial_registry(self):
        bad = _claim()
        bad["evidence"][0]["sha256"] = "not-a-digest"
        self._write(self.claim_path, bad)
        with self.assertRaises(E.EvidenceError):
            E.ingest([self.inputs], self.memdir)
        self.assertFalse((self.memdir / M.EVIDENCE_REGISTRY).exists())

    def test_failed_run_requires_failure_reason(self):
        failed = _run(status="failed")
        del failed["failure_reason"]
        self._write(self.run_path, failed)
        with self.assertRaisesRegex(E.EvidenceError, "failure_reason"):
            E.ingest([self.run_path], self.memdir)

    def test_additive_v1_minor_versions_are_accepted_and_preserved(self):
        run = _run()
        run["schema_version"] = "experiment-run/1.7"
        run["future_optional"] = {"producer": "synthetic-newer-minor"}
        claim = _claim()
        claim["schema_version"] = "claim-evidence/1.4"
        claim["future_optional"] = ["synthetic-newer-minor"]
        self._write(self.run_path, run)
        self._write(self.claim_path, claim)
        self.assertEqual(E.ingest([self.inputs], self.memdir), (2, 0, 2))
        registry = json.loads(
            (self.memdir / M.EVIDENCE_REGISTRY).read_text(encoding="utf-8")
        )
        self.assertEqual(registry["runs"][0]["schema_version"], "experiment-run/1.7")
        self.assertEqual(
            registry["candidate_claims"][0]["schema_version"],
            "claim-evidence/1.4",
        )
        self.assertIn("future_optional", registry["runs"][0]["content"])
        self.assertIn("future_optional", registry["candidate_claims"][0]["content"])

    def test_unknown_major_and_malformed_schema_versions_are_rejected(self):
        for schema_version in (
            "experiment-run/2.0",
            "experiment-run/1.beta",
            "other-format/1.0",
        ):
            with self.subTest(schema_version=schema_version):
                run = _run()
                run["schema_version"] = schema_version
                with self.assertRaises(E.EvidenceError):
                    E.normalize_document(run)

    def test_input_json_rejects_duplicate_keys_without_echoing_them(self):
        text = json.dumps(_run())
        needle = f'"run_id": "{RUN_ID}"'
        text = text.replace(
            needle,
            f'"run_id": "synthetic-first-parser-id", {needle}',
            1,
        )
        self.run_path.write_text(text, encoding="utf-8")
        with self.assertRaises(E.EvidenceError) as raised:
            E.ingest([self.run_path], self.memdir)
        self.assertIn("duplicate keys", str(raised.exception))
        self.assertNotIn("run_id", str(raised.exception))
        self.assertFalse((self.memdir / M.EVIDENCE_REGISTRY).exists())

    def test_input_json_rejects_nonstandard_nonfinite_constants(self):
        run = _run()
        run["execution"]["parameters"]["invalid"] = float("nan")
        self.run_path.write_text(json.dumps(run), encoding="utf-8")
        with self.assertRaisesRegex(E.EvidenceError, "NaN or Infinity"):
            E.ingest([self.run_path], self.memdir)

    def test_non_finite_metric_is_rejected(self):
        run = _run()
        run["metrics"][0]["value"] = float("nan")
        with self.assertRaisesRegex(E.EvidenceError, "finite number"):
            E.normalize_document(run)

    def test_dates_are_strict_rfc3339_including_optional_reviewed_at(self):
        run = _run()
        run["created"] = "2100-W01-1T09:30:00+00:00"
        with self.assertRaisesRegex(E.EvidenceError, "RFC 3339"):
            E.normalize_document(run)

        claim = _claim(review_state="draft")
        claim["reviewed_at"] = "not-a-date"
        with self.assertRaisesRegex(E.EvidenceError, "RFC 3339"):
            E.normalize_document(claim)

    def test_code_ref_is_never_executed(self):
        marker = self.root / "must-not-exist"
        run = _run()
        run["execution"]["code_ref"] = (
            f'{sys.executable} -c "from pathlib import Path; '
            f'Path({str(marker)!r}).write_text(\'bad\')"'
        )
        self._write(self.run_path, run)
        E.ingest([self.run_path], self.memdir)
        self.assertFalse(marker.exists())

    def test_private_paths_and_credentials_are_removed_from_derived_registry(self):
        secret = "-".join(("sk", "ant", "api03", "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"))
        run = _run()
        run["execution"]["parameters"]["private_path"] = (
            r"C:\Users\Researcher\Private Project\input.csv"
        )
        run["execution"]["parameters"]["credential"] = secret
        run["failure_reason"] = "log: /srv/private/project/failure.log"
        self._write(self.run_path, run)
        E.ingest([self.run_path], self.memdir)
        text = (self.memdir / M.EVIDENCE_REGISTRY).read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users", text)
        self.assertNotIn("Private Project", text)
        self.assertNotIn("/srv/private", text)
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED", text)

    def test_concurrent_registry_lock_fails_closed(self):
        target = self.memdir / M.EVIDENCE_REGISTRY
        with E._store_lock(target):
            with self.assertRaisesRegex(E.EvidenceError, "registry is busy"):
                E.ingest([self.run_path], self.memdir)
        self.assertFalse(target.exists())

    def test_tampered_local_review_state_is_rejected(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        document = json.loads(target.read_text(encoding="utf-8"))
        document["candidate_claims"][0]["local_review_state"] = "reviewed"
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(E.EvidenceError, "candidate trust boundary"):
            E.ingest([self.run_path], self.memdir)

    def test_memory_loader_missing_store_is_empty_without_writes(self):
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(M.load_experiment_runs(self.root), [])
        self.assertEqual(M.load_candidate_claims(self.root), [])
        self.assertFalse(self.memdir.exists())
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_memory_loader_rejects_linklike_parent_when_registry_is_missing(self):
        self.memdir.mkdir()
        with mock.patch(
            "provenance.ingest.research._path_is_link_or_reparse",
            side_effect=lambda path, _info: path == self.memdir,
        ):
            with self.assertRaises(M.EvidenceRegistryError):
                M.load_candidate_claims(self.root)

    def test_memory_loader_fails_closed_on_corrupt_registry(self):
        self.memdir.mkdir()
        target = self.memdir / M.EVIDENCE_REGISTRY
        target.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "unreadable"):
            M.load_experiment_runs(self.root)
        with self.assertRaisesRegex(M.EvidenceRegistryError, "unreadable"):
            M.load_candidate_claims(self.root)

    def test_memory_loader_fails_closed_on_candidate_boundary_tampering(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        document = json.loads(target.read_text(encoding="utf-8"))
        document["candidate_claims"][0]["local_review_state"] = "reviewed"
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "candidate boundary"):
            M.load_candidate_claims(self.root)

    def test_memory_loader_fails_closed_on_record_identity_tampering(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        document = json.loads(target.read_text(encoding="utf-8"))
        document["runs"][0]["content"]["run_id"] = "synthetic-other-run"
        document["runs"][0]["content_fingerprint"] = E._semantic_fingerprint(
            document["runs"][0]["content"]
        )
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "identity is inconsistent"):
            M.load_experiment_runs(self.root)

    def test_content_fingerprint_blocks_safe_field_tampering_before_reingest(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        document = json.loads(target.read_text(encoding="utf-8"))
        document["candidate_claims"][0]["content"]["statement"] = (
            "[SYNTHETIC] Tampered after ingest."
        )
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "fingerprint"):
            M.load_candidate_claims(self.root)
        with self.assertRaisesRegex(E.EvidenceError, "fingerprint"):
            E.ingest([self.run_path], self.memdir)

    def test_current_sanitizer_blocks_private_unknown_field_even_with_new_digest(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        document = json.loads(target.read_text(encoding="utf-8"))
        run = document["runs"][0]
        run["content"]["future_private_field"] = (
            r"C:\Users\SyntheticResearcher\Private Study\result.csv"
        )
        run["content_fingerprint"] = E._semantic_fingerprint(run["content"])
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "privacy boundary"):
            M.load_experiment_runs(self.root)
        with self.assertRaisesRegex(E.EvidenceError, "privacy boundary"):
            E.ingest([self.run_path], self.memdir)

    def test_unknown_private_envelope_fields_are_rejected(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        original = target.read_text(encoding="utf-8")
        private_value = r"C:\Users\PrivateResearch\debug.log"
        cases = (
            ("runs", M.load_experiment_runs),
            ("candidate_claims", M.load_candidate_claims),
        )
        for collection, loader in cases:
            with self.subTest(collection=collection):
                document = json.loads(original)
                document[collection][0]["debug_path"] = private_value
                target.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(
                    M.EvidenceRegistryError, "invalid record"
                ) as memory_error:
                    loader(self.root)
                with self.assertRaisesRegex(
                    E.EvidenceError, "invalid record shape"
                ) as ingest_error:
                    E.ingest([self.run_path], self.memdir)
                self.assertNotIn(private_value, str(memory_error.exception))
                self.assertNotIn(private_value, str(ingest_error.exception))

    def test_unknown_benign_envelope_fields_are_rejected(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        original = target.read_text(encoding="utf-8")
        cases = (
            ("runs", M.load_experiment_runs),
            ("candidate_claims", M.load_candidate_claims),
        )
        for collection, loader in cases:
            with self.subTest(collection=collection):
                document = json.loads(original)
                document[collection][0]["future_metadata"] = "synthetic"
                target.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(
                    M.EvidenceRegistryError, "invalid record"
                ):
                    loader(self.root)
                with self.assertRaisesRegex(
                    E.EvidenceError, "invalid record shape"
                ):
                    E.ingest([self.run_path], self.memdir)

    def test_registry_json_rejects_duplicate_keys_and_nonfinite_constants(self):
        E.ingest([self.inputs], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        original = target.read_text(encoding="utf-8")
        duplicate = original.replace(
            "{",
            '{"schema_version": "evidence-registry/1.0",',
            1,
        )
        target.write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "duplicate keys"):
            M.load_experiment_runs(self.root)
        with self.assertRaisesRegex(E.EvidenceError, "unreadable"):
            E.ingest([self.run_path], self.memdir)

        target.write_text(original[:-2] + ', "invalid": NaN}\\n', encoding="utf-8")
        with self.assertRaisesRegex(M.EvidenceRegistryError, "non-finite"):
            M.load_experiment_runs(self.root)

    def test_storage_error_does_not_echo_private_destination_path(self):
        private_detail = f"access denied: {self.memdir}\\evidence-registry.json"
        with mock.patch.object(E, "_atomic_write_json", side_effect=OSError(private_detail)):
            with self.assertRaises(E.EvidenceError) as raised:
                E.ingest([self.run_path], self.memdir)
        self.assertEqual(
            str(raised.exception),
            "evidence registry could not be read or updated",
        )
        self.assertNotIn(str(self.memdir), str(raised.exception))

    def test_cli_error_does_not_echo_selected_private_filename(self):
        private = self.inputs / "private-project-name.json"
        private.write_text("{", encoding="utf-8")
        captured = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        with mock.patch("sys.stderr", captured):
            self.assertEqual(E.main([str(private), "--memory-dir", str(self.memdir)]), 2)
            captured.seek(0)
            message = captured.read()
        self.assertNotIn("private-project-name", message)
        self.assertNotIn(str(self.inputs), message)
        self.assertIn("invalid evidence input #1", message)

    def test_cli_defaults_to_content_free_zero_write_preview(self):
        captured = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        with mock.patch("sys.stdout", captured):
            self.assertEqual(
                E.main(
                    [
                        str(self.inputs),
                        "--memory-dir",
                        str(self.memdir),
                        "--json",
                    ]
                ),
                0,
            )
            captured.seek(0)
            report = json.loads(captured.read())
        self.assertEqual(report["mode"], "preview")
        self.assertRegex(report["plan_id"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            {key: report[key] for key in ("added", "updated", "total")},
            {"added": 2, "updated": 0, "total": 2},
        )
        self.assertFalse(self.memdir.exists())

    def test_cli_run_without_plan_id_fails_before_any_write(self):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as captured:
            with mock.patch("sys.stderr", captured):
                self.assertEqual(
                    E.main(
                        [
                            str(self.inputs),
                            "--memory-dir",
                            str(self.memdir),
                            "--run",
                            "--json",
                        ]
                    ),
                    2,
                )
                captured.seek(0)
                report = json.loads(captured.read())
        self.assertEqual(report["error_code"], "plan-required")
        self.assertFalse(self.memdir.exists())

    def test_cli_wrong_plan_id_fails_before_any_write(self):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as captured:
            with mock.patch("sys.stderr", captured):
                self.assertEqual(
                    E.main(
                        [
                            str(self.inputs),
                            "--memory-dir",
                            str(self.memdir),
                            "--run",
                            "--plan-id",
                            "0" * 64,
                            "--json",
                        ]
                    ),
                    2,
                )
                captured.seek(0)
                report = json.loads(captured.read())
        self.assertEqual(report["error_code"], "plan-mismatch")
        self.assertFalse(self.memdir.exists())

    def test_cli_old_plan_fails_when_existing_registry_changes(self):
        preview = E.preview_plan([self.inputs], self.memdir)
        other = _run()
        other["run_id"] = "synthetic-xq17-run-0002"
        other_path = self.root / "other-run.json"
        self._write(other_path, other)
        E.ingest([other_path], self.memdir)
        target = self.memdir / M.EVIDENCE_REGISTRY
        before = target.read_bytes()

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as captured:
            with mock.patch("sys.stderr", captured):
                self.assertEqual(
                    E.main(
                        [
                            str(self.inputs),
                            "--memory-dir",
                            str(self.memdir),
                            "--run",
                            "--plan-id",
                            preview["plan_id"],
                            "--json",
                        ]
                    ),
                    2,
                )
                captured.seek(0)
                report = json.loads(captured.read())
        self.assertEqual(report["error_code"], "plan-mismatch")
        self.assertEqual(target.read_bytes(), before)

    def test_text_preview_prints_plan_id(self):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as captured:
            with mock.patch("sys.stdout", captured):
                self.assertEqual(E.main([str(self.inputs)]), 0)
                captured.seek(0)
                text = captured.read()
        self.assertRegex(text, r"plan_id=[a-f0-9]{64}")

    def test_cli_run_uses_the_preview_plan_and_writes_only_when_authorized(self):
        expected = E.preview_plan([self.inputs], self.memdir)
        captured = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        with mock.patch("sys.stdout", captured):
            self.assertEqual(
                E.main(
                    [
                        str(self.inputs),
                        "--memory-dir",
                        str(self.memdir),
                        "--run",
                        "--plan-id",
                        expected["plan_id"],
                        "--json",
                    ]
                ),
                0,
            )
            captured.seek(0)
            report = json.loads(captured.read())
        self.assertEqual(report["mode"], "run")
        self.assertEqual(report["plan_id"], expected["plan_id"])
        self.assertEqual(
            (report["added"], report["updated"], report["total"]),
            (expected["added"], expected["updated"], expected["total"]),
        )
        self.assertTrue((self.memdir / M.EVIDENCE_REGISTRY).is_file())

    def test_cli_json_error_is_content_free(self):
        private = self.inputs / "private-project-name.json"
        private.write_text("{", encoding="utf-8")
        captured = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        with mock.patch("sys.stderr", captured):
            self.assertEqual(E.main([str(private), "--json"]), 2)
            captured.seek(0)
            report = json.loads(captured.read())
        self.assertEqual(report["error_code"], "invalid-evidence")
        self.assertNotIn("private-project-name", json.dumps(report))

    def test_installed_console_script_points_to_evidence_ingest(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'prov-ingest-evidence = "provenance.ingest.evidence:main"',
            pyproject,
        )


class TestEvidenceCLIProcess(unittest.TestCase):
    def test_module_help_is_available_without_data_access(self):
        result = subprocess.run(
            [sys.executable, "-m", "provenance.ingest.evidence", "--help"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("experiment-run/claim-evidence", result.stdout)

    def test_module_version_is_available_without_inputs(self):
        result = subprocess.run(
            [sys.executable, "-m", "provenance.ingest.evidence", "--version"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
