# Copyright (c) 2026, Oracle and/or its affiliates.
"""Verify actual candidate objects before any untrusted build command executes."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from scripts.ce_merge.testing import SUITES
import test_coordinator as fixtures


class CandidateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SerialTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.repo.git('clone', str(self.case.repo.path), str(self.case.root / 'trusted'))
        self.case.repo.git('-C', str(self.case.root / 'trusted'), 'checkout', self.case.repo.base)
        self.script = Path(__file__).resolve().parents[1] / 'verify_candidate.py'
        self.env = dict(os.environ, MERGE_REQUEST='a' * 64, MERGE_CANDIDATE=self.case.head,
                        MERGE_BASE=self.case.repo.base, MERGE_TREE=self.case.repo.git('rev-parse', 'HEAD^{tree}'),
                        MERGE_CHECKS=json.dumps(['mtr.yml']), MERGE_SUITES=json.dumps(SUITES), MERGE_MTR_TEST='')

    def run_verify(self, **changes):
        import sys
        return subprocess.run([sys.executable, str(self.script)], cwd=self.case.repo.path,
                              env=dict(self.env, **changes), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10).returncode

    def test_pinned_candidate_and_target_scripts_pass(self):
        self.assertEqual(self.run_verify(), 0)

    def test_changed_candidate_tree_base_or_unsafe_input_fails(self):
        for changes in (dict(MERGE_CANDIDATE=self.case.repo.base), dict(MERGE_TREE='f' * 40),
                        dict(MERGE_BASE=self.case.head), dict(MERGE_REQUEST='x;echo bad'),
                        dict(MERGE_MTR_TEST='main.1st;echo bad'), dict(MERGE_CHECKS='["other.yml"]'),
                        dict(MERGE_SUITES='{"core":"main"}')):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_verify(**changes), 0)


if __name__ == '__main__':
    unittest.main()
