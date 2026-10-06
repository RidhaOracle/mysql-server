# Copyright (c) 2026, Oracle and/or its affiliates.
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked


class PromotionJournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Journal(Path(directory.name))
        self.old = {"pr": 1, "release_tag": "release-1", "base": "lts", "head": "a" * 40}
        self.new = dict(self.old, pr=2, head="b" * 40)

    def test_replacement_survives_restart_and_preserves_history(self):
        self.store.activate_promotion(self.old, "operator")
        self.store.activate_promotion(self.new, "operator")
        store = Journal(self.store.path)
        self.assertEqual(store.active_promotions("release-1"), [self.new])
        self.assertEqual(store.setting("promotion:1"), self.old)
        events = [json.loads(line) for line in (store.path / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["data"]["value"] == self.old for event in events))
        self.assertTrue(any(event["data"]["value"] == self.new for event in events))

    def test_other_targets_and_releases_remain_active(self):
        upper = dict(self.old, pr=3, base="trunk")
        other = dict(self.old, pr=4, release_tag="release-2")
        for manifest in (self.old, upper, other, self.new):
            self.store.activate_promotion(manifest, "operator")
        self.assertCountEqual(self.store.active_promotions("release-1"), [self.new, upper])
        self.assertEqual(self.store.active_promotions("release-2"), [other])

    def test_duplicate_registration_is_idempotent(self):
        self.store.activate_promotion(self.old, "operator")
        before = (self.store.path / "audit.jsonl").read_bytes()
        self.store.activate_promotion(self.old, "operator")
        self.assertEqual((self.store.path / "audit.jsonl").read_bytes(), before)

    def test_legacy_duplicates_require_explicit_registration(self):
        for manifest in (self.old, self.new):
            self.store.set_setting(f'promotion:{manifest["pr"]}', manifest, "operator")
        with self.assertRaisesRegex(Blocked, "Multiple legacy"):
            self.store.active_promotions("release-1")
        self.store.activate_promotion(self.new, "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [self.new])
        self.assertEqual(self.store.setting("promotion:1"), self.old)

    def test_single_legacy_manifest_still_works(self):
        self.store.set_setting("promotion:1", self.old, "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [self.old])

    def test_rebuilt_head_on_same_pr_preserves_previous_audit(self):
        self.store.activate_promotion(self.old, "operator")
        replacement = dict(self.new, pr=1)
        self.store.activate_promotion(replacement, "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [replacement])
        events = [json.loads(line) for line in (self.store.path / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["data"]["value"] == self.old for event in events))

    def test_same_pr_cannot_be_reassigned_to_another_release_or_target(self):
        self.store.activate_promotion(self.old, "operator")
        for updated in (dict(self.old, release_tag="release-2"), dict(self.old, base="trunk")):
            with self.subTest(manifest=updated), self.assertRaisesRegex(Blocked, "reassigned"):
                self.store.activate_promotion(updated, "operator")

    def test_interrupted_replacement_keeps_previous_snapshot(self):
        self.store.activate_promotion(self.old, "operator")
        write = self.store.write
        def fail(path, value):
            if path == self.store.setting_path(self.store.promotion_key(self.new)):
                raise OSError("simulated interrupted activation")
            write(path, value)
        with patch.object(self.store, "write", side_effect=fail), self.assertRaises(OSError):
            self.store.activate_promotion(self.new, "operator")
        self.assertEqual(Journal(self.store.path).active_promotions("release-1"), [self.old])
        self.store.activate_promotion(self.new, "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [self.new])

    def test_uncertain_or_completed_publication_cannot_be_replaced(self):
        self.store.activate_promotion(self.old, "operator")
        pr = {"number": 1, "head": {"sha": self.old["head"]}, "base": {"ref": "lts"}}
        operation = self.store.request(1, pr, "owner")
        op = next(o for o in self.store.operations() if o["id"] == operation)
        for state in ("queued", "prepared", "conflict", "blocked", "publishing", "uncertain", "complete"):
            op["data"]["intent"] = state in ("publishing", "uncertain", "complete")
            self.store.save(op, state, "test-state")
            with self.subTest(state=state), self.assertRaises(Blocked):
                self.store.activate_promotion(self.new, "operator")
        op["data"]["intent"] = False
        self.store.save(op, "aborted", "test-abort")
        self.store.activate_promotion(self.new, "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [self.new])

    def test_retirement_preserves_history_and_suppresses_legacy_fallback_after_restart(self):
        self.store.activate_promotion(self.old, "operator")
        self.store.set_setting("hold", {"release_tag": "release-1", "targets": ["trunk"]}, "operator")
        self.store.retire_promotion("release-1", "lts", "No longer targeted", "operator")
        store = Journal(self.store.path)
        self.assertEqual(store.active_promotions("release-1"), [])
        self.assertEqual(store.setting("promotion:1"), self.old)
        events = [json.loads(line) for line in (store.path / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["data"]["value"] == self.old for event in events))
        self.assertTrue(any(event["data"]["value"].get("retired") for event in events))
        before = (store.path / "audit.jsonl").read_bytes()
        store.retire_promotion("release-1", "lts", "No longer targeted", "operator")
        self.assertEqual((store.path / "audit.jsonl").read_bytes(), before)
        store.activate_promotion(self.new, "operator")
        self.assertEqual(store.active_promotions("release-1"), [self.new])

    def test_retirement_handles_legacy_manifests_without_reactivating_them(self):
        for manifest in (self.old, self.new):
            self.store.set_setting(f'promotion:{manifest["pr"]}', manifest, "legacy")
        self.store.set_setting("hold", {"release_tag": "release-1", "targets": ["trunk"]}, "operator")
        self.store.retire_promotion("release-1", "lts", "Scope reduced", "operator")
        self.assertEqual(Journal(self.store.path).active_promotions("release-1"), [])

    def test_retirement_requires_matching_revised_hold_and_reason(self):
        self.store.activate_promotion(self.old, "operator")
        for hold in (None, {"release_tag": "other", "targets": []},
                     {"release_tag": "release-1", "targets": ["lts"]}):
            self.store.set_setting("hold", hold, "operator")
            with self.subTest(hold=hold), self.assertRaises(Blocked):
                self.store.retire_promotion("release-1", "lts", "Scope reduced", "operator")
        self.store.set_setting("hold", {"release_tag": "release-1", "targets": ["trunk"]}, "operator")
        with self.assertRaises(Blocked):
            self.store.retire_promotion("release-1", "lts", " ", "operator")
        with self.assertRaises(Blocked):
            self.store.retire_promotion("release-1", "unknown", "Scope reduced", "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [self.old])

    def test_retirement_requires_aborted_operations_and_rejects_completed_release(self):
        self.store.activate_promotion(self.old, "operator")
        self.store.set_setting("hold", {"release_tag": "release-1", "targets": ["trunk"]}, "operator")
        pr = {"number": 1, "head": {"sha": self.old["head"]}, "base": {"ref": "lts"}}
        self.store.request(1, pr, "owner")
        op = self.store.operations()[0]
        for state in ("queued", "prepared", "conflict", "blocked", "publishing", "uncertain", "complete"):
            op["data"]["intent"] = state in ("publishing", "uncertain", "complete")
            self.store.save(op, state, "test-state")
            with self.subTest(state=state), self.assertRaises(Blocked):
                self.store.retire_promotion("release-1", "lts", "Scope reduced", "operator")
        op["data"]["intent"] = False
        self.store.save(op, "aborted", "test-abort")
        self.store.retire_promotion("release-1", "lts", "Scope reduced", "operator")
        self.assertEqual(self.store.active_promotions("release-1"), [])


if __name__ == "__main__":
    unittest.main()
