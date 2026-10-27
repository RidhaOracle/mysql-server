# Copyright (c) 2026, Oracle and/or its affiliates.
"""Run one actual main-suite MTR test for a temporary coordinator rehearsal."""
import argparse
import os
from pathlib import Path
import re
import subprocess


def verify_test(root, value):
    if not re.fullmatch(r"main\.[A-Za-z0-9_-]+", value):
        raise ValueError("Rehearsal requires one exact main.test_name; patterns and options are not allowed")
    if not (root / "mysql-test/t" / (value.split(".", 1)[1] + ".test")).is_file():
        raise ValueError("Rehearsal MTR test does not exist in candidate source: " + value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        verify_test(Path.cwd(), args.test)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    notice = f"REHEARSAL ONLY: {args.test}; this is not full MTR suite coverage."
    print(notice, flush=True)
    if args.verify_only:
        return
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(notice + "\n")
    runner = Path(__file__).resolve().parents[1] / "ci/mtr.sh"
    result = subprocess.run([str(runner), "--parallel=1", "--suite=main", args.test])
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
