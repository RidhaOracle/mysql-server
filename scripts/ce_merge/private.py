# Copyright (c) 2026, Oracle and/or its affiliates.
"""Private-host staging tools. Never run this module in public GitHub Actions.

Usage: python3 -m scripts.ce_merge.private SPEC.json --state-dir /private/ce-journal [--apply]
Specifications and resulting ledgers must remain private. Source/target repositories
are operator-managed local clones; only explicit configured target remotes are pushed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .policy import Blocked, check_content, require, sha
from .journal import Journal


def git(path, *args):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="MySQL Integration", GIT_AUTHOR_EMAIL="integration@localhost",
               GIT_COMMITTER_NAME="MySQL Integration", GIT_COMMITTER_EMAIL="integration@localhost")
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=600)
    require(result.returncode == 0, "Private Git operation failed; retained workspace requires inspection")
    return result.stdout.decode().strip()


def local_repository(value):
    path = Path(value)
    require(path.is_absolute() and path.exists(), "Use an absolute path to a trusted local clone")
    return str(path)


def validate(path, commands):
    require(commands and all(isinstance(c, list) and c and all(isinstance(a, str) for a in c)
                             for c in commands), "Explicit validation commands are required")
    # Deliberately no shell, public output, artifacts upload, or public-service credentials.
    for index, command in enumerate(commands):
        with open(path / f"validation-{index}.log", "wb") as log:
            result = subprocess.run(command, cwd=path, stdout=log, stderr=subprocess.STDOUT)
        require(result.returncode == 0, "Private validation failed; inspect retained private logs")


def stage(spec, workspace, policy):
    kind = spec["kind"]
    require(kind in ("ce-sync", "security-pick", "prepare-promotion"), "Unknown private operation")
    source = local_repository(spec["source"])
    targets = spec["targets"]
    require(targets, "At least one private target is required")
    results = []
    for index, target in enumerate(targets):
        repository = local_repository(target["repository"])
        path = workspace / str(index)
        require(not path.exists(), "Staging workspace exists; inspect before retrying failed preparation")
        path.mkdir(parents=True)
        git(path, "clone", "--no-hardlinks", "--no-checkout", repository, ".")
        git(path, "fetch", "--no-tags", source, *[sha(b["source_sha"]) for b in target["branches"]]
            if kind == "ce-sync" else [sha(p["sha"]) for p in target["picks"]])
        branches = []
        for branch in target["branches"]:
            name = branch["name"]
            git(path, "check-ref-format", "refs/heads/" + name)
            before = sha(branch["before"])
            require(git(path, "rev-parse", "refs/remotes/origin/" + name) == before,
                    "Private target differs from approved snapshot")
            git(path, "checkout", "--detach", before)
            ledger = []
            if kind == "ce-sync":
                source_sha = sha(branch["source_sha"])
                try:
                    git(path, "merge-base", "--is-ancestor", source_sha, "HEAD")
                except Blocked:
                    # No unrelated-history or automatic conflict strategy on routine sync.
                    git(path, "merge", "--no-ff", "--no-edit", source_sha)
                git(path, "merge-base", "--is-ancestor", source_sha, "HEAD")
                if branches:
                    git(path, "merge", "--no-ff", "--no-edit", branches[-1]["after"])
            else:
                require(target["picks"], "No security commits selected")
                for pick in target["picks"]:
                    require(pick["kind"] in ("SC", "ST"), "Unknown security item kind")
                    require(kind != "prepare-promotion" or pick["kind"] == "SC",
                            "Private tests cannot be promoted")
                    commit = sha(pick["sha"])
                    parents = git(path, "show", "-s", "--format=%P", commit).split()
                    require(len(parents) == 1, "Security picks must be individual non-merge commits")
                    changed = git(path, "diff", "--name-only", "--no-renames", "-z", parents[0], commit).split("\0")
                    changed = [p for p in changed if p]
                    require(changed, "Empty security change requires ledger reconciliation")
                    if pick["kind"] == "ST":
                        require(all(p.startswith("internal-sec/") for p in changed), "ST modifies non-security-test paths")
                    else:
                        check_content(changed, git(path, "show", "-s", "--format=%B", commit), policy)
                    git(path, "cherry-pick", "-x", commit)
                    ledger.append({"item": pick["item"], "kind": pick["kind"], "source": commit,
                                   "result": git(path, "rev-parse", "HEAD")})
            validated_head = git(path, "rev-parse", "HEAD")
            validate(path, target["validate"])
            require(git(path, "rev-parse", "HEAD") == validated_head, "Validation changed the candidate commit")
            git(path, "diff", "--exit-code")
            git(path, "diff", "--cached", "--exit-code")
            after = git(path, "rev-parse", "HEAD")
            result = {"name": name, "before": before, "after": after, "ledger": ledger}
            if kind == "prepare-promotion":
                require(spec.get("security_approval") and spec.get("release_approval"),
                        "Promotion requires security and release approval")
                require(git(path, "rev-parse", "HEAD^{tree}") == sha(branch["expected_public_tree"]),
                        "Prepared CE source differs from validated release source")
                ref = "refs/heads/ce-promotion-" + str(len(branches))
                git(path, "update-ref", ref, after)
                bundle = path.parent / f"{index}-{len(branches)}-approved-sc.bundle"
                git(path, "bundle", "create", str(bundle), ref, "^" + before)
                result.update(bundle=str(bundle), bundle_ref=ref, expected_tree=branch["expected_public_tree"])
            branches.append(result)
        # The local stage can contain private Git objects even when its HEAD is safe.
        # A promotion stage must NEVER be pushed or copied to a public fork.
        results.append({"workspace": str(path), "push_url": target.get("push_url"), "branches": branches})
    return results


def apply_results(results):
    for target in results:
        path, remote = Path(target["workspace"]), target["push_url"]
        require(remote and not remote.startswith("-"), "Missing private target remote")
        names = ["refs/heads/" + b["name"] for b in target["branches"]]
        remote_heads = dict(line.split()[::-1] for line in git(path, "ls-remote", remote, *names).splitlines())
        pending, leases = [], []
        for branch in target["branches"]:
            current = remote_heads.get("refs/heads/" + branch["name"])
            require(current in (branch["before"], branch["after"]), "Private target moved; reconciliation required")
            if current != branch["after"]:
                # Ancestry is mandatory; exact leases reject a concurrently moved target.
                git(path, "merge-base", "--is-ancestor", branch["before"], branch["after"])
                pending.append(branch["after"] + ":refs/heads/" + branch["name"])
                leases.append("--force-with-lease=refs/heads/" + branch["name"] + ":" + branch["before"])
        if pending:
            git(path, "push", "--atomic", *leases, remote, *pending)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--policy", default=".github/ce-merge-policy.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    policy = json.loads(Path(args.policy).read_text())
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    store = Journal(args.state_dir)
    key = "private:" + digest
    with store.exclusive():
        record = store.setting(key)
        if record is None:
            workspace = Path(args.state_dir).resolve() / ("stage-" + digest)
            store.audit(key, "private-prepare-started", {"kind": spec["kind"], "spec_digest": digest})
            try:
                results = stage(spec, workspace, policy)
            except Exception:
                store.audit(key, "private-prepare-failed", {"workspace": str(workspace)})
                raise
            record = {"state": "prepared", "kind": spec["kind"], "results": results}
            store.set_setting(key, record, str(os.getuid()))
        if args.apply:
            require(record["kind"] != "prepare-promotion", "Promotion preparation is private and cannot push")
            store.audit(key, "private-publish-intent", record)
            try:
                apply_results(record["results"])
            except Exception:
                store.audit(key, "private-publish-needs-reconciliation", {})
                raise
            record["state"] = "complete"
            store.set_setting(key, record, str(os.getuid()))
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Blocked as error:
        raise SystemExit(str(error)) from None
