# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.ce_merge.github import GitHub
from scripts.ce_merge.policy import Blocked, load, rulesets
from scripts.ce_merge.release import publish
import test_coordinator as fixtures


def same_policy():
    value = copy.deepcopy(fixtures.POLICY)
    value.pop('bot_fork')
    value.pop('fork_installation_id')
    return value


class SameRepositoryPolicyTests(unittest.TestCase):
    def load(self, policy):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'policy.json'
            path.write_text(json.dumps(policy))
            return load(path)

    def test_active_default_uses_existing_repository_and_installation(self):
        value = self.load(same_policy())
        self.assertEqual(value['bot_fork'], value['repository'])
        self.assertEqual(value['fork_installation_id'], value['installation_id'])

    def test_explicit_same_repo_ignores_obsolete_fork_installation(self):
        for installation in (0, 999):
            value = self.load(dict(same_policy(), bot_fork='example/ce', fork_installation_id=installation))
            self.assertEqual(value['fork_installation_id'], value['installation_id'])

    def test_explicit_legacy_fork_remains_supported_and_requires_installation(self):
        self.assertEqual(self.load(fixtures.POLICY)['fork_installation_id'], 4)
        for value in (0, None):
            policy = dict(fixtures.POLICY, fork_installation_id=value)
            if value is None:
                policy.pop('fork_installation_id')
            with self.assertRaisesRegex(Blocked, 'separate staging fork'):
                self.load(policy)

    def test_target_cannot_overlap_bot_namespaces(self):
        for branch in ('upmerge', 'upmerge/operation/0', 'promotion', 'promotion/release'):
            for key in ('branches', 'release_branches'):
                policy = dict(same_policy(), **{key: [branch]})
                with self.subTest(branch=branch, key=key), self.assertRaisesRegex(Blocked, 'reserved'):
                    self.load(policy)

    def test_only_staging_rules_grant_bot_write_exceptions_in_reserved_namespace(self):
        rules = {r['name']: r for r in rulesets(same_policy())}
        staging = rules['CE bot staging']
        self.assertEqual(staging['conditions']['ref_name']['include'],
                         ['refs/heads/upmerge/**/*', 'refs/heads/promotion/**/*'])
        self.assertEqual(staging['bypass_actors'],
                         [{'actor_id': 1, 'actor_type': 'Integration', 'bypass_mode': 'always'}])
        self.assertEqual({r['type'] for r in staging['rules']},
                         {'creation', 'update', 'deletion', 'non_fast_forward'})
        self.assertEqual(rules['CE merge quality']['bypass_actors'], [])
        self.assertEqual(rules['CE merge quality']['conditions']['ref_name']['include'],
                         ['refs/heads/lts', 'refs/heads/trunk'])
        self.assertNotIn('CE bot staging', {r['name'] for r in rulesets(fixtures.POLICY)})

    def test_staging_reuses_the_existing_scoped_token(self):
        api = GitHub(same_policy())
        with patch.dict(os.environ, CE_APP_PRIVATE_KEY='/unused/test-key'), \
                patch('scripts.ce_merge.github.subprocess.run', return_value=SimpleNamespace(stdout=b'signature')), \
                patch.object(api, 'request', return_value={'token': 'test-token'}) as request:
            self.assertEqual(api.token(), 'test-token')
            self.assertEqual(api.token(fork=True), 'test-token')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:3],
                         ('POST', '/app/installations/3/access_tokens', {'repositories': ['ce']}))

    def test_legacy_fork_token_keeps_its_installation_scope(self):
        api = GitHub(fixtures.POLICY)
        api.tokens[(3, 'example/ce')] = ('ce-token', time.time()+1000)
        api.tokens[(4, 'bot/ce')] = ('fork-token', time.time()+1000)
        self.assertEqual(api.token(), 'ce-token')
        self.assertEqual(api.token(fork=True), 'fork-token')

    def test_bot_pr_uses_local_head_and_recovers_existing_pr(self):
        api = GitHub(same_policy())
        calls = []
        created = []
        def request(method, path, data=None, **kwargs):
            calls.append((method, path, data))
            if '/git/matching-refs/' in path:
                return [{'ref': 'refs/heads/upmerge/operation/1', 'object': {'sha': 'a'*40}}]
            if '?state=all&head=' in path:
                return created
            if method == 'POST' and path.endswith('/pulls'):
                created.append({'number': 8})
                return created[0]
            self.fail(path)
        api.request = request
        self.assertEqual(api.bot_pr('operation', 1, 'a'*40, 'trunk', 1), 8)
        self.assertEqual(api.bot_pr('operation', 1, 'a'*40, 'trunk', 1), 8)
        writes = [data for method, path, data in calls if method == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['head'], 'upmerge/operation/1')
        self.assertTrue(all(path.startswith('/repos/example/ce/') for _, path, _ in calls))


class SameRepositoryAtomicTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AtomicTests('test_entire_chain_published_with_receipt_and_squash')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case
        case.fork = case.remote
        original_graph = case.graph
        class Graph(original_graph):
            def run(self, *args, **kwargs):
                args = [str(case.remote) if arg == 'https://github.com/example/ce.git' else arg for arg in args]
                return super().run(*args, **kwargs)
        case.graph = Graph
        case.coordinator.graph = Graph
        case.coordinator.policy = same_policy()
        original_bot_pr = case.api.bot_pr
        def bot_pr(*args, **kwargs):
            number = original_bot_pr(*args, **kwargs)
            case.api.pulls[number]['head']['repo']['full_name'] = 'example/ce'
            case.api.pulls[number]['head']['ref'] = f'upmerge/{args[0]}/{args[1]}'
            return number
        case.api.bot_pr = bot_pr
        self.c = case.coordinator

    def test_staging_does_not_publish_targets_and_completed_chain_is_atomic(self):
        op = self.case.prepare()
        refs = self.case.remote_git('for-each-ref', '--format=%(refname)', 'refs/heads/upmerge/')
        self.assertEqual(len(refs.splitlines()), 2)
        self.case.unchanged()
        self.case.ci_ok = True
        self.c.advance(self.case.op())
        op = self.case.op()
        self.assertEqual(op['state'], 'complete')
        lower, upper = op['data']['steps']
        self.assertEqual(self.case.api.branch('lts'), lower['after'])
        self.assertEqual(self.case.api.branch('trunk'), upper['after'])
        self.case.remote_git('merge-base', '--is-ancestor', lower['after'], upper['after'])
        self.assertEqual(self.case.remote_git('rev-parse', op['data']['receipt']['ref']), op['data']['receipt']['sha'])

    def test_failed_generated_pr_review_keeps_all_targets_unchanged(self):
        op = self.case.prepare()
        self.case.api.missing_review.add(op['data']['steps'][1]['pr'])
        self.case.ci_ok = True
        self.c.advance(self.case.op())
        self.case.unchanged()
        self.assertEqual(self.case.op()['state'], 'prepared')

    def test_staging_collision_cannot_overwrite_branch_or_publish_targets(self):
        op = self.case.prepare()
        step = op['data']['steps'][1]
        ref = f'refs/heads/upmerge/{step["stage_id"]}/1'
        self.case.remote_git('update-ref', ref, self.case.repo.base)
        self.case.ci_ok = True
        self.c.advance(self.case.op())
        self.assertEqual(self.case.op()['state'], 'blocked')
        self.assertIn('Staging branch changed', self.case.op()['data']['reason'])
        self.case.unchanged()
        self.assertEqual(self.case.remote_git('rev-parse', ref), self.case.repo.base)

    def test_promotion_requires_reserved_namespace_and_preserves_manifest_checks(self):
        manifest = self.case.registration_manifest()
        self.c.policy = dict(same_policy(), branches=['lts'])
        self.case.api.pulls[1]['head']['repo']['full_name'] = 'example/ce'
        pr = self.case.api.pull(1)
        with self.case.graph(self.c.policy) as graph:
            graph.fetch(['refs/pull/1/head', 'refs/heads/lts'])
            with self.assertRaisesRegex(Blocked, 'protected promotion'):
                self.c.check_promotion(graph, pr, manifest, [self.case.head])
            pr['head']['ref'] = 'promotion/approved'
            self.c.check_promotion(graph, pr, manifest, [self.case.head])
            manifest['expected_tree'] = graph.tree(self.case.repo.base)
            with self.assertRaisesRegex(Blocked, 'validated source tree'):
                self.c.check_promotion(graph, pr, manifest, [self.case.head])

    def test_promotion_publication_uses_same_repo_and_existing_installation(self):
        manifest = self.case.registration_manifest()
        self.c.policy = dict(same_policy(), branches=['lts'])
        ref = 'refs/heads/ce-promotion-test'
        self.case.repo.git('update-ref', ref, self.case.head)
        bundle = self.case.root/'approved.bundle'
        self.case.repo.git('bundle', 'create', str(bundle), ref, '^'+self.case.repo.base)
        manifest.update(bundle=str(bundle), bundle_ref=ref)
        original_repo = self.case.api.repo
        creates = []
        def repo(path, method='GET', data=None, **kwargs):
            if path.startswith('/pulls?'):
                return []
            if path == '/pulls' and method == 'POST':
                creates.append(data)
                self.assertTrue(data['head'].startswith('promotion/'))
                self.case.api.add_pull(5, self.case.head, data['base'], 'example/ce')
                self.case.api.pulls[5]['head']['ref'] = data['head']
                return self.case.api.pull(5)
            return original_repo(path, method, data, **kwargs)
        self.case.api.repo = repo
        real_run = subprocess.run
        pushes = []
        def local_run(args, **kwargs):
            if 'https://github.com/example/ce.git' in args:
                pushes.append(args)
                args = [str(self.case.remote) if a == 'https://github.com/example/ce.git' else a for a in args]
            return real_run(args, **kwargs)
        with patch('scripts.ce_merge.release.PublicGraph', self.case.graph), \
                patch('scripts.ce_merge.release.subprocess.run', side_effect=local_run), \
                patch.object(self.case.api, 'token', return_value='test-token'):
            self.assertEqual(publish(self.c.policy, self.case.store, self.case.api, manifest), 5)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(creates), 1)
        self.case.unchanged()
        self.assertEqual(self.case.store.active_promotions('release-1')[0]['pr'], 5)


if __name__ == '__main__':
    unittest.main()
