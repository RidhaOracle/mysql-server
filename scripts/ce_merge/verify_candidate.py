# Copyright (c) 2026, Oracle and/or its affiliates.
"""Executed from the trusted workflow checkout, before running contributor code."""
import json
import os
import re
import subprocess


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


if __name__ == "__main__":
    for name in ("EXPECTED_BASE", "EXPECTED_MERGE"):
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ[name]):
            raise SystemExit("Invalid candidate identity")
    expected = json.loads(os.environ["EXPECTED_PARENTS"])
    if (not isinstance(expected, list) or len(expected) not in (1, 2, 3) or
            any(not isinstance(p, str) or not re.fullmatch(r"[0-9a-f]{40}", p) for p in expected) or
            expected[0] != os.environ["EXPECTED_BASE"]):
        raise SystemExit("Invalid candidate parents")
    if git("rev-parse", "HEAD") != os.environ["EXPECTED_MERGE"]:
        raise SystemExit("Candidate SHA changed")
    if git("show", "-s", "--format=%P", "HEAD").split() != expected:
        raise SystemExit("Candidate base/head changed")
    print("Verified exact prepared candidate and every parent")
