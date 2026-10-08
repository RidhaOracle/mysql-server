# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import unittest

from scripts.ce_merge.ci import WORKFLOWS, existing_pr_ci
from scripts.ce_merge.policy import Blocked


class ExistingCITests(unittest.TestCase):
    def setUp(self):
        self.policy = {'repository': 'owner/ce'}
        self.pr = {'number': 7, 'base': {'sha': 'a'*40, 'ref': 'trunk', 'repo': {'id': 1}},
                   'head': {'sha': 'b'*40, 'repo': {'id': 2}}}
        self.merge = 'c'*40
        self.runs, self.jobs, self.reads = {}, {}, []
        self.identity = f'pr:7:{"a"*40}:{"b"*40}:{self.merge}'
        for number, (filename, layout) in enumerate(WORKFLOWS.items(), 1):
            self.runs[filename] = [{'id': number, 'run_number': 1, 'run_attempt': 1,
                'display_title': self.identity, 'head_sha': 'b'*40, 'event': 'pull_request',
                'path': '.github/workflows/' + filename, 'workflow_id': number,
                'repository': {'full_name': 'owner/ce'}, 'status': 'completed', 'conclusion': 'success'}]
            self.jobs[number] = []
            for name, required in layout.items():
                steps = list(required)
                if filename == 'mtr.yml':
                    steps += ['Run MTR']
                    if name == 'MTR (services)':
                        steps += ['Run unit tests']
                self.jobs[number].append({'name': name, 'status': 'completed', 'conclusion': 'success',
                    'run_attempt': 1, 'steps': [{'name': step, 'conclusion': 'success'} for step in steps]})
        case = self
        class ReadOnlyAPI:
            def repo(self, path):
                case.reads.append(path)
                filename = path.rsplit('/', 1)[-1]
                return {'id': list(WORKFLOWS).index(filename)+1, 'name': filename,
                        'path': '.github/workflows/' + filename}
            def pages(self, path, key):
                case.reads.append(path)
                if key == 'workflow_runs':
                    return case.runs[path.split('/')[3]]
                return case.jobs[int(path.split('/')[3])]
        self.api = ReadOnlyAPI()

    def evaluate(self, paths=None):
        return existing_pr_ci(self.api, self.policy, self.pr, self.merge, paths or ['sql/change.cc'])

    def rehearsal(self, name='main.1st'):
        self.policy['ci_mtr_test'] = name
        for job in self.jobs[2]:
            next(step for step in job['steps'] if step['name'] == 'Run MTR')['conclusion'] = 'skipped'
            job['steps'] += [{'name': 'Verify rehearsal test', 'conclusion': 'success'},
                             {'name': f'Run MTR rehearsal ({name})', 'conclusion': 'success'}]

    def test_existing_jobs_pass_without_any_write_or_dispatch(self):
        self.assertTrue(self.evaluate()[0])
        self.assertFalse(any('dispatch' in path or 'rerun' in path for path in self.reads))

    def test_no_formatting_job_required_for_sql_only_change(self):
        self.runs['clang-format.yml'] = []
        self.assertTrue(self.evaluate(['mysql-test/t/1st.test'])[0])
        self.assertFalse(any('clang-format' in path for path in self.reads))
        self.assertFalse(self.evaluate(['.clang-format'])[0])

    def test_missing_pending_and_failed_runs_do_not_pass(self):
        self.runs['mtr.yml'][0]['status'] = 'queued'
        self.assertFalse(self.evaluate()[0])
        self.runs['mtr.yml'][0].update(status='completed', conclusion='failure')
        with self.assertRaises(Blocked):
            self.evaluate()
        self.runs['mtr.yml'] = []
        self.assertFalse(self.evaluate()[0])

    def test_newer_pending_run_wins_over_old_success(self):
        old = self.runs['mtr.yml'][0]
        self.runs['mtr.yml'].append(dict(old, id=20, run_number=2, status='queued'))
        self.assertFalse(self.evaluate()[0])

    def test_stale_and_foreign_run_identities_are_rejected(self):
        old = copy.deepcopy(self.runs['mtr.yml'][0])
        for update in ({'head_sha': 'd'*40}, {'event': 'workflow_dispatch'}, {'workflow_id': 99},
                       {'path': '.github/workflows/untrusted.yml'}, {'repository': {'full_name': 'other/ce'}},
                       {'display_title': 'pr:8:' + ':'.join(['a'*40, 'b'*40, 'c'*40])},
                       {'display_title': self.identity.replace('a'*40, 'd'*40)},
                       {'display_title': self.identity.replace('c'*40, 'd'*40)}):
            with self.subTest(update=update):
                self.runs['mtr.yml'][0] = dict(old, **update)
                self.assertFalse(self.evaluate()[0])

    def test_existing_pre_title_run_uses_complete_github_pr_metadata(self):
        run = self.runs['mtr.yml'][0]
        run['display_title'] = 'MTR PR #7 [REHEARSAL main.1st]'
        run['pull_requests'] = [copy.deepcopy(self.pr)]
        self.assertTrue(self.evaluate()[0])
        run['pull_requests'][0]['base']['sha'] = 'd'*40
        self.assertFalse(self.evaluate()[0])
        run['pull_requests'] = []
        self.assertFalse(self.evaluate()[0])

    def test_missing_and_skipped_required_steps_block(self):
        job = self.jobs[1][0]
        job['steps'][0]['conclusion'] = 'skipped'
        with self.assertRaises(Blocked):
            self.evaluate()
        job['steps'].pop(0)
        with self.assertRaises(Blocked):
            self.evaluate()

    def test_missing_jobs_and_unit_tests_block(self):
        services = self.jobs[2][-1]
        next(s for s in services['steps'] if s['name'] == 'Run unit tests')['conclusion'] = 'skipped'
        with self.assertRaises(Blocked):
            self.evaluate()
        self.jobs[2].pop()
        with self.assertRaises(Blocked):
            self.evaluate()

    def test_workflow_changes_need_bootstrap_review(self):
        with self.assertRaisesRegex(Blocked, 'changes a required CI workflow'):
            self.evaluate(['.github/workflows/mtr.yml'])

    def test_failed_job_rerun_reuses_successful_siblings_only(self):
        old = self.jobs[2][0]
        old['conclusion'] = 'failure'
        repaired = copy.deepcopy(old)
        repaired.update(run_attempt=2, conclusion='success')
        self.jobs[2].append(repaired)
        self.runs['mtr.yml'][0]['run_attempt'] = 2
        self.assertTrue(self.evaluate()[0])
        repaired['conclusion'] = 'failure'
        with self.assertRaises(Blocked):
            self.evaluate()

    def test_rehearsal_cannot_satisfy_full_coverage(self):
        self.rehearsal()
        ok, message = self.evaluate()
        self.assertTrue(ok)
        self.assertIn('REHEARSAL', message)
        self.policy.pop('ci_mtr_test')
        with self.assertRaises(Blocked):
            self.evaluate()

    def test_mixed_rehearsal_and_full_shards_block(self):
        self.rehearsal()
        next(s for s in self.jobs[2][0]['steps'] if s['name'] == 'Run MTR')['conclusion'] = 'success'
        with self.assertRaisesRegex(Blocked, 'mix'):
            self.evaluate()

    def test_different_rehearsal_selection_and_skipped_verification_block(self):
        self.rehearsal()
        self.policy['ci_mtr_test'] = 'main.other'
        with self.assertRaises(Blocked):
            self.evaluate()
        self.policy['ci_mtr_test'] = 'main.1st'
        next(s for s in self.jobs[2][0]['steps'] if s['name'] == 'Verify rehearsal test')['conclusion'] = 'skipped'
        with self.assertRaises(Blocked):
            self.evaluate()


if __name__ == '__main__':
    unittest.main()
