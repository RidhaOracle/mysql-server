# Copyright (c) 2026, Oracle and/or its affiliates.
import copy
from pathlib import Path
import subprocess
from scripts.ce_merge.policy import Blocked, require
from scripts.ce_merge.github import integration_body

POLICY = {
    "repository": "example/ce", "branches": ["lts", "trunk"],
    "maintainers": ["owner"], "bot_user_id": 42,
    "oca_label": "OCA Verified", "forbidden_paths": ["internal", "internal-sec"],
    "forbidden_metadata_patterns": ["PRIVATE-123"],
}


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.git("init", "-b", "lts")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        # Model a version baseline with its trusted ordinary CI configuration.
        workflows = self.path / '.github/workflows'
        workflows.mkdir(parents=True)
        root = Path(__file__).resolve().parents[3]
        for name in ('pr-build.yml', 'mtr.yml', 'clang-format.yml'):
            (workflows / name).write_text((root / '.github/workflows' / name).read_text())
        self.git('add', '.github/workflows')
        self.base = self.commit("code.txt", "base\n")
        self.git("branch", "trunk")

    def git(self, *args, input=None):
        return subprocess.check_output(["git", "-C", str(self.path), *args], input=input,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, path, content):
        file = self.path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.git("add", path)
        self.git("commit", "-m", "Public change")
        return self.git("rev-parse", "HEAD")

    def merge_commit(self, base, head, squash=False):
        tree = self.git("merge-tree", "--write-tree", base, head).splitlines()[0]
        args = ["commit-tree", tree, "-p", base]
        if not squash:
            args += ["-p", head]
        return self.git(*args, input=b"Public merge\n")


class FakeGitHub:
    def __init__(self, case):
        self.case = case
        self.approved = True
        self.oca_ok = True
        self.authorized = True
        self.changed_rules = False
        self.missing_review = set()
        self.requested_changes = set()
        self.checks = []
        self.merges = []
        self.reject = set()
        self.pulls = {}
        self.bot_pulls = {}
        self.add_pull(1, case.head, "lts", "contributor/ce")

    def add_pull(self, number, head, branch, repository):
        self.pulls[number] = {"number": number, "state": "open", "draft": False, "body": "Public fix",
            "user": {"id": 9, "login": "contributor"}, "head": {"sha": head, "repo": {"full_name": repository}},
            "base": {"sha": self.branch(branch), "ref": branch, "repo": {"full_name": "example/ce"}}}

    def pull(self, number):
        pr = copy.deepcopy(self.pulls[number])
        pr["base"]["sha"] = self.branch(pr["base"]["ref"])
        return pr

    def branch(self, branch):
        return self.case.remote_git("rev-parse", "refs/heads/" + branch)

    def token(self):
        return None

    def can_integrate(self, actor):
        return actor == "owner" and self.authorized

    def reviewed(self, pr, require_approval=True):
        if pr["number"] in self.requested_changes:
            raise Blocked("A reviewer requested changes")
        if require_approval and (not self.approved or pr["number"] in self.missing_review):
            raise Blocked("Missing current maintainer review")

    def oca(self, pr):
        if not self.oca_ok:
            raise Blocked("OCA missing")

    def status(self, *args, **kwargs):
        self.checks.append((args, kwargs))
        return {"id": len(self.checks), "created_at": "2026-10-01T10:00:00Z"}

    def comment(self, *args):
        pass

    def summary(self, *args):
        pass

    def close_pr(self, pr):
        self.pulls[pr["number"]]["state"] = "closed"

    def advisory_label(self, *args):
        pass

    def bot_pr(self, operation, branch, base, original, detail=""):
        head = self.branch(branch)
        key = (operation, branch, base)
        if key not in self.bot_pulls:
            number = max(self.pulls) + 1
            self.add_pull(number, head, base, "example/ce")
            self.pulls[number]['body'] = integration_body(operation, original, detail)
            self.bot_pulls[key] = number
        number = self.bot_pulls[key]
        self.case.remote_git('update-ref', f'refs/pull/{number}/head', head)
        self.pulls[number]['head'].update(sha=head, ref=branch)
        self.pulls[number]['user'] = {'id': 42, 'login': 'merge-bot'}
        return number

    def merge_pr(self, number, head, method, message, generated=False):
        pr = self.pulls[number]
        require(pr['head']['sha'] == head and not pr.get('merged'), 'Wrong merge head')
        if number in self.reject:
            raise Blocked('GitHub rejected merge')
        base = self.branch(pr['base']['ref'])
        self.case.repo.git('fetch', str(self.case.remote), base, head)
        actual = self.case.repo.merge_commit(base, head, squash=method == 'squash')
        self.case.repo.git('push', str(self.case.remote), actual + ':refs/heads/' + pr['base']['ref'])
        pr.update(state='closed', merged=True, merge_commit_sha=actual)
        self.merges.append((number, method))
        if self.case.lose_response:
            raise TimeoutError('Simulated lost successful merge response')
