# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import unittest
from unittest.mock import patch

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.policy import Blocked
import test_coordinator as fixtures
import test_existing_ci as ci_fixtures


class CandidateRefreshTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AtomicTests('test_entire_chain_published_with_receipt_and_squash')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.c = self.case.coordinator
        self.api = self.case.api
        self.tested = self.case.repo.merge_commit(self.case.repo.base, self.case.head)
        # Deliberately do not publish refs/pull/1/merge. CI evidence attests the
        # tested merge; current mergeability must not depend on GitHub's old ref.
        self.api.pulls[1]['merge_commit_sha'] = self.tested
        self.api.pulls[1]['head']['repo']['id'] = 2
        self.api.pulls[1]['base']['repo']['id'] = 1
        self.ci_case = ci_fixtures.ExistingCITests()
        self.ci_case.setUp()
        self.identity = f'pr:1:{self.case.repo.base}:{self.case.head}:{self.tested}'
        for runs in self.ci_case.runs.values():
            runs[0].update(display_title=self.identity, head_sha=self.case.head,
                           repository={'full_name': self.c.policy['repository']})
        original_repo, original_pages = self.api.repo, self.api.pages
        self.api.repo = lambda path, *a, **kw: (self.ci_case.api.repo(path) if path.startswith('/actions/')
                                               else original_repo(path, *a, **kw))
        self.api.pages = lambda path, key=None: (self.ci_case.api.pages(path, key) if path.startswith('/actions/')
                                                else original_pages(path, key))

    def move_base(self):
        self.case.repo.git('checkout', 'lts')
        head = self.case.repo.commit('target.cc', 'int target;\n')
        self.case.repo.git('push', str(self.case.remote), 'lts:lts', 'lts:trunk')
        return head

    def ci(self):
        pr = self.api.pull(1)
        with self.c.graph_for(pr) as graph:
            merge, paths, _ = self.c.evidence(pr, graph)
            return Coordinator.ci(self.c, pr, merge, paths, graph=graph)

    def assert_no_extra_pr(self):
        self.assertEqual(self.api.bot_pulls, {})
        self.assertEqual(self.c.store.settings('validation:'), [])
        self.assertEqual(self.api.pull(1)['head']['sha'], self.case.head)

    def test_existing_results_pass_without_github_merge_ref_or_extra_pr(self):
        self.assertTrue(self.ci()[0])
        self.assert_no_extra_pr()

    def test_target_advance_accepts_old_base_ci_and_preserves_contributor_head(self):
        base = self.move_base()
        result = self.ci()
        self.assertTrue(result[0])
        self.assertIn('earlier target revision', result[1])
        self.assert_no_extra_pr()
        self.assertEqual(self.api.branch('lts'), base)
        self.assertEqual(self.api.branch('trunk'), base)
        self.assertEqual(self.case.remote_git('tag', '--list', 'ce-integration/*'), '')

    def test_shadow_accepts_same_existing_evidence_without_staging(self):
        self.move_base()
        self.c.policy = dict(self.c.policy, mode='shadow', fork_installation_id=0)
        self.assertTrue(self.ci()[0])
        self.assert_no_extra_pr()

    def test_pending_and_missing_ci_wait_without_creating_pr(self):
        self.move_base()
        self.ci_case.runs['mtr.yml'][0]['status'] = 'in_progress'
        self.assertFalse(self.ci()[0])
        self.ci_case.runs['mtr.yml'] = []
        self.assertFalse(self.ci()[0])
        self.assert_no_extra_pr()

    def test_failed_or_skipped_checks_remain_blocking_after_target_advance(self):
        self.move_base()
        run = self.ci_case.runs['mtr.yml'][0]
        run['conclusion'] = 'failure'
        with self.assertRaises(Blocked):
            self.ci()
        run['conclusion'] = 'success'
        self.ci_case.jobs[2][0]['steps'][0]['conclusion'] = 'skipped'
        with self.assertRaises(Blocked):
            self.ci()
        self.assert_no_extra_pr()

    def test_old_head_unrelated_base_and_malformed_merge_evidence_do_not_pass(self):
        self.move_base()
        run = self.ci_case.runs['mtr.yml'][0]
        for title in (self.identity.replace(self.case.head, 'b'*40),
                      self.identity.replace(self.case.repo.base, 'd'*40),
                      self.identity.replace(self.tested, 'invalid')):
            with self.subTest(title=title):
                run['display_title'] = title
                self.assertFalse(self.ci()[0])
        self.assert_no_extra_pr()

    def test_complete_legacy_metadata_accepts_ancestor_base(self):
        old = self.api.pull(1)
        self.move_base()
        for runs in self.ci_case.runs.values():
            runs[0].update(display_title='Legacy PR CI', pull_requests=[copy.deepcopy(old)])
        self.assertTrue(self.ci()[0])
        self.ci_case.runs['mtr.yml'][0]['pull_requests'][0]['head']['sha'] = 'b'*40
        self.assertFalse(self.ci()[0])

    def test_newer_failed_run_cannot_fall_back_to_old_success(self):
        self.move_base()
        runs = self.ci_case.runs['mtr.yml']
        runs.append(dict(runs[0], id=20, run_number=2, conclusion='failure'))
        with self.assertRaises(Blocked):
            self.ci()
        self.assert_no_extra_pr()

    def test_current_conflict_blocks_despite_passing_existing_checks(self):
        self.case.repo.git('checkout', 'lts')
        self.case.repo.commit('fix.cc', 'conflicting target\n')
        self.case.repo.git('push', str(self.case.remote), 'lts:lts')
        with self.assertRaisesRegex(Blocked, 'conflict'):
            self.ci()
        self.assert_no_extra_pr()

    def test_unpublished_batch_rebuilt_after_base_move_with_same_authorization(self):
        old = self.case.prepare()
        old_steps = copy.deepcopy(old['data']['steps'])
        base = self.move_base()
        self.c.advance(self.case.op())
        rebuilt = self.case.op()
        self.assertEqual(rebuilt['state'], 'prepared')
        self.assertEqual(rebuilt['data']['generation'], 1)
        self.assertEqual(rebuilt['data']['superseded_batches'], [old_steps])
        self.assertEqual(rebuilt['head'], self.case.head)
        self.assertEqual(self.api.branch('lts'), base)
        self.assertTrue(all(s['base_sha'] == base for s in rebuilt['data']['steps']))
        self.case.ci_ok = True
        self.c.advance(self.case.op())
        self.assertEqual(self.case.op()['state'], 'complete')
        for step in self.case.op()['data']['steps']:
            self.assertEqual(self.api.branch(step['branch']), step['after'])

    def test_target_move_during_final_validation_requeues_without_publication(self):
        self.case.prepare()
        calls = []
        def race(*args, **kwargs):
            calls.append(args)
            if len(calls) == 3:  # Both candidates passed; target moves during final CI.
                self.move_base()
            return True, 'passed'
        self.c.ci = race
        self.c.advance(self.case.op())
        op = self.case.op()
        self.assertEqual(op['state'], 'prepared')
        self.assertFalse(op['data']['intent'])
        self.assertIn('Target advanced', op['data']['reason'])
        self.assertEqual(self.case.remote_git('tag', '--list', 'ce-integration/*'), '')

    def test_revoked_source_review_blocks_even_when_candidate_ci_passes(self):
        self.case.prepare()
        self.case.ci_ok = True
        self.api.missing_review.add(1)
        self.c.advance(self.case.op())
        self.assertEqual(self.case.op()['state'], 'blocked')
        self.case.unchanged()

    def test_intent_is_reconciled_without_refreshing_candidates(self):
        op = self.case.prepare()
        op['data']['intent'] = True
        with patch.object(self.c, 'reconcile') as reconcile, patch.object(self.c, 'refresh_targets') as refresh:
            self.c.advance(op)
        reconcile.assert_called_once_with(op)
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
