# Copyright (c) 2026, Oracle and/or its affiliates.
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from scripts.ce_merge.verify_suites import verify_suites


ROOT = Path(__file__).resolve().parents[3]


class SuiteTests(unittest.TestCase):
    def test_workflow_suites_match_public_mtr_and_exist(self):
        def suites(name):
            text = (ROOT / ".github/workflows" / name).read_text()
            values = re.findall(r"^\s+suites: ([^\n]+)$", text, re.MULTILINE)
            self.assertEqual(len(values), 4)
            return values

        configured = suites("ce-merge-validation.yml")
        self.assertEqual(configured, suites("mtr.yml"))
        for value in configured:
            with self.subTest(suites=value):
                verify_suites(ROOT, value)

    def test_public_locations_and_main(self):
        for path in ("mysql-test", "mysql-test/suite/example", "lib/mysql-test/suite/example",
                     "share/mysql-test/suite/example", "plugin/example/tests/example",
                     "plugin/example/tests/mtr"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / path).mkdir(parents=True)
                verify_suites(root, "main" if path == "mysql-test" else "example")

    def test_missing_suites_reported_even_when_another_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mysql-test").mkdir()
            with self.assertRaisesRegex(ValueError, "missing_one, missing_two"):
                verify_suites(root, "main,missing_one,missing_two")

    def test_private_suite_or_regular_file_cannot_satisfy_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "internal/mysql-test/suite/example").mkdir(parents=True)
            (root / "mysql-test/suite").mkdir(parents=True)
            (root / "mysql-test/suite/example").touch()
            with self.assertRaisesRegex(ValueError, "example"):
                verify_suites(root, "example")

    def test_empty_and_malformed_suite_lists_rejected(self):
        for value in ("", "main,", ",main", "../private", "main,,x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_suites(ROOT, value)

    def test_cli_fails_before_build_for_missing_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/ce_merge/verify_suites.py"),
                                     "--source", directory, "--suites", "component_mysql_rest_service"],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("component_mysql_rest_service", result.stderr)


if __name__ == "__main__":
    unittest.main()
