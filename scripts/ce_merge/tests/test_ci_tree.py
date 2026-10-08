# Copyright (c) 2026, Oracle and/or its affiliates.
import unittest
from unittest.mock import patch

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.policy import Blocked
import test_coordinator as fixtures


class CandidateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AtomicTests('test_entire_chain_published_with_receipt_and_squash')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.op = self.case.prepare()
        self.step = self.op['data']['steps'][0]
        self.tested = self.case.repo.merge_commit(self.case.repo.base, self.case.head)
        self.case.repo.git('push', str(self.case.remote), self.tested + ':refs/pull/1/merge')
        self.case.api.pulls[1]['merge_commit_sha'] = self.tested
        self.pr = self.case.api.pull(1)

    def test_squash_reuses_same_tree_without_dispatch(self):
        self.assertNotEqual(self.step['after'], self.tested)
        with self.case.graph(self.case.coordinator.policy) as graph:
            graph.restore_candidates(self.op['data']['steps'], self.case.coordinator.bundle(self.op))
            with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')) as read:
                self.assertTrue(Coordinator.ci(self.case.coordinator, self.pr, self.step['after'], ['fix.cc'],
                    parents=self.step['parents'], staged=True, graph=graph)[0])
                self.assertEqual(read.call_args.args[3], self.tested)

    def test_different_candidate_tree_cannot_reuse_pr_results(self):
        other = self.case.repo.commit('other.cc', 'int unrelated;\n')
        with self.case.graph(self.case.coordinator.policy) as graph:
            graph.restore_candidates(self.op['data']['steps'], self.case.coordinator.bundle(self.op))
            graph.run('fetch', str(self.case.repo.path), other)
            candidate = graph.commit(graph.tree(other), self.step['parents'], 'Different source')
            with patch('scripts.ce_merge.validation.existing_pr_ci') as read:
                with self.assertRaisesRegex(Blocked, 'differs'):
                    Coordinator.ci(self.case.coordinator, self.pr, candidate, ['fix.cc'],
                        parents=self.step['parents'], staged=True, graph=graph)
                read.assert_not_called()

    def test_wrong_candidate_parents_or_stale_pr_merge_cannot_pass(self):
        with self.case.graph(self.case.coordinator.policy) as graph:
            graph.restore_candidates(self.op['data']['steps'], self.case.coordinator.bundle(self.op))
            with self.assertRaises(Blocked):
                Coordinator.ci(self.case.coordinator, self.pr, self.step['after'], ['fix.cc'],
                    parents=[], staged=True, graph=graph)
            self.pr['base']['sha'] = 'd'*40
            with self.assertRaisesRegex(Blocked, 'target changed'):
                Coordinator.ci(self.case.coordinator, self.pr, self.step['after'], ['fix.cc'],
                    parents=self.step['parents'], staged=True, graph=graph)

    def test_only_merge_check_is_published_and_waiting_never_passes(self):
        self.assertTrue(self.case.api.checks)
        self.assertTrue(all(args[1] == 'Merge check' and args[2] == 'failure'
                            for args, kwargs in self.case.api.checks))
        self.case.api.checks.clear()
        self.case.coordinator.publish(self.pr, True, 'Existing CI passed', eligible=True)
        self.assertEqual(len(self.case.api.checks), 1)
        args, kwargs = self.case.api.checks[0]
        self.assertEqual(args[1:3], ('Merge check', 'success'))
        self.assertTrue(kwargs['action'])


if __name__ == '__main__':
    unittest.main()
