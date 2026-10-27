# Copyright (c) 2026, Oracle and/or its affiliates.
"""Protected Git checkpoints for disposable Actions runners. Never executes state files."""
import base64
import os
from pathlib import Path
import re
import subprocess

from .journal import Journal, PersistenceError
from .policy import require


class GitJournal(Journal):
    def __init__(self, checkout, policy, token=None, initialize=False, remote=None):
        self.checkout = Path(checkout).resolve()
        self.checkout.mkdir(parents=True, exist_ok=True)
        require(not any(self.checkout.iterdir()), "Restore state into an empty directory")
        self.remote = remote or "https://github.com/" + policy["repository"] + ".git"
        self.ref = "refs/heads/" + policy["state_branch"]
        self.credential = token
        self.failed = False
        self.run("init", "-b", policy["state_branch"])
        self.run("config", "user.name", "CE Merge Coordinator")
        self.run("config", "user.email", "ce-merge@localhost")
        self.head = self.remote_head()
        if self.head:
            self.run("fetch", "--no-tags", "--depth=1", self.remote, self.ref)
            require(self.run("rev-parse", "FETCH_HEAD") == self.head,
                    "State changed during restore; start a fresh run")
            # Do not check out symlinks, executables, submodules, or arbitrary paths.
            for entry in self.run("ls-tree", "-rz", "FETCH_HEAD").split("\0"):
                if not entry:
                    continue
                metadata, name = entry.split("\t", 1)
                require(metadata.startswith("100644 blob ") and self.allowed(name),
                        "Unexpected file in public state branch")
            self.run("checkout", "-B", policy["state_branch"], self.head)
        else:
            require(initialize, "State branch is missing; run init-state explicitly")
        super().__init__(self.checkout / "journal")
        expected = {"version": 1, "repository": policy["repository"]}
        if self.head:
            require(self.setting("format") == expected, "State belongs to another repository or schema")
        else:
            self.set_setting("format", expected, "init-state")

    @staticmethod
    def allowed(name):
        return bool(re.fullmatch(
            r"journal/(?:audit\.jsonl|operations/[0-9a-f]{32}\.json|"
            r"settings/[0-9a-f]{64}\.json|bundles/[0-9a-f]{32}(?:-[0-9]+)?\.bundle)", name))

    def run(self, *args, input=None):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1")
        if self.credential:
            credentials = base64.b64encode(("x-access-token:" + self.credential).encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credentials)
        try:
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                    cwd=self.checkout, env=env, input=input,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            raise PersistenceError("State Git operation interrupted; restore state on a fresh runner") from None
        if result.returncode:
            raise PersistenceError("State Git operation failed; restore state on a fresh runner")
        return result.stdout.decode().strip()

    def remote_head(self):
        lines = self.run("ls-remote", "--refs", self.remote, self.ref).splitlines()
        return lines[0].split()[0] if lines else None

    def ensure_healthy(self):
        if self.failed:
            raise PersistenceError("A state checkpoint failed; all further remote writes are disabled")

    def checkpoint(self):
        self.ensure_healthy()
        try:
            if self.remote_head() != self.head:
                raise PersistenceError("Another writer changed state; restart from the authoritative branch")
            files = [str(p.relative_to(self.checkout)) for p in self.path.rglob("*")
                     if p.is_file() and self.allowed(str(p.relative_to(self.checkout)))]
            require(files, "No public journal files to checkpoint")
            self.run("add", "--", *files)
            tree = self.run("write-tree")
            if self.head and tree == self.run("rev-parse", self.head + "^{tree}"):
                return
            parents = ["-p", self.head] if self.head else []
            commit = self.run("commit-tree", tree, *parents, input=b"CE merger checkpoint\n")
            self.run("push", "--force-with-lease=" + self.ref + ":" + (self.head or ""),
                     self.remote, commit + ":" + self.ref)
            # A lost push response raises above. Never continue from unconfirmed state.
            self.head = commit
            self.run("update-ref", "HEAD", commit)
        except Exception as error:
            self.failed = True
            if isinstance(error, PersistenceError):
                raise
            raise PersistenceError("State checkpoint failed; restore state on a fresh runner") from None

    def write(self, path, value):
        self.ensure_healthy()
        try:
            super().write(path, value)
        except Exception:
            self.failed = True
            raise PersistenceError("Local state write failed; restore state on a fresh runner") from None

    def audit(self, operation, event, data):
        self.ensure_healthy()
        try:
            super().audit(operation, event, data)
            self.checkpoint()
        except PersistenceError:
            raise
        except Exception:
            self.failed = True
            raise PersistenceError("State audit checkpoint failed; restore state on a fresh runner") from None
