# Copyright (c) 2026, Oracle and/or its affiliates.
"""Controlled publication of an already validated, CE-parented SC bundle."""
import base64
import hashlib
import os
import subprocess
import urllib.parse

from .git import PublicGraph
from .policy import require, sha, staging_repository


def public_release(github, manifest):
    release = github.repo(f'/releases/{int(manifest["release_id"])}')
    require(not release["draft"] and release.get("published_at") and
            release["tag_name"] == manifest["release_tag"], "Corresponding release is not public")


def validate_source(graph, manifest, head, commits):
    """Validate the approved CE source for both imported PRs and bundle publication."""
    require(manifest.get("expected_tree"), "Promotion requires a validated expected_tree")
    expected_tree = sha(manifest["expected_tree"])
    require(head == sha(manifest["head"]), "Promotion head differs from manifest")
    require(manifest["commits"] and len(set(manifest["commits"])) == len(manifest["commits"]) and
            set(commits) == set(manifest["commits"]), "Promotion ancestry differs from approved SC set")
    previous = sha(manifest["base_sha"])
    for commit in manifest["commits"]:
        require(graph.parents(sha(commit)) == [previous], "Promotion contains unapproved ancestry")
        previous = commit
    require(previous == head, "Promotion head differs from approved SC list")
    require(graph.tree(head) == expected_tree, "Promotion differs from validated source tree")


def publish(policy, store, github, manifest):
    require(policy["mode"] == "active", "Shadow mode cannot publish security code")
    public_release(github, manifest)  # Before any public write, including a staging ref.
    require(not store.uncertain(), "Reconcile uncertain CE publication first")
    hold = store.setting("hold")
    require(hold and hold.get("release_tag") == manifest["release_tag"], "Matching promotion hold is required")
    require(manifest["base"] in hold.get("targets", []), "Promotion target is outside the declared batch")
    require(manifest["security_approval"] and manifest["release_approval"] and manifest["validation_record"]
            and manifest["security_approval"] != manifest["release_approval"], "Missing independent promotion signoffs")
    require(manifest["base"] in policy["branches"] + policy.get("release_branches", []), "Unconfigured promotion target")
    head, base = sha(manifest["head"]), sha(manifest["base_sha"])
    require(manifest["commits"] and len(set(manifest["commits"])) == len(manifest["commits"]), "Invalid SC list")
    store.check_promotion_replacement(manifest)
    operation = hashlib.sha256((manifest["release_tag"] + manifest["base"] + head).encode()).hexdigest()
    branch = "promotion/" + operation
    with PublicGraph(policy) as graph:
        graph.fetch(["refs/heads/" + manifest["base"]])
        # Fetch exactly one approved ref. No private remote, mirror push, or source clone.
        require(os.path.isabs(manifest["bundle"]), "Use an absolute approved-bundle path")
        require(manifest["bundle_ref"].startswith("refs/heads/ce-promotion-"), "Invalid approved bundle ref")
        graph.run("fetch", "--no-tags", manifest["bundle"], manifest["bundle_ref"])
        require(graph.run("rev-parse", "FETCH_HEAD") == head, "Bundle head differs from manifest")
        _, commits = graph.inspect(base, head)
        validate_source(graph, manifest, head, commits)
        # Token exists only in the child environment, never in argv, Git config, or logs.
        credentials = base64.b64encode(("x-access-token:" + github.token(fork=True)).encode()).decode()
        env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credentials,
                   GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        store.audit(operation, "promotion-publish-intent", {"head": head, "release": manifest["release_tag"]})
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "push",
                                 "https://github.com/" + staging_repository(policy) + ".git",
                                 head + ":refs/heads/" + branch], cwd=graph.path, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        require(result.returncode == 0, "Publication outcome requires inspection; deterministic ref is safe to retry")
    staging = staging_repository(policy)
    owner = staging.split("/")[0]
    pr_head = branch if staging == policy["repository"] else owner + ":" + branch
    pulls = github.repo("/pulls?state=all&head=" + urllib.parse.quote(owner + ":" + branch, safe=""))
    require(len(pulls) <= 1, "Ambiguous promotion PR")
    pr = pulls[0] if pulls else github.repo("/pulls", "POST", {
        "title": "Released security code for " + manifest["release_tag"],
        "head": pr_head, "base": manifest["base"],
        "body": "Approved code-only promotion after public release. Independent review and CE validation are required."})
    manifest = dict(manifest, pr=pr["number"])
    require(pr["state"] == "open" and not pr.get("draft") and
            pr["base"]["ref"] == manifest["base"] and pr["head"]["sha"] == head,
            "Promotion PR must be open, ready, and match the approved manifest")
    store.activate_promotion(manifest, str(os.getuid()))
    store.audit(operation, "promotion-pr-created", {"pr": pr["number"], "head": head})
    return pr["number"]
