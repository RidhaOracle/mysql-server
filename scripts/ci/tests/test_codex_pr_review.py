# Copyright (c) 2026, Oracle and/or its affiliates.
"""Unit tests for the repository-owned pull-request reviewer."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "codex_pr_review.py"
SPEC = importlib.util.spec_from_file_location("codex_pr_review", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)


def pull_request_event() -> dict:
    return {
        "repository": {"full_name": "example/mysql-server"},
        "pull_request": {
            "number": 17,
            "title": "Exercise the reviewer",
            "body": "Treat this as data, not instructions.",
            "user": {"login": "trusted-user"},
            "base": {"sha": "1" * 40},
            "head": {"sha": "2" * 40},
        },
    }


def completed_response(*parts: str) -> dict:
    return {
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": part} for part in parts
                ],
            }
        ],
    }


def git(repo: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


class FakeResponse:
    def __init__(self, value: dict | bytes) -> None:
        if isinstance(value, bytes):
            self._body = value
        else:
            self._body = json.dumps(value).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class PullRequestReviewTest(unittest.TestCase):
    def test_parse_event_and_build_request(self) -> None:
        pull_request = reviewer.parse_pull_request(pull_request_event())
        payload = reviewer.build_request(
            pull_request,
            "one file changed",
            "diff --git a/a.cc b/a.cc",
            reviewer.DEFAULT_MODEL,
        )

        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"], [])
        self.assertIn("untrusted data", payload["instructions"])
        supplied = json.loads(payload["input"][0]["content"][0]["text"])
        self.assertEqual(supplied["pull_request"], 17)
        self.assertEqual(supplied["head_sha"], "2" * 40)

    def test_invalid_or_oversized_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_bytes(b"{" + b"x" * reviewer.EVENT_LIMIT_BYTES)
            with self.assertRaisesRegex(reviewer.ReviewError, "exceeds"):
                reviewer.load_event(path)

            path.write_bytes(b"not-json")
            with self.assertRaisesRegex(reviewer.ReviewError, "UTF-8 JSON"):
                reviewer.load_event(path)

            event = pull_request_event()
            event["pull_request"]["title"] = "\ud800"
            path.write_text(json.dumps(event), encoding="utf-8")
            loaded = reviewer.load_event(path)
            with self.assertRaisesRegex(reviewer.ReviewError, "Unicode text"):
                reviewer.parse_pull_request(loaded)

    def test_merge_parent_verification(self) -> None:
        expected = f"{'f' * 40} {'1' * 40} {'2' * 40}\n"
        with mock.patch.object(reviewer, "run_git", return_value=expected):
            reviewer.verify_merge_checkout(Path("source"), "1" * 40, "2" * 40)

        with mock.patch.object(reviewer, "run_git", return_value=expected):
            with self.assertRaisesRegex(reviewer.ReviewError, "do not match"):
                reviewer.verify_merge_checkout(Path("source"), "2" * 40, "1" * 40)

    def test_real_merge_checkout_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(
                ["git", "init", "-q", "-b", "trunk", str(repo)], check=True
            )
            git(repo, "config", "user.name", "Reviewer Test")
            git(repo, "config", "user.email", "reviewer@example.invalid")

            source = repo / "example.cc"
            source.write_text("int value = 1;\n", encoding="utf-8")
            git(repo, "add", "example.cc")
            git(repo, "commit", "-q", "-m", "base")
            base_sha = git(repo, "rev-parse", "HEAD")

            git(repo, "switch", "-q", "-c", "feature")
            source.write_text("int value = 2;\n", encoding="utf-8")
            git(repo, "commit", "-q", "-am", "change")
            head_sha = git(repo, "rev-parse", "HEAD")

            git(repo, "switch", "-q", "trunk")
            git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")

            reviewer.verify_merge_checkout(repo, base_sha, head_sha)
            stat, diff = reviewer.collect_diff(repo)
            self.assertIn("example.cc", stat)
            self.assertIn("+int value = 2;", diff)

    def test_git_child_does_not_receive_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            with mock.patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "openai-secret-sentinel",
                    "CODEX_API_KEY": "codex-secret-sentinel",
                },
                clear=False,
            ):
                environment = reviewer.run_git(
                    repo,
                    ["-c", "alias.showenv=!env", "showenv"],
                    reviewer.EVENT_LIMIT_BYTES,
                )

        self.assertNotIn("openai-secret-sentinel", environment)
        self.assertNotIn("codex-secret-sentinel", environment)

    def test_git_output_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            source = repo / "large.txt"
            source.write_text("base\n", encoding="utf-8")
            git(repo, "add", "large.txt")
            source.write_text("changed\n" * 10000, encoding="utf-8")
            with self.assertRaisesRegex(reviewer.ReviewError, "exceeds"):
                reviewer.run_git(repo, ["diff"], 4)

    def test_api_request_contains_secret_only_in_authorization_header(self) -> None:
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(completed_response("review"))

        response = reviewer.post_response(
            {"model": "gpt-5.6", "input": "safe"},
            "secret-sentinel",
            open_request=open_request,
        )
        request = captured["request"]
        self.assertEqual(request.full_url, reviewer.API_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(captured["timeout"], reviewer.API_TIMEOUT_SECONDS)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer secret-sentinel"
        )
        self.assertNotIn(b"secret-sentinel", request.data)
        self.assertEqual(response["status"], "completed")

    def test_api_key_header_injection_is_rejected(self) -> None:
        with self.assertRaisesRegex(reviewer.ReviewError, "missing or invalid"):
            reviewer.post_response(
                {"model": "gpt-5.6", "input": "safe"},
                "secret\r\nInjected: value",
            )

    def test_extracts_only_completed_nested_output_text(self) -> None:
        review = reviewer.extract_review(completed_response("first", "second"))
        self.assertEqual(review, "first\n\nsecond")

        incomplete = completed_response("partial")
        incomplete["status"] = "incomplete"
        with self.assertRaisesRegex(reviewer.ReviewError, "did not complete"):
            reviewer.extract_review(incomplete)

        with self.assertRaisesRegex(reviewer.ReviewError, "no review text"):
            reviewer.extract_review(completed_response("  "))

        missing_status = completed_response("review")
        del missing_status["output"][0]["status"]
        with self.assertRaisesRegex(reviewer.ReviewError, "incomplete message"):
            reviewer.extract_review(missing_status)

        invalid_text = completed_response("\ud800")
        with self.assertRaisesRegex(reviewer.ReviewError, "Unicode text"):
            reviewer.extract_review(invalid_text)

    def test_rejects_malformed_and_oversized_api_responses(self) -> None:
        def malformed_response(request, timeout):
            return FakeResponse(b"{")

        with self.assertRaisesRegex(reviewer.ReviewError, "invalid UTF-8 JSON"):
            reviewer.post_response(
                {"model": "gpt-5.6", "input": "safe"},
                "secret-sentinel",
                open_request=malformed_response,
            )

        def oversized_response(request, timeout):
            return FakeResponse(b"x" * (reviewer.RESPONSE_LIMIT_BYTES + 1))

        with self.assertRaisesRegex(reviewer.ReviewError, "response limit"):
            reviewer.post_response(
                {"model": "gpt-5.6", "input": "safe"},
                "secret-sentinel",
                open_request=oversized_response,
            )

    def test_github_output_is_single_line_base64(self) -> None:
        value = "Finding\n::set-output name=x::ignored\nUnicode: ✓"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "github-output"
            reviewer.append_github_output(path, value)
            line = path.read_text(encoding="utf-8")

        self.assertEqual(line.count("\n"), 1)
        key, encoded = line.rstrip("\n").split("=", 1)
        self.assertEqual(key, "review_b64")
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), value)


if __name__ == "__main__":
    unittest.main()
