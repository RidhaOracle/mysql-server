# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from scripts.ce_merge.__main__ import authorize, signed
from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.github import authorization_identity
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked, check_content, documentation_only, matches, rulesets, upmerge_till


POLICY = {
    "mode": "active", "strategy": "forward", "repository": "example/ce", "bot_fork": "bot/ce",
    "branches": ["lts", "trunk"], "release_branches": [], "maintainers": ["owner"],
    "app_id": 1, "release_app_id": 2, "installation_id": 3, "fork_installation_id": 4,
    "forbidden_paths": ["internal", "internal-sec"], "forbidden_metadata_patterns": ["PRIVATE-123"],
}


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.git("init", "-b", "lts")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.base = self.commit("code.txt", "base\n")
        self.git("branch", "trunk")

    def git(self, *args, input=None):
        return subprocess.check_output(["git", "-C", str(self.path), *args], input=input,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, path, content):
        file = self.path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.git("add", path)
        self.git("commit", "-m", "Public change")
        return self.git("rev-parse", "HEAD")

    def merge_commit(self, base, head, squash=False):
        tree = self.git("merge-tree", "--write-tree", base, head).splitlines()[0]
        args = ["commit-tree", tree, "-p", base]
        if not squash:
            args += ["-p", head]
        return self.git(*args, input=b"Public merge\n")


class FakeGitHub:
    def __init__(self, case):
        self.case = case
        self.approved = True
        self.oca_ok = True
        self.authorized = True
        self.changed_rules = False
        self.missing_review = set()
        self.checks = []
        self.pulls = {}
        self.bot_pulls = {}
        self.add_pull(1, case.head, "lts", "fork/ce")

    def add_pull(self, number, head, branch, repository):
        self.pulls[number] = {"number": number, "state": "open", "draft": False, "body": "",
            "user": {"login": "contributor"}, "head": {"sha": head, "repo": {"full_name": repository}},
            "base": {"sha": self.branch(branch), "ref": branch, "repo": {"full_name": "example/ce"}}}

    def pull(self, number):
        pr = copy.deepcopy(self.pulls[number])
        pr["base"]["sha"] = self.branch(pr["base"]["ref"])
        return pr

    def branch(self, branch):
        return self.case.remote_git("rev-parse", "refs/heads/" + branch)

    def token(self, fork=False):
        return None

    def can_integrate(self, actor):
        return actor == "owner" and self.authorized

    def reviewed(self, pr):
        if not self.approved or pr["number"] in self.missing_review:
            raise Blocked("Missing current independent review")

    def oca(self, pr):
        if not self.oca_ok:
            raise Blocked("OCA missing")

    def check(self, *args, **kwargs):
        self.checks.append((args, kwargs))
        return len(self.checks)

    def close_pr(self, pr):
        self.pulls[pr["number"]]["state"] = "closed"

    def advisory_label(self, *args):
        pass

    def bot_pr(self, operation, index, head, base, original, detail=""):
        key = (operation, index)
        if key not in self.bot_pulls:
            number = max(self.pulls) + 1
            self.case.remote_git("fetch", str(self.case.fork),
                                 f"refs/heads/upmerge/{operation}/{index}:refs/pull/{number}/head")
            self.add_pull(number, head, base, "bot/ce")
            self.bot_pulls[key] = number
        return self.bot_pulls[key]

    def repo(self, suffix, method="GET", data=None, fork=False):
        if suffix == "":
            return {"allow_rebase_merge": False, "allow_auto_merge": False,
                    "allow_merge_commit": True, "allow_squash_merge": True}
        if suffix.startswith("/rulesets/"):
            result = copy.deepcopy(rulesets(self.case.coordinator.policy)[int(suffix.rsplit("/", 1)[1])])
            if self.changed_rules:
                result["bypass_actors"] = [{"actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
            return result
        raise AssertionError(suffix)

    def pages(self, suffix, key=None):
        if suffix.startswith("/rulesets"):
            return [dict(r, id=i) for i, r in enumerate(rulesets(self.case.coordinator.policy))]
        if suffix.startswith("/pulls"):
            return []
        raise AssertionError(suffix)


class AtomicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo(self.root / "work")
        self.remote = self.root / "ce.git"
        self.fork = self.root / "fork.git"
        self.repo.git("clone", "--bare", str(self.repo.path), str(self.remote))
        self.repo.git("clone", "--bare", str(self.repo.path), str(self.fork))
        self.repo.git("checkout", "-b", "fix")
        self.head = self.repo.commit("fix.cc", "int fix;\n")
        self.repo.git("push", str(self.remote), "HEAD:refs/pull/1/head")
        self.store = Journal(self.root / "journal")
        self.api = FakeGitHub(self)
        self.lose_response = False
        case = self

        class Graph(PublicGraph):
            def __init__(self, policy):
                super().__init__(policy, str(case.remote))

            def run(self, *args, **kwargs):
                args = [str(case.fork) if a == "https://github.com/bot/ce.git" else a for a in args]
                return super().run(*args, **kwargs)

            def atomic_publish(self, *args, **kwargs):
                super().atomic_publish(*args, **kwargs)
                if case.lose_response:
                    raise TimeoutError("Simulated lost successful response")

        self.graph = Graph
        self.coordinator = Coordinator(POLICY, self.store, self.api, Graph)
        self.ci_ok = True
        self.coordinator.ci = lambda *a, **kw: (self.ci_ok, "test evidence")
        self.operation = self.store.request(123, self.api.pull(1), "owner")

    def remote_git(self, *args):
        return subprocess.check_output(["git", "--git-dir", str(self.remote), *args],
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def op(self):
        return next(o for o in self.store.operations() if o["id"] == self.operation)

    def unchanged(self):
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), self.repo.base)
        self.assertEqual(self.remote_git("tag", "--list", "ce-integration/*"), "")

    def prepare(self):
        self.ci_ok = False
        self.coordinator.advance(self.op())
        op = self.op()
        self.assertEqual(op["state"], "prepared")
        self.unchanged()
        return op

    def test_entire_chain_published_with_receipt_and_squash(self):
        self.coordinator.advance(self.op())
        op = self.op()
        self.assertEqual(op["state"], "complete")
        lower, upper = op["data"]["steps"]
        self.assertEqual(self.api.branch("lts"), lower["after"])
        self.assertEqual(self.api.branch("trunk"), upper["after"])
        self.assertEqual(self.remote_git("show", "-s", "--format=%P", lower["after"]), self.repo.base)
        self.assertEqual(self.remote_git("show", "-s", "--format=%an <%ae>", lower["after"]), "Test <test@example.com>")
        self.assertEqual(self.remote_git("show", lower["after"] + ":fix.cc"), "int fix;")
        self.remote_git("merge-base", "--is-ancestor", lower["after"], upper["after"])
        self.assertEqual(self.remote_git("rev-parse", op["data"]["receipt"]["ref"]), op["data"]["receipt"]["sha"])

    def test_wait_for_all_ci_before_any_branch_moves(self):
        self.prepare()
        self.store = Journal(self.root / "journal")  # New process reads prepared manifests.
        self.coordinator.store = self.store
        self.ci_ok = True
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "complete")

    def test_missing_generated_review_changes_no_branch(self):
        self.api.missing_review = {2}
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "prepared")
        self.unchanged()

    def test_changed_source_invalidates_authorization(self):
        self.prepare()
        self.api.pulls[1]["body"] = "Changed applicability"
        self.ci_ok = True
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "blocked")
        self.unchanged()

    def test_oca_review_and_actor_gate_before_preparation(self):
        for attr in ("oca_ok", "approved", "authorized"):
            with self.subTest(gate=attr):
                setattr(self.api, attr, False)
                self.coordinator.advance(self.op())
                self.unchanged()
                setattr(self.api, attr, True)

    def test_release_hold_blocks_ordinary_integration(self):
        self.store.set_setting("hold", {"reason": "release"}, "operator")
        self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(self.op()["state"], "blocked")

    def test_ruleset_drift_blocks_publication(self):
        self.api.changed_rules = True
        self.coordinator.advance(self.op())
        self.unchanged()

    def test_rejecting_one_ref_rejects_every_ref_and_receipt(self):
        hook = self.remote / "hooks" / "update"
        hook.write_text('#!/bin/sh\n[ "$1" != "refs/heads/trunk" ]\n')
        hook.chmod(0o755)
        self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(self.op()["state"], "uncertain")

    def test_unsupported_atomic_push_has_no_fallback(self):
        self.remote_git("config", "receive.advertiseAtomic", "false")
        self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(self.op()["state"], "uncertain")

    def test_changed_target_lease_rejects_whole_batch(self):
        op = self.prepare()
        self.repo.git("checkout", "trunk")
        new = self.repo.commit("other.cc", "int other;\n")
        self.repo.git("push", str(self.remote), "trunk:trunk")
        with self.graph(POLICY) as graph:
            graph.restore_candidates(op["data"]["steps"], self.store.bundle(op["id"]))
            receipt = graph.receipt(op["id"], op["data"]["steps"], "owner")
            with self.assertRaises(Blocked):
                graph.atomic_publish(op["data"]["steps"], receipt)
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), new)
        self.assertEqual(self.remote_git("tag", "--list", "ce-integration/*"), "")

    def test_lost_response_reconciles_from_receipt_without_republishing(self):
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "uncertain")
        self.coordinator.tick()
        self.assertEqual(self.op()["state"], "complete")
        self.assertEqual(len(self.remote_git("tag", "--list", "ce-integration/*").splitlines()), 1)

    def test_duplicate_transaction_cannot_publish_again(self):
        self.coordinator.advance(self.op())
        op = self.op()
        with self.graph(POLICY) as graph:
            graph.restore_candidates(op["data"]["steps"], self.store.bundle(op["id"]))
            with self.assertRaises(Blocked):
                graph.atomic_publish(op["data"]["steps"], op["data"]["receipt"])
        self.assertEqual(self.op()["state"], "complete")

    def test_null_merge_keeps_target_tree_and_lower_ancestry(self):
        self.api.pulls[1]["body"] = "Upmerge-Till: lts\nUpmerge-Reason: Not applicable on trunk"
        op = self.op()
        op["data"]["body"] = self.api.pulls[1]["body"]
        self.store.save(op, "queued", "test")
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "complete")
        lower, upper = self.op()["data"]["steps"]
        self.assertEqual(self.remote_git("rev-parse", upper["after"] + "^{tree}"),
                         self.remote_git("rev-parse", self.repo.base + "^{tree}"))
        self.remote_git("merge-base", "--is-ancestor", lower["after"], upper["after"])

    def test_preparation_conflict_changes_no_branch(self):
        self.repo.git("checkout", "trunk")
        tip = self.repo.commit("fix.cc", "conflicting change\n")
        self.repo.git("push", str(self.remote), "trunk:trunk")
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "conflict")
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), tip)

    def test_private_ancestry_cannot_be_hidden_by_reverting_tree(self):
        private = self.repo.commit("internal-sec/test", "private\n")
        self.repo.git("revert", "--no-edit", private)
        head = self.repo.git("rev-parse", "HEAD")
        self.repo.git("push", str(self.remote), "HEAD:refs/pull/1/head")
        self.api.pulls[1]["head"]["sha"] = head
        op = self.op()
        op["head"] = head
        self.store.save(op, "queued", "test")
        self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(self.op()["state"], "blocked")

    def test_duplicate_actions_use_one_json_manifest(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.store.request(123, self.api.pull(1), "owner"), range(4)))
        self.assertEqual(set(ids), {self.operation})
        self.assertEqual(len(self.store.operations()), 1)

    def conflict(self):
        self.repo.git("checkout", "trunk")
        tip = self.repo.commit("fix.cc", "conflicting change\n")
        self.repo.git("push", str(self.remote), "trunk:trunk")
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "conflict")
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), tip)
        return self.op()["data"]["steps"][0]["after"], tip

    def resolution(self, lower, tip):
        self.repo.git("fetch", str(self.fork), lower)
        tree = self.repo.git("rev-parse", lower + "^{tree}")
        head = self.repo.git("commit-tree", tree, "-p", tip, "-p", lower, input=b"Reviewed resolution\n")
        self.repo.git("push", str(self.remote), head + ":refs/pull/3/head")
        self.api.add_pull(3, head, "trunk", "fork/ce")
        return head

    def test_reviewed_resolution_publishes_whole_chain_after_restart(self):
        lower, tip = self.conflict()
        head = self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), tip)
        self.coordinator.store = Journal(self.root / "journal")
        self.coordinator.advance(self.op())
        op = self.op()
        self.assertEqual(op["state"], "complete")
        upper = op["data"]["steps"][1]["after"]
        self.remote_git("merge-base", "--is-ancestor", lower, upper)
        self.remote_git("merge-base", "--is-ancestor", head, upper)

    def test_resolution_without_review_or_lower_ancestry_is_rejected(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.api.missing_review.add(3)
        with self.assertRaises(Blocked):
            self.coordinator.repair(self.op(), 3)
        self.api.missing_review.clear()
        self.api.pulls[3]["head"]["sha"] = tip
        with self.assertRaises(Blocked):
            self.coordinator.repair(self.op(), 3)
        self.assertEqual(self.api.branch("lts"), self.repo.base)

    def test_resolution_changed_after_acceptance_blocks_publication(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.api.pulls[3]["body"] = "Altered resolution"
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "prepared")
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), tip)

    def test_policy_change_requires_new_authorization(self):
        self.prepare()
        self.coordinator.policy = dict(POLICY, branches=["lts", "new-lts", "trunk"])
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "blocked")
        self.unchanged()

    def test_final_ci_revocation_leaves_every_target_unchanged(self):
        calls = []
        def ci(*args, **kwargs):
            calls.append(args)
            return len(calls) <= 2, "test"
        self.coordinator.ci = ci
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "blocked")
        self.unchanged()

    def test_failing_ci_is_reported_on_exact_candidate(self):
        def fail(*args, **kwargs):
            raise Blocked("Public CI failed")
        self.coordinator.ci = fail
        self.coordinator.advance(self.op())
        self.unchanged()
        failures = [(args, kwargs) for args, kwargs in self.api.checks if args[1:3] == ("CE / public-ci", "failure")]
        self.assertEqual(len(failures), 2)
        self.assertEqual({kw["candidate"] for _, kw in failures}, {s["after"] for s in self.op()["data"]["steps"]})

    def test_report_failure_cannot_undo_complete_and_retries(self):
        self.coordinator.advance(self.op())
        check = self.api.check
        def fail(*args, **kwargs):
            raise TimeoutError("Reporting unavailable")
        self.api.check = fail
        with self.assertRaises(TimeoutError):
            self.coordinator.report(self.op())
        self.assertEqual(self.op()["state"], "complete")
        self.assertEqual(self.api.pulls[1]["state"], "open")
        self.api.check = check
        self.coordinator.report(self.op())
        self.assertEqual(self.api.pulls[1]["state"], "closed")
        count = len(self.api.checks)
        self.coordinator.report(self.op())
        self.assertEqual(len(self.api.checks), count)

    def test_competing_prepared_batch_cannot_overwrite_first(self):
        first = self.prepare()
        self.api.add_pull(4, self.head, "lts", "fork/ce")
        self.remote_git("update-ref", "refs/pull/4/head", self.head)
        second_id = self.store.request(456, self.api.pull(4), "owner")
        second = next(o for o in self.store.operations() if o["id"] == second_id)
        self.coordinator.advance(second)
        self.ci_ok = True
        self.coordinator.advance(first)
        heads = [self.api.branch(b) for b in POLICY["branches"]]
        self.coordinator.advance(second)
        self.assertNotEqual(second["state"], "complete")
        self.assertEqual(heads, [self.api.branch(b) for b in POLICY["branches"]])

    def test_three_branch_forward_chain_and_null_suffix(self):
        self.remote_git("update-ref", "refs/heads/middle", self.repo.base)
        self.coordinator.policy = dict(POLICY, branches=["lts", "middle", "trunk"])
        self.api.pulls[1]["body"] = "Upmerge-Till: middle\nUpmerge-Reason: Already fixed on trunk"
        op = self.op()
        op["data"]["body"] = self.api.pulls[1]["body"]
        self.store.save(op, "queued", "test")
        self.coordinator.advance(self.op())
        op = self.op()
        self.assertEqual(op["state"], "complete")
        lower, middle, upper = op["data"]["steps"]
        for before, after in ((lower, middle), (middle, upper)):
            self.remote_git("merge-base", "--is-ancestor", before["after"], after["after"])
        self.assertEqual(self.remote_git("show", middle["after"] + ":fix.cc"), "int fix;")
        self.assertEqual(self.remote_git("rev-parse", upper["after"] + "^{tree}"),
                         self.remote_git("rev-parse", self.repo.base + "^{tree}"))

    def test_ref_movement_after_snapshot_is_rejected_by_git_lease(self):
        op = self.prepare()
        self.repo.git("checkout", "trunk")
        moved = self.repo.commit("other.cc", "int other;\n")
        self.remote_git("fetch", str(self.repo.path), moved)
        with self.graph(POLICY) as graph:
            graph.restore_candidates(op["data"]["steps"], self.store.bundle(op["id"]))
            receipt = graph.receipt(op["id"], op["data"]["steps"], "owner")
            run = graph.run
            def racing_run(*args, **kwargs):
                if args[0] == "push":
                    self.remote_git("update-ref", "refs/heads/trunk", moved)
                return run(*args, **kwargs)
            graph.run = racing_run
            with self.assertRaises(Blocked):
                graph.atomic_publish(op["data"]["steps"], receipt)
        self.assertEqual(self.api.branch("lts"), self.repo.base)
        self.assertEqual(self.api.branch("trunk"), moved)
        self.assertEqual(self.remote_git("tag", "--list", "ce-integration/*"), "")

    def test_receipt_rejection_rejects_every_branch(self):
        hook = self.remote / "hooks" / "update"
        hook.write_text('#!/bin/sh\ncase "$1" in refs/tags/ce-integration/*) exit 1;; esac\nexit 0\n')
        hook.chmod(0o755)
        self.coordinator.advance(self.op())
        self.unchanged()

    def test_action_bound_to_target_head_and_body(self):
        pr = self.api.pull(1)
        payload = {"action": "requested_action", "requested_action": {"identifier": "integrate"},
                   "repository": {"full_name": POLICY["repository"]}, "installation": {"id": 3},
                   "sender": {"login": "owner"}, "check_run": {"id": 123, "name": "CE / policy",
                   "conclusion": "success", "app": {"id": 1}, "head_sha": pr["head"]["sha"],
                   "external_id": authorization_identity(pr)}}
        self.assertEqual(authorize(payload, "delivery", POLICY, self.store, self.api), self.operation)
        self.api.pulls[1]["base"]["ref"] = "trunk"
        with self.assertRaises(Blocked):
            authorize(payload, "delivery2", POLICY, self.store, self.api)


class PolicyTests(unittest.TestCase):
    def test_malformed_or_backward_metadata_fails_closed(self):
        for body in ("Upmerge-Till:\nUpmerge-Reason: example", "Upmerge-Till: trunk\nUpmerge-Till: trunk",
                     "Backport-To: lts", "Upmerge-Till: lts\nUpmerge-Reason: \nNext section"):
            with self.subTest(body=body), self.assertRaises(Blocked):
                upmerge_till(body, "lts", POLICY["branches"])

    def test_unknown_base_fails_with_public_reason(self):
        with self.assertRaises(Blocked):
            upmerge_till("", "unknown", POLICY["branches"])
    def test_target_defaults_to_innovation(self):
        self.assertEqual(upmerge_till("", "lts", POLICY["branches"]), "trunk")

    def test_hidden_template_example_is_not_an_instruction(self):
        self.assertEqual(upmerge_till("<!--\nUpmerge-Till: lts\n-->", "lts", POLICY["branches"]), "trunk")

    def test_early_stop_requires_reason(self):
        with self.assertRaises(Blocked):
            upmerge_till("Upmerge-Till: lts", "lts", POLICY["branches"])

    def test_no_backward_upmerge(self):
        with self.assertRaises(Blocked):
            upmerge_till("Upmerge-Till: lts\nUpmerge-Reason: test", "trunk", POLICY["branches"])

    def test_docs_exemption_is_narrow(self):
        self.assertTrue(documentation_only(["Docs/test.md"]))
        self.assertFalse(documentation_only([".github/readme.md"]))
        self.assertFalse(documentation_only([]))

    def test_quality_has_no_bypass_but_publisher_has_narrow_exception(self):
        rules = {r["name"]: r for r in rulesets(POLICY)}
        self.assertEqual(rules["CE merge quality"]["bypass_actors"], [])
        self.assertEqual(rules["CE merge executor"]["bypass_actors"][0]["bypass_mode"], "always")
        self.assertEqual(rules["CE immutable tags"]["bypass_actors"], [])

    def test_signed_webhooks(self):
        body, secret = b"payload", "secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(signed(secret, body, signature))
        self.assertFalse(signed(secret, b"tampered", signature))


if __name__ == "__main__":
    unittest.main()
