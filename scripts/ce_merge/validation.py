# Copyright (c) 2026, Oracle and/or its affiliates.
"""Refresh CI through bot-owned PRs, without rewriting contributor branches."""
import hashlib
import json

from .ci import STALE_CI, WORKFLOWS, existing_pr_ci
from .policy import require, sha


def validation_head(graph, base, tree, source):
    # Stable bytes make a crash before/after push recoverable without a bundle.
    # This single-parent snapshot carries no contributor history into the fork.
    raw = (f'tree {sha(tree)}\nparent {sha(base)}\n'
           'author CE Merge Coordinator <ce-merge@localhost> 0 +0000\n'
           'committer CE Merge Coordinator <ce-merge@localhost> 0 +0000\n\n'
           f'CE validation of {sha(source)} against {base}\n')
    return graph.run('hash-object', '-t', 'commit', '-w', '--stdin', input=raw.encode())


def current_merge(pr, graph, tree):
    merge = pr.get('merge_commit_sha')
    if not merge:
        return None
    sha(merge)
    graph.fetch([f'refs/pull/{pr["number"]}/merge'])
    # The API SHA and merge ref may race: missing objects fail closed for this
    # poll; mismatched parents/tree require a newly prepared validation PR.
    if (graph.parents(merge) == [pr['base']['sha'], pr['head']['sha']]
            and graph.tree(merge) == tree):
        return merge
    return None


def candidate_ci(coordinator, pr, graph, tree, paths):
    policy, store, github = coordinator.policy, coordinator.store, coordinator.github
    require(not any('.github/workflows/' + name in paths for name in WORKFLOWS),
            'PR changes a required CI workflow; bootstrap its reviewed update before trusting it')
    identity = [policy['repository'], policy['bot_fork'], pr['number'], pr['base']['ref'],
                pr['base']['sha'], pr['head']['sha'], tree, coordinator.policy_identity()]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    key = 'validation:' + digest
    record = store.setting(key)
    if not record:
        tested = current_merge(pr, graph, tree)
        if tested:
            result = existing_pr_ci(github, policy, pr, tested, paths)
            # Do not duplicate pending/failed CI for an unchanged candidate.
            if STALE_CI not in result[1]:
                return result
        if policy['mode'] != 'active':
            return False, ('Shadow mode: current-target CI needs a bot validation PR; '
                           'active mode prepares it without rebasing the contributor branch')
        require(policy.get('fork_installation_id', 0) > 0,
                'Install the App on the controlled bot fork to prepare current-target CI')
        record = {'key': key, 'operation': 'validate-' + digest, 'source_pr': pr['number'],
                  'source_head': pr['head']['sha'], 'base': pr['base']['sha'],
                  'branch': pr['base']['ref'], 'tree': tree,
                  'head': validation_head(graph, pr['base']['sha'], tree, pr['head']['sha'])}
        # Save intent before the first network mutation. Deterministic ref names
        # and bot_pr's lookup recover an uncertain push or create-PR response.
        store.set_setting(key, record, 'coordinator')
    require(not record.get('retired'), 'Validation PR retired; inspect the current source and target')
    if not record.get('pr'):
        require(policy['mode'] == 'active', 'Shadow mode cannot stage validation PRs')
        head = validation_head(graph, record['base'], record['tree'], record['source_head'])
        require(head == record['head'], 'Validation snapshot identity changed')
        remote = 'https://github.com/' + policy['bot_fork'] + '.git'
        ref = f'refs/heads/upmerge/{record["operation"]}/0'
        current = {line.split()[1]: line.split()[0] for line in
                   graph.run('ls-remote', '--refs', remote, ref).splitlines()}
        require(current.get(ref, head) == head, 'Validation branch changed unexpectedly')
        if ref not in current:
            graph.run('push', '--atomic', '--force-with-lease=' + ref + ':', remote,
                      head + ':' + ref, token=github.token(fork=True))
        record['pr'] = github.bot_pr(record['operation'], 0, head, record['branch'],
                                    pr['number'], validation=True,
                                    detail=f'Source `{record["source_head"]}`; target `{record["base"]}`; tree `{tree}`.')
        store.set_setting(key, record, 'coordinator')
    candidate = github.pull(record['pr'])
    coordinator.basic(candidate)
    require(candidate['head']['repo']['full_name'] == policy['bot_fork'] and
            candidate['head']['sha'] == record['head'] and candidate['base']['sha'] == record['base'] and
            candidate['base']['ref'] == record['branch'],
            'Validation PR changed; wait for a fresh target snapshot')
    link = f'[Validation PR #{record["pr"]}](https://github.com/{policy["repository"]}/pull/{record["pr"]})'
    tested = current_merge(candidate, graph, tree)
    if not tested:
        return False, link + ': waiting for GitHub to generate its merge candidate'
    result = existing_pr_ci(github, policy, candidate, tested, paths)
    return result[0], link + ': ' + result[1]


def retire_validations(coordinator):
    """Close obsolete CI-only PRs; retain their manifests and workflow history."""
    if coordinator.policy['mode'] != 'active':
        return
    for record in coordinator.store.settings('validation:'):
        if record.get('retired') or not record.get('pr'):
            continue
        source = coordinator.github.pull(record['source_pr'])
        if (source['state'] != 'open' or source['head']['sha'] != record['source_head'] or
                source['base']['ref'] != record['branch'] or source['base']['sha'] != record['base']):
            coordinator.github.close_pr(coordinator.github.pull(record['pr']))
            coordinator.store.set_setting(record['key'], dict(record, retired=True), 'coordinator')
