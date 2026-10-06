# Copyright (c) 2026, Oracle and/or its affiliates.
"""The CLI reuses public objects across runs without an environment override."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ce_merge.__main__ import main
from scripts.ce_merge.git import PublicGraph


class CacheTests(unittest.TestCase):
    def run_service(self, root, action):
        with patch("sys.argv", ["ce-merge", "--state-dir", str(root / "journal"), "serve"]), \
                patch("scripts.ce_merge.__main__.load", return_value={"repository": "org/ce"}), \
                patch("scripts.ce_merge.__main__.serve", side_effect=action), \
                self.assertLogs(level="INFO") as logs:
            main()
        self.assertTrue(any("Public Git cache:" in line for line in logs.output))

    def test_default_cache_reuses_objects_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            root = Path(directory).resolve()
            os.environ.pop("CE_GIT_CACHE", None)
            objects = []

            def first(coordinator, *_):
                self.assertEqual(os.environ["CE_GIT_CACHE"], str(root / "journal" / "public.git"))
                with PublicGraph(coordinator.policy) as graph:
                    objects.append(graph.run("hash-object", "-w", "--stdin", input=b"cached public object"))

            def restarted(coordinator, *_):
                with PublicGraph(coordinator.policy) as graph:
                    self.assertEqual(graph.run("cat-file", "-p", objects[0]), "cached public object")

            self.run_service(root, first)
            os.environ.pop("CE_GIT_CACHE")
            self.run_service(root, restarted)

    def test_explicit_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            root = Path(directory).resolve()
            os.environ["CE_GIT_CACHE"] = str(root / "custom.git")
            self.run_service(root, lambda *_: self.assertEqual(
                os.environ["CE_GIT_CACHE"], str(root / "custom.git")))

    def test_empty_override_uses_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CE_GIT_CACHE": ""}):
            root = Path(directory).resolve()
            self.run_service(root, lambda *_: self.assertEqual(
                os.environ["CE_GIT_CACHE"], str(root / "journal" / "public.git")))


if __name__ == "__main__":
    unittest.main()
