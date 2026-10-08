# Copyright (c) 2026, Oracle and/or its affiliates.
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.ce_merge.mtr_rehearsal import verify_test
from scripts.ce_merge.policy import Blocked, rehearsal_test


ROOT = Path(__file__).resolve().parents[3]


class MTRRehearsalTests(unittest.TestCase):
    def test_default_and_exact_name(self):
        self.assertEqual(rehearsal_test({}), "")
        self.assertEqual(rehearsal_test({"ci_mtr_test": "main.1st"}), "main.1st")
        verify_test(ROOT, "main.1st")

    def test_options_patterns_and_paths_rejected(self):
        for value in ("--help", "main.*", "main.1st main.foo", "main.1st;true", "../1st",
                      "innodb.1st", "main.1st.test", "main.$(id)", "main."):
            with self.subTest(value=value):
                with self.assertRaises(Blocked):
                    rehearsal_test({"ci_mtr_test": value})
                with self.assertRaises(ValueError):
                    verify_test(ROOT, value)
        with self.assertRaises(Blocked):
            rehearsal_test({"ci_mtr_test": None})

    def test_missing_candidate_test_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                verify_test(Path(directory), "main.1st")

    def test_real_cli_forwards_one_test_and_propagates_runner_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "mysql-test/t").mkdir(parents=True)
            (source / "mysql-test/t/1st.test").write_text("SELECT 1;\n")
            helper = root / "trusted/scripts/ce_merge/mtr_rehearsal.py"
            helper.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts/ce_merge/mtr_rehearsal.py", helper)
            runner = root / "trusted/scripts/ci/mtr.sh"
            runner.parent.mkdir(parents=True)
            runner.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$MTR_CAPTURE\"\nexit 7\n")
            runner.chmod(0o755)
            capture, summary = root / "args", root / "summary"
            env = dict(os.environ, MTR_CAPTURE=str(capture), GITHUB_STEP_SUMMARY=str(summary))
            verify = subprocess.run([sys.executable, str(helper), "--test", "main.1st", "--verify-only"],
                                    cwd=source, env=env, capture_output=True, text=True)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertFalse(capture.exists())
            run = subprocess.run([sys.executable, str(helper), "--test", "main.1st"],
                                 cwd=source, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 7, run.stderr)
            self.assertEqual(capture.read_text().splitlines(), ["--parallel=1", "--suite=main", "main.1st"])
            self.assertIn("not full MTR suite coverage", summary.read_text())


if __name__ == "__main__":
    unittest.main()
