# Copyright (c) 2026, Oracle and/or its affiliates.
"""Check public source suite locations before spending time on a CI build."""
import argparse
from pathlib import Path
import re


def verify_suites(root, suites):
    names = suites.split(",")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names):
        raise ValueError("MTR suites must be a nonempty comma-separated list of suite names")
    missing = []
    for name in names:
        # Public locations from mysql-test/lib/mtr_cases.pm:get_suite_dir().
        # Internal locations and PB2's optional-suite exception are not public CI inputs.
        if name == "main":
            paths = [root / "mysql-test"]
        else:
            bases = [root / "lib/mysql-test/suite", root / "mysql-test/suite",
                     root / "plugin" / name / "tests", root / "share/mysql-test/suite"]
            paths = [base / leaf for base in bases for leaf in (name, "mtr")]
        if not any(path.is_dir() for path in paths):
            missing.append(name)
    if missing:
        raise ValueError("Configured MTR suites missing from public source: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--suites", required=True)
    args = parser.parse_args()
    try:
        verify_suites(args.source, args.suites)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print("All configured MTR suites exist in the public source")


if __name__ == "__main__":
    main()
