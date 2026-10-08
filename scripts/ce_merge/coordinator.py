# Copyright (c) 2026, Oracle and/or its affiliates.
"""Single-writer public integration state machine."""
from datetime import datetime
import hashlib
import json

from .validation import candidate_ci, validation_head
from .git import MergeConflict, PublicGraph
from .policy import Blocked, check_content, documentation_only, matches, require, rulesets, sha, upmerge_till, rehearsal_test, staging_repository
from .release import validate_source


def ruleset_revision(value):
    # GitHub formats dates using the authenticated viewer's timezone.
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "Ruleset revision must include a timezone")
        return stamp
    except (AttributeError, TypeError, ValueError):
        raise Blocked("Ruleset changed since administrator verification; invalid revision") from None


class TargetMoved(Blocked):
    """An unpublished snapshot needs refresh on the next coordinator poll."""


class Coordinator:
    def __init__(self, policy, store, github, graph=PublicGraph):
        self.policy, self.store, self.github, self.graph = policy, store, github, graph

    def policy_identity(self):
        return hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()

    def bundle(self, op):
        return self.store.bundle(op["id"], op["data"].get("generation", 0))

    def prepared_steps(self, op):
        return [s for s in op["data"]["steps"] if "after" in s]

    def check_prepared_policy(self, op):
        require(op["data"].get("policy") == self.policy_identity(),
                "Deployment policy changed; abort unpublished work and authorize a new batch")

    def attest_deployment(self, snapshot, operator):
        """Operator supplies a full ruleset export made with administrator access."""
        require(snapshot.get("repository") == self.policy["repository"],
                "Ruleset snapshot belongs to another repository")
        attestation = dict(snapshot, policy=self.policy_identity())
        self.deployment(attestation)
        self.store.set_setting("deployment-attestation", attestation, operator)

    def deployment(self, attestation=None):
        """Fail closed if protections drift. The App never changes its own protections."""
        require(self.policy["mode"] == "active", "Shadow mode cannot execute integration")
        repository = self.github.repo("")
        require(not repository["allow_rebase_merge"] and not repository.get("allow_auto_merge", False)
                and repository["allow_merge_commit"] and repository["allow_squash_merge"],
                "Repository merge settings differ from approved policy")
        actual = {r["name"]: r for r in self.github.pages("/rulesets?includes_parents=false")}
        attestation = attestation or self.store.setting("deployment-attestation")
        for expected in rulesets(self.policy):
            require(expected["name"] in actual, "Required CE ruleset is missing")
            found = self.github.repo(f'/rulesets/{actual[expected["name"]]["id"]}')
            if "bypass_actors" not in found:
                # GitHub hides this field unless the caller can WRITE rulesets.
                # Retain read-only App permissions and bind the operator's full
                # export to this policy and the live ruleset's identity/revision.
                require(attestation and attestation.get("policy") == self.policy_identity()
                        and attestation.get("repository") == self.policy["repository"],
                        "Ruleset bypasses are hidden; administrator must attest this deployment")
                snapshots = [r for r in attestation.get("rulesets", [])
                             if r.get("name") == expected["name"]]
                require(len(snapshots) == 1, "Administrator ruleset snapshot is missing or ambiguous")
                snapshot = snapshots[0]
                require(found.get("id") and found.get("updated_at")
                        and snapshot.get("id") == found["id"]
                        and ruleset_revision(snapshot.get("updated_at")) == ruleset_revision(found["updated_at"])
                        and found.get("source_type") == snapshot.get("source_type") == "Repository"
                        and found.get("source") == snapshot.get("source") == self.policy["repository"],
                        "Ruleset changed since administrator verification; attest deployment again")
                require(matches(expected, snapshot), "Administrator ruleset snapshot differs from approved policy")
                found = dict(found, bypass_actors=snapshot["bypass_actors"])
            for key, value in expected.items():
                require(matches(value, found.get(key)), "CE ruleset differs from approved policy")

    def basic(self, pr):
        require(pr["state"] == "open" and not pr["draft"], "PR must be open and ready")
        require(pr["base"]["repo"]["full_name"] == self.policy["repository"], "Wrong PR repository")
        require(pr["base"]["ref"] in self.policy["branches"] + self.policy.get("release_branches", []),
                "Target branch is not configured")
        require(pr["head"].get("repo"), "PR source repository is unavailable")
        sha(pr["base"]["sha"])
        sha(pr["head"]["sha"])

    def graph_for(self, pr):
        graph = self.graph(self.policy)
        try:
            graph.fetch(["refs/heads/" + b for b in self.policy["branches"] +
                         self.policy.get("release_branches", [])] +
                        [f'refs/pull/{pr["number"]}/head'])
            return graph
        except Exception:
            graph.__exit__()
            raise

    def evidence(self, pr, graph):
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        _, commits = graph.inspect(base, head)
        tree = graph.merge_tree(base, head)
        # GitHub may retain a merge ref for an older base. Prepare locally;
        # neither the contributor's branch nor protected CE refs need to move.
        merge = validation_head(graph, base, tree, head)
        paths = graph.paths(base, merge)
        check_content(paths, "", self.policy)
        return merge, paths, commits

    def ci(self, pr, merge, paths, parents=None, staged=False, graph=None):
        require(graph is not None, "Candidate validation needs its Git graph")
        if staged:
            require(graph.parents(merge) == parents, "Prepared candidate parents changed")
            require(parents and parents[0] == pr["base"]["sha"],
                    "Prepared target changed; rebuild the unpublished batch")
            graph.fetch([f'refs/pull/{pr["number"]}/head'])
            require(graph.merge_tree(pr["base"]["sha"], pr["head"]["sha"]) == graph.tree(merge),
                    "Prepared candidate differs from the reviewed source tree")
        if documentation_only(paths):
            return True, "Builds not applicable: documentation-only allowlist"
        return candidate_ci(self, pr, graph, graph.tree(merge), paths)

    def publish(self, pr, ci_ok, summary, eligible=False):
        key = f'published:{pr["number"]}'
        fingerprint = ["Merge check", self.policy["mode"], pr["head"]["sha"], pr["base"]["sha"], pr.get("body"),
                       ci_ok, summary, bool(eligible), rehearsal_test(self.policy)]
        if self.store.setting(key) == fingerprint:
            return
        if self.policy["mode"] == "active":
            self.github.advisory_label(pr, eligible)
        check_id = self.github.check(pr, "Merge check", "success" if eligible else "failure",
                                     ("Ready for maintainer authorization" +
                                      (" — REHEARSAL: " + rehearsal_test(self.policy) + " only per MTR shard"
                                       if rehearsal_test(self.policy) else "") + "\n\n" + summary) if eligible else summary,
                                     action=eligible and self.policy["mode"] == "active")
        self.store.set_setting(key, fingerprint, "coordinator")

    def promotion(self, pr):
        value = self.store.setting(f'promotion:{pr["number"]}')
        if value:
            require(value in self.store.active_promotions(value["release_tag"]),
                    "Promotion manifest has been superseded; use the active replacement PR")
            require(value["head"] == pr["head"]["sha"] and value["base"] == pr["base"]["ref"],
                    "Promotion manifest does not match PR")
            release = self.github.request("GET", "/repos/" + self.policy["repository"] +
                                          f'/releases/{int(value["release_id"])}')
            require(not release["draft"] and release.get("published_at")
                    and release["tag_name"] == value["release_tag"],
                    "Corresponding release is not public")
        return value

    def evaluate(self, pr):
        try:
            self.basic(pr)
            promotion = self.promotion(pr)
            if not promotion:
                require(pr["base"]["ref"] in self.policy["branches"], "Release branches require promotion")
                self.github.oca(pr)
                upmerge_till(pr.get("body"), pr["base"]["ref"], self.policy["branches"])
            self.github.reviewed(pr)
            with self.graph_for(pr) as graph:
                merge, paths, commits = self.evidence(pr, graph)
                if promotion:
                    self.check_promotion(graph, pr, promotion, commits)
                ok, summary = self.ci(pr, merge, paths, graph=graph)
            hold = self.store.setting("hold")
            permitted = not hold or (promotion and hold.get("release_tag") == promotion["release_tag"])
            self.publish(pr, ok, summary, ok and permitted)
        except Blocked as error:
            self.publish(pr, False, str(error))

    def check_promotion(self, graph, pr, manifest, commits):
        require(pr["head"]["repo"]["full_name"] == staging_repository(self.policy),
                "Promotion must use the configured public staging repository")
        if staging_repository(self.policy) == self.policy["repository"]:
            require(pr["head"].get("ref", "").startswith("promotion/"),
                    "Promotion must use the protected promotion branch namespace")
        validate_source(graph, manifest, pr["head"]["sha"], commits)
        require(graph.ancestor(manifest["base_sha"], pr["base"]["sha"]), "Promotion base is unrelated")

    def source(self, pr, op=None):
        self.basic(pr)
        if op:
            require(pr["head"]["sha"] == op["head"] and
                    (pr.get("body") or "") == op["data"]["body"] and
                    pr["base"]["ref"] == op["data"]["base"], "PR changed after authorization")
            require(self.github.can_integrate(op["data"]["actor"]), "Integration authorization revoked")
        promotion = self.promotion(pr)
        hold = self.store.setting("hold")
        if promotion and op and op["data"]["steps"]:
            # Prepared steps are the durable target snapshot, including for old
            # journals. Recheck it on advance, before publication, and on retry.
            steps = op["data"]["steps"]
            require(hold and hold.get("release_tag") == promotion["release_tag"] and
                    set(hold.get("targets", [])) == {s["branch"] for s in steps} and
                    all(s.get("promotion", {}).get("release_tag") == promotion["release_tag"] for s in steps),
                    "Promotion target set changed; abort unpublished batch and authorize a new batch")
        require(not hold or (promotion and hold.get("release_tag") == promotion["release_tag"]), "CE is on hold")
        if not promotion:
            require(pr["base"]["ref"] in self.policy["branches"], "Release targets require promotion")
            self.github.oca(pr)
        self.github.reviewed(pr)
        return promotion

    def prepare(self, op):
        root = self.github.pull(op["pr"])
        promotion = self.source(root, op)
        chain = self.policy["branches"]
        branches = chain[chain.index(root["base"]["ref"]):] if not promotion else []
        sources = [root]
        if promotion:
            hold = self.store.setting("hold")
            require(hold and hold.get("release_tag") == promotion["release_tag"] and hold.get("targets"),
                    "Release requires a hold declaring every atomic promotion target")
            manifests = self.store.active_promotions(promotion["release_tag"])
            require(len(manifests) == len(hold["targets"]) and
                    {m["base"] for m in manifests} == set(hold["targets"]),
                    "Register exactly one approved promotion per declared target before integration")
            branches = [b for b in chain + self.policy.get("release_branches", []) if b in hold["targets"]]
            development = [b for b in branches if b in chain]
            require(not development or development == chain[chain.index(development[0]):],
                    "Promotion must include every successor through Innovation")
            sources = [self.github.pull(next(m["pr"] for m in manifests if m["base"] == b)) for b in branches]
            require(root["number"] == sources[0]["number"], "Authorize the oldest target to publish the release batch")
        else:
            stop = upmerge_till(root.get("body"), root["base"]["ref"], chain)
        with self.graph(self.policy) as graph:
            refs = ["refs/heads/" + b for b in chain + self.policy.get("release_branches", [])]
            advertised = graph.remote_refs(refs)
            heads = {b: sha(advertised.get("refs/heads/" + b)) for b in chain + self.policy.get("release_branches", [])}
            graph.fetch(list(heads.values()) + [f'refs/pull/{p["number"]}/head' for p in sources])
            for lower, higher in zip(chain, chain[1:]):
                require(graph.ancestor(heads[lower], heads[higher]), "CE ancestry needs reconciliation before cutover")
            steps = []
            if promotion:
                previous = None
                for branch, pr in zip(branches, sources):
                    manifest = self.source(pr)
                    require(manifest and manifest["release_tag"] == promotion["release_tag"], "Unapproved promotion source")
                    _, commits = graph.inspect(manifest["base_sha"], pr["head"]["sha"])
                    self.check_promotion(graph, pr, manifest, commits)
                    base = heads[branch]
                    parents = [base, pr["head"]["sha"]]
                    # Branch-specific SC content is already reviewed. Carry lower
                    # ancestry with an explicitly approved null content merge.
                    if previous and branch in chain:
                        require(manifest.get("ancestry_reason"), "Release ancestry-only merge needs a reviewed explanation")
                        parents.append(previous)
                    tree = graph.merge_tree(base, pr["head"]["sha"])
                    after = graph.commit(tree, parents, f'Released SC: CE integration {op["id"]} into {branch}')
                    graph.inspect(base, after)
                    steps.append({"branch": branch, "base_sha": base, "after": after, "tree": tree,
                                  "parents": parents, "null": len(parents) > 2,
                                  "source_pr": pr["number"], "source_head": pr["head"]["sha"],
                                  "source_body": pr.get("body") or "", "promotion": manifest})
                    previous = after if branch in chain else None
            else:
                steps = [{"branch": b, "base_sha": heads[b], "null": chain.index(b) > chain.index(stop)}
                         for b in branches]
                steps[0]["message"] = root.get("title", "CE contribution") + f' (#{root["number"]})'
                try:
                    graph.candidates(steps, op["head"], op["id"])
                except MergeConflict:
                    index = next(i for i, step in enumerate(steps) if "after" not in step)
                    require(index > 0, "Original PR conflicts with its target; update the PR and authorize a new batch")
                    steps[index]["conflict"] = True
                    op["data"]["conflict_index"] = index
                    op["data"]["reason"] = f'Upmerge into {steps[index]["branch"]} needs a reviewed resolution PR'
                require(steps[0]["tree"] != graph.tree(steps[0]["base_sha"]),
                        "Contribution is already present or has no changes on the current target")
                steps[0].update(source_pr=root["number"], source_head=op["head"])
            graph.export_candidates([s for s in steps if "after" in s], self.bundle(op))
        op["data"]["steps"] = steps
        op["data"]["policy"] = self.policy_identity()
        self.store.save(op, "prepared", "candidates-prepared")

    def stage(self, op, graph):
        steps = [s for s in op["data"]["steps"] if "after" in s or s.get("conflict")]
        remote = "https://github.com/" + staging_repository(self.policy) + ".git"
        for step in steps:
            step.setdefault("stage_id", op["id"] + (f'-r{op["data"]["generation"]}' if op["data"].get("generation") else ""))
        refs = [f'refs/heads/upmerge/{s["stage_id"]}/{i}' for i, s in enumerate(steps)]
        heads = [s.get("after", s.get("input_head")) for s in steps]
        current = {line.split()[1]: line.split()[0] for line in
                   graph.run("ls-remote", "--refs", remote, *refs).splitlines()}
        pending = [(ref, head) for ref, head in zip(refs, heads) if ref not in current]
        require(all(current.get(ref, head) == head for ref, head in zip(refs, heads)),
                "Staging branch changed unexpectedly")
        if pending:
            graph.run("push", "--atomic", *["--force-with-lease=" + ref + ":" for ref, _ in pending],
                      remote, *[head + ":" + ref for ref, head in pending], token=self.github.token(fork=True))
        for index, (step, head) in enumerate(zip(steps, heads)):
            if "pr" not in step:
                # The first ordinary PR remains the contributor's PR. Additional
                # PRs review the exact candidate, including any null merge.
                if index == 0 and not step.get("promotion"):
                    step.update(pr=op["pr"], review_head=op["head"])
                else:
                    detail = ("Ancestry-only propagation: the target tree must remain unchanged. "
                              "Review the original PR's applicability explanation.\n" if step["null"] else "Forward content propagation.\n")
                    if step.get("conflict"):
                        detail += "This input conflicts with the target. Supply a reviewed resolution PR; no CE branch has moved.\n"
                    detail += f'Input `{head}`; recorded target `{step["base_sha"]}`.'
                    step.update(pr=self.github.bot_pr(step["stage_id"], index, head, step["branch"], op["pr"], detail=detail),
                                review_head=head)
                self.store.save(op, "prepared", "candidate-review-linked")

    def repair(self, op, number):
        """Accept a reviewed resolution while every CE target is still unchanged."""
        data = op["data"]
        require(not data["intent"] and "conflict_index" in data, "No unpublished upmerge conflict to repair")
        self.deployment()
        self.check_prepared_policy(op)
        self.source(self.github.pull(op["pr"]), op)
        index = data["conflict_index"]
        step = data["steps"][index]
        pr = self.github.pull(number)
        self.basic(pr)
        self.github.oca(pr)
        self.github.reviewed(pr)
        require(pr["base"]["ref"] == step["branch"] and pr["base"]["sha"] == step["base_sha"],
                "Resolution PR must target the conflicting branch at the recorded base")
        with self.graph(self.policy) as graph:
            graph.restore_candidates(self.prepared_steps(op), self.bundle(op))
            # The saved bundle covers only prepared steps. A fresh graph also
            # needs the recorded bases of the conflicting and later targets.
            graph.fetch([f'refs/pull/{number}/head'] +
                        [s["base_sha"] for s in data["steps"][index:]])
            heads = graph.remote_refs(["refs/heads/" + s["branch"] for s in data["steps"]])
            require(all(heads.get("refs/heads/" + s["branch"]) == s["base_sha"] for s in data["steps"]),
                    "A target moved; abort and authorize a new batch")
            head = pr["head"]["sha"]
            require(graph.ancestor(step["input_head"], head) and graph.ancestor(step["base_sha"], head),
                    "Resolution must merge the exact lower candidate and recorded target base")
            graph.inspect(step["base_sha"], head)
            tree = graph.tree(head)
            require(not step["null"] or tree == graph.tree(step["base_sha"]), "Null merge must preserve the target tree")
            replacement = {"branch": step["branch"], "base_sha": step["base_sha"], "null": step["null"],
                           "parents": [step["base_sha"], head], "tree": tree, "input_head": step["input_head"],
                           "pr": number, "review_head": head, "repair_body": pr.get("body") or ""}
            replacement["after"] = graph.commit(tree, replacement["parents"], f'Reviewed resolution #{number} for CE {op["id"]}')
            steps = data["steps"][:index] + [replacement] + [
                {k: s[k] for k in ("branch", "base_sha", "null")} for s in data["steps"][index + 1:]]
            data.pop("conflict_index")
            data.pop("reason", None)
            previous = replacement["after"]
            for next_index in range(index + 1, len(steps)):
                steps[next_index]["squash"] = False
                try:
                    graph.candidates([steps[next_index]], previous, op["id"])
                except MergeConflict:
                    steps[next_index]["conflict"] = True
                    data["conflict_index"] = next_index
                    data["reason"] = f'Upmerge into {steps[next_index]["branch"]} needs a reviewed resolution PR'
                    break
                previous = steps[next_index]["after"]
            data["generation"] = data.get("generation", 0) + 1
            graph.export_candidates([s for s in steps if "after" in s], self.bundle(op))
            if step.get("pr") and step["pr"] != number:
                data.setdefault("superseded_prs", []).append(step["pr"])
            data["steps"] = steps
            self.store.save(op, "prepared", "reviewed-resolution-accepted")

    def verify_step(self, step, graph):
        pr = self.github.pull(step["pr"])
        self.basic(pr)
        require(pr["head"]["sha"] == step["review_head"] and pr["base"]["ref"] == step["branch"],
                "Candidate changed; abort and prepare a new batch")
        if pr["base"]["sha"] != step["base_sha"]:
            raise TargetMoved("Target advanced; rebuilding unpublished candidates on the next poll")
        self.github.reviewed(pr)
        if "repair_body" in step:
            require((pr.get("body") or "") == step["repair_body"], "Resolution instructions changed")
            self.github.oca(pr)
        if step.get("promotion"):
            source = self.github.pull(step["source_pr"])
            require(source["head"]["sha"] == step["source_head"] and
                    (source.get("body") or "") == step["source_body"] and
                    self.source(source) == step["promotion"], "Promotion approval changed after preparation")
            _, commits = graph.inspect(step["promotion"]["base_sha"], source["head"]["sha"])
            self.check_promotion(graph, source, step["promotion"], commits)
        return pr

    def refresh_targets(self, op):
        """Discard only unpublished candidates; retain authorization on the source head."""
        data = op["data"]
        require(not data["intent"], "Reconcile publication before rebuilding candidates")
        changed = any(self.github.branch(s["branch"]) != s["base_sha"] for s in data["steps"])
        if not changed:
            return
        require(not any(s.get("promotion") or "repair_body" in s for s in data["steps"]),
                "Target changed for a promotion or reviewed resolution; abort and prepare a reviewed replacement")
        data.setdefault("superseded_batches", []).append(data["steps"])
        data.setdefault("superseded_prs", []).extend(s["pr"] for s in data["steps"]
                                                   if s.get("pr") and s["pr"] != op["pr"])
        data["generation"] = data.get("generation", 0) + 1
        data["steps"] = []
        data.pop("conflict_index", None)
        data.pop("reason", None)
        self.store.save(op, "queued", "target-moved-rebuild-unpublished-batch")

    def advance(self, op):
        data = op["data"]
        try:
            if data["intent"]:
                self.reconcile(op)
                return
            self.deployment()
            self.source(self.github.pull(op["pr"]), op)
            if data["steps"]:
                self.check_prepared_policy(op)
                self.refresh_targets(op)
            if not data["steps"]:
                self.prepare(op)
            self.check_prepared_policy(op)
            with self.graph(self.policy) as graph:
                graph.restore_candidates(self.prepared_steps(op), self.bundle(op))
                self.stage(op, graph)
                if "conflict_index" in data:
                    self.store.save(op, "conflict", "waiting-for-reviewed-resolution")
                    return
                waiting = []
                for step in data["steps"]:
                    pr = self.github.pull(step["pr"])
                    paths, _ = graph.inspect(step["base_sha"], step["after"])
                    try:
                        ok, message = self.ci(pr, step["after"], paths, parents=step["parents"], staged=True, graph=graph)
                    except Blocked as error:
                        ok, message = False, str(error)
                    try:
                        self.verify_step(step, graph)
                    except Blocked as error:
                        ok, message = False, str(error)
                    self.github.check(pr, "Merge check", "success" if ok else "failure",
                                      "Prepared batch; publication awaits every target\n\n" + message if ok else message,
                                      candidate=step["after"])
                    if not ok:
                        waiting.append(message)
                if waiting:
                    data["reason"] = "; ".join(sorted(set(waiting)))
                    self.store.save(op, "prepared", "waiting-for-batch-validation")
                    return
                # Revalidate authorization and every review immediately before
                # publishing. Expected-old-SHA leases reject any target movement.
                self.deployment()
                self.source(self.github.pull(op["pr"]), op)
                for step in data["steps"]:
                    pr = self.verify_step(step, graph)
                    paths, _ = graph.inspect(step["base_sha"], step["after"])
                    require(self.ci(pr, step["after"], paths, parents=step["parents"], staged=True, graph=graph)[0],
                            "Candidate CI changed before publication")
                data["receipt"] = graph.receipt(op["id"], data["steps"], data["actor"])
                data["intent"] = True
                self.store.save(op, "publishing", "atomic-push-intent")
                graph.atomic_publish(data["steps"], data["receipt"], token=self.github.token())
            self.reconcile(op)
        except TargetMoved as error:
            data["reason"] = str(error)
            self.store.save(op, "uncertain" if data["intent"] else "prepared", "target-moved")
        except Blocked as error:
            data["reason"] = str(error)
            self.store.save(op, "uncertain" if data["intent"] else "blocked", "blocked")
        except Exception:
            data["reason"] = "Inspect private service diagnostics and reconcile publication receipt"
            self.store.save(op, "uncertain" if data["intent"] else "blocked", "unexpected-error")
            raise

    def reconcile(self, op):
        receipt = op["data"]["receipt"]
        with self.graph(self.policy) as graph:
            actual = graph.remote_refs([receipt["ref"]]).get(receipt["ref"])
        if actual == receipt["sha"]:
            op["data"].pop("reason", None)
            self.store.save(op, "complete", "atomic-receipt-confirmed")
        else:
            require(actual is None, "Unexpected publication receipt; investigate before continuing")
            self.store.save(op, "uncertain", "receipt-not-yet-observed")

    def retry(self, op):
        """Operator-requested retry of the SAME transaction; never rebuild uncertain work.

        Identical receipt creation and old-SHA leases make concurrent/lost-response
        retries safe: at most one of these equivalent transactions can succeed.
        """
        require(op["data"]["intent"], "No publication intent to retry")
        self.reconcile(op)
        if op["state"] == "complete":
            return
        self.deployment()
        self.check_prepared_policy(op)
        self.source(self.github.pull(op["pr"]), op)
        with self.graph(self.policy) as graph:
            graph.restore_candidates(op["data"]["steps"], self.bundle(op))
            # Recheck current-head PR CI, including reruns/revocations.
            for step in op["data"]["steps"]:
                pr = self.verify_step(step, graph)
                paths, _ = graph.inspect(step["base_sha"], step["after"])
                require(self.ci(pr, step["after"], paths, parents=step["parents"], staged=True, graph=graph)[0],
                        "Candidate CI is pending")
            graph.atomic_publish(op["data"]["steps"], op["data"]["receipt"], token=self.github.token())
        self.reconcile(op)

    def verify_chain(self):
        with self.graph(self.policy) as graph:
            refs = graph.remote_refs(["refs/heads/" + b for b in self.policy["branches"]])
            heads = {b: sha(refs.get("refs/heads/" + b)) for b in self.policy["branches"]}
            graph.fetch(list(heads.values()))
            for lower, higher in zip(self.policy["branches"], self.policy["branches"][1:]):
                require(graph.ancestor(heads[lower], heads[higher]), "CE branch ancestry is incomplete")
        return heads

    def report(self, op):
        """Public-safe progress is retried independently of the Git transaction."""
        data = op["data"]
        key = "operation-report:" + op["id"]
        summary = f'CE operation `{op["id"]}`: **{op["state"]}**.\n\n'
        if op["state"] == "complete":
            summary += f'All targets published atomically. Receipt: `{data["receipt"]["ref"]}`.\n\n'
        else:
            summary += data.get("reason", "Preparing all branch candidates before publication.") + "\n\n"
        for step in data["steps"]:
            summary += f'- `{step["branch"]}`: ' + (f'PR #{step["pr"]}; ' if "pr" in step else "")
            summary += (f'candidate `{step["after"]}`' if "after" in step else "awaiting preparation")
            summary += "; ancestry only\n" if step["null"] else "\n"
        if "conflict_index" in data:
            step = data["steps"][data["conflict_index"]]
            summary += (f'\nResolve `{step["branch"]}` by merging lower candidate `{step["input_head"]}` '
                        f'with target `{step["base_sha"]}` in a personal fork. Open a resolution PR against '
                        f'`{step["branch"]}`. After OCA and approval from a reviewer with Write, Maintain, or Admin access, the merge captain runs '
                        f'`repair-upmerge {op["id"]} --pr NUMBER`. Every candidate still needs CI before publication.')
        fingerprint = hashlib.sha256(summary.encode()).hexdigest()
        if self.store.setting(key) == fingerprint:
            return
        conclusion = "success" if op["state"] == "complete" else (
            "action_required" if op["state"] in ("blocked", "conflict", "uncertain") else "neutral")
        reviews = {op["pr"]: op["head"]}
        reviews.update({s["pr"]: s["review_head"] for s in data["steps"] if "pr" in s})
        for number, head in reviews.items():
            pr = self.github.pull(number)
            self.github.check(pr, "CE / operation", conclusion, summary, candidate=head)
            if pr["head"]["sha"] == head:
                self.github.advisory_label(pr, False)
        for number in data.get("superseded_prs", []):
            pr = self.github.pull(number)
            self.github.check(pr, "CE / operation", "neutral",
                              f'Superseded by a reviewed resolution in operation `{op["id"]}`.\n\n' + summary)
            self.github.close_pr(pr)
        # A squash does not make the original contributor commit reachable;
        # close it only after the atomic receipt, with the SHA recorded above.
        if op["state"] == "complete" and data["steps"] and not data["steps"][0].get("promotion"):
            pr = self.github.pull(op["pr"])
            if (pr["head"]["sha"] == op["head"] and pr["base"]["ref"] == data["base"]
                    and (pr.get("body") or "") == data["body"]):
                self.github.close_pr(pr)
        self.store.set_setting(key, fingerprint, "coordinator")

    def tick(self):
        try:
            self.work()
        finally:
            if self.policy["mode"] == "active":
                for op in self.store.operations():
                    self.report(op)

    def work(self):
        # Caller holds only a process-scoped publisher mutex, not a durable merge lock.
        # Waiting for review never results in partially updated CE branches.
        if self.policy["mode"] == "active":
            if self.store.uncertain():
                for op in self.store.uncertain():
                    self.reconcile(op)
                return
            pending = next((o for o in self.store.operations() if o["state"] in ("queued", "prepared")), None)
            if pending:
                self.advance(pending)
                return
        generated = {s["pr"] for o in self.store.operations()
                     for s in o["data"]["steps"] if "pr" in s and (
                         s["pr"] != o["pr"] or o["state"] not in ("aborted", "complete"))}
        generated.update(n for o in self.store.operations() for n in o["data"].get("superseded_prs", []))
        for pr in self.github.pages("/pulls?state=open"):
            if pr["number"] not in generated and pr["base"]["ref"] in self.policy["branches"] + self.policy.get("release_branches", []):
                self.evaluate(self.github.pull(pr["number"]))
