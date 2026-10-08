# Copyright (c) 2026, Oracle and/or its affiliates.
"""Prepare local candidates and read CI on the existing PR."""
from .ci import WORKFLOWS, existing_pr_ci
from .policy import require, sha


def validation_head(graph, base, tree, source):
    # Stable local snapshots inspect the merged tree without moving any branch.
    # These temporary commits are never pushed or used to create a PR.
    raw = (f'tree {sha(tree)}\nparent {sha(base)}\n'
           'author CE Merge Coordinator <ce-merge@localhost> 0 +0000\n'
           'committer CE Merge Coordinator <ce-merge@localhost> 0 +0000\n\n'
           f'CE validation of {sha(source)} against {base}\n')
    return graph.run('hash-object', '-t', 'commit', '-w', '--stdin', input=raw.encode())


def candidate_ci(coordinator, pr, graph, tree, paths):
    """Read CI from the original PR only; never create validation branches or PRs."""
    require(not any('.github/workflows/' + name in paths for name in WORKFLOWS),
            'PR changes a required CI workflow; bootstrap its reviewed update before trusting it')
    require(graph.merge_tree(pr['base']['sha'], pr['head']['sha']) == tree,
            'Candidate differs from the current clean merge of the reviewed PR')
    return existing_pr_ci(coordinator.github, coordinator.policy, pr,
                          pr.get('merge_commit_sha'), paths, graph=graph)
