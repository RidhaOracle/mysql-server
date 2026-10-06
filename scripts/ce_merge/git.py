# Copyright (c) 2026, Oracle and/or its affiliates.
"""Inspect public Git objects without checking out or executing contributor code."""
import base64
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .policy import Blocked, check_content, require, sha


class MergeConflict(Blocked):
    """A content conflict, distinct from a missing object or failed Git command."""


class PublicGraph:
    def __init__(self, policy, remote=None):
        self.policy = policy
        self.remote = remote or "https://github.com/" + policy["repository"] + ".git"
        cache = os.environ.get("CE_GIT_CACHE") if remote is None else None
        if cache:
            require(Path(cache).is_absolute(), "Git cache must use an absolute private filesystem path")
            Path(cache).mkdir(parents=True, exist_ok=True)
            marker = Path(cache) / "ce-cache-repository"
            if (Path(cache) / "HEAD").exists():
                require(marker.is_file() and marker.read_text() == policy["repository"],
                        "Cache is not dedicated to this public CE repository")
            else:
                marker.write_text(policy["repository"])
        self.temp = None if cache else tempfile.TemporaryDirectory(prefix="ce-graph-")
        self.path = cache or self.temp.name
        self.run("init", "--bare", ".")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.temp:
            self.temp.cleanup()

    def run(self, *args, input=None, token=None, author=None):
        fetching = args[0] == "fetch"
        timeout = 600
        if fetching:
            try:
                timeout = int(os.environ.get("CE_GIT_FETCH_TIMEOUT", "3600"))
            except ValueError:
                raise Blocked("CE_GIT_FETCH_TIMEOUT must be a positive integer in seconds") from None
            require(timeout > 0, "CE_GIT_FETCH_TIMEOUT must be a positive integer in seconds")
            logging.info("Public Git fetch started (timeout: %s seconds)", timeout)
        started = time.monotonic()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="CE Merge Coordinator",
                   GIT_AUTHOR_EMAIL="ce-merge@localhost", GIT_COMMITTER_NAME="CE Merge Coordinator",
                   GIT_COMMITTER_EMAIL="ce-merge@localhost", GIT_NO_REPLACE_OBJECTS="1")
        if author:
            env.update(GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1])
        if token:
            credentials = base64.b64encode(("x-access-token:" + token).encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credentials)
        # Terminate helpers as well as Git on timeout/interruption. Otherwise
        # inherited pipes can keep communicate() waiting past the deadline.
        with subprocess.Popen(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.path,
                              env=env, stdin=subprocess.PIPE if input is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True) as process:
            try:
                stdout, _ = process.communicate(input, timeout=timeout)
            except BaseException as error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                if fetching and isinstance(error, subprocess.TimeoutExpired):
                    message = (f"Public Git fetch timed out after {timeout} seconds; "
                               "check connectivity or increase CE_GIT_FETCH_TIMEOUT")
                    logging.warning(message)
                    raise Blocked(message) from None
                raise
        if process.returncode:
            if args[0] == "merge-tree" and process.returncode == 1:
                raise MergeConflict("Content conflict requires a reviewed resolution")
            if fetching:
                logging.warning("Public Git fetch failed (exit code: %s)", process.returncode)
            raise Blocked("Git validation failed; inspect the operation privately")
        if fetching:
            logging.info("Public Git fetch completed in %.1f seconds", time.monotonic() - started)
        return stdout.decode("utf-8", errors="strict").strip()

    def fetch(self, refs):
        self.run("fetch", "--no-tags", self.remote, *refs)

    def ancestor(self, lower, higher):
        try:
            self.run("merge-base", "--is-ancestor", sha(lower), sha(higher))
            return True
        except Blocked:
            return False

    def tree(self, commit):
        return self.run("rev-parse", sha(commit) + "^{tree}")

    def parents(self, commit):
        return self.run("show", "-s", "--format=%P", sha(commit)).split()

    def paths(self, base, head):
        value = self.run("diff", "--name-only", "--no-renames", "-z", sha(base), sha(head))
        return [p for p in value.split("\0") if p]

    def inspect(self, base, head):
        self.run("merge-base", sha(base), sha(head))  # Reject unrelated history.
        commits = self.run("rev-list", sha(head), "--not", sha(base)).splitlines()
        require(len(commits) <= 10000, "Candidate history exceeds inspection limit")
        paths = self.paths(base, head)
        for commit in commits:
            changed = self.run("diff-tree", "--root", "-m", "--no-commit-id", "--name-only",
                               "--no-renames", "-r", "-z", sha(commit)).split("\0")
            message = self.run("show", "-s", "--format=%B%n%an%n%ae%n%cn%n%ce", sha(commit))
            check_content(changed, message, self.policy)
        check_content(paths, "", self.policy)
        return paths, commits

    def merge_tree(self, base, head):
        result = self.run("merge-tree", "--write-tree", sha(base), sha(head))
        return sha(result.splitlines()[0])

    def commit(self, tree, parents, message="CE candidate", author=None):
        args = ["commit-tree", sha(tree)]
        for parent in parents:
            args.extend(["-p", sha(parent)])
        return self.run(*args, input=(message + "\n").encode(), author=author)

    def candidates(self, steps, head, operation):
        previous = head
        for index, step in enumerate(steps):
            base = step["base_sha"]
            step["input_head"] = previous
            # Inspect original history even when the resulting commit is squashed.
            self.inspect(base, previous)
            tree = self.tree(base) if step["null"] else self.merge_tree(base, previous)
            squash = step.get("squash", index == 0)
            step["parents"] = [base] if squash else [base, previous]
            step["tree"] = tree
            author = self.run("show", "-s", "--format=%an%n%ae", previous).splitlines() if squash else None
            authors = self.run("log", "--format=Co-authored-by: %an <%ae>", previous, "--not", base).splitlines() if squash else []
            step["after"] = self.commit(tree, step["parents"],
                (step.get("message", "") + "\n\n" if step.get("message") else "") +
                f'CE integration {operation} into {step["branch"]}' +
                (" (reviewed ancestry-only propagation)" if step["null"] else "") +
                ("\n\n" + "\n".join(dict.fromkeys(authors)) if squash else ""), author=author)
            previous = step["after"]
            self.inspect(base, previous)
        return steps

    def receipt(self, operation, steps, actor):
        name = "ce-integration/" + operation
        payload = {"operation": operation, "authorized_by": actor,
                   "branches": [{"branch": s["branch"], "before": s["base_sha"],
                                 "after": s["after"], "pr": s["pr"]} for s in steps]}
        raw = (f'object {steps[-1]["after"]}\ntype commit\ntag {name}\n'
               'tagger CE Merge Coordinator <ce-merge@localhost> 0 +0000\n\n' +
               json.dumps(payload, sort_keys=True) + "\n")
        oid = self.run("hash-object", "-t", "tag", "-w", "--stdin", input=raw.encode())
        return {"ref": "refs/tags/" + name, "sha": oid, "raw": raw}

    def remote_refs(self, refs):
        return {line.split()[1]: line.split()[0] for line in
                self.run("ls-remote", "--refs", self.remote, *refs).splitlines()}

    def atomic_publish(self, steps, receipt, token=None):
        """No fallback, no individual ref retry, and no non-fast-forward updates."""
        require(steps and len({s["branch"] for s in steps}) == len(steps), "Invalid publication targets")
        require(receipt["ref"].startswith("refs/tags/ce-integration/"), "Invalid receipt namespace")
        current = self.remote_refs(["refs/heads/" + s["branch"] for s in steps] + [receipt["ref"]])
        require(receipt["ref"] not in current and all(
            current.get("refs/heads/" + s["branch"]) == s["base_sha"] for s in steps),
            "Publication snapshot changed; reconcile before retrying")
        leases, refs = [], []
        for step in steps:
            ref = "refs/heads/" + step["branch"]
            self.run("check-ref-format", ref)
            require(step["base_sha"] != step["after"] and
                    self.ancestor(step["base_sha"], step["after"]), "Publication must advance every branch without rewriting")
            leases.append("--force-with-lease=" + ref + ":" + sha(step["base_sha"]))
            refs.append(sha(step["after"]) + ":" + ref)
        self.run("check-ref-format", receipt["ref"])
        require(self.run("hash-object", "-t", "tag", "-w", "--stdin", input=receipt["raw"].encode()) == receipt["sha"],
                "Receipt object changed")
        leases.append("--force-with-lease=" + receipt["ref"] + ":")
        refs.append(sha(receipt["sha"]) + ":" + receipt["ref"])
        self.run("push", "--atomic", *leases, self.remote, *refs, token=token)

    def export_candidates(self, steps, path):
        refs = []
        for i, step in enumerate(steps):
            ref = f"refs/ce-candidates/{i}"
            self.run("update-ref", ref, step["after"])
            refs.append(ref)
        temporary = str(path) + ".tmp"
        self.run("bundle", "create", temporary, *refs,
                 *["^" + step["base_sha"] for step in steps])
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(Path(path).parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def restore_candidates(self, steps, path):
        self.fetch([step["base_sha"] for step in steps])
        self.run("fetch", "--no-tags", str(path),
                 *[f"refs/ce-candidates/{i}" for i in range(len(steps))])
