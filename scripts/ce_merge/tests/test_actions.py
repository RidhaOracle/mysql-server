# Copyright (c) 2026, Oracle and/or its affiliates.
"""Actions authorization uses live comments and durable, revision-bound readiness."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.ce_merge.__main__ import authorize, main, operate
from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.github import authorization_identity
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked, load, resolution_number, resolution_prs, rulesets
from test_coordinator import POLICY


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Journal(self.temp.name)
        self.github = MagicMock()
        self.coordinator = Coordinator(POLICY, self.store, self.github)
        self.pr = {"number": 1, "state": "open", "draft": False, "body": "Public fix",
                   "head": {"sha": "a" * 40, "repo": {"full_name": "contributor/ce"}},
                   "base": {"sha": "b" * 40, "ref": "lts", "repo": {"full_name": POLICY["repository"]}}}
        self.comment = {"id": 123, "body": "/integrate", "user": {"id": 5, "login": "owner"},
                        "created_at": "2026-10-01T10:00:01Z", "updated_at": "2026-10-01T10:00:01Z",
                        "issue_url": "https://api.github.com/repos/example/ce/issues/1"}
        self.payload = {"action": "created", "repository": {"full_name": POLICY["repository"]},
                        "comment": copy.deepcopy(self.comment), "issue": {"number": 1, "pull_request": {"url": "test"}}}
        self.github.repo.return_value = self.comment
        self.github.pull.return_value = self.pr
        self.ready()

    def ready(self, created_at="2026-10-01T10:00:00Z"):
        self.store.set_setting("ready:1", {"identity": authorization_identity(self.pr), "eligible": True,
                               "policy": self.coordinator.policy_identity(), "created_at": created_at}, "test")

    def test_authorization_and_replay_create_one_operation(self):
        operation = authorize(self.payload, self.coordinator)
        self.assertEqual(authorize(self.payload, self.coordinator), operation)
        self.assertEqual(len(self.store.operations()), 1)
        self.assertEqual(self.store.operations()[0]["head"], self.pr["head"]["sha"])

    def test_replay_after_completion_does_not_reauthorize(self):
        identity = authorize(self.payload, self.coordinator)
        op = self.store.operations()[0]
        self.store.save(op, "complete", "test")
        self.pr["head"]["sha"] = "c" * 40
        self.ready()
        self.assertEqual(authorize(self.payload, self.coordinator), identity)
        self.assertEqual(len(self.store.operations()), 1)

    def test_permission_and_source_gates(self):
        for change in ("permission", "oca", "review", "draft", "hold"):
            with self.subTest(change=change):
                self.github.can_integrate.return_value = change != "permission"
                self.github.oca.side_effect = Blocked("OCA missing") if change == "oca" else None
                self.github.reviewed.side_effect = Blocked("Review missing") if change == "review" else None
                self.pr["draft"] = change == "draft"
                self.store.set_setting("hold", {"reason": "paused"} if change == "hold" else None, "test")
                self.ready()
                with self.assertRaises(Blocked):
                    authorize(self.payload, self.coordinator)
        self.assertEqual(self.store.operations(), [])

    def test_edited_wrong_repository_non_pr_and_non_command_events(self):
        variants = [dict(self.payload, action="edited"), dict(self.payload, repository={"full_name": "wrong/repo"}),
                    dict(self.payload, issue={"number": 1}),
                    dict(self.payload, comment=dict(self.comment, body="Please /integrate"))]
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(Blocked):
                authorize(payload, self.coordinator)
        self.comment["updated_at"] = "2026-10-01T10:00:02Z"
        with self.assertRaisesRegex(Blocked, "changed"):
            authorize(self.payload, self.coordinator)

    def test_live_author_and_issue_are_verified(self):
        for update in ({"user": {"id": 8, "login": "owner"}}, {"issue_url": "https://api.github.com/repos/example/ce/issues/2"},
                       {"body": "edited"}):
            self.github.repo.return_value = dict(self.comment, **update)
            with self.subTest(update=update), self.assertRaises(Blocked):
                authorize(self.payload, self.coordinator)

    def test_head_base_body_and_policy_changes_invalidate_readiness(self):
        mutations = [lambda: self.pr["head"].update(sha="c" * 40),
                     lambda: self.pr["base"].update(ref="trunk"),
                     lambda: self.pr.update(body="Changed integration instructions"),
                     lambda: setattr(self.coordinator, "policy", dict(POLICY, forbidden_paths=["different"]))]
        for change in mutations:
            before = copy.deepcopy(self.pr)
            self.ready()
            change()
            with self.assertRaisesRegex(Blocked, "readiness"):
                authorize(self.payload, self.coordinator)
            self.pr.clear()
            self.pr.update(before)
            self.coordinator.policy = POLICY

    def test_queued_comment_cannot_use_new_or_same_second_readiness(self):
        for stamp in ("2026-10-01T10:00:01Z", "2026-10-01T10:00:02Z"):
            self.ready(stamp)
            with self.subTest(stamp=stamp), self.assertRaisesRegex(Blocked, "predates"):
                authorize(self.payload, self.coordinator)

    def test_missing_or_failed_readiness_cannot_authorize(self):
        for value in (None, {"eligible": False}):
            self.store.set_setting("ready:1", value, "test")
            with self.assertRaises(Blocked):
                authorize(self.payload, self.coordinator)

    def test_readiness_status_changes_when_body_changes_on_same_commit(self):
        self.github.status.return_value = {"created_at": "2026-10-01T10:00:00Z"}
        self.coordinator.publish(self.pr, True, "Passed")
        first = self.github.status.call_args.args[3]
        self.pr["body"] = "Updated instructions"
        self.coordinator.publish(self.pr, True, "Passed")
        self.assertNotEqual(first, self.github.status.call_args.args[3])

    def test_operator_cannot_abort_or_resume_uncertain_work(self):
        authorize(self.payload, self.coordinator)
        op = self.store.operations()[0]
        op["data"]["intent"] = True
        self.store.save(op, "uncertain", "test")
        args = MagicMock(operation=op["id"])
        for command in ("abort", "resume"):
            with self.subTest(command=command), self.assertRaisesRegex(Blocked, "uncertain"):
                operate(self.coordinator, command, args, "owner")


class PolicyAndWorkflowTests(unittest.TestCase):
    def test_only_public_metadata_is_accepted(self):
        self.assertEqual(resolution_prs('', 'lts', POLICY), {})
        for body in ('Merge-Through: trunk', 'Merge-Through: lts', 'Merge-Reason: older versions only',
                     'Upmerge: trunk', 'Upmerge-Till: trunk',
                     'Upmerge-Reason: already fixed', 'Backport-To: lts'):
            with self.subTest(body=body), self.assertRaises(Blocked):
                resolution_prs(body, 'lts', POLICY)

    def test_resolution_links_are_repository_and_version_bound(self):
        body = 'Resolution-PR: trunk https://github.com/example/ce/pull/123'
        self.assertEqual(resolution_prs(body, 'lts', POLICY), {'trunk': 123})
        self.assertEqual(resolution_number('#123', POLICY['repository']), 123)
        self.assertEqual(resolution_prs('<!-- ' + body + ' -->', 'lts', POLICY), {})
        for bad in (body + '\n' + body, 'Resolution-PR: trunk',
                    'Resolution-PR: lts #123', 'Resolution-PR: trunk https://github.com/attacker/ce/pull/123',
                    'Resolution-PR: trunk https://github.com/example/ce/pull/123?head=other'):
            with self.subTest(body=bad), self.assertRaises(Blocked):
                resolution_prs(bad, 'lts', POLICY)

    def test_state_and_version_names_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            for change in ({"state_branch": "lts"}, {"branches": ["ce-merge-state/child"]},
                           {"bot_user_id": 0}, {"state_branch": "bad..name"}):
                path.write_text(json.dumps(dict(POLICY, **change)))
                with self.subTest(change=change), self.assertRaises(Blocked):
                    load(path)
            path.write_text(json.dumps(POLICY))
            self.assertEqual(load(path), POLICY)

    def test_rulesets_protect_native_pr_merges_and_only_the_persistent_state_branch(self):
        generated = {r["name"]: r for r in rulesets(POLICY)}
        for name in ("CE merge executor", "CE reviewed changes", "CE merge quality", "CE branch lifecycle"):
            self.assertEqual(generated[name]["conditions"]["ref_name"]["include"], ["refs/heads/lts", "refs/heads/trunk"])
        actor = generated["CE merge executor"]["bypass_actors"]
        self.assertEqual(actor, [{"actor_id": 42, "actor_type": "User", "bypass_mode": "pull_request"}])
        self.assertEqual(generated["CE state history"]["bypass_actors"], [])
        self.assertEqual(generated["CE merge quality"]["bypass_actors"], [])
        self.assertEqual(generated["CE reviewed changes"]["bypass_actors"], actor)
        self.assertEqual(generated["CE pull requests"]["bypass_actors"], [])
        self.assertNotIn("CE receipt creation", generated)
        for name in ('CE merge state', 'CE state history'):
            self.assertEqual(generated[name]['conditions']['ref_name']['include'], ['refs/heads/ce-merge-state'])

    def test_manual_commands_including_once_require_a_current_maintainer(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_REPOSITORY": POLICY["repository"], "GITHUB_REF": "refs/heads/trunk"}
            for command in ("once", "abort", "init-state"):
                event.write_text(json.dumps({"repository": {"default_branch": "trunk"},
                                             "sender": {"login": "contributor"}, "inputs": {"command": command}}))
                with self.subTest(command=command), patch.dict(os.environ, env), patch("sys.argv", ["ce-merge"]), \
                        patch("scripts.ce_merge.__main__.load", return_value=POLICY), \
                        patch("scripts.ce_merge.__main__.GitHub") as client, \
                        patch("scripts.ce_merge.__main__.GitJournal") as state:
                    client.return_value.can_integrate.return_value = False
                    with self.assertRaisesRegex(Blocked, "configured maintainer"):
                        main()
                    state.assert_not_called()

    def test_manual_dispatch_rejects_an_untrusted_ref_before_using_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"repository": {"default_branch": "trunk"}}))
            env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_REPOSITORY": POLICY["repository"], "GITHUB_REF": "refs/heads/feature"}
            with patch.dict(os.environ, env), patch("sys.argv", ["ce-merge"]), \
                    patch("scripts.ce_merge.__main__.load", return_value=POLICY), \
                    patch("scripts.ce_merge.__main__.GitHub") as client:
                with self.assertRaisesRegex(Blocked, "trusted default branch"):
                    main()
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
