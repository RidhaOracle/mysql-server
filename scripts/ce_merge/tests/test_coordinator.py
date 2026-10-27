# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.github import GitHub, integration_body
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked, require, documentation_only, rulesets, upmerge_till


POLICY = {
    "repository": "example/ce", "branches": ["lts", "trunk"],
    "maintainers": ["owner"], "bot_user_id": 42, "state_branch": "ce-merge-state",
    "oca_label": "OCA Verified", "forbidden_paths": ["internal", "internal-sec"],
    "forbidden_metadata_patterns": ["PRIVATE-123"],
}


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.git("init", "-b", "lts")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        # Model a version baseline with its trusted ordinary CI configuration.
        workflows = self.path / '.github/workflows'
        workflows.mkdir(parents=True)
        root = Path(__file__).resolve().parents[3]
        for name in ('pr-build.yml', 'mtr.yml', 'clang-format.yml'):
            (workflows / name).write_text((root / '.github/workflows' / name).read_text())
        self.git('add', '.github/workflows')
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
        self.requested_changes = set()
        self.checks = []
        self.merges = []
        self.reject = set()
        self.pulls = {}
        self.bot_pulls = {}
        self.add_pull(1, case.head, "lts", "contributor/ce")

    def add_pull(self, number, head, branch, repository):
        self.pulls[number] = {"number": number, "state": "open", "draft": False, "body": "Upmerge: trunk",
            "user": {"id": 9, "login": "contributor"}, "head": {"sha": head, "repo": {"full_name": repository}},
            "base": {"sha": self.branch(branch), "ref": branch, "repo": {"full_name": "example/ce"}}}

    def pull(self, number):
        pr = copy.deepcopy(self.pulls[number])
        pr["base"]["sha"] = self.branch(pr["base"]["ref"])
        return pr

    def branch(self, branch):
        return self.case.remote_git("rev-parse", "refs/heads/" + branch)

    def token(self):
        return None

    def can_integrate(self, actor):
        return actor == "owner" and self.authorized

    def reviewed(self, pr, require_approval=True):
        if pr["number"] in self.requested_changes:
            raise Blocked("A reviewer requested changes")
        if require_approval and (not self.approved or pr["number"] in self.missing_review):
            raise Blocked("Missing current maintainer review")

    def oca(self, pr):
        if not self.oca_ok:
            raise Blocked("OCA missing")

    def status(self, *args, **kwargs):
        self.checks.append((args, kwargs))
        return {"id": len(self.checks), "created_at": "2026-10-01T10:00:00Z"}

    def comment(self, *args):
        pass

    def summary(self, *args):
        pass

    def close_pr(self, pr):
        self.pulls[pr["number"]]["state"] = "closed"

    def advisory_label(self, *args):
        pass

    def bot_pr(self, operation, index, base, original, detail=""):
        head = self.branch(f"merge/{operation}/{index}")
        key = (operation, index)
        if key not in self.bot_pulls:
            number = max(self.pulls) + 1
            self.add_pull(number, head, base, "example/ce")
            self.pulls[number]['body'] = integration_body(operation, original, detail)
            self.bot_pulls[key] = number
        number = self.bot_pulls[key]
        self.case.remote_git('update-ref', f'refs/pull/{number}/head', head)
        self.pulls[number]['head'].update(sha=head, ref=f'merge/{operation}/{index}')
        self.pulls[number]['user'] = {'id': 42, 'login': 'merge-bot'}
        return number

    def merge_pr(self, number, head, method, message, generated=False):
        pr = self.pulls[number]
        require(pr['head']['sha'] == head and not pr.get('merged'), 'Wrong merge head')
        if number in self.reject:
            raise Blocked('GitHub rejected merge')
        base = self.branch(pr['base']['ref'])
        self.case.repo.git('fetch', str(self.case.remote), base, head)
        actual = self.case.repo.merge_commit(base, head, squash=method == 'squash')
        self.case.repo.git('push', str(self.case.remote), actual + ':refs/heads/' + pr['base']['ref'])
        pr.update(state='closed', merged=True, merge_commit_sha=actual)
        self.merges.append((number, method))
        if self.case.lose_response:
            raise TimeoutError('Simulated lost successful merge response')

    def repo(self, suffix, method="GET", data=None, fork=False):
        if suffix == "":
            return {"default_branch": "trunk", "allow_rebase_merge": False, "allow_auto_merge": False,
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


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo(self.root / "work")
        self.remote = self.root / "ce.git"
        self.repo.git("clone", "--bare", str(self.repo.path), str(self.remote))
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

        self.graph = Graph
        self.coordinator = Coordinator(POLICY, self.store, self.api, Graph)
        self.ci_ok = True
        self.coordinator.ci = lambda *a, **kw: (self.ci_ok, "test evidence")
        self.coordinator.tests = lambda *a, **kw: (self.ci_ok, "version test evidence")
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

    def finish(self):
        for _ in range(6):
            self.coordinator.advance(self.op())
            if self.op()['state'] == 'complete':
                return self.op()
        self.fail(str(self.op()))

    def test_native_squash_then_serial_merge_preserves_actual_commit(self):
        self.coordinator.advance(self.op())
        op = self.op()
        lower, upper = op['data']['steps']
        self.assertNotEqual(lower['after'], lower['merged_sha'])
        self.assertEqual(self.api.branch('lts'), lower['merged_sha'])
        self.assertEqual(self.api.branch('trunk'), self.repo.base)
        self.assertTrue(self.api.pulls[1]['merged'])
        self.assertEqual(self.store.setting('merge-lock'), op['id'])
        op = self.finish()
        lower, upper = op['data']['steps']
        self.assertEqual(self.api.merges, [(1, 'squash'), (2, 'merge')])
        self.assertEqual(self.api.branch('trunk'), upper['merged_sha'])
        self.remote_git('merge-base', '--is-ancestor', lower['merged_sha'], upper['merged_sha'])
        self.assertEqual(self.remote_git('show', '-s', '--format=%P', lower['merged_sha']), self.repo.base)
        self.assertEqual(self.remote_git('show', '-s', '--format=%P', upper['merged_sha']),
                         self.repo.base + ' ' + upper['review_head'])
        self.assertEqual(self.remote_git('show', lower['merged_sha'] + ':fix.cc'), 'int fix;')
        self.assertIsNone(self.store.setting('merge-lock'))
        self.assertEqual(self.remote_git('tag', '--list', 'ce-integration/*'), '')

    def test_wait_for_source_ci_without_moving_any_target(self):
        self.prepare()
        self.assertEqual(self.api.merges, [])
        self.ci_ok = True
        self.finish()

    def test_all_versions_tested_without_generated_prs(self):
        self.remote_git('update-ref', 'refs/heads/middle', self.repo.base)
        self.coordinator.policy = dict(POLICY, branches=['lts', 'middle', 'trunk'])
        observed = []
        def tests(op, graph, index):
            self.assertEqual(len(self.api.pulls), 1)
            self.assertEqual(self.api.merges, [])
            observed.append(index)
            return False, 'Pending'
        self.coordinator.tests = tests
        self.coordinator.advance(self.op())
        self.assertEqual(observed, [0, 1, 2])
        self.unchanged()

    def test_later_version_tests_block_first_merge(self):
        self.coordinator.tests = lambda op, graph, i: (i != 1, 'Later tests pending')
        self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(len(self.api.pulls), 1)
        self.coordinator.tests = lambda *a: (True, 'Passed')
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.assertEqual(len(self.api.pulls), 1)
        self.finish()
        self.assertEqual(len(self.api.pulls), 2)

    def test_failed_tests_keep_reconciling_and_other_versions_can_start(self):
        seen = []
        def tests(op, graph, index):
            seen.append(index)
            if index == 0:
                raise Blocked('Existing CI rerun failed')
            return False, 'Version testing started'
        self.coordinator.tests = tests
        self.coordinator.advance(self.op())
        self.assertEqual(seen, [0, 1])
        self.assertEqual(self.op()['state'], 'prepared')
        self.assertEqual(self.api.merges, [])
        self.coordinator.tests = lambda *args: (True, 'Latest rerun passed')
        self.coordinator.work()
        self.assertEqual(self.api.merges, [(1, 'squash')])

    def test_generated_pr_does_not_require_additional_approval(self):
        self.api.missing_review.add(2)
        self.finish()
        self.assertEqual(self.api.merges, [(1, 'squash'), (2, 'merge')])

    def test_new_pr_waits_for_native_mergeability_without_an_uncertain_merge(self):
        self.coordinator.advance(self.op())
        create = self.api.bot_pr
        def pending(*args, **kwargs):
            number = create(*args, **kwargs)
            self.api.pulls[number]['mergeable'] = None
            return number
        with patch.object(self.api, 'bot_pr', side_effect=pending):
            self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'prepared')
        self.assertFalse(self.op()['data']['intent'])
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.api.pulls[2]['mergeable'] = True
        self.finish()

    def test_integration_pr_starts_with_actual_lower_parent(self):
        self.prepare()
        self.assertEqual(len(self.api.pulls), 1)
        self.ci_ok = True
        self.coordinator.advance(self.op())
        actual = self.api.branch('lts')
        self.assertEqual(len(self.api.pulls), 1)
        self.finish()
        step = self.op()['data']['steps'][1]
        self.assertEqual(self.remote_git('show', '-s', '--format=%P', step['review_head']),
                         self.repo.base + ' ' + actual)
        self.assertIn('[skip ci]', self.remote_git('show', '-s', '--format=%B', step['review_head']))

    def test_candidate_tests_revalidated_after_actual_lower_merge(self):
        observed = []
        def tests(op, graph, index):
            observed.append((index, op['data']['steps'][index]['input_head']))
            return True, 'Passed'
        self.coordinator.tests = tests
        self.coordinator.advance(self.op())
        actual = self.api.branch('lts')
        self.finish()
        self.assertIn((1, actual), observed)

    def test_target_movement_requires_tests_again_before_opening_next_pr(self):
        self.coordinator.advance(self.op())
        self.repo.git('checkout', 'trunk')
        tip = self.repo.commit('target-added.cc', 'new target content\n')
        self.repo.git('push', str(self.remote), 'trunk:trunk')
        checked = []
        def tests(op, graph, index):
            checked.append(op['data']['steps'][index]['base_sha'])
            return False, 'Fresh CI required'
        self.coordinator.tests = tests
        self.coordinator.advance(self.op())
        self.assertIn(tip, checked)
        self.assertEqual(len(self.api.pulls), 1)
        self.assertEqual(self.api.merges, [(1, 'squash')])

    def test_revoked_tests_after_first_merge_block_next_pr(self):
        self.coordinator.advance(self.op())
        self.ci_ok = False
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.assertEqual(len(self.api.pulls), 1)

    def test_generated_body_change_blocks_publication(self):
        self.coordinator.advance(self.op())
        self.api.requested_changes.add(2)
        self.coordinator.advance(self.op())
        self.api.pulls[2]['body'] += '\nChanged instructions'
        self.api.requested_changes.clear()
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.assertIn('identity or body changed', self.op()['data']['reason'])

    def test_operator_can_force_new_candidate_tests(self):
        from scripts.ce_merge.__main__ import operate
        from unittest.mock import MagicMock
        self.coordinator.advance(self.op())
        operate(self.coordinator, 'refresh-checks', MagicMock(operation=self.operation), 'owner')
        step = self.op()['data']['steps'][1]
        self.assertTrue(step['fresh_tests'])
        self.assertEqual(step['test_attempt'], 1)
        self.ci_ok = False
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [(1, 'squash')])

    def test_generated_change_request_keeps_earlier_merge_and_lock(self):
        self.coordinator.advance(self.op())
        self.api.requested_changes.add(2)
        lower = self.api.branch('lts')
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'blocked')
        self.assertEqual(self.api.branch('lts'), lower)
        self.assertEqual(self.api.branch('trunk'), self.repo.base)
        self.assertEqual(self.store.setting('merge-lock'), self.operation)
        self.api.requested_changes.clear()
        self.finish()
        self.assertEqual(self.api.branch('lts'), lower)

    def test_later_merge_rejection_keeps_successful_lower_merge(self):
        self.coordinator.advance(self.op())
        lower = self.api.branch('lts')
        self.api.reject.add(2)
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'uncertain')
        self.assertEqual(self.api.branch('lts'), lower)
        self.assertEqual(self.api.branch('trunk'), self.repo.base)
        self.api.reject.clear()
        self.coordinator.retry(self.op())
        self.assertEqual(self.op()['state'], 'complete')
        self.assertEqual(self.api.merges, [(1, 'squash'), (2, 'merge')])

    def test_lost_response_reconciles_actual_merge_without_duplicate(self):
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.coordinator.advance(self.op())
        lower = self.api.branch('lts')
        self.assertEqual(self.op()['state'], 'uncertain')
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.branch('lts'), lower)
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.lose_response = False
        self.finish()
        self.assertEqual(self.api.merges, [(1, 'squash'), (2, 'merge')])

    def test_cli_success_is_not_evidence_of_merge(self):
        with patch.object(self.api, 'merge_pr'):
            self.coordinator.advance(self.op())
        self.unchanged()
        self.assertEqual(self.op()['state'], 'uncertain')
        self.assertEqual(self.store.setting('merge-lock'), self.operation)

    def test_complete_operation_cannot_merge_again(self):
        self.finish()
        before = list(self.api.merges)
        self.coordinator.work()
        self.assertEqual(self.api.merges, before)

    def test_preflight_conflict_prevents_original_squash(self):
        self.conflict()
        self.assertEqual(self.api.merges, [])

    def test_reviewed_conflict_resolution_survives_runner_and_actual_squash(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.coordinator.store = Journal(self.root / 'journal')
        self.finish()
        steps = self.op()['data']['steps']
        self.remote_git('merge-base', '--is-ancestor', steps[0]['merged_sha'], steps[1]['merged_sha'])
        self.assertEqual(self.remote_git('show', steps[1]['merged_sha'] + ':fix.cc'), 'int fix;')

    def test_resolution_needs_review_and_exact_lower_ancestry(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.api.missing_review.add(3)
        with self.assertRaises(Blocked):
            self.coordinator.repair(self.op(), 3)
        self.api.missing_review.clear()
        self.api.pulls[3]['head']['sha'] = self.head
        self.remote_git('update-ref', 'refs/pull/3/head', self.head)
        with self.assertRaises(Blocked):
            self.coordinator.repair(self.op(), 3)
        self.assertEqual(self.api.merges, [])

    def test_resolution_revocation_before_squash_blocks_all_targets(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.api.missing_review.add(3)
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [])

    def test_resolution_retargeting_invalidates_accepted_repair(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.api.pulls[3]['base']['ref'] = 'lts'
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [])
        self.assertEqual(self.op()['state'], 'blocked')

    def test_moved_resolution_target_can_be_repaired_again(self):
        lower, tip = self.conflict()
        self.resolution(lower, tip)
        self.coordinator.repair(self.op(), 3)
        self.repo.git('checkout', 'trunk')
        moved = self.repo.commit('target-moved.cc', 'new target content\n')
        self.repo.git('push', str(self.remote), 'trunk:trunk')
        self.coordinator.advance(self.op())
        op = self.op()
        self.assertEqual(op['state'], 'conflict')
        step = op['data']['steps'][op['data']['conflict_index']]
        self.assertEqual(step['base_sha'], moved)
        self.assertNotIn('resolution', step)
        self.assertTrue(step['input_ref'].startswith('refs/heads/merge-tests/'))
        self.resolution(step['input_head'], moved)
        self.coordinator.repair(self.op(), 3)
        self.finish()

    def test_metadata_resolution_link_is_consumed_without_opening_an_integration_pr(self):
        self.api.pulls[1]['body'] += '\nResolution-PR: trunk https://github.com/example/ce/pull/3'
        op = self.op()
        op['data']['body'] = self.api.pulls[1]['body']
        self.store.save(op, 'queued', 'test-authorized-metadata')
        self.repo.git('checkout', 'trunk')
        tip = self.repo.commit('fix.cc', 'conflicting change\n')
        self.repo.git('push', str(self.remote), 'trunk:trunk')
        # Prepare/expose exact inputs before the contributor supplies the linked head.
        self.coordinator.prepare(op)
        self.coordinator.expose_conflict(op)
        lower = op['data']['steps'][0]['after']
        self.resolution(lower, tip)
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['data']['steps'][1]['resolution']['pr'], 3)
        self.assertEqual(self.api.merges, [])
        self.assertEqual(set(self.api.pulls), {1, 3})
        self.finish()

    def test_authorization_revoked_after_first_merge_blocks_next(self):
        self.coordinator.advance(self.op())
        self.api.authorized = False
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'blocked')
        self.assertEqual(self.api.merges, [(1, 'squash')])
        self.assertEqual(self.api.branch('trunk'), self.repo.base)

    def test_changed_body_after_first_merge_blocks_next(self):
        self.coordinator.advance(self.op())
        self.api.pulls[1]['body'] = 'Upmerge: lts\nUpmerge-Reason: changed'
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'blocked')
        self.assertEqual(self.api.merges, [(1, 'squash')])

    def test_final_review_revocation_stops_native_merge(self):
        reviewed = self.api.reviewed
        calls = []
        def revoke(pr):
            calls.append(pr['number'])
            if len(calls) >= 5:
                raise Blocked('Review revoked')
            return reviewed(pr)
        with patch.object(self.api, 'reviewed', side_effect=revoke):
            self.coordinator.advance(self.op())
        self.unchanged()

    def test_final_ci_revocation_stops_native_merge(self):
        calls = []
        def ci(*args, **kwargs):
            calls.append(args)
            return len(calls) <= 2, 'CI revoked'
        self.coordinator.tests = ci
        self.coordinator.advance(self.op())
        self.unchanged()

    def test_target_movement_repeats_whole_preflight_before_squash(self):
        self.prepare()
        self.repo.git('checkout', 'trunk')
        tip = self.repo.commit('fix.cc', 'new conflict\n')
        self.repo.git('push', str(self.remote), 'trunk:trunk')
        self.ci_ok = True
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'conflict')
        self.assertEqual(self.api.merges, [])
        self.assertEqual(self.api.branch('trunk'), tip)

    def test_target_moves_during_merge_requires_manual_inspection(self):
        merge = self.api.merge_pr
        def race(*args, **kwargs):
            self.repo.git('checkout', 'lts')
            self.repo.commit('target.cc', 'moving target\n')
            self.repo.git('push', str(self.remote), 'lts:lts')
            merge(*args, **kwargs)
        with patch.object(self.api, 'merge_pr', side_effect=race):
            self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'uncertain')
        self.assertIn('Target moved', self.op()['data']['reason'])
        self.assertEqual(self.api.branch('trunk'), self.repo.base)

    def test_other_operation_cannot_take_lock_while_waiting(self):
        self.prepare()
        self.api.add_pull(4, self.head, 'lts', 'contributor/ce')
        other_id = self.store.request(456, self.api.pull(4), 'owner')
        other = next(o for o in self.store.operations() if o['id'] == other_id)
        self.coordinator.advance(other)
        self.assertEqual(other['state'], 'blocked')
        self.assertEqual(self.store.setting('merge-lock'), self.operation)
        self.unchanged()

    def test_reporting_failure_cannot_repeat_merges(self):
        self.finish()
        before = list(self.api.merges)
        with patch.object(self.api, 'comment', side_effect=TimeoutError('report unavailable')):
            with self.assertRaises(TimeoutError):
                self.coordinator.report(self.op())
        self.assertEqual(self.op()['state'], 'complete')
        self.coordinator.tick()
        self.assertEqual(self.api.merges, before)
        self.assertTrue(self.api.pulls[1]['merged'])

    def test_null_suffix_preserves_target_tree_and_actual_lower_ancestry(self):
        self.api.pulls[1]['body'] = 'Upmerge: lts\nUpmerge-Reason: Already fixed on trunk'
        op = self.op()
        op['data']['body'] = self.api.pulls[1]['body']
        self.store.save(op, 'queued', 'test')
        steps = self.finish()['data']['steps']
        self.remote_git('merge-base', '--is-ancestor', steps[0]['merged_sha'], steps[1]['merged_sha'])
        self.assertEqual(self.remote_git('rev-parse', steps[1]['merged_sha'] + '^{tree}'),
                         self.remote_git('rev-parse', self.repo.base + '^{tree}'))

    def backport_null_chain(self, base):
        # Different later-version implementations deliberately conflict with
        # the source patch. Null merges must preserve those versions verbatim.
        self.repo.git('checkout', 'trunk')
        middle = self.repo.commit('fix.cc', 'int version97;\n')
        self.repo.git('push', str(self.remote), middle + ':refs/heads/middle')
        trunk = self.repo.commit('fix.cc', 'int trunk_version;\n')
        self.repo.git('push', str(self.remote), trunk + ':refs/heads/trunk')
        self.coordinator.policy = dict(POLICY, branches=['lts', 'middle', 'trunk'])
        if base == 'middle':
            self.repo.git('checkout', '-b', 'backport-fix', middle)
            self.head = self.repo.commit('fix.cc', 'int fix;\n')
            self.repo.git('push', '--force-with-lease=refs/pull/1/head:' + self.api.pulls[1]['head']['sha'],
                          str(self.remote), 'HEAD:refs/pull/1/head')
            self.api.pulls[1]['head']['sha'] = self.head
        self.api.pulls[1]['base']['ref'] = base
        self.api.pulls[1]['body'] = 'Upmerge-Till: ' + base
        op = self.op()
        op['head'] = self.head
        op['data'].update(base=base, body=self.api.pulls[1]['body'])
        self.store.save(op, 'queued', 'backport-null-test')
        bases = {'lts': self.repo.base, 'middle': middle, 'trunk': trunk}
        self.coordinator.advance(self.op())
        self.assertEqual(len(self.api.merges), 1)
        # Recreate the coordinator between hops to verify saved null intent.
        self.store = Journal(self.store.path)
        self.coordinator = Coordinator(self.coordinator.policy, self.store, self.api, self.graph)
        self.coordinator.tests = lambda *a, **kw: (True, 'version test evidence')
        steps = self.finish()['data']['steps']
        self.assertEqual([s['null'] for s in steps], [False] + [True] * (len(steps) - 1))
        self.assertEqual(self.remote_git('show', steps[0]['merged_sha'] + ':fix.cc'), 'int fix;')
        for lower, upper in zip(steps, steps[1:]):
            self.remote_git('merge-base', '--is-ancestor', lower['merged_sha'], upper['merged_sha'])
            self.assertEqual(self.remote_git('rev-parse', upper['merged_sha'] + '^{tree}'),
                             self.remote_git('rev-parse', bases[upper['branch']] + '^{tree}'))
            self.assertTrue(self.api.pulls[upper['pr']]['merged'])
        self.assertTrue(self.api.pulls[1]['merged'])
        self.assertIsNone(self.store.setting('merge-lock'))
        return steps

    def test_backport_to_oldest_null_merges_all_later_versions(self):
        self.assertEqual(len(self.backport_null_chain('lts')), 3)

    def test_backport_to_middle_leaves_older_version_untouched(self):
        self.assertEqual(len(self.backport_null_chain('middle')), 2)
        self.assertEqual(self.api.branch('lts'), self.repo.base)

    def test_three_branch_chain_preserves_each_actual_merge(self):
        self.remote_git('update-ref', 'refs/heads/middle', self.repo.base)
        self.coordinator.policy = dict(POLICY, branches=['lts', 'middle', 'trunk'])
        steps = self.finish()['data']['steps']
        for lower, upper in zip(steps, steps[1:]):
            self.remote_git('merge-base', '--is-ancestor', lower['merged_sha'], upper['merged_sha'])
        self.assertEqual([method for _, method in self.api.merges], ['squash', 'merge', 'merge'])

    def test_initial_chain_must_be_in_sync(self):
        self.repo.git('checkout', 'lts')
        self.repo.commit('unpropagated.cc', 'missing in trunk\n')
        self.repo.git('push', str(self.remote), 'lts:lts')
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()['state'], 'blocked')
        self.assertEqual(self.api.merges, [])

    def test_legacy_atomic_operation_fails_closed(self):
        op = self.op()
        op['data'].pop('protocol')
        self.store.save(op, 'queued', 'legacy fixture')
        self.coordinator.advance(self.op())
        self.assertEqual(self.api.merges, [])
        self.assertIn('Legacy operation', self.op()['data']['reason'])

    def test_abort_partial_merge_is_rejected(self):
        from scripts.ce_merge.__main__ import operate
        from unittest.mock import MagicMock
        self.coordinator.advance(self.op())
        with self.assertRaisesRegex(Blocked, 'already started'):
            operate(self.coordinator, 'abort', MagicMock(operation=self.operation), 'owner')
        self.assertEqual(self.store.setting('merge-lock'), self.operation)

    def test_abort_unpublished_releases_lock(self):
        from scripts.ce_merge.__main__ import operate
        from unittest.mock import MagicMock
        self.prepare()
        operate(self.coordinator, 'abort', MagicMock(operation=self.operation), 'owner')
        self.assertIsNone(self.store.setting('merge-lock'))
        self.unchanged()

    def advanced_base_candidate(self):
        self.repo.git("checkout", "-b", "documentation", self.repo.base)
        head = self.repo.commit("Docs/mock.md", "Documentation-only test\n")
        self.repo.git("push", str(self.remote), "+" + head + ":refs/pull/1/head")
        self.api.pulls[1]["head"]["sha"] = head
        self.repo.git("checkout", "lts")
        base = self.repo.commit("target-only.cc", "int target_only;\n")
        self.repo.git("push", str(self.remote), "lts:lts")
        merge = self.repo.merge_commit(base, head)
        self.repo.git("push", str(self.remote), merge + ":refs/pull/1/merge")
        raw = self.api.pull(1)
        raw["base"]["sha"] = self.repo.base
        raw["merge_commit_sha"] = merge
        api = GitHub(POLICY)
        api.repo = lambda path: (copy.deepcopy(raw) if path == "/pulls/1" else
                                 {"commit": {"sha": self.api.branch("lts")}})
        return api, head, base, merge

    def test_old_pr_base_uses_current_candidate_and_keeps_documentation_exemption(self):
        api, head, base, merge = self.advanced_base_candidate()
        pr = api.pull(1)
        with self.coordinator.graph_for(pr) as graph:
            candidate, paths, commits = self.coordinator.evidence(pr, graph)
        self.assertEqual(pr["base"]["sha"], base)
        with self.coordinator.graph_for(pr) as graph:
            candidate, _, _ = self.coordinator.evidence(pr, graph)
            graph.fetch([merge])
            self.assertEqual(graph.tree(candidate), graph.tree(merge))
            self.assertEqual(graph.parents(candidate), [base])
        self.assertEqual(paths, ["Docs/mock.md"])
        self.assertEqual(commits, [head])
        self.assertTrue(documentation_only(paths))

    def test_old_pr_merge_ref_does_not_prevent_current_target_inspection(self):
        api, _, _, _ = self.advanced_base_candidate()
        self.repo.commit("later.cc", "int later;\n")
        self.repo.git("push", str(self.remote), "lts:lts")
        pr = api.pull(1)
        with self.coordinator.graph_for(pr) as graph:
            candidate, paths, _ = self.coordinator.evidence(pr, graph)
            self.assertEqual(graph.parents(candidate), [pr["base"]["sha"]])
            self.assertEqual(paths, ["Docs/mock.md"])

    def test_missing_source_repository_blocks_publication(self):
        self.api.pulls[1]["head"]["repo"] = None
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "blocked")
        self.assertIn("PR source repository is unavailable", self.op()["data"]["reason"])
        self.unchanged()

    def test_same_repository_pr_still_requires_oca_review_and_authorization(self):
        self.api.pulls[1]["head"]["repo"]["full_name"] = POLICY["repository"]
        self.test_oca_review_and_actor_gate_before_preparation()

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

    def test_duplicate_delivery_restores_the_same_operation(self):
        # Actions serializes runs; a replacement runner restores the journal
        # before processing another delivery of the same command.
        self.store = Journal(self.root / 'journal')
        repeated = self.store.request(123, self.api.pull(1), 'owner')
        self.assertEqual(repeated, self.operation)
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
        self.repo.git("fetch", str(self.remote), lower)
        tree = self.repo.git("rev-parse", lower + "^{tree}")
        head = self.repo.git("commit-tree", tree, "-p", tip, "-p", lower, input=b"Reviewed resolution\n")
        self.repo.git("push", str(self.remote), '+' + head + ":refs/pull/3/head")
        self.api.add_pull(3, head, "trunk", "contributor/ce")
        return head

    def test_policy_change_requires_new_authorization(self):
        self.prepare()
        self.coordinator.policy = dict(POLICY, branches=["lts", "new-lts", "trunk"])
        self.coordinator.advance(self.op())
        self.assertEqual(self.op()["state"], "blocked")
        self.unchanged()


class PolicyTests(unittest.TestCase):
    def test_malformed_or_backward_metadata_fails_closed(self):
        for body in ("Upmerge-Till:\nUpmerge-Reason: example", "Upmerge-Till: trunk\nUpmerge-Till: trunk",
                     "Backport-To: lts", "Upmerge-Till: lts\nUpmerge-Reason: \nNext section"):
            with self.subTest(body=body), self.assertRaises(Blocked):
                upmerge_till(body, "lts", POLICY["branches"])

    def test_unknown_base_fails_with_public_reason(self):
        with self.assertRaises(Blocked):
            upmerge_till("", "unknown", POLICY["branches"])
    def test_explicit_upmerge_field_is_required(self):
        with self.assertRaises(Blocked):
            upmerge_till('', 'lts', POLICY['branches'])
        self.assertEqual(upmerge_till('Upmerge: trunk', 'lts', POLICY['branches']), 'trunk')

    def test_hidden_template_example_is_not_an_instruction(self):
        with self.assertRaises(Blocked):
            upmerge_till("<!--\nUpmerge: trunk\n-->", "lts", POLICY["branches"])

    def test_target_equal_to_stop_selects_backport_null_without_reason(self):
        branches = ['8.4', '9.7', 'trunk']
        for field in ('Merge-Through', 'Upmerge', 'Upmerge-Till'):
            for base in branches:
                with self.subTest(field=field, base=base):
                    self.assertEqual(upmerge_till(f'{field}: {base}', base, branches), base)

    def test_intermediate_early_stop_requires_reason(self):
        with self.assertRaises(Blocked):
            upmerge_till('Upmerge-Till: 9.7', '8.4', ['8.4', '9.7', 'trunk'])
        self.assertEqual(upmerge_till('Upmerge-Till: 9.7\nUpmerge-Reason: Already fixed on trunk',
                                     '8.4', ['8.4', '9.7', 'trunk']), '9.7')

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
        self.assertEqual(rules["CE merge executor"]["bypass_actors"][0]["bypass_mode"], "pull_request")
        self.assertEqual(rules["CE immutable tags"]["bypass_actors"], [])
        review = rules["CE reviewed changes"]["rules"][0]["parameters"]
        self.assertFalse(review["require_code_owner_review"])
        self.assertEqual(review["required_approving_review_count"], 1)




if __name__ == "__main__":
    unittest.main()
