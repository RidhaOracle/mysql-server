# Copyright (c) 2026, Oracle and/or its affiliates.
"""Candidate CI provenance and real-Git, content-bound reuse of ordinary PR CI."""
import copy
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from scripts.ce_merge.ci import WORKFLOWS
from scripts.ce_merge.policy import Blocked
from scripts.ce_merge.testing import WORKFLOW, SUITES, candidate_tests, validate_run
from scripts.ce_merge.coordinator import Coordinator
import test_coordinator as fixtures


def successful_jobs(checks, candidate=False):
    jobs = []
    for filename in checks:
        for name, required in WORKFLOWS[filename].items():
            steps = ['Verify candidate' if candidate and s == 'Verify PR merge commit' else s for s in required]
            if filename == 'mtr.yml':
                steps += ['Run MTR']
                if name == 'MTR (services)':
                    steps += ['Run unit tests']
            jobs.append(dict(name=name, run_attempt=1, status='completed', conclusion='success',
                             steps=[dict(name=s, conclusion='success') for s in steps]))
    return jobs


class CandidateRunTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(request='a' * 64, trusted='b' * 40, checks=list(WORKFLOWS), mtr_test='')
        self.run = dict(id=77, run_number=1, run_attempt=1, event='workflow_dispatch',
                        display_title='merge-test:' + self.record['request'], head_sha=self.record['trusted'],
                        path='.github/workflows/' + WORKFLOW, workflow_id=7,
                        actor={'id': 42}, repository={'full_name': fixtures.POLICY['repository']},
                        status='completed', conclusion='success')
        self.runs = [self.run]
        self.jobs = successful_jobs(WORKFLOWS, candidate=True)
        self.api = MagicMock()
        self.api.repo.return_value = dict(id=7, path='.github/workflows/' + WORKFLOW)
        self.api.pages.side_effect = lambda path, key: self.jobs if '/jobs?' in path else self.runs

    def check(self):
        return validate_run(self.api, fixtures.POLICY, self.record)

    def test_complete_pinned_run_passes_without_reading_artifacts(self):
        self.assertTrue(self.check()[0])
        self.assertTrue(all('/artifacts' not in call.args[0] for call in self.api.pages.call_args_list))

    def test_wrong_request_actor_workflow_trusted_revision_or_repository_is_ignored(self):
        original = copy.deepcopy(self.run)
        changes = [dict(actor={'id': 9}), dict(display_title='merge-test:' + 'c' * 64),
                   dict(head_sha='d' * 40), dict(event='pull_request'), dict(workflow_id=8),
                   dict(repository={'full_name': 'attacker/repo'}), dict(path='other.yml')]
        for change in changes:
            self.runs = [dict(original, **change)]
            with self.subTest(change=change):
                self.assertFalse(self.check()[0])

    def test_latest_failed_run_cannot_be_hidden_by_an_earlier_success(self):
        self.runs.append(dict(self.run, id=78, run_number=2, conclusion='failure'))
        with self.assertRaisesRegex(Blocked, 'did not pass'):
            self.check()

    def test_missing_failed_skipped_or_rerun_job_blocks(self):
        initial = copy.deepcopy(self.jobs)
        variants = [initial[:-1], [dict(initial[0], conclusion='failure')] + initial[1:],
                    [dict(initial[0], conclusion='skipped')] + initial[1:],
                    initial + [dict(initial[0], run_attempt=2, conclusion='failure')]]
        for jobs in variants:
            self.jobs = jobs
            self.run['run_attempt'] = 2
            with self.subTest(jobs=len(jobs)), self.assertRaises(Blocked):
                self.check()

    def test_successful_job_requires_actual_tests_and_unit_step(self):
        service = next(j for j in self.jobs if j['name'] == 'MTR (services)')
        service['steps'] = [s for s in service['steps'] if s['name'] != 'Run unit tests']
        with self.assertRaisesRegex(Blocked, 'unit tests'):
            self.check()

    def test_partial_rerun_accepts_latest_job_and_rejects_duplicate_attempt(self):
        self.run['run_attempt'] = 2
        first = self.jobs[0]
        self.jobs.append(dict(first, run_attempt=2))
        first['conclusion'] = 'failure'
        self.assertTrue(self.check()[0])
        self.jobs.append(dict(self.jobs[-1]))
        with self.assertRaisesRegex(Blocked, 'Ambiguous'):
            self.check()

    def test_unselected_jobs_are_allowed_but_unknown_running_jobs_are_not(self):
        self.record['checks'] = ['pr-build.yml']
        self.jobs.append(dict(name='Optional future check', run_attempt=1, conclusion='skipped'))
        self.assertTrue(self.check()[0])
        self.jobs[-1]['conclusion'] = 'success'
        with self.assertRaisesRegex(Blocked, 'Unexpected'):
            self.check()

    def test_full_coverage_and_rehearsal_are_distinct(self):
        self.record['mtr_test'] = 'main.1st'
        with self.assertRaisesRegex(Blocked, 'rehearsal'):
            self.check()
        for job in self.jobs:
            if job['name'].startswith('MTR ('):
                job['steps'] += [dict(name=n, conclusion='success')
                                 for n in ('Verify rehearsal test', 'Run MTR rehearsal')]
        self.assertTrue(self.check()[0])


class PreparationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SerialTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.prepare()
        self.case.store.head = 'd' * 40  # GitJournal's already-confirmed public checkpoint.
        self.runs, self.jobs, self.dispatches = {}, {}, []
        self.workflows = {name: dict(id=i, path='.github/workflows/' + name, name=name)
                          for i, name in enumerate((*WORKFLOWS, WORKFLOW), 1)}
        repo, pages = self.case.api.repo, self.case.api.pages
        def ci_repo(path, method='GET', data=None, **kwargs):
            if path.endswith('/dispatches'):
                self.dispatches.append(copy.deepcopy(data))
                return None
            if path.startswith('/actions/workflows/'):
                return self.workflows[path.rsplit('/', 1)[1]]
            return repo(path, method, data, **kwargs)
        def ci_pages(path, key=None):
            if path.startswith('/actions/workflows/'):
                filename = path.split('/')[3]
                head = parse_qs(urlsplit(path).query)['head_sha'][0]
                return self.runs.get((filename, head), [])
            if path.startswith('/actions/runs/'):
                return self.jobs[int(path.split('/')[3])]
            return pages(path, key)
        self.case.api.repo, self.case.api.pages = ci_repo, ci_pages

    def ordinary_ci(self, number, checks=WORKFLOWS):
        pr = self.case.api.pull(number)
        for filename in checks:
            run_id = number * 100 + self.workflows[filename]['id']
            self.runs[filename, pr['head']['sha']] = [dict(
                id=run_id, run_number=1, run_attempt=1, workflow_id=self.workflows[filename]['id'],
                event='pull_request', head_sha=pr['head']['sha'], path='.github/workflows/' + filename,
                repository={'full_name': fixtures.POLICY['repository']},
                display_title=f'pr:{number}:{pr["base"]["sha"]}:{pr["head"]["sha"]}:{pr["head"]["sha"]}',
                status='completed', conclusion='success')]
            self.jobs[run_id] = successful_jobs([filename])

    def check(self, index):
        op = self.case.op()
        with self.case.graph(self.case.coordinator.policy) as graph:
            graph.restore_candidates(self.case.coordinator.prepared_steps(op), self.case.coordinator.bundle(op))
            result = candidate_tests(self.case.coordinator, op, graph, index)
        self.case.store.save(op, 'prepared', 'test-checkpoint')
        return result

    def test_source_pr_ci_can_satisfy_all_checks_without_dispatch(self):
        self.ordinary_ci(1)
        self.assertTrue(self.check(0)[0])
        self.assertEqual(self.dispatches, [])

    def already_applied_request(self, path, content, replace_source=False):
        self.case.repo.git('checkout', 'trunk')
        tip = self.case.repo.commit(path, content)
        self.case.repo.git('push', str(self.case.remote), tip + ':refs/heads/trunk')
        if replace_source:
            self.case.repo.git('checkout', '-b', 'docs-source', self.case.repo.base)
            head = self.case.repo.commit(path, content)
            self.case.repo.git('push', str(self.case.remote), '+' + head + ':refs/pull/1/head')
            self.case.api.pulls[1]['head']['sha'] = head
        op = self.case.op()
        op['head'] = self.case.api.pulls[1]['head']['sha']
        op['data'].update(body=self.case.api.pulls[1]['body'], steps=[])
        self.case.store.save(op, 'queued', 'already-applied-source')
        self.case.coordinator.prepare(op)

    def test_already_applied_documentation_needs_no_compilation(self):
        self.already_applied_request('Docs/rehearsal.md', 'Rehearsal\n', replace_source=True)
        ok, message = self.check(1)
        self.assertTrue(ok)
        self.assertIn('target tree unchanged', message)
        self.assertEqual(self.dispatches, [])

    def test_already_applied_code_still_requires_version_tests(self):
        self.already_applied_request('fix.cc', 'int fix;\n')
        self.assertFalse(self.check(1)[0])
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(json.loads(self.dispatches[0]['inputs']['checks']), ['pr-build.yml', 'mtr.yml'])

    def test_only_missing_check_group_is_dispatched(self):
        self.ordinary_ci(1, ['pr-build.yml', 'mtr.yml'])
        self.assertFalse(self.check(0)[0])
        self.assertEqual(json.loads(self.dispatches[0]['inputs']['checks']), ['clang-format.yml'])
        self.assertEqual(len(self.case.api.pulls), 1)

    def test_dispatch_is_pinned_checkpointed_and_not_repeated_on_new_runner(self):
        self.assertFalse(self.check(1)[0])
        record = self.case.op()['data']['steps'][1]['test_request']
        self.assertEqual(record['state'], 'd' * 40)
        self.assertEqual(record['bundle'], self.case.coordinator.bundle(self.case.op()).name)
        self.assertEqual(self.case.remote_git('for-each-ref', '--format=%(refname)', 'refs/heads'),
                         'refs/heads/lts\nrefs/heads/trunk')
        self.assertNotIn('ref', record)
        from scripts.ce_merge.journal import Journal
        self.case.coordinator.store = Journal(self.case.root / 'journal')
        self.assertFalse(self.check(1)[0])
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(len(self.case.api.pulls), 1)

    def test_resolution_pr_ci_is_reused_for_its_exact_resolved_tree(self):
        # Reset prepared state before introducing a real conflicting target.
        self.case.repo.git('checkout', 'trunk')
        tip = self.case.repo.commit('fix.cc', 'conflicting change\n')
        self.case.repo.git('push', str(self.case.remote), 'trunk:trunk')
        self.case.coordinator.advance(self.case.op())
        lower = self.case.op()['data']['steps'][0]['after']
        self.case.resolution(lower, tip)
        self.case.coordinator.repair(self.case.op(), 3)
        self.ordinary_ci(3)
        self.assertTrue(self.check(1)[0])
        self.assertEqual(self.dispatches, [])

    def test_failed_existing_ci_is_not_replaced_with_a_new_run(self):
        self.ordinary_ci(1)
        self.runs['pr-build.yml', self.case.head][0]['conclusion'] = 'failure'
        with self.assertRaisesRegex(Blocked, 'did not pass'):
            self.check(0)
        self.assertEqual(self.dispatches, [])

    def test_custom_version_suite_profile_requires_matching_candidate_mtr(self):
        self.case.coordinator.policy = dict(fixtures.POLICY, ci_suites={
            'lts': dict(SUITES, core='main')})
        self.ordinary_ci(1)
        self.assertFalse(self.check(0)[0])
        self.assertEqual(json.loads(self.dispatches[0]['inputs']['checks']), ['mtr.yml'])
        self.assertEqual(json.loads(self.dispatches[0]['inputs']['suites'])['core'], 'main')

    def test_dispatch_lost_response_does_not_repeat_request(self):
        original = self.case.api.repo
        def lose(path, method='GET', data=None, **kw):
            result = original(path, method, data, **kw)
            if path.endswith('/dispatches'):
                raise ConnectionError('Lost response')
            return result
        self.case.api.repo = lose
        with self.assertRaises(ConnectionError):
            self.check(1)
        # The same mutable op was saved before the dispatch call.
        self.case.api.repo = original
        self.assertFalse(self.check(1)[0])
        self.assertEqual(len(self.dispatches), 1)

    def test_failed_checkpoint_prevents_dispatch(self):
        from scripts.ce_merge.journal import PersistenceError
        save = self.case.store.save
        def fail(op, state, event):
            if event == 'candidate-test-dispatch-intent':
                raise PersistenceError('Competing writer rejected dispatch checkpoint')
            return save(op, state, event)
        with patch.object(self.case.store, 'save', side_effect=fail):
            with self.assertRaises(PersistenceError):
                self.check(1)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.case.api.merges, [])

    def test_interrupted_staging_intent_recovers_without_duplicate_dispatch(self):
        from scripts.ce_merge.journal import PersistenceError
        save = self.case.store.save
        def interrupt(op, state, event):
            save(op, state, event)
            if event == 'candidate-test-staging-intent':
                raise PersistenceError('Runner stopped after staging checkpoint')
        with patch.object(self.case.store, 'save', side_effect=interrupt):
            with self.assertRaises(PersistenceError):
                self.check(1)
        self.assertFalse(self.check(1)[0])  # Clears known-not-dispatched intent.
        self.assertFalse(self.check(1)[0])  # Stages and dispatches a single request.
        self.assertEqual(len(self.dispatches), 1)

    def test_passing_candidate_ci_unlocks_serial_prs_after_runner_replacement(self):
        self.ordinary_ci(1)
        self.case.coordinator.tests = Coordinator.tests.__get__(self.case.coordinator)
        self.case.api.missing_review.add(2)  # No second approval for generated PR.
        self.case.coordinator.advance(self.case.op())
        self.assertEqual(self.case.api.merges, [])
        self.assertEqual(len(self.case.api.pulls), 1)
        record = self.case.op()['data']['steps'][1]['test_request']
        run_id = 700
        self.runs[WORKFLOW, record['trusted']] = [dict(
            id=run_id, run_number=1, run_attempt=1, workflow_id=self.workflows[WORKFLOW]['id'],
            event='workflow_dispatch', head_sha=record['trusted'], path='.github/workflows/' + WORKFLOW,
            actor={'id': 42}, repository={'full_name': fixtures.POLICY['repository']},
            display_title='merge-test:' + record['request'], status='completed', conclusion='success')]
        self.jobs[run_id] = successful_jobs(record['checks'], candidate=True)
        from scripts.ce_merge.journal import Journal
        self.case.coordinator.store = Journal(self.case.root / 'journal')
        self.case.coordinator.advance(self.case.op())
        self.assertEqual(self.case.api.merges, [(1, 'squash')])
        self.assertEqual(len(self.case.api.pulls), 1)
        self.case.coordinator.advance(self.case.op())
        self.assertEqual(self.case.api.merges, [(1, 'squash'), (2, 'merge')])
        self.assertEqual(len(self.dispatches), 1)
        self.assertTrue(all(p['merged'] for p in self.case.api.pulls.values()))
        self.case.remote_git('merge-base', '--is-ancestor', self.case.api.branch('lts'), self.case.api.branch('trunk'))


if __name__ == '__main__':
    unittest.main()
