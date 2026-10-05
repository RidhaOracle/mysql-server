# Copyright (c) 2026, Oracle and/or its affiliates.
"""Exercise trusted review/OCA evidence with API-shaped responses, without network."""
import base64
import copy
import unittest
from scripts.ce_merge.github import GitHub
from scripts.ce_merge.policy import Blocked


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 12, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40},
                   "user": {"login": "contributor"}, "labels": [{"name": "OCA Verified"}]}
        self.owner_file = "* @owner\n"
        self.owner_errors = []
        self.decision = "APPROVED"
        self.resolved = True
        self.permission = "write"
        self.reviews = [{"user": {"login": "owner"}, "state": "APPROVED", "commit_id": "b" * 40}]
        self.events = [{"event": "labeled", "label": {"name": "OCA Verified"}, "actor": {"login": "owner"}}]
        self.api = GitHub({"repository": "org/ce", "maintainers": ["owner"], "oca_label": "OCA Verified"})
        self.api.repo = self.repo
        self.api.pages = lambda suffix: self.reviews if suffix.endswith("/reviews") else self.events
        self.api.request = self.request

    def repo(self, path, *args, **kwargs):
        if path.startswith("/contents/"):
            return {"encoding": "base64", "size": len(self.owner_file),
                    "content": base64.b64encode(self.owner_file.encode()).decode()}
        if path.startswith("/codeowners/errors"):
            return {"errors": self.owner_errors}
        if path.startswith("/collaborators/"):
            return {"permission": self.permission}
        raise AssertionError(path)

    def request(self, method, path, payload):
        if "reviewThreads" in payload["query"]:
            value = {"reviewThreads": {"nodes": [{"isResolved": self.resolved}],
                     "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        else:
            value = {"reviewDecision": self.decision}
        return {"data": {"repository": {"pullRequest": value}}}

    def test_current_independent_ownership_review_passes(self):
        self.api.reviewed(self.pr)
        self.api.oca(self.pr)

    def test_native_owner_decision_and_resolved_threads_required(self):
        self.decision = "REVIEW_REQUIRED"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)
        self.decision, self.resolved = "APPROVED", False
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_stale_self_and_dismissed_approvals_rejected(self):
        valid = copy.deepcopy(self.reviews)
        for update in ({"commit_id": "c" * 40}, {"user": {"login": "contributor"}}, {"state": "DISMISSED"}):
            self.reviews = copy.deepcopy(valid)
            self.reviews[0].update(update)
            with self.subTest(update=update), self.assertRaises(Blocked):
                self.api.reviewed(self.pr)

    def test_latest_change_request_revokes_approval(self):
        self.reviews.append(dict(self.reviews[0], state="CHANGES_REQUESTED"))
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_ownerless_rules_and_invalid_owners_rejected(self):
        self.owner_file = "* @owner\nprivate/\n"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)
        self.owner_file, self.owner_errors = "* @owner\n", [{"message": "invalid owner"}]
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_read_only_reviewer_cannot_approve(self):
        self.permission = "read"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_oca_label_requires_latest_trusted_verification(self):
        self.events[0]["actor"]["login"] = "contributor"
        with self.assertRaises(Blocked):
            self.api.oca(self.pr)
        self.events[0]["actor"]["login"] = "owner"
        self.events.append(dict(self.events[0], event="unlabeled"))
        with self.assertRaises(Blocked):
            self.api.oca(self.pr)


if __name__ == "__main__":
    unittest.main()
