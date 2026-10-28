# Copyright (c) 2026, Oracle and/or its affiliates.
"""Pinned candidate tests, and per-check reuse of ordinary resolution PR CI."""
import hashlib
import json
import re

from .ci import WORKFLOWS, checked_jobs, existing_pr_ci, required_checks, workflow_runs
from .policy import Blocked, documentation_only, rehearsal_test, require, sha


WORKFLOW = 'merge-version-tests.yml'
SUITES = {
    'replication': 'binlog,binlog_gtid,binlog_nogtid,clone,federated,group_replication,rpl,rpl_gtid,rpl_nogtid',
    'storage': 'encryption,innodb,innodb_fts,innodb_gis,innodb_undo,innodb_zip,parts',
    'core': 'auth_sec,collations,component_connection_control,component_keyring_file,connection_control,funcs_2,gcol,gis,information_schema,interactive_utilities,jdv,json,main,opt_trace,query_rewrite_plugins,x',
    'services': 'perfschema,router,secondary_engine,service_status_var_registration,service_sys_var_registration,service_udf_registration,sys_vars,sysschema,test_service_sql_api,test_services',
}


def profile(policy, branch):
    suites = policy.get('ci_suites', {}).get(branch, SUITES)
    require(isinstance(suites, dict) and set(suites) == set(SUITES), 'Version test profile needs all four MTR shards')
    require(all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*', v)
                for v in suites.values()), 'Invalid version MTR suite list')
    return suites


def matching_pr_mtr_profile(graph, base, suites):
    """Conservatively recognize the trusted ordinary workflow's suite coverage.

    Unknown YAML layouts are not guessed: candidate testing supplies the proof.
    """
    try:
        workflow = graph.run('show', base + ':.github/workflows/mtr.yml')
    except Blocked:
        return False
    rows = re.findall(r'^\s*- shard: ([A-Za-z0-9_]+)\s*\n\s+suites: ([A-Za-z0-9_,]+)\s*$', workflow, re.M)
    return len(rows) == len(SUITES) and dict(rows) == suites


def validate_run(github, policy, record):
    """Read GitHub-owned run/job metadata, never a downloaded candidate artifact."""
    _, runs = workflow_runs(github, policy, WORKFLOW, 'workflow_dispatch', record['trusted'])
    runs = [r for r in runs if r.get('display_title') == 'merge-test:' + record['request']
            and r.get('actor', {}).get('id') == policy['bot_user_id']]
    if not runs:
        return False, 'Waiting for candidate test workflow; use refresh-checks if dispatch was interrupted'
    run = max(runs, key=lambda r: (r['run_number'], r['id']))
    if run['status'] != 'completed':
        return False, 'Candidate tests: ' + run['status']
    require(run['conclusion'] == 'success', 'Candidate tests did not pass')
    # The same trusted default-branch workflow may be manually dispatched by a
    # different actor, but that run cannot satisfy a bot-created request.
    expected = {name: ['Verify candidate' if s == 'Verify PR merge commit' else s for s in steps]
                for filename in record['checks'] for name, steps in WORKFLOWS[filename].items()}
    for name, outcomes in checked_jobs(github, run, expected, allow_unselected=True):
        if name.startswith('MTR ('):
            if record['mtr_test']:
                require(outcomes.get('Verify rehearsal test') == 'success'
                        and outcomes.get('Run MTR rehearsal') == 'success', 'Missing MTR rehearsal coverage')
            else:
                require(outcomes.get('Run MTR') == 'success', 'Missing full MTR coverage')
            if name == 'MTR (services)':
                require(outcomes.get('Run unit tests') == 'success', 'Missing candidate unit tests')
    return True, f'Candidate tests passed: https://github.com/{policy["repository"]}/actions/runs/{run["id"]}'


def candidate_tests(coordinator, op, graph, index):
    """Reuse individual equivalent PR checks; dispatch only missing checks."""
    step = op['data']['steps'][index]
    paths = graph.paths(step['base_sha'], step['after'])
    if not paths:
        source = op['data']['steps'][0]
        if documentation_only(graph.paths(source['base_sha'], source['after'])):
            return True, 'Documentation-only history propagation; target tree unchanged'
    required = required_checks(paths)
    require(not any('.github/workflows/' + p in paths for p in (*WORKFLOWS, WORKFLOW)),
            'Required workflow changes need a reviewed bootstrap before integration')
    suites = profile(coordinator.policy, step['branch'])
    missing = []
    number = op['pr'] if index == 0 else step.get('resolution', {}).get('pr')
    pr = coordinator.github.pull(number) if number else None
    equivalent = bool(pr and pr['base']['ref'] == step['branch'] and pr['base']['sha'] == step['base_sha'])
    if equivalent:
        graph.fetch([f'refs/pull/{number}/head'])
        equivalent = graph.merge_tree(step['base_sha'], pr['head']['sha']) == step['tree']
    mtr_matches = equivalent and suites == SUITES and matching_pr_mtr_profile(graph, step['base_sha'], suites)
    for filename in required:
        ok = False
        if equivalent and not step.get('fresh_tests') and (filename != 'mtr.yml' or mtr_matches):
            # A failed existing run raises; never hide it behind a fresh run.
            ok, _ = existing_pr_ci(coordinator.github, coordinator.policy, pr, pr.get('merge_commit_sha'),
                                   paths, graph=graph, selected=[filename], exact_base=True)
        if not ok:
            missing.append(filename)
    if not missing:
        return True, 'Required tests satisfied by existing PR CI' if required else 'Documentation-only exemption'

    github, store = coordinator.github, coordinator.store
    default = github.repo('')['default_branch']
    trusted = sha(github.branch(default))
    identity = dict(policy=coordinator.policy_identity(), operation=op['id'], index=index,
                    base=step['base_sha'], tree=step['tree'], trusted=trusted, suites=suites,
                    mtr_test=rehearsal_test(coordinator.policy), checks=missing,
                    attempt=step.get('test_attempt', 0))
    request = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    record = step.get('test_request')
    if not record or record['request'] != request:
        record = dict(identity, request=request, candidate=step['after'], bundle=coordinator.bundle(op).name)
        step['test_request'] = record
        store.save(op, 'prepared', 'candidate-test-staging-intent')
        record['state'] = sha(store.head)
        # This checkpoint precedes dispatch. An uncertain response is reconciled
        # by request ID; refresh-checks explicitly creates a new attempt if needed.
        record['dispatched'] = True
        store.save(op, 'prepared', 'candidate-test-dispatch-intent')
        github.repo('/actions/workflows/' + WORKFLOW + '/dispatches', 'POST', {
            'ref': default, 'inputs': {
                'request': request, 'candidate': record['candidate'], 'base': record['base'],
                'tree': record['tree'], 'checks': json.dumps(missing),
                'suites': json.dumps(suites), 'mtr_test': record['mtr_test'],
                'state': record['state'], 'bundle': record['bundle']}})
    elif not record.get('dispatched'):
        # A runner interrupted between staging intent and dispatch has made no
        # uncertain dispatch. Recreate the request on the next bounded pass.
        step.pop('test_request')
        return False, 'Candidate staging interrupted; retrying next pass'
    return validate_run(github, coordinator.policy, record)
