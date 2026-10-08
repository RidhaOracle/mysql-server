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



if __name__ == '__main__':
    unittest.main()
