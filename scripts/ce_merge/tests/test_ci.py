# Copyright (c) 2026, Oracle and/or its affiliates.
"""Ordinary PR CI gates preserve revision, workflow, step, and rerun identity."""
import copy
import unittest
from unittest.mock import MagicMock

from scripts.ce_merge.ci import WORKFLOWS, existing_pr_ci
from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.policy import Blocked
from test_coordinator import POLICY


class CITests(unittest.TestCase):
    def setUp(self):
        self.base, self.head, self.merge, self.tree = (c * 40 for c in "abcd")
        self.pr = {"number": 1, "head": {"sha": self.head},
                   "base": {"sha": self.base, "ref": "lts"}, "merge_commit_sha": self.merge}
        self.graph = MagicMock()
        self.graph.ancestor.side_effect = lambda old, current: old in (self.base, "e" * 40) and current == self.base
        self.graph.merge_tree.return_value = self.tree
        self.graph.tree.return_value = self.tree
        self.runs, self.jobs, self.workflows = {}, {}, {}
        for i, (name, expected) in enumerate(WORKFLOWS.items(), 1):
            path = ".github/workflows/" + name
            self.workflows[name] = {"id": i, "path": path, "name": name}
            self.runs[name] = [{"id": 100 + i, "run_number": 1, "run_attempt": 1, "workflow_id": i,
                                "event": "pull_request", "head_sha": self.head, "path": path,
                                "repository": {"full_name": POLICY["repository"]},
                                "display_title": f"pr:1:{self.base}:{self.head}:{self.merge}",
                                "status": "completed", "conclusion": "success"}]
            self.jobs[100 + i] = []
            for job, required in expected.items():
                steps = list(required)
                if name == "mtr.yml":
                    steps += ["Run MTR"]
                    if job == "MTR (services)":
                        steps += ["Run unit tests"]
                self.jobs[100 + i].append({"name": job, "run_attempt": 1, "status": "completed", "conclusion": "success",
                                           "steps": [{"name": step, "conclusion": "success"} for step in steps]})
        self.api = MagicMock()
        self.api.repo.side_effect = lambda path: self.workflows[path.rsplit("/", 1)[1]]
        self.api.pages.side_effect = self.pages

    def pages(self, path, key=None):
        if "/jobs" in path:
            return self.jobs[int(path.split("/")[3])]
        return self.runs[path.split("/")[3]]

    def check(self, policy=POLICY):
        return existing_pr_ci(self.api, policy, self.pr, self.merge, ["sql/a.cc"], self.graph)

    def test_complete_trusted_ci_passes(self):
        self.assertTrue(self.check()[0])

    def test_accepted_target_ancestor_passes_for_unchanged_head(self):
        for runs in self.runs.values():
            runs[0]["display_title"] = f'pr:1:{"e" * 40}:{self.head}:{self.merge}'
        self.assertTrue(self.check()[0])

    def test_unrelated_target_or_changed_head_waits(self):
        run = self.runs["pr-build.yml"][0]
        for title in (f'pr:1:{"f" * 40}:{self.head}:{self.merge}', f'pr:1:{self.base}:{"f" * 40}:{self.merge}'):
            run["display_title"] = title
            with self.subTest(title=title):
                self.assertFalse(self.check()[0])

    def test_wrong_event_workflow_path_repository_or_pr_is_not_evidence(self):
        before = copy.deepcopy(self.runs["pr-build.yml"][0])
        variants = ({"event": "workflow_dispatch"}, {"workflow_id": 1000}, {"path": "other.yml"},
                    {"repository": {"full_name": "other/ce"}}, {"display_title": f"pr:2:{self.base}:{self.head}:{self.merge}"})
        for update in variants:
            self.runs["pr-build.yml"][0] = dict(before, **update)
            with self.subTest(update=update):
                self.assertFalse(self.check()[0])

    def test_missing_failed_or_skipped_evidence_cannot_pass(self):
        before = copy.deepcopy(self.jobs[101])
        for value in ([], [dict(before[0], conclusion="failure")],
                      [dict(before[0], steps=[{"name": "Build", "conclusion": "skipped"}]), before[1]]):
            self.jobs[101] = value
            with self.subTest(value=value), self.assertRaises(Blocked):
                self.check()

    def test_latest_run_and_latest_job_attempt_control_results(self):
        newer = dict(self.runs["pr-build.yml"][0], id=500, run_number=2, conclusion="failure")
        self.runs["pr-build.yml"].append(newer)
        with self.assertRaisesRegex(Blocked, "did not pass"):
            self.check()
        self.runs["pr-build.yml"].pop()
        self.runs["pr-build.yml"][0]["run_attempt"] = 2
        self.jobs[101].append(dict(self.jobs[101][0], run_attempt=2, conclusion="failure"))
        with self.assertRaisesRegex(Blocked, "did not pass"):
            self.check()

    def test_partial_rerun_keeps_successful_jobs_from_earlier_attempt(self):
        self.runs["pr-build.yml"][0]["run_attempt"] = 2
        failed = self.jobs[101][0]
        self.jobs[101].append(dict(failed, run_attempt=2))
        failed['conclusion'] = 'failure'
        self.assertTrue(self.check()[0])

    def test_duplicate_or_inconsistent_job_attempts_block(self):
        original = copy.deepcopy(self.jobs[101])
        for extra in (original[0], dict(original[0], run_attempt=0), dict(original[0], run_attempt=2)):
            self.jobs[101] = original + [extra]
            with self.subTest(extra=extra), self.assertRaises(Blocked):
                self.check()

    def test_missing_mtr_or_unit_test_steps_fail(self):
        before = copy.deepcopy(self.jobs[102])
        for omitted in ("Run MTR", "Run unit tests"):
            self.jobs[102] = copy.deepcopy(before)
            for job in self.jobs[102]:
                job["steps"] = [step for step in job["steps"] if step["name"] != omitted]
            with self.subTest(omitted=omitted), self.assertRaises(Blocked):
                self.check()

    def test_explicit_rehearsal_is_accepted_and_mixed_coverage_rejected(self):
        test = "main.1st"
        for job in self.jobs[102]:
            for step in job["steps"]:
                if step["name"] == "Run MTR":
                    step["conclusion"] = "skipped"
            job["steps"] += [{"name": "Verify rehearsal test", "conclusion": "success"},
                              {"name": f"Run MTR rehearsal ({test})", "conclusion": "success"}]
        self.assertTrue(self.check(dict(POLICY, ci_mtr_test=test))[0])
        with self.assertRaises(Blocked):
            self.check()
        for step in self.jobs[102][0]["steps"]:
            if step["name"] == "Run MTR":
                step["conclusion"] = "success"
        with self.assertRaisesRegex(Blocked, "mix"):
            self.check(dict(POLICY, ci_mtr_test=test))

    def test_ci_workflow_edits_cannot_certify_themselves(self):
        with self.assertRaisesRegex(Blocked, "bootstrap"):
            existing_pr_ci(self.api, POLICY, self.pr, self.merge, [".github/workflows/mtr.yml"], self.graph)

    def test_current_target_must_still_merge_to_the_candidate_tree(self):
        self.graph.merge_tree.return_value = "f" * 40
        with self.assertRaisesRegex(Blocked, "differs"):
            Coordinator(POLICY, None, self.api).ci(self.pr, self.merge, ["sql/a.cc"], graph=self.graph)

    def test_documentation_exemption_is_explicit(self):
        ok, message = Coordinator(POLICY, None, self.api).ci(self.pr, self.merge, ["Docs/guide.md"], graph=self.graph)
        self.assertTrue(ok)
        self.assertIn("documentation-only", message)
        self.api.pages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
