# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import tempfile
import unittest

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked, rulesets
from test_coordinator import POLICY


class RulesAPI:
    def __init__(self, policy):
        self.rows = [dict(r, id=i+1, updated_at='2026-10-08T12:00:00.001Z',
                          source=policy['repository'], source_type='Repository')
                     for i, r in enumerate(rulesets(policy))]
        self.hidden = True

    def pages(self, suffix):
        return copy.deepcopy(self.rows)

    def repo(self, suffix):
        if not suffix:
            return dict(allow_merge_commit=True, allow_squash_merge=True,
                        allow_rebase_merge=False, allow_auto_merge=False)
        row = copy.deepcopy(self.rows[int(suffix.rsplit('/', 1)[1])-1])
        if self.hidden:
            row.pop('bypass_actors')
        return row


class DeploymentAttestationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.policy = copy.deepcopy(POLICY)
        self.api = RulesAPI(self.policy)
        self.store = Journal(folder.name)
        self.c = Coordinator(self.policy, self.store, self.api)
        self.snapshot = dict(repository=self.policy['repository'], rulesets=copy.deepcopy(self.api.rows))

    def test_hidden_bypasses_fail_without_attestation(self):
        with self.assertRaisesRegex(Blocked, 'administrator must attest'):
            self.c.deployment()

    def test_trusted_snapshot_enables_read_only_verification_and_survives_restart(self):
        self.c.attest_deployment(self.snapshot, 'admin')
        Coordinator(self.policy, Journal(self.store.path), self.api).deployment()
        self.assertIn('deployment-attestation', (self.store.path/'audit.jsonl').read_text())

    def test_changed_ruleset_revision_identity_or_origin_invalidates_snapshot(self):
        self.c.attest_deployment(self.snapshot, 'admin')
        for key, value in [('updated_at', 'later'), ('id', 1000), ('source', 'elsewhere/ce'),
                           ('source_type', 'Organization')]:
            with self.subTest(key=key):
                old = self.api.rows[0][key]
                self.api.rows[0][key] = value
                # Keep list IDs stable so the fake API still retrieves the row.
                if key == 'id':
                    original_pages = self.api.pages
                    self.api.pages = lambda suffix: copy.deepcopy(self.snapshot['rulesets'])
                try:
                    with self.assertRaisesRegex(Blocked, 'changed since administrator'):
                        self.c.deployment()
                finally:
                    self.api.rows[0][key] = old
                    if key == 'id': self.api.pages = original_pages

    def test_equivalent_timestamp_offsets_match(self):
        for row in self.snapshot['rulesets']:
            row['updated_at'] = '2026-10-08T14:00:00.001+02:00'
        self.c.attest_deployment(self.snapshot, 'admin')
        self.c.deployment()

    def test_changed_policy_requires_new_attestation(self):
        self.c.attest_deployment(self.snapshot, 'admin')
        self.policy['maintainers'].append('new-maintainer')
        with self.assertRaisesRegex(Blocked, 'administrator must attest'):
            self.c.deployment()

    def test_live_visible_rules_are_still_checked(self):
        self.c.attest_deployment(self.snapshot, 'admin')
        self.api.rows[0]['enforcement'] = 'disabled'
        with self.assertRaisesRegex(Blocked, 'differs from approved policy'):
            self.c.deployment()

    def test_bad_snapshot_is_never_stored(self):
        cases = []
        wrong_repo = copy.deepcopy(self.snapshot); wrong_repo['repository'] = 'wrong/repo'; cases.append(wrong_repo)
        stale = copy.deepcopy(self.snapshot); stale['rulesets'][0]['updated_at'] = 'old'; cases.append(stale)
        hidden = copy.deepcopy(self.snapshot); hidden['rulesets'][0].pop('bypass_actors'); cases.append(hidden)
        bypass = copy.deepcopy(self.snapshot); bypass['rulesets'][0]['bypass_actors'] = []; cases.append(bypass)
        duplicate = copy.deepcopy(self.snapshot); duplicate['rulesets'].append(duplicate['rulesets'][0]); cases.append(duplicate)
        missing = copy.deepcopy(self.snapshot); missing['rulesets'] = []; cases.append(missing)
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(Blocked):
                self.c.attest_deployment(snapshot, 'admin')
            self.assertIsNone(self.store.setting('deployment-attestation'))

    def test_visible_bypasses_use_live_data_even_after_attestation(self):
        self.c.attest_deployment(self.snapshot, 'admin')
        self.api.hidden = False
        self.api.rows[0]['bypass_actors'] = []
        with self.assertRaisesRegex(Blocked, 'differs from approved policy'):
            self.c.deployment()
