# Copyright (c) 2026, Oracle and/or its affiliates.
"""Single-writer public integration state machine."""
from datetime import datetime
import hashlib
import json

from .ci import WORKFLOWS, existing_pr_ci, validation_head
from .github import authorization_identity, integration_body
from .testing import candidate_tests
from .journal import PersistenceError
from .git import MergeConflict, PublicGraph
from .policy import Blocked, check_content, documentation_only, matches, require, rulesets, sha, rehearsal_test, resolution_prs


def ruleset_revision(value):
    # GitHub formats dates using the authenticated viewer's timezone.
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "Ruleset revision must include a timezone")
        return stamp
    except (AttributeError, TypeError, ValueError):
        raise Blocked("Ruleset changed since administrator verification; invalid revision") from None


class Coordinator:
    def __init__(self, policy, store, github, graph=PublicGraph):
        self.policy, self.store, self.github, self.graph = policy, store, github, graph

    def policy_identity(self):
        return hashlib.sha256(json.dumps({"protocol": "forward-only-v1", "policy": self.policy}, sort_keys=True).encode()).hexdigest()

    def bundle(self, op):
        return self.store.bundle(op["id"], op["data"].get("generation", 0))

    def prepared_steps(self, op):
        return [s for s in op["data"]["steps"] if "after" in s]

    def check_prepared_policy(self, op):
        require(op["data"].get("policy") == self.policy_identity(),
                "Deployment policy changed; restore the authorized policy before continuing")

    def attest_deployment(self, snapshot, operator):
        """Operator supplies a full ruleset export made with administrator access."""
        require(snapshot.get("repository") == self.policy["repository"],
                "Ruleset snapshot belongs to another repository")
        attestation = dict(snapshot, policy=self.policy_identity())
        self.deployment(attestation)
        self.store.set_setting("deployment-attestation", attestation, operator)

    def deployment(self, attestation=None):
        """Fail closed if protections drift. The workflow never changes its own protections."""
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
                # Retain read-only administration permissions and bind the operator's full
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
        require(pr["base"]["ref"] in self.policy["branches"],
                "Target branch is not configured")
        require(pr["head"].get("repo"), "PR source repository is unavailable")
        sha(pr["base"]["sha"])
        sha(pr["head"]["sha"])

    def graph_for(self, pr):
        graph = self.graph(self.policy)
        try:
            graph.fetch(["refs/heads/" + b for b in self.policy["branches"]] +
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

    def ci(self, pr, merge, paths, graph=None):
        require(graph is not None, "Candidate validation needs its Git graph")
        if documentation_only(paths):
            return True, "Builds not applicable: documentation-only allowlist"
        require(not any('.github/workflows/' + name in paths for name in WORKFLOWS),
                'PR changes a required CI workflow; bootstrap its reviewed update before trusting it')
        require(graph.merge_tree(pr['base']['sha'], pr['head']['sha']) == graph.tree(merge),
                'Candidate differs from the current clean merge of the reviewed PR')
        return existing_pr_ci(self.github, self.policy, pr, pr.get('merge_commit_sha'), paths, graph=graph)

    def publish(self, pr, eligible, summary):
        key = f'published:{pr["number"]}'
        fingerprint = [self.policy_identity(), authorization_identity(pr), pr["base"]["sha"],
                       summary, bool(eligible)]
        if self.store.setting(key) == fingerprint:
            return
        self.store.checkpoint()
        self.github.advisory_label(pr, eligible)
        heading = "Ready: an authorized maintainer may comment `/integrate`." if eligible else "Integration blocked."
        if rehearsal_test(self.policy):
            heading += f' REHEARSAL: {rehearsal_test(self.policy)} only per MTR shard.'
        revision = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
        description = f'Ready for /integrate [{revision}]' if eligible else heading
        result = self.github.status(pr, "Merge check", "success" if eligible else "failure", description)
        self.github.comment(pr, "readiness", heading + "\n\n" + summary)
        # GitHub's timestamp, not the runner clock, orders readiness and commands.
        self.store.set_setting(f'ready:{pr["number"]}', {
            "identity": authorization_identity(pr), "policy": self.policy_identity(),
            "eligible": bool(eligible), "created_at": result["created_at"]}, "coordinator")
        self.store.set_setting(key, fingerprint, "coordinator")

    def publish_unmanaged(self, pr):
        key = f'published:{pr["number"]}'
        if not self.store.setting(key):
            return
        self.store.checkpoint()
        self.github.advisory_label(pr, False)
        self.github.status(pr, "Merge check", "success", "Target is outside the configured CE branch chain.")
        self.github.comment(pr, "readiness", "Target is outside the configured CE branch chain; integration is disabled.")
        self.store.set_setting(f'ready:{pr["number"]}', None, "coordinator")
        self.store.set_setting(key, None, "coordinator")

    def evaluate(self, pr):
        try:
            self.source(pr)
            with self.graph_for(pr) as graph:
                merge, paths, _ = self.evidence(pr, graph)
                ok, summary = self.ci(pr, merge, paths, graph=graph)
            self.publish(pr, ok, summary)
        except Blocked as error:
            self.publish(pr, False, str(error))

    def source(self, pr, op=None):
        merged = bool(op and op['data']['steps'] and op['data']['steps'][0].get('merged_sha'))
        if merged:
            require(pr.get('merged') and pr.get('merge_commit_sha') == op['data']['steps'][0]['merged_sha'],
                    'Original squash merge changed')
        else:
            self.basic(pr)
        resolution_prs(pr.get('body'), pr['base']['ref'], self.policy)
        if op:
            require(pr["head"]["sha"] == op["head"] and
                    (pr.get("body") or "") == op["data"]["body"] and
                    pr["base"]["ref"] == op["data"]["base"], "PR changed after authorization")
            require(self.github.can_integrate(op["data"]["actor"]), "Integration authorization revoked")
        require(self.store.setting('merge-lock') in (None, op['id'] if op else None),
                'CE soft merge lock is held by another operation')
        require(not self.store.setting("hold"), "CE is on hold")
        self.github.oca(pr)
        self.github.reviewed(pr)

    def candidates(self, op, graph, steps, previous, start=0, invalidated=()):
        """Build a suffix until its first conflict, using one path for every rehearsal."""
        data = op['data']
        data.pop('conflict_index', None)
        for index in range(start, len(steps)):
            step = steps[index]
            step['squash'] = index == 0
            step['input_head'] = previous
            try:
                # A moved target must get a fresh reviewed resolution even if
                # its old resolved tree would still apply without a Git conflict.
                if index in invalidated:
                    raise MergeConflict('Reviewed resolution target moved')
                graph.candidates([step], previous, op['id'])
            except MergeConflict:
                require(index > 0, 'Original PR conflicts with its target; update the PR and authorize a new operation')
                data['conflict_index'] = index
                break
            previous = step['after']

    def prepare(self, op):
        root = self.github.pull(op["pr"])
        self.source(root, op)
        chain = self.policy["branches"]
        op['data']['resolution_prs'] = resolution_prs(root.get('body'), root['base']['ref'], self.policy)
        branches = chain[chain.index(root["base"]["ref"]):]
        with self.graph(self.policy) as graph:
            refs = ["refs/heads/" + b for b in chain]
            advertised = graph.remote_refs(refs)
            heads = {b: sha(advertised.get("refs/heads/" + b)) for b in chain}
            graph.fetch(list(heads.values()) + [f'refs/pull/{root["number"]}/head'])
            for lower, higher in zip(chain, chain[1:]):
                require(graph.ancestor(heads[lower], heads[higher]), "CE ancestry needs reconciliation before cutover")
            steps = [{"branch": b, "base_sha": heads[b]} for b in branches]
            steps[0]["message"] = root.get("title", "CE contribution") + f' (#{root["number"]})'
            self.candidates(op, graph, steps, op["head"])
            require(steps[0]["tree"] != graph.tree(steps[0]["base_sha"]),
                    "Contribution is already present or has no changes on the current target")
            graph.export_candidates([s for s in steps if "after" in s], self.bundle(op))
        op["data"]["steps"] = steps
        op["data"]["policy"] = self.policy_identity()
        self.store.save(op, "prepared", "candidates-prepared")

    def repair(self, op, number):
        """Accept a reviewed resolution before merging the conflicting hop."""
        data = op["data"]
        self.acquire(op)
        require(not data["intent"] and "conflict_index" in data, "No unpublished version conflict to resolve")
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
            require(all(heads.get("refs/heads/" + s["branch"]) == s.get("merged_sha", s["base_sha"]) for s in data["steps"]),
                    "A target moved; refresh preflight before accepting the resolution")
            head = pr["head"]["sha"]
            require(graph.ancestor(step["input_head"], head) and graph.ancestor(step["base_sha"], head),
                    "Resolution must merge the exact lower candidate and recorded target base")
            graph.inspect(step["base_sha"], head)
            tree = graph.tree(head)
            replacement = {"branch": step["branch"], "base_sha": step["base_sha"],
                           "parents": [step["base_sha"], head], "tree": tree, "input_head": step["input_head"],
                           "resolved_tree": tree, "resolution": {"pr": number, "head": head, "body": pr.get("body") or ""}}
            replacement["after"] = graph.commit(tree, replacement["parents"], f'Reviewed resolution #{number} for CE {op["id"]}')
            steps = data["steps"][:index] + [replacement] + [
                {k: s[k] for k in ("branch", "base_sha")} for s in data["steps"][index + 1:]]
            data.pop("reason", None)
            self.candidates(op, graph, steps, replacement["after"], start=index + 1)
            data["generation"] = data.get("generation", 0) + 1
            graph.export_candidates([s for s in steps if "after" in s], self.bundle(op))
            if step.get("pr") and step["pr"] != number:
                data.setdefault("superseded_prs", []).append(step["pr"])
            data["steps"] = steps
            self.store.save(op, "prepared", "reviewed-resolution-accepted")

    def acquire(self, op):
        require(op['data'].get('protocol') == 'forward-only-v1',
                'Legacy operation: reconcile or abort it with the previous coordinator before cutover')
        owner = self.store.setting('merge-lock')
        require(owner in (None, op['id']), 'CE soft merge lock belongs to another operation')
        if owner is None:
            self.store.set_setting('merge-lock', op['id'], op['data']['actor'])
        self.store.checkpoint()

    def release(self, op):
        if self.store.setting('merge-lock') == op['id']:
            self.store.set_setting('merge-lock', None, op['data']['actor'])

    def stage(self, op, graph, index):
        """Use real version branches, or a maintainer's reviewed resolution PR."""
        step = op['data']['steps'][index]
        if index == 0:
            step.update(pr=op['pr'], review_head=op['head'])
        else:
            if step.get('resolution'):
                saved = step['resolution']
                head = saved['head']
                if not graph.ancestor(step['input_head'], head):
                    op['data']['conflict_index'] = index
                    raise MergeConflict('Update the resolution PR to include the actual lower version commit, then obtain fresh approval')
                step.update(pr=saved['pr'], review_head=head, pr_kind='resolution')
            else:
                lower = op['data']['steps'][index - 1]
                head, branch = lower['merged_sha'], lower['branch']
                require(self.github.branch(branch) == head, 'Lower version branch moved')
                try:
                    tree = graph.merge_tree(step['base_sha'], head)
                except MergeConflict:
                    op['data']['conflict_index'] = index
                    raise
                if tree != step['tree']:
                    op['data']['conflict_index'] = index
                    raise MergeConflict('A normal version-branch merge would change the required tree; provide a reviewed resolution PR')
                detail = f'From `{branch}` into `{step["branch"]}`.\n'
                self.store.save(op, 'prepared', 'version-pr-create-intent')
                number = self.github.bot_pr(op['id'], branch, step['branch'], op['pr'], detail=detail)
                require(step.get('pr', number) == number, 'Integration PR identity changed')
                step.update(pr=number, review_head=head, head_branch=branch, pr_kind='version',
                            pr_body=integration_body(op['id'], op['pr'], detail))
        self.store.save(op, 'prepared', 'integration-pr-linked')

    def test_chain(self, op, graph, indices):
        """Collect all missing evidence without opening any integration PRs."""
        waiting = []
        for index in indices:
            step = op['data']['steps'][index]
            try:
                ok, message = self.tests(op, graph, index)
            except Blocked as error:
                ok, message = False, str(error)
            step['test_summary'] = message
            if not ok:
                waiting.append(step['branch'] + ': ' + message)
        if waiting:
            op['data']['reason'] = '; '.join(waiting)
        else:
            op['data'].pop('reason', None)
        self.store.save(op, 'prepared', 'candidate-tests-checked')
        return not waiting

    def tests(self, op, graph, index):
        return candidate_tests(self, op, graph, index)

    def verify_step(self, step, graph, index):
        pr = self.github.pull(step['pr'])
        self.basic(pr)
        require(pr['head']['sha'] == step['review_head'] and pr['base']['ref'] == step['branch'],
                'Integration PR changed; investigate before continuing')
        require(pr['base']['sha'] == step['base_sha'], 'Target advanced; repeat testing before merging')
        if index > 0:
            require(graph.ancestor(step['input_head'], pr['head']['sha'])
                    and graph.merge_tree(step['base_sha'], pr['head']['sha']) == step['tree'],
                    'Integration PR differs from the tested candidate')
            if step['pr_kind'] == 'version':
                require(pr.get('user', {}).get('id') == self.policy['bot_user_id']
                        and pr['head']['repo']['full_name'] == self.policy['repository']
                        and pr['head']['ref'] == step['head_branch']
                        and pr['head']['sha'] == step['input_head']
                        and self.github.branch(step['head_branch']) == step['input_head']
                        and (pr.get('body') or '') == step['pr_body'],
                        'Version PR identity or body changed')
                self.github.reviewed(pr, require_approval=False)
            else:
                require((pr.get('body') or '') == step['resolution']['body'], 'Resolution PR body changed')
                self.github.oca(pr)
                self.github.reviewed(pr)
        else:
            self.github.reviewed(pr)
        return pr

    def rebuild(self, op):
        """Rehearse every remaining hop from the last actual merge before proceeding."""
        data = op['data']
        start = next(i for i, s in enumerate(data['steps']) if not s.get('merged_sha'))
        old = data['steps']
        if start == 0:
            self.verify_chain()
        heads = {s['branch']: self.github.branch(s['branch']) for s in old[start:]}
        changed = any(heads[s['branch']] != s['base_sha'] for s in old[start:])
        previous = old[start - 1]['merged_sha'] if start else op['head']
        if not changed and old[start].get('input_head') == previous:
            return
        needs_resolution = {i for i in range(start, len(old)) if changed and old[i].get('resolution')}
        steps = old[:start] + [{k: s[k] for k in ('branch', 'resolved_tree', 'resolution', 'message', 'test_request', 'test_attempt', 'fresh_tests') if k in s}
                              for s in old[start:]]
        for i in needs_resolution:
            steps[i].pop('resolution', None)
            steps[i].pop('resolved_tree', None)
        with self.graph(self.policy) as graph:
            # Bundles preserve preflight resolution trees across runner replacement.
            graph.restore_candidates(self.prepared_steps(op), self.bundle(op))
            graph.fetch(list(heads.values()) +
                        ([old[start - 1]['merged_sha']] if start else [op['head']]))
            for step in steps[start:]:
                step['base_sha'] = heads[step['branch']]
            self.candidates(op, graph, steps, previous, start=start, invalidated=needs_resolution)
            data['generation'] = data.get('generation', 0) + 1
            data['steps'] = steps
            graph.export_candidates(self.prepared_steps(op), self.bundle(op))
        self.store.save(op, 'prepared', 'remaining-chain-preflight')

    def expose_conflict(self, op):
        """Expose the saved public bundle without publishing a Git branch."""
        data = op['data']
        index = data['conflict_index']
        step = data['steps'][index]
        step['input_bundle'] = self.bundle(op).name
        self.store.save(op, 'conflict', 'conflict-input-bundled')

    def check_resolutions(self, op):
        for step in op['data']['steps']:
            if 'resolution' in step and not step.get('merged_sha'):
                saved = step['resolution']
                pr = self.github.pull(saved['pr'])
                self.basic(pr)
                require(pr['head']['sha'] == saved['head'] and (pr.get('body') or '') == saved['body']
                        and pr['base']['ref'] == step['branch'] and pr['base']['sha'] == step['base_sha'],
                        'Reviewed resolution changed')
                self.github.oca(pr)
                self.github.reviewed(pr)

    def advance(self, op):
        data = op['data']
        if op['state'] in ('complete', 'aborted'):
            return
        try:
            self.acquire(op)
            if data['intent']:
                self.reconcile(op)
                return
            self.deployment()
            require(all(self.github.branch(s['branch']) == s['merged_sha']
                        for s in data['steps'] if s.get('merged_sha')),
                    'A previously merged target moved; investigate before continuing propagation')
            self.source(self.github.pull(op['pr']), op)
            if not data['steps']:
                self.prepare(op)
            self.check_prepared_policy(op)
            self.rebuild(op)
            self.check_resolutions(op)
            # An optional Resolution-PR can supply an adapted version even when
            # Git's automatic merge is clean. Validate links in chain order.
            linked = data.get('resolution_prs', {})
            for i, step in enumerate(data['steps']):
                if step['branch'] in linked and not step.get('resolution') and not step.get('merged_sha'):
                    if 'conflict_index' not in data or i <= data['conflict_index']:
                        data['conflict_index'] = i
                        self.expose_conflict(op)
                        self.repair(op, linked[step['branch']])
                        return  # Validate the next linked version on the next bounded pass.
                    break
            if 'conflict_index' in data:
                self.expose_conflict(op)
                data['reason'] = f'Testing conflicts at {data["steps"][data["conflict_index"]]["branch"]}; provide a reviewed resolution PR'
                self.store.save(op, 'conflict', 'waiting-for-reviewed-resolution')
                return
            with self.graph(self.policy) as graph:
                graph.restore_candidates(self.prepared_steps(op), self.bundle(op))
                index = next(i for i, step in enumerate(data['steps']) if not step.get('merged_sha'))
                indices = list(range(index, len(data['steps'])))
                # Testing is a separate phase. Every remaining version must pass
                # before opening the next PR or changing any target.
                if not self.test_chain(op, graph, indices):
                    return
                step = data['steps'][index]
                self.stage(op, graph, index)
                self.deployment()
                self.source(self.github.pull(op['pr']), op)
                self.check_resolutions(op)
                if not self.test_chain(op, graph, indices):
                    return
                self.source(self.github.pull(op['pr']), op)
                self.check_resolutions(op)
                pr = self.verify_step(step, graph, index)
                if pr.get('mergeable', True) is None:
                    data['reason'] = 'Waiting for GitHub to calculate PR mergeability'
                    self.store.save(op, 'prepared', 'waiting-for-native-mergeability')
                    return
                require(pr.get('mergeable', True), 'GitHub reports an integration conflict')
                require(all(self.github.branch(s['branch']) == s['base_sha']
                            for s in data['steps'][index:]), 'A remaining target moved; repeat testing')
                self.store.checkpoint()
                self.github.status(self.github.pull(step['pr']), 'Merge check', 'success',
                    'Reviewed source/resolutions; matching version test evidence revalidated')
                data['merge_intent'] = dict(index=index, pr=step['pr'], head=step['review_head'],
                                            base=step['base_sha'], tree=step['tree'],
                                            method='squash' if index == 0 else 'merge',
                                            message=graph.run('show', '-s', '--format=%B', step['after']))
                data['intent'] = True
                self.store.save(op, 'publishing', 'github-pr-merge-intent')
                self.github.merge_pr(step['pr'], step['review_head'], data['merge_intent']['method'],
                                     data['merge_intent']['message'], generated=index > 0)
            self.reconcile(op)
        except PersistenceError:
            raise
        except MergeConflict as error:
            self.expose_conflict(op)
            data['reason'] = str(error)
            self.store.save(op, 'conflict', 'waiting-for-reviewed-resolution')
        except Blocked as error:
            data['reason'] = str(error)
            self.store.save(op, 'uncertain' if data['intent'] else 'blocked', 'blocked')
        except Exception:
            data['reason'] = ('Reconcile the recorded PR merge before continuing' if data['intent'] else
                              'Operation interrupted; resume from its checkpoint')
            self.store.save(op, 'uncertain' if data['intent'] else 'prepared', 'unexpected-error')
            raise

    def reconcile(self, op):
        """GitHub's actual merge commit, never CLI exit status, confirms a hop."""
        data = op['data']
        self.acquire(op)
        require(data.get('merge_intent') and data['intent'], 'No serial PR merge intent to reconcile')
        intent = data['merge_intent']
        step = data['steps'][intent['index']]
        pr = self.github.pull(intent['pr'])
        require(pr['head']['sha'] == intent['head'] and pr['base']['ref'] == step['branch'],
                'PR identity changed while merge outcome was uncertain')
        if not pr.get('merged'):
            data['reason'] = 'PR merge is unconfirmed; inspect GitHub and use retry after resolving protections or queue state'
            self.store.save(op, 'uncertain', 'pr-merge-not-yet-observed')
            return
        actual = sha(pr.get('merge_commit_sha'))
        with self.graph(self.policy) as graph:
            graph.fetch([actual, self.github.branch(step['branch'])])
            parents = graph.parents(actual)
            require(parents and parents[0] == intent['base'], 'Target moved during GitHub merge; inspect the partial integration')
            require(graph.tree(actual) == intent['tree'], 'GitHub merged an unexpected tree; inspect the partial integration')
            require(len(parents) == (1 if intent['method'] == 'squash' else 2), 'Unexpected GitHub merge method')
            require(intent['method'] == 'squash' or parents[1] == intent['head'], 'GitHub did not preserve the verified integration head')
            require(graph.ancestor(actual, self.github.branch(step['branch'])), 'Merged commit is missing from target history')
        step['merged_sha'] = actual
        data['intent'] = False
        data.pop('reason', None)
        data.setdefault('merges', []).append(dict(intent, commit=actual))
        data.pop('merge_intent')
        done = intent['index'] == len(data['steps']) - 1
        self.store.save(op, 'complete' if done else 'prepared', 'github-merge-confirmed')
        if done:
            self.release(op)

    def retry(self, op):
        """The same PR can merge only once. Reconcile first, then recheck every gate."""
        require(op['data']['intent'], 'No PR merge intent to retry')
        self.reconcile(op)
        if not op['data']['intent']:
            return
        pr = self.github.pull(op['data']['merge_intent']['pr'])
        require(pr['state'] == 'open' and not pr.get('auto_merge'),
                'PR is closed or queued; resolve its GitHub state before retrying')
        op['data']['intent'] = False
        self.store.save(op, 'prepared', 'retry-same-pr-after-reconciliation')
        self.advance(op)

    def verify_chain(self):
        with self.graph(self.policy) as graph:
            refs = graph.remote_refs(["refs/heads/" + b for b in self.policy["branches"]])
            heads = {b: sha(refs.get("refs/heads/" + b)) for b in self.policy["branches"]}
            graph.fetch(list(heads.values()))
            for lower, higher in zip(self.policy["branches"], self.policy["branches"][1:]):
                require(graph.ancestor(heads[lower], heads[higher]), "CE branch ancestry is incomplete")
        return heads

    def report(self, op):
        """Reporting is retried independently of successful GitHub merges."""
        data = op['data']
        summary = f'Merge operation `{op["id"]}`: **{op["state"]}**.\n\n'
        summary += data.get('reason', 'Test changes across versions, then create and merge integration PRs in sequence.') + '\n\n'
        url = 'https://github.com/' + self.policy['repository']
        for step in data['steps']:
            summary += f'- `{step["branch"]}`: '
            if step.get('pr'):
                summary += f'[PR #{step["pr"]}]({url}/pull/{step["pr"]}); '
            commit = step.get('merged_sha')
            summary += (f'[merged `{commit}`]({url}/commit/{commit})' if commit else 'awaiting merge')
            summary += '\n'
            if step.get('test_summary'):
                summary += '  ' + step['test_summary'] + '\n'
            if step.get('resolution'):
                summary += f'  Reviewed resolution: [#{step["resolution"]["pr"]}]({url}/pull/{step["resolution"]["pr"]}).\n'
        if 'conflict_index' in data:
            step = data['steps'][data['conflict_index']]
            if step.get('input_bundle'):
                summary += (f'\nConflict input bundle: `journal/bundles/{step["input_bundle"]}` '
                            f'on `{self.policy["state_branch"]}`.\n')
            summary += (f'\nConflict repair: merge exact input `{step["input_head"]}` and target '
                        f'`{step["base_sha"]}` in a resolution PR to `{step["branch"]}`. '
                        'After OCA and current-head approval, run `use-resolution` with the operation ID and resolution PR. '
                        'The repaired chain is rehearsed again before the next merge.\n')
        summary += '\nSerial merges are durable: earlier merges remain if a later PR is blocked.\n'
        key = 'operation-report:' + op['id']
        fingerprint = hashlib.sha256(summary.encode()).hexdigest()
        if self.store.setting(key) == fingerprint:
            return
        self.store.checkpoint()
        conclusion = 'success' if op['state'] == 'complete' else 'neutral'
        reviews = {op['pr']: op['head']}
        reviews.update({s['pr']: s['review_head'] for s in data['steps'] if 'pr' in s})
        for number, head in reviews.items():
            pr = self.github.pull(number)
            self.github.status(pr, 'Merge progress', conclusion, f'Merge operation {op["id"]}: {op["state"]}', candidate=head)
            self.github.comment(pr, 'operation:' + op['id'], summary)
            self.github.advisory_label(pr, False)
        for number in data.get('superseded_prs', []):
            self.github.close_pr(self.github.pull(number))
        self.github.summary(summary)
        self.store.set_setting(key, fingerprint, 'coordinator')

    def tick(self):
        try:
            self.work()
        finally:
            self.store.ensure_healthy()
            for op in self.store.operations():
                self.report(op)

    def work(self):
        operations = self.store.operations()
        require(all(o['data'].get('protocol') == 'forward-only-v1' or o['state'] in ('complete', 'aborted')
                    for o in operations), 'Reconcile or abort legacy operations before cutover')
        owner = self.store.setting('merge-lock')
        if owner:
            pending = next((o for o in operations if o['id'] == owner), None)
            require(pending is not None, 'Unknown CE soft lock owner; investigate state')
            if pending['state'] in ('complete', 'aborted'):
                self.release(pending)
                pending = None
        else:
            pending = next((o for o in operations if o['state'] in ('queued', 'prepared', 'publishing', 'uncertain')), None)
        if pending and pending['state'] in ('queued', 'prepared', 'publishing', 'uncertain', 'conflict'):
            self.advance(pending)
        generated = {s['pr'] for o in operations for s in o['data']['steps'] if s.get('pr')}
        generated.update(o['pr'] for o in operations if o['state'] not in ('complete', 'aborted'))
        generated.update(n for o in operations for n in o['data'].get('superseded_prs', []))
        generated.update(s['resolution']['pr'] for o in operations for s in o['data']['steps'] if s.get('resolution'))
        # Do not advertise another integration while the durable lock is held.
        for pr in self.github.pages('/pulls?state=open'):
            if pr['number'] in generated:
                continue
            if pr['base']['ref'] in self.policy['branches']:
                self.evaluate(self.github.pull(pr['number']))
            else:
                self.publish_unmanaged(pr)
