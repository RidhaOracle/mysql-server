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


def load(path):
    policy = json.loads(Path(path).read_text())
    require(policy["mode"] in ("shadow", "active"), "Invalid coordinator mode")
    require(policy.get("strategy") == "forward", "Only the forward integration strategy is supported")
    for key in ("repository", "bot_fork"):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", policy[key]),
                "Invalid repository name")
    require(policy["repository"] != policy["bot_fork"], "Bot must use a separate fork")
    branches = policy["branches"]
    require(branches and len(set(branches)) == len(branches), "Invalid branch chain")
    for branch in branches + policy.get("release_branches", []):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch)
                and ".." not in branch and "//" not in branch
                and not branch.endswith(("/", ".", ".lock")), "Invalid branch name")
    require(not set(branches) & set(policy.get("release_branches", [])),
            "Release and development branches overlap")
    require(policy["forbidden_paths"] and policy["maintainers"], "Missing merge policy")
    if policy["mode"] == "active":
        sha(policy["ci_revision"])
        require(all(policy[k] > 0 for k in ("app_id", "installation_id", "fork_installation_id", "release_app_id")),
                "Configure the GitHub App before activation")
    return policy


def matches(expected, actual):
    """Compare API policy semantically, permitting server-added default fields."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            any(matches(value, candidate) for candidate in actual) for value in expected)
    return expected == actual


def upmerge_till(body, base, branches):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    require(base in branches, "Target branch is not in the forward chain")
    require(not re.search(r"^[ \t]*Backport[^:\n]*:", body, re.M | re.I),
            "Backport instructions are not supported: target the oldest applicable branch")
    values = re.findall(r"^[ \t]*Upmerge-Till:[ \t]*(.*)$", body, re.M)
    require(len(values) <= 1, "Duplicate Upmerge-Till metadata")
    target = values[0].strip() if values else branches[-1]
    require(target in branches and branches.index(target) >= branches.index(base),
            "Invalid Upmerge-Till target")
    if target != branches[-1]:
        reasons = re.findall(r"^[ \t]*Upmerge-Reason:[ \t]*(.*)$", body, re.M)
        require(len(reasons) == 1 and reasons[0].strip(),
                "Early content stop requires Upmerge-Reason and reviewed applicability")
    return target


def check_content(paths, messages, policy):
    # Prefix checks also reject the directory itself, not just its descendants.
    for path in paths:
        require(not any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                        for p in policy["forbidden_paths"]), "Forbidden private path")
    for pattern in policy.get("forbidden_metadata_patterns", []):
        require(not re.search(pattern, messages, re.I), "Forbidden private metadata")


def documentation_only(paths):
    # Narrow allowlist. Markdown in automation, tests, or source is not exempt.
    return bool(paths) and all(p.startswith("Docs/") or p in
                              ("README", "README.md", "CONTRIBUTING.md") for p in paths)


def rulesets(policy):
    """App-only atomic publication; checks and history protections have no bypass."""
    targets = ["refs/heads/" + b for b in
               policy["branches"] + policy.get("release_branches", [])]
    app = [{"actor_id": policy["app_id"], "actor_type": "Integration", "bypass_mode": "always"}]

    def rule(name, rules, bypass=None, target="branch", include=None, exclude=None):
        return {"name": name, "target": target, "enforcement": "active",
                "conditions": {"ref_name": {"include": include or targets, "exclude": exclude or []}},
                "bypass_actors": bypass or [], "rules": rules}

    return [
        rule("CE merge executor", [{"type": "update", "parameters": {
            "update_allows_fetch_and_merge": False}}], app),
        # The App verifies the linked PRs before a single atomic push. Native
        # PR-only merging cannot publish multiple branch refs in one transaction.
        rule("CE reviewed changes", [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": True, "require_last_push_approval": True,
            "required_review_thread_resolution": True, "allowed_merge_methods": ["merge", "squash"]}}], app),
        rule("CE merge quality", [
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": name, "integration_id": policy["app_id"]}
                                           for name in ("CE / public-ci", "CE / policy")]}},
            {"type": "non_fast_forward"}, {"type": "deletion"}]),
        rule("CE branch lifecycle", [{"type": "creation"}, {"type": "deletion"},
                                     {"type": "non_fast_forward"}]),
        rule("CE immutable tags", [{"type": "update", "parameters": {
            "update_allows_fetch_and_merge": False}}, {"type": "deletion"}],
             target="tag", include=["~ALL"]),
        rule("CE release tag creation", [{"type": "creation"}], [{
            "actor_id": policy["release_app_id"], "actor_type": "Integration",
            "bypass_mode": "always"}], target="tag", include=["~ALL"],
             exclude=["refs/tags/ce-integration/*"]),
        rule("CE receipt creation", [{"type": "creation"}], app,
             target="tag", include=["refs/tags/ce-integration/*"]),
    ]
