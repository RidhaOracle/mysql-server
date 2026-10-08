# Copyright (c) 2026, Oracle and/or its affiliates.
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.policy import Blocked
from scripts.ce_merge.private import apply_results, stage
from scripts.ce_merge.release import publish
from scripts.ce_merge.journal import Journal
from test_coordinator import POLICY, Repo


class PrivateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ce = Repo(self.root / "ce")
        self.target = self.root / "target"
        self.ce.git("clone", str(self.ce.path), str(self.target))
        self.ce.git("clone", "--bare", str(self.ce.path), str(self.root / "remote.git"))
        self.sc = self.ce.commit("fix.cc", "int security_fix;\n")
        self.st = self.ce.commit("internal-sec/test", "private test\n")
        self.spec = {"kind": "ce-sync", "source": str(self.ce.path), "targets": [{
            "repository": str(self.target), "push_url": str(self.root / "remote.git"),
            "branches": [{"name": "lts", "before": self.ce.base, "source_sha": self.sc}],
            "validate": [["git", "diff", "--exit-code"]]}]}

    def test_ce_sync_preserves_ancestry_and_retry_is_idempotent(self):
        results = stage(self.spec, self.root / "stage", POLICY)
        result = results[0]["branches"][0]
        graph = PublicGraph(POLICY, str(self.root / "stage" / "0"))
        with graph:
            graph.fetch([result["after"]])
            self.assertTrue(graph.ancestor(self.sc, result["after"]))
            self.assertEqual(graph.parents(result["after"]), [self.ce.base, self.sc])
        apply_results(results)
        apply_results(results)
        actual = subprocess.check_output(["git", "--git-dir", str(self.root / "remote.git"),
                                          "rev-parse", "lts"], text=True).strip()
        self.assertEqual(actual, result["after"])

    def test_validation_failure_does_not_push(self):
        self.spec["targets"][0]["validate"] = [["git", "rev-parse", "missing-ref"]]
        with self.assertRaises(Blocked):
            stage(self.spec, self.root / "stage", POLICY)
        actual = subprocess.check_output(["git", "--git-dir", str(self.root / "remote.git"),
                                          "rev-parse", "lts"], text=True).strip()
        self.assertEqual(actual, self.ce.base)

    def security_spec(self, promotion=False):
        self.spec["kind"] = "prepare-promotion" if promotion else "security-pick"
        self.spec["security_approval"], self.spec["release_approval"] = "security-owner", "release-owner"
        target = self.spec["targets"][0]
        target["picks"] = [{"kind": "SC", "sha": self.sc, "item": "private-item"}]
        if not promotion:
            target["picks"].append({"kind": "ST", "sha": self.st, "item": "private-item"})
        target["branches"][0]["expected_public_tree"] = self.ce.git("rev-parse", self.sc + "^{tree}")
        return self.spec

    def test_security_picks_keep_code_and_tests_separate(self):
        result = stage(self.security_spec(), self.root / "stage", POLICY)[0]["branches"][0]
        self.assertEqual([p["kind"] for p in result["ledger"]], ["SC", "ST"])
        self.assertNotEqual(result["ledger"][0]["result"], self.sc)

    def test_promotion_bundle_has_no_private_test_ancestry(self):
        result = stage(self.security_spec(True), self.root / "stage", POLICY)[0]["branches"][0]
        with PublicGraph(POLICY, str(self.target)) as graph:
            graph.fetch([self.ce.base])
            graph.run("fetch", result["bundle"], result["bundle_ref"])
            paths, commits = graph.inspect(self.ce.base, result["after"])
            self.assertEqual(paths, ["fix.cc"])
            self.assertEqual(commits, [result["ledger"][0]["result"]])
            with self.assertRaises(Blocked):
                graph.run("cat-file", "-e", self.st)

    def test_promotion_rejects_security_tests(self):
        spec = self.security_spec(True)
        spec["targets"][0]["picks"] = [{"kind": "ST", "sha": self.st, "item": "private-item"}]
        with self.assertRaises(Blocked):
            stage(spec, self.root / "stage", POLICY)

    def test_promotion_requires_validated_source_tree(self):
        spec = self.security_spec(True)
        spec["targets"][0]["branches"][0]["expected_public_tree"] = "a" * 40
        with self.assertRaises(Blocked):
            stage(spec, self.root / "stage", POLICY)

    def test_no_public_write_before_release(self):
        class API:
            def repo(self, path):
                return {"draft": True, "published_at": None, "tag_name": "release"}
        with self.assertRaises(Blocked):
            publish(POLICY, None, API(), {"release_id": 1, "release_tag": "release"})


class CITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = dict(POLICY, ci_ref="trusted", app_slug="merge")
        self.revision = "d" * 40
        self.store = Journal(Path(self.temp.name) / "ci-journal")
        self.pr = {"number": 1, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}
        self.merge = "c" * 40
        self.run = {"display_title": "ce:1:" + "a" * 40 + ":" + "b" * 40 + ":" + "c" * 40 + ":" + self.revision,
                    "path": ".github/workflows/ce-merge-validation.yml", "actor": {"login": "merge[bot]"},
                    "head_sha": "d" * 40, "event": "workflow_dispatch",
                    "id": 1, "run_attempt": 1, "status": "completed", "conclusion": "success"}
        self.jobs = []
        for name in ("build (gcc)", "build (clang)", "mtr (core)", "mtr (storage)",
                     "mtr (replication)", "mtr (services)", "format"):
            steps = ["Verify candidate"] + (["Check formatting"] if name == "format" else ["Build"])
            if name.startswith("mtr"):
                steps.extend(["Verify MTR suites", "Run MTR"])
            if name == "mtr (services)":
                steps.append("Run unit tests")
            self.jobs.append({"name": name, "conclusion": "success", "steps": [
                {"name": step, "conclusion": "success"} for step in steps]})
        case = self

        class API:
            def revision(self, ref):
                case.assertEqual(ref, case.policy["ci_ref"])
                return case.revision

            def pages(self, path, key):
                return [case.run] if "workflows/" in path else case.jobs

            def repo(self, path, method="GET", data=None):
                if method == "GET":
                    return case.run
                case.dispatched = True
                case.dispatch = data

        self.dispatched = False
        self.coordinator = Coordinator(self.policy, self.store, API())
        # Establish the durable request just as the first eligibility poll would.
        self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.dispatched = False

    def test_rehearsal_selection_is_bound_to_dispatch_and_result(self):
        self.policy["ci_mtr_test"] = "main.1st"
        passed, message = self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.assertFalse(passed)
        self.assertIn("REHEARSAL", message)
        self.assertEqual(self.dispatch["inputs"]["mtr_test"], "main.1st")
        self.run["display_title"] += ":mtr=main.1st"
        self.coordinator.store = Journal(self.store.path)
        passed, message = self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.assertTrue(passed)
        self.assertIn("REHEARSAL (main.1st only per MTR shard)", message)
        self.policy.pop("ci_mtr_test")
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        with self.assertRaises(Blocked):
            self.coordinator.rerun_ci(1, "operator")

    def test_changing_rehearsal_test_dispatches_new_identity(self):
        for test in ("main.1st", "main.ce_merge_smoke"):
            self.policy["ci_mtr_test"] = test
            self.dispatched = False
            self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
            self.assertTrue(self.dispatched)
            self.assertEqual(self.dispatch["inputs"]["mtr_test"], test)

    def test_default_dispatch_does_not_require_new_workflow_input(self):
        self.assertNotIn("mtr_test", self.dispatch["inputs"])

    def test_failed_rehearsal_remains_a_failure(self):
        self.policy["ci_mtr_test"] = "main.1st"
        self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.run["display_title"] += ":mtr=main.1st"
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(Blocked, "REHEARSAL.*failed"):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])

    def test_complete_trusted_ci_passes(self):
        self.assertTrue(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])

    def test_request_records_resolved_revision_and_survives_restart(self):
        request = self.store.setting(self.coordinator.ci_request_key(self.run["display_title"]))
        self.assertEqual(request["ref"], "trusted")
        self.assertEqual(request["revision"], self.revision)
        self.assertEqual(self.dispatch["inputs"]["workflow_revision"], self.revision)
        self.coordinator.store = Journal(self.store.path)
        self.assertTrue(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertFalse(self.dispatched)

    def test_unrecorded_run_does_not_satisfy_ci(self):
        self.coordinator.store = Journal(self.store.path / "fresh")
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertTrue(self.dispatched)

    def test_moving_ref_redispatches_without_old_revision_throttle(self):
        self.revision = "e" * 40
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertTrue(self.dispatched)
        self.assertEqual(self.dispatch["inputs"]["workflow_revision"], self.revision)
        self.dispatched = False
        # A dispatch race can run new workflow code with the OLD request title.
        self.run["head_sha"] = self.revision
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.run["display_title"] = self.run["display_title"].rsplit(":", 1)[0] + ":" + self.revision
        self.assertTrue(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])

    def test_same_revision_dispatch_is_throttled(self):
        self.run["display_title"] = "unrelated"
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertFalse(self.dispatched)

    def test_invalid_resolved_revision_fails_closed(self):
        self.revision = "not-a-sha"
        with self.assertRaises(Blocked):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.assertFalse(self.dispatched)

    def test_rerun_requires_recorded_current_revision_and_app_identity(self):
        self.coordinator.rerun_ci(1, "operator")
        self.assertTrue(self.dispatched)
        self.dispatched = False
        self.revision = "e" * 40
        with self.assertRaises(Blocked):
            self.coordinator.rerun_ci(1, "operator")
        self.revision = "d" * 40
        self.run["actor"]["login"] = "contributor"
        with self.assertRaises(Blocked):
            self.coordinator.rerun_ci(1, "operator")
        self.assertFalse(self.dispatched)

    def test_dispatch_race_stops_candidate_verification_before_git(self):
        script = Path(__file__).resolve().parents[1] / "verify_candidate.py"
        result = subprocess.run([sys.executable, str(script)], cwd=self.temp.name,
                                env=dict(os.environ, EXPECTED_WORKFLOW="d" * 40, ACTUAL_WORKFLOW="e" * 40),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Trusted CI ref moved", result.stderr)

    def test_workflow_and_candidate_verifier_accepts_exact_identities(self):
        import json
        repo = Repo(Path(self.temp.name) / "candidate")
        head = repo.commit("fix.cc", "int fix;\n")
        script = Path(__file__).resolve().parents[1] / "verify_candidate.py"
        result = subprocess.run([sys.executable, str(script)], cwd=repo.path,
                                env=dict(os.environ, EXPECTED_WORKFLOW=self.revision, ACTUAL_WORKFLOW=self.revision,
                                         EXPECTED_BASE=repo.base, EXPECTED_MERGE=head,
                                         EXPECTED_PARENTS=json.dumps([repo.base])),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lost_dispatch_response_retains_revision_for_reconciliation(self):
        self.revision = "e" * 40
        repo = self.coordinator.github.repo
        def disconnected(*args):
            raise TimeoutError("lost response")
        self.coordinator.github.repo = disconnected
        with self.assertRaises(TimeoutError):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        self.coordinator.github.repo = repo
        self.coordinator.store = Journal(self.store.path)
        self.run["display_title"] = self.run["display_title"].rsplit(":", 1)[0] + ":" + self.revision
        self.run["head_sha"] = self.revision
        self.assertTrue(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertFalse(self.dispatched)

    def test_skipped_required_step_rejected(self):
        self.jobs[0]["steps"][1]["conclusion"] = "skipped"
        with self.assertRaises(Blocked):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])

    def test_missing_job_rejected(self):
        self.jobs.pop()
        with self.assertRaises(Blocked):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])

    def test_missing_or_skipped_suite_validation_rejected(self):
        job = next(job for job in self.jobs if job["name"] == "mtr (services)")
        step = next(step for step in job["steps"] if step["name"] == "Verify MTR suites")
        step["conclusion"] = "skipped"
        with self.assertRaises(Blocked):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])
        job["steps"].remove(step)
        with self.assertRaises(Blocked):
            self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])

    def test_stale_base_cannot_satisfy_ci(self):
        self.pr["base"]["sha"] = "e" * 40
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.assertTrue(self.dispatched)

    def test_untrusted_actor_cannot_satisfy_ci(self):
        self.run["actor"]["login"] = "contributor"
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])

    def test_unpinned_revision_and_wrong_event_cannot_satisfy_ci(self):
        self.run["head_sha"] = "e" * 40
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])
        self.run["head_sha"], self.run["event"] = "d" * 40, "pull_request"
        self.assertFalse(self.coordinator.ci(self.pr, self.merge, ["sql/a.cc"])[0])

    def test_documentation_receives_explicit_not_applicable(self):
        passed, message = self.coordinator.ci(self.pr, self.merge, ["Docs/manual.md"])
        self.assertTrue(passed)
        self.assertIn("not applicable", message)
        self.assertFalse(self.dispatched)


if __name__ == "__main__":
    unittest.main()
