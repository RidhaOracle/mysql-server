# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import tempfile
import unittest
from unittest.mock import Mock

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked
from test_coordinator import POLICY


class CheckScopeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Journal(folder.name)
        self.api = Mock()
        self.policy = copy.deepcopy(POLICY)
        self.policy['branches'] = ['rehearsal']
        self.pr = {'number': 56, 'head': {'sha': 'a'*40}, 'base': {'ref': 'trunk'}, 'labels': []}
        self.c = Coordinator(self.policy, self.store, self.api)
        self.api.pages.return_value = [self.pr]

    def seed_previous(self):
        self.store.set_setting('published:56', ['Merge check', 'shadow', 'OCA verification is missing'], 'test')

    def test_scope_change_supersedes_stale_failure_without_merge_action(self):
        self.seed_previous()
        self.c.work()
        args, kwargs = self.api.check.call_args
        self.assertEqual(args[:3], (self.pr, 'Merge check', 'neutral'))
        self.assertIn('`trunk`', args[3])
        self.assertIn('outside', args[3])
        self.assertFalse(kwargs['action'])
        self.api.advisory_label.assert_called_once_with(self.pr, False)
        self.api.oca.assert_not_called()
        self.api.pull.assert_not_called()
        self.c.work()
        self.api.check.assert_called_once()

    def test_never_managed_pr_is_ignored(self):
        self.c.work()
        self.api.check.assert_not_called()

    def test_retarget_to_configured_branch_gets_normal_evaluation(self):
        self.seed_previous()
        self.c.work()
        self.pr['base']['ref'] = 'rehearsal'
        self.api.pull.return_value = self.pr
        self.c.evaluate = Mock()
        self.c.work()
        self.c.evaluate.assert_called_once_with(self.pr)
        with self.assertRaises(Blocked):
            self.c.publish_unmanaged(self.pr)

    def test_failure_to_publish_does_not_cache_new_status(self):
        self.seed_previous()
        previous = self.store.setting('published:56')
        self.api.check.side_effect = ConnectionError('unavailable')
        with self.assertRaises(ConnectionError):
            self.c.work()
        self.assertEqual(self.store.setting('published:56'), previous)

    def test_new_head_replaces_outdated_scope_result(self):
        self.seed_previous()
        self.c.work()
        self.pr['head']['sha'] = 'b'*40
        self.c.work()
        self.assertEqual(self.api.check.call_count, 2)
        self.assertEqual(self.store.setting('published:56')[2], 'b'*40)
