# Copyright (c) 2026, Oracle and/or its affiliates.
"""Consume existing pull-request workflow results. Never dispatch or rerun CI."""
import re
import urllib.parse

from .policy import documentation_only, require, rehearsal_test, sha


STALE_CI = ": waiting for CI on the current PR revision and an accepted target ancestor"


WORKFLOWS = {
    'pr-build.yml': {'Debug build (gcc)': ['Verify PR merge commit', 'Build'],
                     'Debug build (clang)': ['Verify PR merge commit', 'Build']},
    'mtr.yml': {f'MTR ({shard})': ['Verify PR merge commit', 'Build']
                for shard in ('replication', 'storage', 'core', 'services')},
    'clang-format.yml': {'clang-format': ['Verify PR merge commit', 'Check formatting of changed files']},
}


def for_pr(run, pr):
    title = run.get('display_title', '')
    if title.startswith('pr:'):
        return title.split(':')[1:2] == [str(pr['number'])]
    return any(ref.get('number') == pr['number'] for ref in run.get('pull_requests', []))


def tested_revision(run, pr, merge, graph=None, exact_base=False):
    title = run.get('display_title', '')
    def accepted_base(base):
        # Target advancement does not invalidate checks on an unchanged PR head.
        # Still reject unrelated/rewritten targets. Required job verification
        # attests the recorded test merge; mergeability against today's base is
        # checked locally by the coordinator before accepting these results.
        return bool(isinstance(base, str) and re.fullmatch(r'[0-9a-f]{40}', base) and
                    (base == pr['base']['sha'] if exact_base or graph is None else
                     graph.ancestor(base, pr['base']['sha'])))

    if title.startswith('pr:'):
        parts = title.split(':')
        return (len(parts) == 5 and parts[1] == str(pr['number']) and
                accepted_base(parts[2]) and parts[3] == pr['head']['sha'] and
                bool(re.fullmatch(r'[0-9a-f]{40}', parts[4])) and
                (graph is not None or parts[4] == merge))
    # Existing runs predate run-title evidence. Accept their GitHub-provided
    # PR/base/head association only when complete; empty fork metadata blocks.
    for ref in run.get('pull_requests', []):
        if ref.get('number') != pr['number']:
            continue
        if (accepted_base(ref.get('base', {}).get('sha')) and
                ref.get('head', {}).get('sha') == pr['head']['sha'] and
                all(ref.get(side, {}).get('repo', {}).get('id') is not None and
                    ref[side]['repo']['id'] == pr[side]['repo'].get('id') for side in ('base', 'head')) and
                ref['base'].get('ref') == pr['base']['ref']):
            return True
    return False


def checked_jobs(github, run, expected, allow_unselected=False):
    """Validate required jobs using their latest attempt, including partial reruns."""
    jobs = github.pages(f'/actions/runs/{run["id"]}/jobs?filter=all', 'jobs')
    require(all(0 < job['run_attempt'] <= run['run_attempt'] for job in jobs),
            'CI rerun metadata is not yet consistent')
    if allow_unselected:
        known = {name for checks in WORKFLOWS.values() for name in checks}
        require(all(job['name'] in known or job.get('conclusion') == 'skipped' for job in jobs),
                'Unexpected candidate test job')
    else:
        require({job['name'] for job in jobs} == set(expected), 'Incomplete or unexpected CI jobs')
    for name, required in expected.items():
        attempts = [job for job in jobs if job['name'] == name]
        require(attempts, 'Missing CI job: ' + name)
        latest = max(job['run_attempt'] for job in attempts)
        current = [job for job in attempts if job['run_attempt'] == latest]
        require(len(current) == 1, 'Ambiguous job evidence in ' + name)
        job = current[0]
        require(job['status'] == 'completed' and job['conclusion'] == 'success', name + ' did not pass')
        steps = {step['name']: step['conclusion'] for step in job['steps']}
        require(all(steps.get(step) == 'success' for step in required), 'Required CI step did not pass in ' + name)
        yield name, steps


def workflow_runs(github, policy, filename, event, head):
    """Fetch runs bound to the repository's named workflow, event, and revision."""
    path = '.github/workflows/' + filename
    workflow = github.repo('/actions/workflows/' + filename)
    require(workflow.get('path') == path, 'Required workflow identity changed')
    runs = github.pages('/actions/workflows/' + filename + '/runs?event=' + event + '&head_sha=' +
                        urllib.parse.quote(head, safe=''), 'workflow_runs')
    return workflow, [run for run in runs if run.get('event') == event and run.get('head_sha') == head
                      and run.get('path', '').split('@')[0] == path and run.get('workflow_id') == workflow['id']
                      and run.get('repository', {}).get('full_name') == policy['repository']]


def existing_pr_ci(github, policy, pr, merge, paths, graph=None, selected=None, exact_base=False):
    selected = selected if selected is not None else required_checks(paths)
    test = rehearsal_test(policy)
    pending = []
    rehearsal = False
    for filename in selected:
        require('.github/workflows/' + filename not in paths,
                'PR changes a required CI workflow; bootstrap its reviewed update before trusting it')
        workflow, runs = workflow_runs(github, policy, filename, 'pull_request', pr['head']['sha'])
        runs = [run for run in runs if for_pr(run, pr)]
        label = workflow.get('name', filename)
        if not runs:
            pending.append(label + ': waiting for existing PR workflow')
            continue
        run = max(runs, key=lambda r: (r['run_number'], r['id']))
        if not tested_revision(run, pr, merge, graph, exact_base):
            pending.append(label + STALE_CI)
            continue
        if run['status'] != 'completed':
            pending.append(label + ': ' + run['status'])
            continue
        require(run['conclusion'] == 'success', label + ' did not pass; use GitHub Actions to rerun or update the PR')
        scopes = set()
        for name, steps in checked_jobs(github, run, WORKFLOWS[filename]):
            if filename == 'mtr.yml':
                if steps.get('Run MTR') == 'success':
                    scopes.add('full')
                else:
                    require(test and steps.get('Run MTR') == 'skipped' and
                            steps.get('Verify rehearsal test') == 'success' and
                            steps.get(f'Run MTR rehearsal ({test})') == 'success',
                            'MTR evidence does not satisfy the configured test coverage')
                    scopes.add(test)
                if name == 'MTR (services)':
                    require(steps.get('Run unit tests') == 'success', 'Required unit tests did not pass')
        require(len(scopes) <= 1, 'MTR shards mix full-suite and rehearsal coverage')
        rehearsal |= bool(scopes and scopes != {'full'})
    if pending:
        return False, '; '.join(pending)
    prefix = f'REHEARSAL ({test} only per MTR shard): ' if rehearsal else ''
    return True, prefix + 'Existing PR CI passed for the unchanged PR head; current target merges cleanly. CI may cover an earlier target revision.'


def required_checks(paths):
    if documentation_only(paths):
        return []
    result = ['pr-build.yml', 'mtr.yml']
    if any(p == '.clang-format' or p.endswith(('.c', '.cc', '.cpp', '.h', '.hpp')) for p in paths):
        result.append('clang-format.yml')
    return result


def validation_head(graph, base, tree, source):
    # Stable local snapshots inspect the merged tree without moving any branch.
    # These temporary commits are never pushed or used to create a PR.
    raw = (f'tree {sha(tree)}\nparent {sha(base)}\n'
           'author CE Merge Coordinator <ce-merge@localhost> 0 +0000\n'
           'committer CE Merge Coordinator <ce-merge@localhost> 0 +0000\n\n'
           f'CE validation of {sha(source)} against {base}\n')
    return graph.run('hash-object', '-t', 'commit', '-w', '--stdin', input=raw.encode())
