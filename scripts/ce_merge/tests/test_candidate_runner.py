# Copyright (c) 2026, Oracle and/or its affiliates.
"""Verify actual candidate objects before any untrusted build command executes."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import unittest

from scripts.ce_merge.testing import SUITES
import test_coordinator as fixtures


class CandidateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SerialTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        op = self.case.prepare()
        candidate = op['data']['steps'][1]['after']
        self.case.repo.git('clone', str(self.case.repo.path), str(self.case.root / 'trusted'))
        self.case.repo.git('-C', str(self.case.root / 'trusted'), 'checkout', self.case.repo.base)
        self.case.repo.git('remote', 'add', 'origin', str(self.case.remote))
        state = self.case.root / 'state'
        self.case.repo.git('init', '-b', 'ce-merge-state', str(state))
        self.case.repo.git('-C', str(state), 'config', 'user.name', 'Test')
        self.case.repo.git('-C', str(state), 'config', 'user.email', 'test@example.com')
        self.bundle = state / 'journal/bundles' / self.case.coordinator.bundle(op).name
        self.bundle.parent.mkdir(parents=True)
        shutil.copyfile(self.case.coordinator.bundle(op), self.bundle)
        self.case.repo.git('-C', str(state), 'add', 'journal')
        self.case.repo.git('-C', str(state), 'commit', '-m', 'Public candidate checkpoint')
        checkpoint = self.case.repo.git('-C', str(state), 'rev-parse', 'HEAD')
        self.script = Path(__file__).resolve().parents[1] / 'verify_candidate.py'
        self.env = dict(os.environ, MERGE_REQUEST='a' * 64, MERGE_CANDIDATE=candidate,
                        MERGE_BASE=self.case.repo.base, MERGE_TREE=self.case.repo.git('rev-parse', 'HEAD^{tree}'),
                        MERGE_CHECKS=json.dumps(['mtr.yml']), MERGE_SUITES=json.dumps(SUITES), MERGE_MTR_TEST='',
                        MERGE_STATE=checkpoint, MERGE_BUNDLE=self.bundle.name)

    def run_verify(self, **changes):
        import sys
        return subprocess.run([sys.executable, str(self.script)], cwd=self.case.repo.path,
                              env=dict(self.env, **changes), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10).returncode

    def test_pinned_candidate_and_target_scripts_pass(self):
        self.assertEqual(self.run_verify(), 0)
        self.assertEqual(self.case.repo.git('rev-parse', 'HEAD'), self.env['MERGE_CANDIDATE'])
        self.assertEqual(self.case.remote_git('for-each-ref', '--format=%(refname)', 'refs/heads'),
                         'refs/heads/lts\nrefs/heads/trunk')

    def test_changed_candidate_tree_base_or_unsafe_input_fails(self):
        for changes in (dict(MERGE_CANDIDATE=self.case.repo.base), dict(MERGE_TREE='f' * 40),
                        dict(MERGE_BASE=self.case.head), dict(MERGE_REQUEST='x;echo bad'),
                        dict(MERGE_MTR_TEST='main.1st;echo bad'), dict(MERGE_CHECKS='["other.yml"]'),
                        dict(MERGE_SUITES='{"core":"main"}'), dict(MERGE_STATE='f' * 40),
                        dict(MERGE_BUNDLE='../outside.bundle'), dict(MERGE_CANDIDATE=self.case.head)):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_verify(**changes), 0)

    def test_changed_bundle_cannot_execute_a_different_candidate(self):
        with self.bundle.open('ab') as stream:
            stream.write(b'tampered')
        self.assertNotEqual(self.run_verify(), 0)


if __name__ == '__main__':
    unittest.main()
