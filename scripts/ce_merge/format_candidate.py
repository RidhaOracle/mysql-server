# Copyright (c) 2026, Oracle and/or its affiliates.
"""Formatting check with NUL-safe paths and no shell evaluation."""
import subprocess

if __name__ == "__main__":
    paths = subprocess.check_output([
        "git", "diff", "--name-only", "--diff-filter=ACM", "--no-renames", "-z", "HEAD^1", "HEAD",
    ]).decode().split("\0")
    for path in paths:
        if path.endswith((".c", ".cc", ".cpp", ".h", ".hpp")):
            subprocess.run(["clang-format-15", "--style=file", "--dry-run", "--Werror", "--", path], check=True)
