# Copyright (c) 2026, Oracle and/or its affiliates.
"""Small token-authenticated GitHub client. Never retries a write with an uncertain outcome."""
import hashlib
import http.client
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .policy import Blocked, require, sha


def authorization_identity(pr):
    """Bind an integration request to a PR revision, target, and propagation instructions."""
    digest = hashlib.sha256(json.dumps([pr["base"]["ref"], pr["head"]["sha"],
                                       pr.get("body") or ""]).encode()).hexdigest()
    return f'{pr["number"]}:{digest}'


def integration_body(operation, original, detail=''):
    return (f'Original PR: #{original}\nMerge operation: {operation}\n\n'
            'Version testing passed before this integration PR was opened. '
            'The bot revalidates reviewed source/resolution content and test evidence before merging. '
            'Separate approval of this generated PR is not required by the configured bot policy.\n\n' + detail)


class GitHub:
    def __init__(self, policy):
        self.policy = policy

    def token(self):
        token = os.environ.get("CE_MERGE_TOKEN", "")
        require(token.strip(), "CE_MERGE_TOKEN is required")
        return token

    def verify_identity(self):
        identity = self.request("GET", "/user")
        require(self.policy.get("bot_user_id", 0) > 0 and
                identity["id"] == self.policy["bot_user_id"],
                "CE_MERGE_TOKEN must belong to the configured automation account")

    def request(self, method, path, data=None):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        req = urllib.request.Request("https://api.github.com" + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization": "Bearer " + self.token(),
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mysql-ce-merge"})
        attempts = 3 if method in ("GET", "HEAD") else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as error:
                # Never put API response bodies or credentials into diagnostics.
                code = error.code
                error.close()
                if attempts == 1 or code not in (502, 503, 504):
                    raise Blocked(f"GitHub API returned HTTP {code}") from None
            except (urllib.error.URLError, http.client.RemoteDisconnected,
                    http.client.IncompleteRead, ConnectionError, TimeoutError):
                if attempts == 1:
                    # A write may already have succeeded. Let its caller reconcile
                    # recorded intent instead of blindly submitting it again.
                    raise ConnectionError("GitHub API connection failed; request outcome may be unknown") from None
            if attempt + 1 == attempts:
                raise Blocked("GitHub API read unavailable after 3 attempts; will reevaluate on the next poll") from None
            delay = attempt + 1
            logging.warning("GitHub API read interrupted; retrying in %s second(s), attempt %s/%s",
                            delay, attempt + 2, attempts)
            time.sleep(delay)

    def repo(self, suffix, method="GET", data=None):
        return self.request(method, "/repos/" + self.policy["repository"] + suffix, data)

    def pages(self, suffix, key=None):
        result = []
        for page in range(1, 1001):
            response = self.repo(suffix + ("&" if "?" in suffix else "?") +
                                 f"per_page=100&page={page}")
            items = response[key] if key else response
            result.extend(items)
            if len(items) < 100:
                return result
        raise Blocked("API pagination limit exceeded")

    def pull(self, number):
        pr = self.repo(f"/pulls/{int(number)}")
        # The PR payload can retain its original base SHA after the target moves.
        # Use the current branch tip; candidate-parent checks still reject races.
        pr["base"]["sha"] = sha(self.branch(pr["base"]["ref"]))
        return pr

    def branch(self, branch):
        return self.repo("/branches/" + urllib.parse.quote(branch, safe=""))["commit"]["sha"]

    def can_integrate(self, login):
        return login in self.policy["maintainers"] and self.repo(
            "/collaborators/" + urllib.parse.quote(login, safe="") + "/permission")["permission"] in (
                "admin", "maintain", "write")

    def reviewed(self, pr, require_approval=True):
        """Source/resolution reviews are exact-head; generated PRs still honor objections."""
        head = sha(pr['head']['sha'])
        owner, name = self.policy["repository"].split("/")
        cursor = None
        while True:
            threads = self.request("POST", "/graphql", {
                "query": "query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}",
                "variables": {"owner": owner, "name": name, "number": pr["number"], "after": cursor}})
            require(not threads.get("errors"), "Review conversations unavailable")
            pull = threads["data"]["repository"]["pullRequest"]
            value = pull["reviewThreads"]
            require(all(t["isResolved"] for t in value["nodes"]), "Unresolved review conversations")
            if not value["pageInfo"]["hasNextPage"]:
                break
            cursor = value["pageInfo"]["endCursor"]
        latest = {}
        for review in self.pages(f'/pulls/{pr["number"]}/reviews'):
            if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[review["user"]["login"]] = review
        approved = False
        for login, review in latest.items():
            if review["state"] not in ("APPROVED", "CHANGES_REQUESTED"):
                continue
            access = self.repo("/collaborators/" + urllib.parse.quote(login, safe="") + "/permission")
            # Personal-repository collaborators have the Write role.
            if access.get("role_name") not in ("write", "maintain", "admin"):
                continue
            require(review["state"] != "CHANGES_REQUESTED", "A reviewer with Write, Maintain, or Admin access has requested changes")
            approved |= review["commit_id"] == head
        require(not require_approval or approved,
                "Current-head approval from a collaborator with Write, Maintain, or Admin access is missing")

    def oca(self, pr):
        label = self.policy["oca_label"]
        require(any(x["name"] == label for x in pr["labels"]), "OCA verification is missing")
        events = [e for e in self.pages(f'/issues/{pr["number"]}/events')
                  if e["event"] in ("labeled", "unlabeled") and e.get("label", {}).get("name") == label]
        require(events and events[-1]["event"] == "labeled"
                and self.can_integrate(events[-1]["actor"]["login"]),
                "OCA label lacks trusted verification provenance")

    def status(self, pr, name, conclusion, summary, candidate=None):
        state = {"success": "success", "neutral": "pending", "action_required": "error"}.get(conclusion, conclusion)
        head = sha(candidate or pr["head"]["sha"])
        description = summary.splitlines()[0][:140]
        # Avoid exhausting GitHub's per-context status limit while awaiting review.
        statuses = self.pages(f"/commits/{head}/statuses")
        previous = next((s for s in statuses if s["context"] == name and
                         s.get("creator", {}).get("id") == self.policy["bot_user_id"]), None)
        if previous and previous["state"] == state and previous.get("description") == description:
            return previous
        payload = {"context": name, "state": state, "description": description}
        run_id = os.environ.get("GITHUB_RUN_ID")
        if run_id:
            payload["target_url"] = f'https://github.com/{self.policy["repository"]}/actions/runs/{int(run_id)}'
        return self.repo(f"/statuses/{head}", "POST", payload)

    def comment(self, pr, key, body):
        marker = f"<!-- ce-merge:{key} -->"
        text = marker + "\n" + body
        comments = self.pages(f'/issues/{pr["number"]}/comments')
        previous = next((c for c in comments if c.get("user", {}).get("id") == self.policy["bot_user_id"]
                         and c.get("body", "").startswith(marker + "\n")), None)
        if previous:
            if previous["body"] != text:
                self.repo(f'/issues/comments/{previous["id"]}', "PATCH", {"body": text})
        else:
            self.repo(f'/issues/{pr["number"]}/comments', "POST", {"body": text})

    @staticmethod
    def summary(text):
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a") as stream:
                stream.write(text + "\n\n")

    def advisory_label(self, pr, ready):
        present = any(label["name"] == "Integrate" for label in pr.get("labels", []))
        if ready and not present:
            self.repo(f'/issues/{pr["number"]}/labels', "POST", {"labels": ["Integrate"]})
        elif present and not ready:
            self.repo(f'/issues/{pr["number"]}/labels/Integrate', "DELETE")

    def close_pr(self, pr):
        if pr["state"] == "open":
            self.repo(f'/pulls/{pr["number"]}', "PATCH", {"state": "closed"})

    def merge_pr(self, number, head, method, message, generated=False):
        """One attempt only. The journal reconciles GitHub's merged state afterward."""
        require(method in ("squash", "merge"), "Unsupported merge method")
        require(not generated or method == 'merge', 'Review exception is only for generated merge PRs')
        env = dict(os.environ, GH_TOKEN=self.token(), GH_HOST="github.com",
                   GH_PROMPT_DISABLED="1")
        # --admin suppresses gh's local BLOCKED-state refusal for the configured
        # PR-only review exception. It grants no server permissions: quality and
        # history rules have no bot exception. Never use it for the source PR.
        subject, _, body = message.partition('\n')
        try:
            result = subprocess.run(
                ["gh", "pr", "merge", str(int(number)), "--repo", self.policy["repository"],
                 "--" + method, *(['--admin'] if generated else []), "--match-head-commit", sha(head),
                 '--subject', subject, '--body-file', '-'],
                env=env, input=body.lstrip('\n').encode(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked("GitHub merge interrupted; reconcile before retrying") from None
        require(result.returncode == 0, "GitHub merge was not confirmed; reconcile before retrying")

    def bot_pr(self, operation, index, base, original, detail=""):
        """Deterministic names and lookup make retry after create-PR failure safe."""
        branch = f"merge/{operation}/{index}"
        owner = self.policy["repository"].split("/")[0]
        pulls = self.repo("/pulls?state=all&head=" + urllib.parse.quote(owner + ":" + branch, safe=""))
        require(len(pulls) <= 1, "Ambiguous generated PR")
        if pulls:
            return pulls[0]["number"]
        return self.repo("/pulls", "POST", {
            "title": f"Merge #{original} into {base}", "head": branch,
            "base": base, "body": integration_body(operation, original, detail)
        })["number"]
