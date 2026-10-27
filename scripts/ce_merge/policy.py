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
    policy.setdefault("state_branch", "ce-merge-state")
    branch_name(policy["state_branch"])
    reserved = ("merge", "merge-tests")
    require(policy["state_branch"] not in branches and not any(
            policy["state_branch"] == p or policy["state_branch"].startswith(p + "/") for p in reserved),
            "State branch must be separate from integration and staging branches")
    all_names = branches + [policy["state_branch"]]
    require(not any(a != b and b.startswith(a + "/") for a in all_names for b in all_names),
            "Branch names cannot occupy each other's ref namespaces")
    for branch in branches:
        branch_name(branch)
        require(not any(branch == p or branch.startswith(p + "/") for p in reserved),
                "CE targets cannot use the reserved bot staging namespace")
    require(policy["forbidden_paths"] and policy["maintainers"], "Missing merge policy")
    require(type(policy.get("bot_user_id")) is int and policy["bot_user_id"] > 0,
            "Configure bot_user_id with the automation account's GitHub ID")
    rehearsal_test(policy)
    profiles = policy.get('ci_suites', {})
    require(isinstance(profiles, dict) and set(profiles) <= set(branches),
            'Test profiles must name configured versions')
    for suites in profiles.values():
        require(isinstance(suites, dict) and set(suites) == {'replication', 'storage', 'core', 'services'}
                and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*', v)
                        for v in suites.values()), 'Invalid version test profile')
    return policy


def rehearsal_test(policy):
    """An explicit deployment-only shortcut; an empty value keeps full MTR."""
    value = policy.get("ci_mtr_test", "")
    require(isinstance(value, str) and (not value or re.fullmatch(r"main\.[A-Za-z0-9_-]+", value)),
            "ci_mtr_test must be empty or one main.test_name for rehearsal")
    return value


def matches(expected, actual):
    """Compare API policy semantically, permitting server-added default fields."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            any(matches(value, candidate) for candidate in actual) for value in expected)
    return expected == actual


def merge_through(body, base, branches):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    require(base in branches, "Target branch is not in the forward chain")
    require(not re.search(r"^[ \t]*Backport[^:\n]*:", body, re.M | re.I),
            "Backport instructions are not supported: target the oldest applicable branch")
    require(not re.search(r"^[ \t]*Upmerge(?:-Till|-Reason)?:", body, re.M),
            "Use Merge-Through and Merge-Reason metadata")
    values = re.findall(r"^[ \t]*Merge-Through:[ \t]*([^\n]*)$", body, re.M)
    require(len(values) == 1, "Exactly one Merge-Through: <last-content-branch> field is required")
    target = values[0].strip()
    require(target in branches and branches.index(target) >= branches.index(base),
            "Invalid Merge-Through target")
    if target != branches[-1]:
        reasons = re.findall(r"^[ \t]*Merge-Reason:[ \t]*(.*)$", body, re.M)
        # Stopping at the PR base explicitly selects a backport-only change;
        # every later version receives an ancestry-only merge.
        require((target == base and not reasons) or (len(reasons) == 1 and reasons[0].strip()),
                "Early content stop beyond the PR target requires Merge-Reason; supplied reasons must be non-empty")
    return target


def resolution_number(value, repository):
    value = str(value).strip()
    match = re.fullmatch(r"(?:#)?([1-9][0-9]*)", value) or re.fullmatch(
        r"https://github\.com/" + re.escape(repository) + r"/pull/([1-9][0-9]*)/?", value)
    require(match, "Resolution PR must be a number or a PR URL in this repository")
    return int(match[1])


def resolution_prs(body, base, policy):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    result = {}
    for line in re.findall(r"^[ \t]*Resolution-PR:[ \t]*([^\n]*)$", body, re.M):
        parts = line.split()
        require(len(parts) == 2, "Use Resolution-PR: <target-branch> <PR-link>")
        branch, value = parts
        require(branch in policy["branches"] and
                policy["branches"].index(branch) > policy["branches"].index(base),
                "Resolution PR must target a later configured version")
        require(branch not in result, "Duplicate Resolution-PR target")
        result[branch] = resolution_number(value, policy["repository"])
    return result


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
    """Bot review exception is PR-only; quality, PR use, and history remain mandatory."""
    require(policy["bot_user_id"] > 0, "Configure bot_user_id before generating rulesets")
    targets = ["refs/heads/" + b for b in policy["branches"]]
    bot = [{"actor_id": policy["bot_user_id"], "actor_type": "User", "bypass_mode": "always"}]
    bot_pr = [dict(bot[0], bypass_mode="pull_request")]

    def rule(name, rules, bypass=None, target="branch", include=None):
        return {"name": name, "target": target, "enforcement": "active",
                "conditions": {"ref_name": {"include": include or targets, "exclude": []}},
                "bypass_actors": bypass or [], "rules": rules}

    update = {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}
    history = [{"type": "non_fast_forward"}, {"type": "deletion"}]
    return [
        rule("CE merge executor", [update], bot_pr),
        rule("CE pull requests", [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False, "require_last_push_approval": False,
            "required_review_thread_resolution": True, "allowed_merge_methods": ["merge", "squash"]}}]),
        rule("CE reviewed changes", [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": False, "require_last_push_approval": True,
            "required_review_thread_resolution": True, "allowed_merge_methods": ["merge", "squash"]}}], bot_pr),
        rule("CE merge quality", [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [{"context": "Merge check"}]}}] + history),
        rule("CE branch lifecycle", [{"type": "creation"}]),
        rule("CE immutable tags", [update, {"type": "deletion"}], target="tag", include=["~ALL"]),
        rule("CE bot staging and state", [{"type": "creation"}, update], bot,
             include=["refs/heads/merge/**/*", "refs/heads/merge-tests/**/*", "refs/heads/" + policy["state_branch"]]),
        rule("CE bot history", history,
             include=["refs/heads/merge/**/*", "refs/heads/merge-tests/**/*", "refs/heads/" + policy["state_branch"]]),
    ]
