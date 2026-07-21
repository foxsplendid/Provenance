#!/usr/bin/env python3
"""CLI contract tests for prov-status."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from provenance import status


class TestStatusCLI(unittest.TestCase):
    def test_version_is_exact_and_does_not_resolve_data_root(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(status.C, "data_root") as data_root:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    status.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "0.17.0\n")
        self.assertEqual(stderr.getvalue(), "")
        data_root.assert_not_called()

    def test_unknown_argument_is_a_usage_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(status.C, "data_root") as data_root:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    status.main(["--unknown"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn("unrecognized arguments: --unknown", stderr.getvalue())
        data_root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
