# Copyright (c) 2026, Oracle and/or its affiliates.
"""Exercise trusted review/OCA evidence with API-shaped responses, without network."""
import copy
import os
import unittest
from unittest.mock import MagicMock, patch
from scripts.ce_merge.github import GitHub
from scripts.ce_merge.policy import Blocked


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 12, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40},
                   "user": {"login": "contributor"}, "labels": [{"name": "OCA Verified"}]}
        self.review_decision = 'APPROVED'
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
            value = {"reviewDecision": self.review_decision, "reviewThreads": {"nodes": [{"isResolved": self.resolved}],
                     "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        else:
            raise AssertionError(payload["query"])
        return {"data": {"repository": {"pullRequest": value}}}

    def test_current_maintainer_review_passes_without_codeowners(self):
        self.api.reviewed(self.pr)
        self.api.oca(self.pr)

    def test_pull_refreshes_old_base_from_current_target_branch(self):
        self.pr["base"].update(ref="release/lts")
        calls = []
        def repo(path):
            calls.append(path)
            if path == "/pulls/12":
                return copy.deepcopy(self.pr)
            if path == "/branches/release%2Flts":
                return {"commit": {"sha": "c" * 40}}
            raise AssertionError(path)
        self.api.repo = repo
        pr = self.api.pull(12)
        self.assertEqual(pr["base"]["sha"], "c" * 40)
        self.assertEqual(pr["head"], self.pr["head"])
        self.assertEqual(calls, ["/pulls/12", "/branches/release%2Flts"])

    def test_pull_does_not_fall_back_to_old_base_when_branch_unavailable(self):
        self.pr["base"]["ref"] = "lts"
        def repo(path):
            if path == "/pulls/12":
                return copy.deepcopy(self.pr)
            raise Blocked("GitHub API returned HTTP 404")
        self.api.repo = repo
        with self.assertRaisesRegex(Blocked, "404"):
            self.api.pull(12)

    def test_pull_rejects_invalid_current_branch_sha(self):
        self.pr["base"]["ref"] = "lts"
        self.api.repo = lambda path: copy.deepcopy(self.pr) if path == "/pulls/12" else {"commit": {"sha": "invalid"}}
        with self.assertRaisesRegex(Blocked, "Invalid commit SHA"):
            self.api.pull(12)


    def test_no_review_exception_still_checks_change_requests_and_threads(self):
        self.reviews = []
        self.api.reviewed(self.pr, require_approval=False)
        self.reviews = [{"user": {"login": "owner"}, "state": "CHANGES_REQUESTED", "commit_id": "b" * 40}]
        with self.assertRaisesRegex(Blocked, 'requested changes'):
            self.api.reviewed(self.pr, require_approval=False)
        self.reviews = []
        self.resolved = False
        with self.assertRaisesRegex(Blocked, 'Unresolved'):
            self.api.reviewed(self.pr, require_approval=False)

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
        self.role = "read"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_latest_change_request_revokes_approval(self):
        self.reviews.append(dict(self.reviews[0], state="CHANGES_REQUESTED"))
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_admin_reviewer_passes(self):
        self.role, self.permission = "admin", "admin"
        self.api.reviewed(self.pr)

    def test_write_collaborator_review_passes_without_integration_permission(self):
        self.role = "write"
        self.api.policy["maintainers"] = ["someone-else"]
        self.api.reviewed(self.pr)
        self.assertFalse(self.api.can_integrate("owner"))

    def test_read_only_or_missing_role_cannot_approve(self):
        for role in ("triage", "read", "none", None):
            self.role = role
            with self.subTest(role=role), self.assertRaises(Blocked):
                self.api.reviewed(self.pr)

    def test_approval_does_not_require_integration_allowlist_membership(self):
        self.api.policy["maintainers"] = ["someone-else"]
        self.api.reviewed(self.pr)

    def test_current_role_is_rechecked(self):
        self.api.reviewed(self.pr)
        self.role = "read"
        with self.assertRaises(Blocked):
            self.api.reviewed(self.pr)

    def test_another_maintainer_change_request_blocks_approval(self):
        self.reviews.append(dict(self.reviews[0], user={"login": "second-maintainer"}, state="CHANGES_REQUESTED"))
        with self.assertRaisesRegex(Blocked, "requested changes"):
            self.api.reviewed(self.pr)

    def test_write_collaborator_change_request_blocks_approval(self):
        self.role = "write"
        self.reviews.append(dict(self.reviews[0], user={"login": "collaborator"}, state="CHANGES_REQUESTED"))
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


class IdentityAndReportingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("GITHUB_RUN_ID", None)
        self.api = GitHub({"repository": "example/ce", "bot_user_id": 42})
        self.api.repo = MagicMock()
        self.api.pages = MagicMock(return_value=[])
        self.pr = {"number": 1, "head": {"sha": "a" * 40}}

    def test_token_must_belong_to_the_configured_automation_account(self):
        self.api.request = MagicMock(return_value={"id": 42})
        self.api.verify_identity()
        self.api.request.return_value = {"id": 99}
        with self.assertRaisesRegex(Blocked, "configured automation account"):
            self.api.verify_identity()

    def test_status_uses_commit_statuses_and_deduplicates_unchanged_results(self):
        self.api.status(self.pr, "Merge check", "failure", "CI did not pass")
        self.api.repo.assert_called_once_with("/statuses/" + "a" * 40, "POST", {
            "context": "Merge check", "state": "failure", "description": "CI did not pass"})
        self.api.repo.reset_mock()
        status = {"context": "Merge check", "state": "failure", "description": "CI did not pass",
                  "creator": {"id": 42}, "created_at": "2026-10-01T10:00:00Z"}
        self.api.pages.return_value = [status]
        self.assertEqual(self.api.status(self.pr, "Merge check", "failure", "CI did not pass"), status)
        self.api.repo.assert_not_called()

    def test_bot_only_updates_its_own_progress_comments(self):
        marker = "<!-- ce-merge:readiness -->\n"
        spoof = {"id": 1, "body": marker + "Spoof", "user": {"id": 99}}
        self.api.pages.return_value = [spoof]
        self.api.comment(self.pr, "readiness", "Ready")
        self.api.repo.assert_called_once_with("/issues/1/comments", "POST", {"body": marker + "Ready"})
        self.api.repo.reset_mock()
        self.api.pages.return_value = [spoof, {"id": 2, "body": marker + "Old", "user": {"id": 42}}]
        self.api.comment(self.pr, "readiness", "Ready")
        self.api.repo.assert_called_once_with("/issues/comments/2", "PATCH", {"body": marker + "Ready"})

    def test_lost_comment_response_is_recovered_by_lookup(self):
        body = "<!-- ce-merge:operation:test -->\nComplete"
        self.api.repo.side_effect = ConnectionError("Lost response")
        with self.assertRaises(ConnectionError):
            self.api.comment(self.pr, "operation:test", "Complete")
        self.api.repo.reset_mock()
        self.api.pages.return_value = [{"id": 2, "body": body, "user": {"id": 42}}]
        self.api.comment(self.pr, "operation:test", "Complete")
        self.api.repo.assert_not_called()

    def test_native_merge_uses_exact_head_and_explicit_method_without_bypass(self):
        self.api.token = lambda: 'test-secret'
        for method in ('squash', 'merge'):
            with self.subTest(method=method), patch('scripts.ce_merge.github.subprocess.run') as run:
                run.return_value.returncode = 0
                self.api.merge_pr(12, 'b' * 40, method, 'Reviewed title\n\nCo-authored-by: Person <person@example.com>')
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ['gh', 'pr', 'merge', '12'])
                self.assertIn('--' + method, command)
                self.assertIn('--match-head-commit', command)
                self.assertIn('b' * 40, command)
                self.assertNotIn('--admin', command)
                self.assertNotIn('--auto', command)
                self.assertNotIn('test-secret', command)
                self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], 'test-secret')
                self.assertIn(b'Co-authored-by:', run.call_args.kwargs['input'])

    def test_merge_failure_is_never_blindly_retried(self):
        self.api.token = lambda: 'test-secret'
        for result in (1, TimeoutError('lost connection')):
            with self.subTest(result=result), patch('scripts.ce_merge.github.subprocess.run') as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value.returncode = result
                with self.assertRaises((Blocked, TimeoutError)):
                    self.api.merge_pr(12, 'b' * 40, 'merge', 'Reviewed title')
                run.assert_called_once()

    def test_only_generated_merge_prs_use_the_explicit_review_exception(self):
        self.api.token = lambda: 'test-secret'
        with patch('scripts.ce_merge.github.subprocess.run') as run:
            run.return_value.returncode = 0
            self.api.merge_pr(12, 'b' * 40, 'merge', 'Tested integration', generated=True)
            self.assertIn('--admin', run.call_args.args[0])
            self.assertIn('--match-head-commit', run.call_args.args[0])
            self.assertNotIn('--auto', run.call_args.args[0])
        with self.assertRaises(Blocked):
            self.api.merge_pr(12, 'b' * 40, 'squash', 'Original PR', generated=True)

    def test_existing_generated_pr_is_reused_without_any_write(self):
        def repo(path, method="GET", data=None):
            self.assertEqual(method, "GET")
            if path.startswith("/git/matching-refs/"):
                return [{"ref": "refs/heads/merge/test/1", "object": {"sha": "b" * 40}}]
            self.assertTrue(path.startswith("/pulls?state=all&head="))
            return [{"number": 2}]
        self.api.repo.side_effect = repo
        self.assertEqual(self.api.bot_pr("test", 1, "trunk", 1), 2)


if __name__ == "__main__":
    unittest.main()
