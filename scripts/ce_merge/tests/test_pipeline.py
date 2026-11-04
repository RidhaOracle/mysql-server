# Copyright (c) 2026, Oracle and/or its affiliates.
"""Exercise the fixed pipeline against real disposable Git repositories."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.pipeline import prepare, publish
from scripts.ce_merge.policy import Blocked
from scripts.ce_merge.tests.fixtures import Repo, FakeGitHub, POLICY


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = Repo(self.root / 'source')
        self.remote = self.root / 'remote.git'
        self.repo.git('clone', '--bare', str(self.repo.path), str(self.remote))
        self.repo.git('checkout', '-b', 'fix')
        self.head = self.repo.commit('fix.cc', 'int fix;\n')
        self.repo.git('push', str(self.remote), 'HEAD:refs/pull/1/head')
        self.api = FakeGitHub(self)
        self.api.pulls[1]['title'] = 'Public fix'
        self.lose_response = False
        case = self

        class Graph(PublicGraph):
            def __init__(self, policy):
                super().__init__(policy, str(case.remote))

        graph = patch('scripts.ce_merge.pipeline.PublicGraph', Graph)
        graph.start()
        self.addCleanup(graph.stop)
        env = patch.dict(os.environ, GITHUB_RUN_ID='123')
        env.start()
        self.addCleanup(env.stop)
        self.directory = self.root / 'pipeline'
        self.directory.mkdir()

    def remote_git(self, *args):
        return subprocess.check_output(['git', '--git-dir', str(self.remote), *args], text=True).strip()

    def plan(self):
        prepare(self.api, POLICY, 1, self.directory, 'owner')
        return json.loads((self.directory / 'plan.json').read_text())

    def test_prepare_changes_no_remote_branches_and_exports_testable_bundle(self):
        plan = self.plan()
        self.assertEqual(self.api.branch('lts'), self.repo.base)
        self.assertEqual(self.api.branch('trunk'), self.repo.base)
        self.assertEqual(self.api.merges, [])
        with PublicGraph(POLICY, str(self.remote)) as graph:
            graph.restore_candidates(plan['steps'], self.directory / 'candidates.bundle')
            for step in plan['steps']:
                self.assertEqual(graph.tree(step['after']), step['tree'])
                self.assertEqual(graph.parents(step['after'])[0], step['base_sha'])

    def test_prepare_exports_bundle_with_relative_output_directory(self):
        before = Path.cwd()
        try:
            os.chdir(self.root)
            prepare(self.api, POLICY, 1, Path('pipeline'), 'owner')
            self.assertTrue((self.directory / 'candidates.bundle').is_file())
            self.assertTrue((self.directory / 'plan.json').is_file())
        finally:
            os.chdir(before)

    def test_publish_native_merges_preserve_source_commit_in_trunk(self):
        plan = self.plan()
        publish(self.api, POLICY, plan)
        self.assertEqual(self.api.merges, [(1, 'squash'), (2, 'merge')])
        source = self.api.branch('lts')
        trunk = self.api.branch('trunk')
        self.assertEqual(self.remote_git('show', '-s', '--format=%P', trunk).split(), [self.repo.base, source])
        self.assertEqual(self.remote_git('rev-parse', trunk + '^{tree}'), plan['steps'][1]['tree'])

    def test_moved_target_stops_before_first_merge(self):
        plan = self.plan()
        moved = self.repo.commit('other.cc', 'int other;\n')
        self.repo.git('push', str(self.remote), moved + ':trunk')
        with self.assertRaisesRegex(Blocked, 'target moved'):
            publish(self.api, POLICY, plan)
        self.assertEqual(self.api.merges, [])

    def test_changed_source_or_revoked_approval_stops_publication(self):
        plan = self.plan()
        for change in ('body', 'head', 'review', 'oca', 'maintainer'):
            self.api = FakeGitHub(self)
            if change == 'body':
                self.api.pulls[1]['body'] = 'Edited'
            elif change == 'head':
                self.api.pulls[1]['head']['sha'] = 'f' * 40
            elif change == 'review':
                self.api.approved = False
            elif change == 'oca':
                self.api.oca_ok = False
            else:
                self.api.authorized = False
            with self.subTest(change=change), self.assertRaises(Blocked):
                publish(self.api, POLICY, plan)
            self.assertEqual(self.api.merges, [])

    def test_lost_merge_response_is_not_retried(self):
        plan = self.plan()
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            publish(self.api, POLICY, plan)
        self.assertEqual(self.api.merges, [(1, 'squash')])
        with self.assertRaises(Blocked):
            publish(self.api, POLICY, plan)
        self.assertEqual(self.api.merges, [(1, 'squash')])

    def test_conflict_stops_preparation_before_any_merge(self):
        self.repo.git('checkout', 'trunk')
        target = self.repo.commit('fix.cc', 'conflicting change\n')
        self.repo.git('push', str(self.remote), target + ':trunk')
        with self.assertRaises(Blocked):
            self.plan()
        self.assertEqual(self.api.merges, [])
        self.assertFalse((self.directory / 'plan.json').exists())


class AuthorizationTests(unittest.TestCase):
    def test_only_live_unedited_maintainer_comments_authorize(self):
        from unittest.mock import MagicMock
        from scripts.ce_merge.pipeline import authorize
        comment = dict(id=1, body='/integrate', user=dict(id=2, login='owner'),
                       created_at='2026-11-03T10:00:00Z', updated_at='2026-11-03T10:00:00Z',
                       issue_url='https://api.github.com/repos/example/ce/issues/1')
        event = dict(action='created', comment=copy.deepcopy(comment),
                     issue=dict(number=1, pull_request=dict(url='test')))
        api = MagicMock()
        api.repo.return_value = comment
        api.can_integrate.return_value = True
        with patch.dict(os.environ, GITHUB_REPOSITORY='example/ce'):
            self.assertEqual(authorize(api, POLICY, event), (1, 'owner'))
            for change in ('permission', 'edited', 'author', 'issue', 'repository'):
                api.repo.return_value = copy.deepcopy(comment)
                api.can_integrate.return_value = change != 'permission'
                if change == 'edited':
                    api.repo.return_value['updated_at'] = '2026-11-03T10:00:01Z'
                elif change == 'author':
                    api.repo.return_value['user']['id'] = 99
                elif change == 'issue':
                    api.repo.return_value['issue_url'] += '2'
                with self.subTest(change=change), patch.dict(os.environ,
                        GITHUB_REPOSITORY='wrong/repo' if change == 'repository' else 'example/ce'), \
                        self.assertRaises(Blocked):
                    authorize(api, POLICY, event)
