# Copyright (c) 2026, Oracle and/or its affiliates.
"""Bound Git fetches independently and clean up inherited helper pipes."""
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from scripts.ce_merge.git import PublicGraph
from scripts.ce_merge.policy import Blocked


class GitTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.graph = PublicGraph({"repository": "org/ce"}, remote="unused-test-remote")
        self.addCleanup(self.graph.__exit__)

    def test_fetch_default_override_and_other_command_deadlines(self):
        for override, command, expected in ((None, "fetch", 3600), ("7200", "fetch", 7200),
                                            ("7200", "rev-parse", 600)):
            process = MagicMock()
            process.__enter__.return_value = process
            process.communicate.return_value = (b"result\n", b"")
            process.returncode = 0
            with self.subTest(override=override, command=command), patch.dict(os.environ), \
                    patch("scripts.ce_merge.git.subprocess.Popen", return_value=process):
                os.environ.pop("CE_GIT_FETCH_TIMEOUT", None)
                if override is not None:
                    os.environ["CE_GIT_FETCH_TIMEOUT"] = override
                self.assertEqual(self.graph.run(command), "result")
                process.communicate.assert_called_once_with(None, timeout=expected)

    def test_invalid_fetch_timeout_is_rejected_before_starting_git(self):
        for invalid in ("", "0", "-1", "nan", "1.5"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {"CE_GIT_FETCH_TIMEOUT": invalid}), \
                    patch("scripts.ce_merge.git.subprocess.Popen") as launch, \
                    self.assertRaisesRegex(Blocked, "positive integer"):
                self.graph.fetch(["refs/heads/trunk"])
            launch.assert_not_called()

    def test_fetch_timeout_terminates_helpers_holding_output_pipes(self):
        marker = Path(self.graph.path) / "helper-started"
        child = "from pathlib import Path; import time; Path(" + repr(str(marker)) + ").touch(); time.sleep(6)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); time.sleep(6)"
        launch = subprocess.Popen

        def slow_git(_command, **kwargs):
            return launch([sys.executable, "-c", parent], **kwargs)

        started = time.monotonic()
        with patch.dict(os.environ, {"CE_GIT_FETCH_TIMEOUT": "1"}), \
                patch("scripts.ce_merge.git.subprocess.Popen", side_effect=slow_git), \
                self.assertLogs(level="INFO") as logs, \
                self.assertRaisesRegex(Blocked, "fetch timed out after 1 seconds"):
            self.graph.fetch(["refs/heads/trunk"])
        self.assertTrue(marker.exists(), "The helper must start before the timeout")
        self.assertLess(time.monotonic() - started, 4, "An orphaned helper kept output pipes open")
        self.assertTrue(any("fetch started" in line for line in logs.output))
        self.assertTrue(any("CE_GIT_FETCH_TIMEOUT" in line for line in logs.output))

    def test_push_timeout_and_interruption_preserve_original_exception(self):
        for error in (subprocess.TimeoutExpired("git", 600), KeyboardInterrupt()):
            process = MagicMock()
            process.__enter__.return_value = process
            process.pid = 123
            process.communicate.side_effect = [error, (b"", b"")]
            with self.subTest(error=type(error).__name__), \
                    patch("scripts.ce_merge.git.subprocess.Popen", return_value=process), \
                    patch("scripts.ce_merge.git.os.killpg") as terminate, \
                    self.assertRaises(type(error)) as caught:
                self.graph.run("push", "--atomic")
            self.assertIs(caught.exception, error)
            terminate.assert_called_once_with(123, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
