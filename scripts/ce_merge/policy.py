# Copyright (c) 2026, Oracle and/or its affiliates.
"""Deployment policy and pure validation helpers. No credentials or network I/O."""
import json
import re
from pathlib import Path


class Blocked(Exception):
    """A public-safe reason why an operation cannot proceed."""


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value),
            "Invalid commit SHA")
    return value


def branch_name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value)
            and ".." not in value and "//" not in value and "@{" not in value
            and all(not part.endswith((".", ".lock")) for part in value.split("/"))
            and not value.endswith("/"), "Invalid branch name")
    return value


def load(path):
    policy = json.loads(Path(path).read_text())
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", policy["repository"]),
            "Invalid repository name")
    branches = policy["branches"]
    require(branches and len(set(branches)) == len(branches), "Invalid branch chain")
    for branch in branches:
        branch_name(branch)
    require(policy["forbidden_paths"] and policy["maintainers"], "Missing merge policy")
    require(type(policy.get("bot_user_id")) is int and policy["bot_user_id"] > 0,
            "Configure bot_user_id with the automation account's GitHub ID")
    profiles = policy.get('ci_suites', {})
    require(isinstance(profiles, dict) and set(profiles) <= set(branches),
            'Test profiles must name configured versions')
    for suites in profiles.values():
        require(isinstance(suites, dict) and set(suites) == {'replication', 'storage', 'core', 'services'}
                and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*', v)
                        for v in suites.values()), 'Invalid version test profile')
    return policy


def check_content(paths, messages, policy):
    # Prefix checks also reject the directory itself, not just its descendants.
    for path in paths:
        require(not any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                        for p in policy["forbidden_paths"]), "Forbidden private path")
    for pattern in policy.get("forbidden_metadata_patterns", []):
        require(not re.search(pattern, messages, re.I), "Forbidden private metadata")
