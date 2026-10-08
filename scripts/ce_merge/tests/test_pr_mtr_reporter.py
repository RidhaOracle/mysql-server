# Copyright (c) 2026, Oracle and/or its affiliates.
"""Execute the real reporter JavaScript against full and limited CI evidence."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
HARNESS = r'''
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const repo = {id: 10, full_name: 'owner/ce', owner: {login: 'owner'}};
const sha = 'a'.repeat(40);
const run = {id: 90, run_number: 9, run_attempt: 1, workflow_id: 5, name: 'MTR',
  path: '.github/workflows/mtr.yml', head_sha: sha, head_branch: 'feature',
  head_repository: repo, repository: repo, status: 'completed', event: 'pull_request',
  conclusion: input.failed ? 'failure' : 'success', html_url: 'https://example.test/run/90'};
const pull = {number: 1, state: 'open', base: {ref: 'trunk', repo},
  head: {ref: 'feature', sha, repo}};
const jobs = ['replication', 'storage', 'core', 'services'].map((shard, i) => {
  const selection = input.selections[i];
  const steps = [{name: 'Run MTR', conclusion: selection ? 'skipped' : 'success'}];
  if (selection) steps.push({name: `Run MTR rehearsal (${selection})`,
    conclusion: input.skipped ? 'skipped' : input.failed ? 'failure' : 'success'});
  if (shard === 'services') steps.push({name: 'Run unit tests', conclusion: 'success'});
  return {name: `MTR (${shard})`, status: 'completed', run_attempt: 1, steps};
});
const outputs = {}, statuses = [], added = [], removed = [];
const core = {setOutput: (key, value) => outputs[key] = value,
  warning: () => {}, info: () => {}};
const github = {rest: {
  actions: {getWorkflow: async () => ({data: {id: 5, name: 'MTR', path: run.path}}),
    getWorkflowRun: async () => ({data: run}), listJobsForWorkflowRun: () => {}, listWorkflowRuns: () => {}},
  pulls: {list: () => {}, listFiles: () => {}, get: async () => ({data: pull})},
  repos: {createCommitStatus: async value => statuses.push(value)},
  issues: {getLabel: async () => ({}), updateLabel: async () => {}, createLabel: async () => {},
    removeLabel: async value => removed.push(value.name), addLabels: async value => added.push(...value.labels)}
}};
github.paginate = async (fn) => {
  if (fn === github.rest.actions.listJobsForWorkflowRun) return jobs;
  if (fn === github.rest.actions.listWorkflowRuns) return [run];
  if (fn === github.rest.pulls.list) return [pull];
  if (fn === github.rest.pulls.listFiles) return input.workflowEdited ? [{filename: run.path}] : [];
  throw new Error('Unexpected paginate call');
};
const context = {repo: {owner: 'owner', repo: 'ce'}, payload: {repository: repo, workflow_run: run}};
(async () => {
  await new AsyncFunction('github', 'core', 'context', input.scripts[0])(github, core, context);
  const mapping = {head_sha:'HEAD_SHA', pr_number:'PR_NUMBER', classification:'RESULT',
    mtr_test:'MTR_TEST', untrusted_reason:'UNTRUSTED_REASON', run_attempt:'RUN_ATTEMPT',
    run_id:'RUN_ID', workflow_key:'WORKFLOW_KEY', workflow_name:'WORKFLOW_NAME'};
  for (const [key, suffix] of Object.entries(mapping)) process.env['SOURCE_' + suffix] = outputs[key] || '';
  await new AsyncFunction('github', 'core', 'context', input.scripts[1])(github, core, context);
  process.stdout.write(JSON.stringify({outputs, statuses, added, removed}));
})().catch(error => {console.error(error); process.exit(1)});
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js required for actual GitHub reporter JavaScript')
class MTRReporterTests(unittest.TestCase):
    def report(self, selections, **extra):
        workflow = (ROOT / '.github/workflows/pr-ci-report.yml').read_text()
        scripts = [textwrap.dedent(s) for s in re.findall(
            r'^          script: \|\n((?:            .*\n|\n)*)', workflow, re.M)]
        self.assertEqual(len(scripts), 2)
        result = subprocess.run(['node', '-e', HARNESS], input=json.dumps(
            dict(scripts=scripts, selections=selections, **extra)), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_full_suite_keeps_normal_success_and_label(self):
        result = self.report([''] * 4)
        self.assertEqual([(s['context'], s['state']) for s in result['statuses']], [('MTR', 'success')])
        self.assertEqual(result['added'], ['MTR Passed'])

    def test_rehearsal_has_separate_success_and_revokes_full_coverage(self):
        result = self.report(['main.1st'] * 4)
        self.assertEqual([(s['context'], s['state']) for s in result['statuses']],
                         [('MTR', 'pending'), ('MTR rehearsal', 'success')])
        self.assertEqual(result['added'], [])
        self.assertEqual(set(result['removed']), {'MTR Passed', 'MTR Failed'})
        self.assertIn('full suites not run', result['statuses'][-1]['description'])

    def test_rehearsal_failure_stays_failure(self):
        result = self.report(['main.1st'] * 4, failed=True)
        self.assertEqual(result['statuses'][-1]['state'], 'failure')
        self.assertEqual(result['statuses'][-1]['context'], 'MTR rehearsal')
        self.assertEqual(result['added'], [])

    def test_mixed_shard_coverage_or_tests_cannot_pass(self):
        for selections in (['main.1st', '', '', ''], ['main.1st'] * 3 + ['main.other']):
            with self.subTest(selections=selections):
                result = self.report(selections)
                self.assertEqual(result['outputs']['classification'], 'error')
                self.assertFalse(any(s['state'] == 'success' for s in result['statuses']))

    def test_skipped_or_invalid_rehearsal_cannot_pass(self):
        for kwargs in ({'selections': ['main.1st'] * 4, 'skipped': True},
                       {'selections': ['--help'] * 4}):
            with self.subTest(kwargs=kwargs):
                result = self.report(**kwargs)
                self.assertEqual(result['outputs']['classification'], 'error')
                self.assertFalse(any(s['state'] == 'success' for s in result['statuses']))

    def test_workflow_edit_remains_untrusted(self):
        result = self.report(['main.1st'] * 4, workflowEdited=True)
        self.assertEqual(result['statuses'][0]['state'], 'error')
        self.assertIn('PR edits this workflow', result['statuses'][0]['description'])


if __name__ == '__main__':
    unittest.main()
