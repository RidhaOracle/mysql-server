# Copyright (c) 2026, Oracle and/or its affiliates.
"""Real Git fetches retain negotiation tips without updating public branches."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.tests.test_coordinator import Repo


class PublicFetchCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = Repo(self.root / "source")
        env = patch.dict(os.environ, {"CE_GIT_CACHE": str(self.root / "public.git")})
        env.start()
        self.addCleanup(env.stop)

    def graph(self):
        graph = PublicGraph({"repository": "example/ce"})
        # file:// exercises upload-pack negotiation instead of a local clone copy.
        graph.remote = self.repo.path.as_uri()
        return graph

    def test_restart_negotiates_from_cached_tip_for_new_commit(self):
        source = "refs/heads/lts"
        with self.graph() as graph:
            graph.fetch([source])
            self.assertEqual(graph.run("rev-parse", graph.cache_ref(source)), self.repo.base)
        head = self.repo.commit("new.cc", "int new_change;\n")
        trace = self.root / "fetch-trace"
        with self.graph() as graph, patch.dict(os.environ, {"GIT_TRACE_PACKET": str(trace)}):
            graph.fetch([source])
            self.assertEqual(graph.run("rev-parse", graph.cache_ref(source)), head)
            self.assertEqual(graph.run("show", head + ":new.cc"), "int new_change;")
        self.assertIn("have " + self.repo.base, trace.read_text())

    def test_rewritten_pr_tip_updates_only_cache_reference(self):
        first = self.repo.commit("first.cc", "int first;\n")
        source = "refs/pull/44/head"
        self.repo.git("update-ref", source, first)
        with self.graph() as graph:
            graph.fetch([source])
        self.repo.git("checkout", "-b", "replacement", self.repo.base)
        second = self.repo.commit("second.cc", "int second;\n")
        self.repo.git("update-ref", source, second)
        with self.graph() as graph:
            graph.fetch([source])
            self.assertEqual(graph.run("rev-parse", graph.cache_ref(source)), second)
            self.assertEqual(graph.run("for-each-ref", "--format=%(refname)"), graph.cache_ref(source))
        self.assertEqual(self.repo.git("rev-parse", "lts"), first)
        self.assertEqual(self.repo.git("rev-parse", source), second)

    def test_exact_sha_fetch_retains_tip_across_restart(self):
        with self.graph() as graph:
            graph.fetch([self.repo.base])
        with self.graph() as graph:
            self.assertEqual(graph.run("rev-parse", graph.cache_ref(self.repo.base)), self.repo.base)


if __name__ == "__main__":
    unittest.main()
