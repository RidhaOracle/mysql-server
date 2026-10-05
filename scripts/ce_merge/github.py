# Copyright (c) 2026, Oracle and/or its affiliates.
"""Small GitHub App client. Never retries a write with an uncertain outcome."""
import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .policy import Blocked, require


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def authorization_identity(pr):
    """Bind the App action to a PR revision, target, and propagation instructions."""
    digest = hashlib.sha256(json.dumps([pr["base"]["ref"], pr["head"]["sha"],
                                       pr.get("body") or ""]).encode()).hexdigest()
    return f'{pr["number"]}:{digest}'


class GitHub:
    def __init__(self, policy):
        self.policy = policy
        self.tokens = {}

    def token(self, fork=False):
        installation = self.policy["fork_installation_id" if fork else "installation_id"]
        cache_key = (installation, self.policy["bot_fork" if fork else "repository"])
        token, expires = self.tokens.get(cache_key, (None, 0))
        if expires > time.time() + 60:
            return token
        now = int(time.time())
        header = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = encode(json.dumps({"iat": now - 60, "exp": now + 540,
                                   "iss": str(self.policy["app_id"])}).encode())
        unsigned = header + "." + claims
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", os.environ["CE_APP_PRIVATE_KEY"]],
            input=unsigned.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        result = self.request("POST", f"/app/installations/{installation}/access_tokens",
                              {"repositories": [self.policy["bot_fork" if fork else "repository"].split("/")[1]]},
                              token=unsigned + "." + encode(signature))
        self.tokens[cache_key] = (result["token"], time.time() + 3000)
        return result["token"]

    def request(self, method, path, data=None, token=None, fork=False):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        req = urllib.request.Request("https://api.github.com" + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization": "Bearer " + (token or self.token(fork)),
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mysql-ce-merge"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            # Never put API response bodies or credentials into public diagnostics.
            raise Blocked(f"GitHub API returned HTTP {error.code}") from None

    def repo(self, suffix, method="GET", data=None, fork=False):
        name = self.policy["bot_fork" if fork else "repository"]
        return self.request(method, "/repos/" + name + suffix, data, fork=fork)

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
        return self.repo(f"/pulls/{int(number)}")

    def branch(self, branch):
        return self.repo("/branches/" + urllib.parse.quote(branch, safe=""))["commit"]["sha"]

    def can_integrate(self, login):
        return login in self.policy["maintainers"] and self.repo(
            "/collaborators/" + urllib.parse.quote(login, safe="") + "/permission")["permission"] in (
                "admin", "maintain", "write")

    def reviewed(self, pr):
        owner, name = self.policy["repository"].split("/")
        cursor = None
        while True:
            threads = self.request("POST", "/graphql", {
                "query": "query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}",
                "variables": {"owner": owner, "name": name, "number": pr["number"], "after": cursor}})
            require(not threads.get("errors"), "Review conversations unavailable")
            value = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
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
            if login.lower() == pr["user"]["login"].lower():
                continue
            access = self.repo("/collaborators/" + urllib.parse.quote(login, safe="") + "/permission")
            # GitHub's legacy permission maps Maintain to Write; use the actual role.
            if access.get("role_name") not in ("maintain", "admin"):
                continue
            require(review["state"] != "CHANGES_REQUESTED", "A repository maintainer has requested changes")
            approved |= review["commit_id"] == pr["head"]["sha"]
        require(approved, "Independent current-head approval from a repository maintainer (Maintain or Admin) is missing")

    def oca(self, pr):
        label = self.policy["oca_label"]
        require(any(x["name"] == label for x in pr["labels"]), "OCA verification is missing")
        events = [e for e in self.pages(f'/issues/{pr["number"]}/events')
                  if e["event"] in ("labeled", "unlabeled") and e.get("label", {}).get("name") == label]
        require(events and events[-1]["event"] == "labeled"
                and self.can_integrate(events[-1]["actor"]["login"]),
                "OCA label lacks trusted verification provenance")

    def check(self, pr, name, conclusion, summary, action=False, candidate=None):
        result = self.repo("/check-runs", "POST", {
            "name": name, "head_sha": candidate or pr["head"]["sha"], "status": "completed",
            "conclusion": conclusion, "external_id": authorization_identity(pr),
            "output": {"title": name, "summary": summary},
            "actions": [{"label": "Integrate", "description": "Authorize this reviewed revision",
                         "identifier": "integrate"}] if action else []})
        return result["id"]

    def advisory_label(self, pr, ready):
        present = any(label["name"] == "Integrate" for label in pr.get("labels", []))
        if ready and not present:
            self.repo(f'/issues/{pr["number"]}/labels', "POST", {"labels": ["Integrate"]})
        elif present and not ready:
            self.repo(f'/issues/{pr["number"]}/labels/Integrate', "DELETE")

    def close_pr(self, pr):
        if pr["state"] == "open":
            self.repo(f'/pulls/{pr["number"]}', "PATCH", {"state": "closed"})

    def bot_pr(self, operation, index, head, base, original, detail=""):
        """Deterministic names and lookup make retry after create-PR failure safe."""
        require(self.policy["mode"] == "active", "Shadow mode cannot create bot branches")
        branch = f"upmerge/{operation}/{index}"
        refs = self.repo("/git/matching-refs/heads/" + branch, fork=True)
        exact = [r for r in refs if r["ref"] == "refs/heads/" + branch]
        if exact:
            require(exact[0]["object"]["sha"] == head, "Bot branch changed unexpectedly")
        else:
            self.repo("/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": head}, fork=True)
        owner = self.policy["bot_fork"].split("/")[0]
        pulls = self.repo("/pulls?state=all&head=" + urllib.parse.quote(owner + ":" + branch, safe=""))
        require(len(pulls) <= 1, "Ambiguous generated PR")
        if pulls:
            return pulls[0]["number"]
        return self.repo("/pulls", "POST", {
            "title": f"Upmerge #{original} into {base}", "head": owner + ":" + branch,
            "base": base, "body": f"Original PR: #{original}\nCE operation: {operation}\n\n"
                                     "Review this exact prepared candidate. All affected CE branches publish together "
                                     "only after every candidate passes review and CI; no separate PR merge occurs.\n\n" + detail
        })["number"]
