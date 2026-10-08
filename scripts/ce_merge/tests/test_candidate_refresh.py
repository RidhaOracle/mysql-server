# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import unittest
from unittest.mock import patch

from scripts.ce_merge.ci import STALE_CI
from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked
from scripts.ce_merge.validation import retire_validations
import test_coordinator as fixtures


class CandidateRefreshTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AtomicTests('test_entire_chain_published_with_receipt_and_squash')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.c = self.case.coordinator
        self.api = self.case.api
        self.tested = self.case.repo.merge_commit(self.case.repo.base, self.case.head)
        self.case.repo.git('push', str(self.case.remote), self.tested + ':refs/pull/1/merge')
        self.api.pulls[1]['merge_commit_sha'] = self.tested
        self.create = self.api.bot_pr
        self.api.bot_pr = self.bot_pr

    def bot_pr(self, *args, **kwargs):
        number = self.create(*args, **kwargs)
        pr = self.api.pull(number)
        self.case.repo.git('fetch', str(self.case.fork), pr['head']['sha'])
        tested = self.case.repo.merge_commit(pr['base']['sha'], pr['head']['sha'])
        self.case.repo.git('push', str(self.case.remote), '+' + tested + f':refs/pull/{number}/merge')
        self.api.pulls[number]['merge_commit_sha'] = tested
        return number

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

    def records(self):
        return self.c.store.settings('validation:')

    def test_current_results_reused_pending_and_failed_ci_never_duplicated(self):
        with patch('scripts.ce_merge.validation.existing_pr_ci') as read:
            for result in ((True, 'passed'), (False, 'MTR: in_progress')):
                read.return_value = result
                self.assertEqual(self.ci(), result)
                self.assertEqual(self.records(), [])
            read.side_effect = Blocked('MTR failed')
            with self.assertRaisesRegex(Blocked, 'MTR failed'):
                self.ci()
        self.assertEqual(self.api.bot_pulls, {})

    def test_stale_ref_refresh_preserves_source_and_survives_restart_without_cache(self):
        base = self.move_base()
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')) as read:
            self.assertTrue(self.ci()[0])
            record = self.records()[0]
            self.assertEqual(read.call_args.args[2]['number'], record['pr'])
            self.c.store = Journal(self.c.store.path)
            self.assertTrue(self.ci()[0])
        self.assertEqual(len(self.api.bot_pulls), 1)
        self.assertEqual(self.api.pull(1)['head']['sha'], self.case.head)
        self.assertEqual(self.api.branch('lts'), base)
        self.assertEqual(self.api.branch('trunk'), base)
        self.assertEqual(self.case.remote_git('tag', '--list', 'ce-integration/*'), '')
        self.assertEqual(self.records()[0]['head'], record['head'])

    def test_stale_ci_on_current_merge_creates_validation_pr(self):
        def result(github, policy, pr, merge, paths):
            return (False, 'MTR' + STALE_CI) if pr['number'] == 1 else (True, 'passed')
        with patch('scripts.ce_merge.validation.existing_pr_ci', side_effect=result):
            self.assertTrue(self.ci()[0])
        self.assertEqual(len(self.records()), 1)

    def test_lost_create_response_recovers_same_pr_without_duplicate(self):
        self.move_base()
        def uncertain(*args, **kwargs):
            self.bot_pr(*args, **kwargs)
            raise TimeoutError('lost response')
        self.api.bot_pr = uncertain
        with self.assertRaises(TimeoutError):
            self.ci()
        self.assertNotIn('pr', self.records()[0])
        self.api.bot_pr = self.bot_pr
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')):
            self.assertTrue(self.ci()[0])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.api.bot_pulls), 1)

    def test_validation_tampering_and_closed_pr_never_pass(self):
        self.move_base()
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')) as read:
            self.ci()
            record = self.records()[0]
            original = copy.deepcopy(self.api.pulls[record['pr']])
            for field in ('head', 'repository', 'state'):
                pr = self.api.pulls[record['pr']] = copy.deepcopy(original)
                if field == 'head':
                    pr['head']['sha'] = self.case.head
                elif field == 'repository':
                    pr['head']['repo']['full_name'] = 'untrusted/ce'
                else:
                    pr['state'] = 'closed'
                read.reset_mock()
                with self.subTest(field=field), self.assertRaises(Blocked):
                    self.ci()
                read.assert_not_called()

    def test_target_advance_retires_old_validation_and_requires_new_ci(self):
        self.move_base()
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')):
            self.ci()
        old = self.records()[0]
        self.case.repo.commit('next.cc', 'int next;\n')
        self.case.repo.git('push', str(self.case.remote), 'lts:lts', 'lts:trunk')
        retire_validations(self.c)
        self.assertEqual(self.api.pull(old['pr'])['state'], 'closed')
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(False, 'MTR: queued')) as read:
            self.assertFalse(self.ci()[0])
            self.assertNotEqual(read.call_args.args[2]['number'], old['pr'])
        self.assertEqual(len(self.records()), 2)
        self.assertTrue(self.c.store.setting(old['key'])['retired'])
        self.assertEqual(self.api.pull(1)['head']['sha'], self.case.head)

    def test_shadow_reports_refresh_requirement_without_writes(self):
        self.move_base()
        self.c.policy = dict(self.c.policy, mode='shadow')
        result = self.ci()
        self.assertFalse(result[0])
        self.assertIn('without rebasing', result[1])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.api.bot_pulls, {})

    def test_missing_fork_installation_is_actionable(self):
        self.move_base()
        self.c.policy = dict(self.c.policy, fork_installation_id=0)
        with self.assertRaisesRegex(Blocked, 'Install the App'):
            self.ci()
        self.assertEqual(self.records(), [])

    def test_conflicts_require_resolution_without_staging(self):
        self.case.repo.git('checkout', 'lts')
        self.case.repo.commit('fix.cc', 'conflicting target\n')
        self.case.repo.git('push', str(self.case.remote), 'lts:lts')
        with self.assertRaisesRegex(Blocked, 'conflict'):
            self.ci()
        self.assertEqual(self.records(), [])

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

    def test_validation_pr_not_evaluated_as_an_independent_contribution(self):
        self.move_base()
        with patch('scripts.ce_merge.validation.existing_pr_ci', return_value=(True, 'passed')):
            self.ci()
        # Drop the fixture's queued operation so work reaches PR evaluation.
        self.c.store.save(self.case.op(), 'aborted', 'fixture')
        pulls = [self.api.pull(n) for n in self.api.pulls]
        with patch.object(self.api, 'pages', return_value=pulls), patch.object(self.c, 'evaluate') as evaluate:
            self.c.work()
        self.assertEqual([c.args[0]['number'] for c in evaluate.call_args_list], [1])

    def test_intent_is_reconciled_without_refreshing_candidates(self):
        op = self.case.prepare()
        op['data']['intent'] = True
        with patch.object(self.c, 'reconcile') as reconcile, patch.object(self.c, 'refresh_targets') as refresh:
            self.c.advance(op)
        reconcile.assert_called_once_with(op)
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
