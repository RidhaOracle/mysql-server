# Copyright (c) 2026, Oracle and/or its affiliates.
"""Real Git checkpoints survive runner loss and reject conflicting publishers."""
import unittest
from unittest.mock import patch

from scripts.ce_merge.journal import PersistenceError
from scripts.ce_merge.policy import Blocked
from scripts.ce_merge.state import GitJournal
import test_coordinator as fixtures

POLICY = fixtures.POLICY


class StateTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.SerialTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.counter = 0
        self.restore(initialize=True)
        self.case.operation = self.case.store.request(123, self.case.api.pull(1), "owner")

    def new_store(self, initialize=False):
        self.counter += 1
        return GitJournal(self.case.root / f"runner-{self.counter}", POLICY,
                          initialize=initialize, remote=str(self.case.remote))

    def restore(self, initialize=False):
        self.case.store = self.new_store(initialize)
        self.case.coordinator.store = self.case.store

    def test_prepared_bundle_and_audit_survive_a_fresh_runner(self):
        before = self.case.prepare()
        self.restore()
        self.assertEqual(self.case.op(), before)
        self.assertTrue(self.case.coordinator.bundle(self.case.op()).is_file())
        self.assertIn("candidates-prepared", (self.case.store.path / "audit.jsonl").read_text())
        self.case.ci_ok = True
        self.case.finish()
        self.assertEqual(self.case.op()["state"], "complete")
        self.restore()
        self.assertEqual(self.case.op()["state"], "complete")
        files = self.case.remote_git("ls-tree", "-r", "--name-only", "ce-merge-state").splitlines()
        self.assertTrue(all(GitJournal.allowed(name) for name in files))
        self.assertFalse(any(name.endswith(".lock") for name in files))

    def test_a_second_writer_cannot_checkpoint_stale_state(self):
        stale = self.new_store()
        self.case.store.set_setting("hold", {"reason": "first writer"}, "owner")
        with self.assertRaisesRegex(PersistenceError, "Another writer"):
            stale.set_setting("hold", {"reason": "stale writer"}, "owner")
        with self.assertRaises(PersistenceError):
            stale.checkpoint()
        self.restore()
        self.assertEqual(self.case.store.setting("hold"), {"reason": "first writer"})

    def test_local_disk_failure_disables_all_further_remote_writes(self):
        store = self.case.store
        with patch("scripts.ce_merge.journal.tempfile.mkstemp", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(PersistenceError, "Local state write"):
                store.set_setting("hold", {"reason": "not persisted"}, "owner")
        with self.assertRaises(PersistenceError):
            store.checkpoint()
        self.restore()
        self.assertIsNone(self.case.store.setting("hold"))
        self.case.unchanged()

    def test_missing_state_branch_is_not_silently_reinitialized(self):
        self.case.remote_git("update-ref", "-d", "refs/heads/ce-merge-state")
        with self.assertRaisesRegex(Blocked, "init-state"):
            self.new_store()
        self.case.unchanged()

    def test_rejected_preparation_checkpoint_stops_before_staging(self):
        hook = self.case.remote / "hooks/update"
        hook.write_text('#!/bin/sh\n[ "$1" != "refs/heads/ce-merge-state" ]\n')
        hook.chmod(0o755)
        with self.assertRaises(PersistenceError):
            self.case.coordinator.tick()
        self.case.unchanged()
        self.assertEqual(self.case.remote_git("for-each-ref", "--format=%(refname)", "refs/heads/merge"), "")
        self.assertEqual(self.case.remote_git("for-each-ref", "--format=%(refname)", "refs/heads/merge-tests"), "")
        hook.unlink()
        self.restore()
        self.assertEqual(self.case.op()["state"], "queued")

    def test_intent_must_be_durable_before_any_target_publishes(self):
        store = self.case.store
        checkpoint = store.checkpoint

        def fail_intent():
            if any(o["data"]["intent"] for o in store.operations()):
                store.failed = True
                raise PersistenceError("Simulated rejected publication intent checkpoint")
            return checkpoint()

        with patch.object(store, "checkpoint", side_effect=fail_intent):
            with self.assertRaises(PersistenceError):
                self.case.coordinator.tick()
        self.case.unchanged()
        self.restore()
        self.assertFalse(self.case.op()["data"]["intent"])
        self.assertEqual(self.case.op()["state"], "prepared")

    def test_lost_state_push_response_latches_failure_and_restores_remote_truth(self):
        store = self.case.store
        run = store.run

        def lose_response(*args, **kwargs):
            result = run(*args, **kwargs)
            if args[0] == "push":
                raise PersistenceError("Simulated lost checkpoint response")
            return result

        with patch.object(store, "run", side_effect=lose_response):
            with self.assertRaises(PersistenceError):
                store.set_setting("hold", {"reason": "checkpoint reached server"}, "owner")
        with self.assertRaises(PersistenceError):
            store.ensure_healthy()
        self.restore()
        self.assertEqual(self.case.store.setting("hold"), {"reason": "checkpoint reached server"})
        self.case.unchanged()

    def test_successful_merge_with_lost_confirmation_checkpoint_reconciles(self):
        store = self.case.store
        checkpoint = store.checkpoint

        def lose_confirmation():
            if any(o['data']['steps'] and o['data']['steps'][0].get('merged_sha')
                   for o in store.operations()):
                store.failed = True
                raise PersistenceError('Confirmation checkpoint unavailable')
            return checkpoint()

        with patch.object(store, 'checkpoint', side_effect=lose_confirmation):
            with self.assertRaises(PersistenceError):
                self.case.coordinator.tick()
        lower = self.case.api.branch('lts')
        self.restore()
        self.assertTrue(self.case.op()['data']['intent'])
        self.case.coordinator.tick()
        self.assertFalse(self.case.op()['data']['intent'])
        self.assertEqual(self.case.api.branch('lts'), lower)
        self.assertEqual(self.case.api.merges, [(1, 'squash')])
        self.case.finish()

    def test_lost_gh_merge_response_reconciles_after_runner_replacement(self):
        self.case.lose_response = True
        with self.assertRaises(TimeoutError):
            self.case.coordinator.advance(self.case.op())
        self.restore()
        self.assertEqual(self.case.op()['state'], 'uncertain')
        self.case.coordinator.tick()
        self.assertEqual(self.case.api.merges, [(1, 'squash')])
        self.case.lose_response = False
        self.case.finish()

    def test_partial_merge_and_soft_lock_survive_a_fresh_runner(self):
        self.case.coordinator.advance(self.case.op())
        lower = self.case.api.branch('lts')
        self.case.api.requested_changes.add(2)
        self.restore()
        self.assertEqual(self.case.store.setting('merge-lock'), self.case.operation)
        self.case.coordinator.tick()
        self.assertEqual(self.case.api.branch('lts'), lower)
        self.assertEqual(self.case.api.branch('trunk'), self.case.repo.base)
        self.case.api.requested_changes.clear()
        self.case.finish()
        self.assertEqual(self.case.api.merges, [(1, 'squash'), (2, 'merge')])

    def test_restore_rejects_symlinks_before_checkout(self):
        store = self.case.store
        blob = store.run("hash-object", "-w", "--stdin", input=b"/tmp/untrusted-link")
        name = "journal/settings/" + "a" * 64 + ".json"
        store.run("update-index", "--add", "--cacheinfo", f"120000,{blob},{name}")
        tree = store.run("write-tree")
        commit = store.run("commit-tree", tree, "-p", store.head, input=b"Invalid state fixture\n")
        store.run("push", store.remote, commit + ":" + store.ref)
        with self.assertRaisesRegex(Blocked, "Unexpected file"):
            self.new_store()


if __name__ == "__main__":
    unittest.main()
