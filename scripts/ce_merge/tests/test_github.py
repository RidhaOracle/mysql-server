# Copyright (c) 2026, Oracle and/or its affiliates.
"""Exercise trusted review/OCA evidence with API-shaped responses, without network."""
import copy
import unittest
from scripts.ce_merge.github import GitHub
from scripts.ce_merge.policy import Blocked


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 12, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40},
                   "user": {"login": "contributor"}, "labels": [{"name": "OCA Verified"}]}
        self.resolved = True
        self.permission = "write"
        self.role = "maintain"
        self.reviews = [{"user": {"login": "owner"}, "state": "APPROVED", "commit_id": "b" * 40}]
        self.events = [{"event": "labeled", "label": {"name": "OCA Verified"}, "actor": {"login": "owner"}}]
        self.api = GitHub({"repository": "org/ce", "maintainers": ["owner"], "oca_label": "OCA Verified"})
        self.api.repo = self.repo
        self.api.pages = lambda suffix: self.reviews if suffix.endswith("/reviews") else self.events
        self.api.request = self.request

    def repo(self, path, *args, **kwargs):
        if path.startswith("/collaborators/"):
            return {"permission": self.permission, "role_name": self.role}
        raise AssertionError(path)

    def request(self, method, path, payload):
        if "reviewThreads" in payload["query"]:
            value = {"reviewThreads": {"nodes": [{"isResolved": self.resolved}],
                     "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        else:
            raise AssertionError(payload["query"])
        return {"data": {"repository": {"pullRequest": value}}}

    def test_current_maintainer_review_passes_without_codeowners(self):
        self.api.reviewed(self.pr)
        self.api.oca(self.pr)

    def test_resolved_threads_required(self):
        self.resolved = False
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_stale_and_dismissed_approvals_rejected(self):
        valid = copy.deepcopy(self.reviews)
        for update in ({"commit_id": "c" * 40}, {"state": "DISMISSED"}):
            self.reviews = copy.deepcopy(valid)
            self.reviews[0].update(update)
            with self.subTest(update=update), self.assertRaises(Blocked):
                self.api.reviewed(self.pr)

    def test_bot_does_not_filter_author_reviews(self):
        # Synthetic evidence: GitHub itself prevents author APPROVE reviews.
        self.reviews[0]["user"]["login"] = self.pr["user"]["login"]
        self.api.reviewed(self.pr)
        self.role = "write"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_latest_change_request_revokes_approval(self):
        self.reviews.append(dict(self.reviews[0], state="CHANGES_REQUESTED"))
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_admin_reviewer_passes(self):
        self.role, self.permission = "admin", "admin"
        self.api.reviewed(self.pr)

    def test_non_maintainer_or_missing_role_cannot_approve(self):
        for role in ("write", "triage", "read", None):
            self.role = role
            with self.subTest(role=role), self.assertRaises(Blocked):
                self.api.reviewed(self.pr)

    def test_approval_does_not_require_integration_allowlist_membership(self):
        self.api.policy["maintainers"] = ["someone-else"]
        self.api.reviewed(self.pr)

    def test_current_role_is_rechecked(self):
        self.api.reviewed(self.pr)
        self.role = "write"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_another_maintainer_change_request_blocks_approval(self):
        self.reviews.append(dict(self.reviews[0], user={"login": "second-maintainer"}, state="CHANGES_REQUESTED"))
        with self.assertRaisesRegex(Blocked, "requested changes"):
            self.api.reviewed(self.pr)

    def test_dismissed_change_request_no_longer_blocks(self):
        request = dict(self.reviews[0], user={"login": "second-maintainer"}, state="CHANGES_REQUESTED")
        self.reviews.extend([request, dict(request, state="DISMISSED")])
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
