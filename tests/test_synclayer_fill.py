#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the public, path-safe pending-fill entrypoint."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provenance.synclayer import paths, pending_fill, summarize_driver, summary  # noqa: E402


VALID_FILL = {
    "schema_version": "summary-fill/1.0",
    "timeline": ["validated the public pending-fill boundary"],
    "status": "active",
    "confidence": "high",
}


class PendingFillTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.summary_id = "codex-safe-session-1234abcd"
        self.pending = paths.pending_dir(self.root) / self.summary_id
        self.pending.mkdir(parents=True)
        self.scaffold = self.pending / "scaffold.json"
        self.scaffold.write_text(
            json.dumps(
                {
                    "schema_version": summary.SCAFFOLD_SCHEMA,
                    "summary_id": self.summary_id,
                    "project": "alpha",
                    "created": "2026-07-16T10:00:00+00:00",
                    "session_id": "safe-session",
                    "title": "A sanitized session",
                    "turns": [],
                }
            ),
            encoding="utf-8",
        )

    def assert_code(self, code, callback):
        with self.assertRaises(pending_fill.PendingFillError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_accepts_valid_fill_atomically_and_refuses_duplicate(self):
        result = pending_fill.submit_fill(
            self.summary_id, dict(VALID_FILL), root=self.root
        )

        self.assertEqual(result, {"state": "accepted"})
        fill_path = self.pending / "fill.json"
        self.assertEqual(
            json.loads(fill_path.read_text(encoding="utf-8")), VALID_FILL
        )
        self.assertEqual(list(self.pending.glob(".fill-*.tmp")), [])

        original = fill_path.read_bytes()
        self.assert_code(
            "fill_exists",
            lambda: pending_fill.submit_fill(
                self.summary_id, dict(VALID_FILL), root=self.root
            ),
        )
        self.assertEqual(fill_path.read_bytes(), original)

    def test_rechecks_target_after_acquiring_worker_lock(self):
        original_validate = pending_fill._validated_paths
        calls = 0

        def inject_duplicate(root, summary_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.pending / "fill.json").write_text(
                    "external writer", encoding="utf-8"
                )
            return original_validate(root, summary_id)

        with mock.patch.object(
            pending_fill, "_validated_paths", side_effect=inject_duplicate
        ):
            self.assert_code(
                "fill_exists",
                lambda: pending_fill.submit_fill(
                    self.summary_id, dict(VALID_FILL), root=self.root
                ),
            )

        self.assertEqual(
            (self.pending / "fill.json").read_text(encoding="utf-8"),
            "external writer",
        )

    def test_final_publish_never_replaces_a_racing_fill(self):
        original_publish = pending_fill._publish_no_replace
        external = b"externally-created-final-fill"

        def inject_after_final_check(binding, temp):
            binding.fill_path.write_bytes(external)
            return original_publish(binding, temp)

        with mock.patch.object(
            pending_fill,
            "_publish_no_replace",
            side_effect=inject_after_final_check,
        ):
            self.assert_code(
                "fill_exists",
                lambda: pending_fill.submit_fill(
                    self.summary_id, dict(VALID_FILL), root=self.root
                ),
            )

        self.assertEqual((self.pending / "fill.json").read_bytes(), external)
        self.assertEqual(list(self.pending.glob(".fill-*.tmp")), [])

    def test_rejects_invalid_scaffold_identity_and_project(self):
        base = json.loads(self.scaffold.read_text(encoding="utf-8"))
        for field, value in (
            ("schema_version", "summary-scaffold/9.9"),
            ("summary_id", "another-summary"),
            ("project", None),
            ("project", ""),
        ):
            with self.subTest(field=field, value=value):
                invalid = dict(base)
                invalid[field] = value
                self.scaffold.write_text(json.dumps(invalid), encoding="utf-8")
                self.assert_code(
                    "invalid_scaffold",
                    lambda: pending_fill.submit_fill(
                        self.summary_id, dict(VALID_FILL), root=self.root
                    ),
                )
                self.assertFalse((self.pending / "fill.json").exists())
        self.scaffold.write_text(json.dumps(base), encoding="utf-8")

    def test_rejects_invalid_fill_and_identity_overrides(self):
        for invalid in (
            {"schema_version": "summary-fill/1.0", "timeline": []},
            {**VALID_FILL, "project": "retargeted"},
            {**VALID_FILL, "summary_id": "forged"},
            {**VALID_FILL, "unknown_private_payload": "do not persist"},
        ):
            with self.subTest(invalid=invalid):
                self.assert_code(
                    "invalid_fill",
                    lambda: pending_fill.submit_fill(
                        self.summary_id, invalid, root=self.root
                    ),
                )
        self.assertFalse((self.pending / "fill.json").exists())

    def test_rejects_path_traversal_summary_id(self):
        self.assert_code(
            "invalid_summary_id",
            lambda: pending_fill.submit_fill(
                "../outside", dict(VALID_FILL), root=self.root
            ),
        )

    def test_reparse_metadata_is_link_like(self):
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        metadata = mock.Mock(
            st_mode=stat.S_IFDIR, st_file_attributes=reparse_flag
        )
        path = mock.Mock()
        path.is_junction.return_value = False

        with mock.patch.object(
            pending_fill.stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse_flag,
            create=True,
        ):
            self.assertTrue(pending_fill._metadata_is_link_like(path, metadata))

    @unittest.skipUnless(os.name == "nt", "Windows handle policy")
    def test_windows_binding_holds_each_directory_without_share_delete(self):
        scaffold = self.scaffold.read_bytes()
        calls = []

        def fake_open(path, **kwargs):
            calls.append((Path(path), kwargs))
            return len(calls)

        with (
            mock.patch.object(pending_fill, "_win_open", side_effect=fake_open),
            mock.patch.object(
                pending_fill, "_read_windows_handle", return_value=scaffold
            ),
            mock.patch.object(pending_fill, "_require_absent"),
        ):
            binding = pending_fill._open_windows_binding(
                self.root, self.summary_id
            )

        self.assertEqual(binding.windows_handles, [1, 2, 3, 4, 5])
        for _path, kwargs in calls[:4]:
            self.assertTrue(kwargs["directory"])
            self.assertEqual(
                kwargs["share"],
                pending_fill._FILE_SHARE_READ | pending_fill._FILE_SHARE_WRITE,
            )
            self.assertFalse(kwargs["share"] & 0x00000004)
        self.assertFalse(calls[4][1]["directory"])
        self.assertEqual(calls[4][1]["share"], pending_fill._FILE_SHARE_READ)

    @unittest.skipUnless(os.name == "nt", "Windows handle policy")
    def test_windows_pending_list_holds_root_chain_during_enumeration(self):
        opened = []
        closed = []

        def fake_open(path, **kwargs):
            opened.append((Path(path), kwargs))
            return len(opened)

        def fake_iterdir(_path):
            self.assertEqual(closed, [])
            return iter(())

        with (
            mock.patch.object(pending_fill, "_win_open", side_effect=fake_open),
            mock.patch.object(pending_fill, "_win_close", side_effect=closed.append),
            mock.patch.object(Path, "iterdir", fake_iterdir),
        ):
            self.assertEqual(pending_fill.list_pending_ids(root=self.root), [])

        self.assertEqual(len(opened), 3)
        self.assertEqual(closed, [3, 2, 1])
        for _path, kwargs in opened:
            self.assertEqual(
                kwargs["share"],
                pending_fill._FILE_SHARE_READ | pending_fill._FILE_SHARE_WRITE,
            )
            self.assertFalse(kwargs["share"] & 0x00000004)

    @unittest.skipUnless(os.name == "nt", "Windows handle policy")
    def test_windows_handle_validation_rejects_reparse_directory(self):
        info = mock.Mock(
            dwFileAttributes=(
                pending_fill._FILE_ATTRIBUTE_DIRECTORY
                | pending_fill._FILE_ATTRIBUTE_REPARSE_POINT
            )
        )

        self.assert_code(
            "unsafe_path",
            lambda: pending_fill._win_validate_info(
                info, directory=True, error_code="invalid_pending_state"
            ),
        )

    def test_rejects_reparse_scaffold_metadata(self):
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        reparse_metadata = mock.Mock(
            st_mode=stat.S_IFREG, st_file_attributes=reparse_flag
        )
        original_lstat = pending_fill._lstat

        def fake_lstat(path):
            if Path(path) == self.scaffold:
                return reparse_metadata
            return original_lstat(path)

        with (
            mock.patch.object(
                pending_fill.stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                reparse_flag,
                create=True,
            ),
            mock.patch.object(pending_fill, "_lstat", side_effect=fake_lstat),
        ):
            self.assert_code(
                "unsafe_path",
                lambda: pending_fill.submit_fill(
                    self.summary_id, dict(VALID_FILL), root=self.root
                ),
            )

    def test_rejects_broken_fill_symlink_when_supported(self):
        fill_path = self.pending / "fill.json"
        try:
            os.symlink(self.root / "missing-target.json", fill_path)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        self.assertFalse(fill_path.exists())
        self.assertTrue(fill_path.is_symlink())
        self.assert_code(
            "unsafe_path",
            lambda: pending_fill.submit_fill(
                self.summary_id, dict(VALID_FILL), root=self.root
            ),
        )

    def test_rejects_linked_pending_directory_when_supported(self):
        target = self.root / "external-pending"
        target.mkdir()
        (target / "scaffold.json").write_bytes(self.scaffold.read_bytes())
        linked_id = "codex-linked-session"
        linked = paths.pending_dir(self.root) / linked_id
        try:
            os.symlink(target, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink creation is unavailable: {exc}")

        self.assert_code(
            "unsafe_path",
            lambda: pending_fill.submit_fill(
                linked_id, dict(VALID_FILL), root=self.root
            ),
        )

    def test_pending_list_fails_closed_on_unsafe_candidate(self):
        with mock.patch.object(
            pending_fill,
            "_bound_pending_candidate",
            side_effect=pending_fill.PendingFillError("unsafe_path"),
        ):
            self.assert_code(
                "unsafe_path",
                lambda: pending_fill.list_pending_ids(root=self.root),
            )

    def test_default_root_uses_provenance_home(self):
        with mock.patch.dict(
            os.environ, {"PROVENANCE_HOME": str(self.root)}, clear=False
        ):
            self.assertEqual(
                pending_fill.list_pending_ids(), [self.summary_id]
            )

    def test_pending_json_and_fill_json_never_emit_absolute_paths(self):
        pending_stdout = io.StringIO()
        with redirect_stdout(pending_stdout):
            exit_code = summarize_driver.main(
                ["--provenance-home", str(self.root), "--json"]
            )
        self.assertEqual(exit_code, 0)
        pending_report = json.loads(pending_stdout.getvalue())
        self.assertEqual(pending_report["summary_ids"], [self.summary_id])
        self.assertNotIn(str(self.root), pending_stdout.getvalue())

        scaffold_data = json.loads(self.scaffold.read_text(encoding="utf-8"))
        scaffold_data["absolute_private_path"] = str(self.root / "do-not-emit")
        self.scaffold.write_text(json.dumps(scaffold_data), encoding="utf-8")
        get_stdout = io.StringIO()
        with redirect_stdout(get_stdout):
            exit_code = summarize_driver.main(
                [
                    self.summary_id,
                    "--provenance-home",
                    str(self.root),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        get_report = json.loads(get_stdout.getvalue())
        self.assertEqual(get_report["operation"], "pending.get")
        self.assertEqual(get_report["scaffold"]["summary_id"], self.summary_id)
        self.assertNotIn("absolute_private_path", get_report["scaffold"])
        self.assertNotIn(str(self.root), get_stdout.getvalue())

        fill_stdout = io.StringIO()
        fill_stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(VALID_FILL))),
            redirect_stdout(fill_stdout),
            redirect_stderr(fill_stderr),
        ):
            exit_code = pending_fill.main(
                [self.summary_id, "--provenance-home", str(self.root), "--json"]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(fill_stdout.getvalue())
        self.assertEqual(report["status"], "accepted")
        emitted = fill_stdout.getvalue() + fill_stderr.getvalue()
        self.assertNotIn(str(self.root), emitted)
        self.assertNotIn(self.summary_id, emitted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
