# Copyright (c) 2026, Oracle and/or its affiliates.
"""A single Actions run: prepare, test every version, then publish in order."""
import argparse
import json
import os
import re
from pathlib import Path

from .git import PublicGraph
from .github import GitHub, authorization_identity
from .policy import Blocked, require, load, sha


SUITES = {
    'replication': 'binlog,binlog_gtid,binlog_nogtid,clone,federated,group_replication,rpl,rpl_gtid,rpl_nogtid',
    'storage': 'encryption,innodb,innodb_fts,innodb_gis,innodb_undo,innodb_zip,parts',
    'core': 'auth_sec,collations,component_connection_control,component_keyring_file,connection_control,funcs_2,gcol,gis,information_schema,interactive_utilities,jdv,json,main,opt_trace,query_rewrite_plugins,x',
    'services': 'perfschema,router,secondary_engine,service_status_var_registration,service_sys_var_registration,service_udf_registration,sys_vars,sysschema,test_service_sql_api,test_services',
}


def source(github, policy, number, identity=None, merged=False):
    pr = github.pull(number)
    require(pr['base']['repo']['full_name'] == policy['repository'], 'Wrong repository')
    require(pr['base']['ref'] in policy['branches'], 'Unsupported target branch')
    require(pr.get('merged') if merged else pr['state'] == 'open' and not pr['draft'],
            'Source PR is not in the expected state')
    require(identity is None or authorization_identity(pr) == identity, 'Source PR changed; start a new run')
    body = re.sub(r'<!--.*?-->', '', pr.get('body') or '', flags=re.S)
    require(not re.search(r'^\s*(?:Resolution-PR|Merge-Through|Merge-Reason|Upmerge[^:]*|Backport[^:]*):',
                          body, re.M | re.I), 'Remove obsolete merge metadata; this pipeline uses only the fixed branch chain')
    github.oca(pr)
    github.reviewed(pr)
    return pr


def authorize(github, policy, event):
    require(os.environ['GITHUB_REPOSITORY'] == policy['repository'], 'Wrong workflow repository')
    require(event.get('action') == 'created' and event.get('issue', {}).get('pull_request'),
            'A new PR comment is required')
    comment = event['comment']
    require(comment['body'].strip() == '/integrate', 'Expected /integrate')
    live = github.repo(f'/issues/comments/{int(comment["id"])}')
    require(live['body'] == comment['body'] and live['created_at'] == live['updated_at']
            and live['created_at'] == comment['created_at'] and live['user']['id'] == comment['user']['id'],
            'Integration comment changed')
    require(live['issue_url'].endswith(f'/repos/{policy["repository"]}/issues/{event["issue"]["number"]}'),
            'Wrong PR comment')
    require(github.can_integrate(live['user']['login']), 'Only a configured maintainer may integrate')
    return int(event['issue']['number']), live['user']['login']


def prepare(github, policy, number, directory, actor):
    pr = source(github, policy, number)
    chain = policy['branches']
    with PublicGraph(policy) as graph:
        heads = {b: sha(github.branch(b)) for b in chain}
        graph.fetch(list(heads.values()) + [f'refs/pull/{number}/head'])
        require(graph.run('rev-parse', graph.cache_ref(f'refs/pull/{number}/head')) == pr['head']['sha'],
                'PR changed while preparing')
        for lower, higher in zip(chain, chain[1:]):
            require(graph.ancestor(heads[lower], heads[higher]), 'Version branches need forward merging first')
        steps = [{'branch': b, 'base_sha': heads[b]} for b in chain[chain.index(pr['base']['ref']):]]
        graph.candidates(steps, pr['head']['sha'], str(number))
        require(steps[0]['tree'] != graph.tree(steps[0]['base_sha']), 'Source has no changes')
        graph.export_candidates(steps, directory / 'candidates.bundle')
    for step in steps:
        step['suites'] = policy.get('ci_suites', {}).get(step['branch'], SUITES)
    plan = dict(pr=number, actor=actor, identity=authorization_identity(pr), head=pr['head']['sha'],
                title=pr['title'], steps=steps)
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2))
    return steps


def publish(github, policy, plan):
    """Publish once; never retry an uncertain write automatically."""
    require(github.can_integrate(plan['actor']), 'Integration maintainer no longer has access')
    source(github, policy, plan['pr'], plan['identity'])
    steps = plan['steps']
    require(all(github.branch(s['branch']) == s['base_sha'] for s in steps),
            'A target moved during testing; rerun the whole pipeline')
    previous = None
    with PublicGraph(policy) as graph:
        for index, step in enumerate(steps):
            source(github, policy, plan['pr'], plan['identity'], merged=index > 0)
            require(github.branch(step['branch']) == step['base_sha'], 'Target moved; stop publication')
            if index == 0:
                number, head, method = plan['pr'], plan['head'], 'squash'
            else:
                lower = steps[index - 1]['branch']
                require(github.branch(lower) == previous, 'Lower branch moved; stop publication')
                graph.fetch([step['base_sha'], previous])
                require(graph.merge_tree(step['base_sha'], previous) == step['tree'],
                        'Actual forward merge differs from tested files')
                number = github.bot_pr(os.environ['GITHUB_RUN_ID'], lower, step['branch'], plan['pr'])
                head, method = previous, 'merge'
                pr = github.pull(number)
                require(pr['user']['id'] == policy['bot_user_id']
                        and pr['head']['repo']['full_name'] == policy['repository']
                        and pr['head']['ref'] == lower and pr['head']['sha'] == head
                        and pr['base']['ref'] == step['branch'] and pr['base']['sha'] == step['base_sha'],
                        'Forward merge PR changed')
                github.reviewed(pr, require_approval=False)
            github.status(github.pull(number), 'Merge check', 'success',
                          'All version jobs passed in this pipeline run', candidate=head)
            github.merge_pr(number, head, method, plan['title'], generated=index > 0)
            pr = github.pull(number)
            require(pr.get('merged'), 'Merge outcome unconfirmed; inspect GitHub before rerunning')
            actual = sha(pr.get('merge_commit_sha'))
            graph.fetch([actual, github.branch(step['branch'])])
            parents = graph.parents(actual)
            require(parents == ([step['base_sha']] if index == 0 else [step['base_sha'], previous])
                    and graph.tree(actual) == step['tree']
                    and graph.ancestor(actual, github.branch(step['branch'])),
                    'Unexpected merged result; stop publication')
            previous = actual
            github.summary(f'Merged #{number} into `{step["branch"]}`: `{actual}`')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'publish'])
    parser.add_argument('--directory', default='pipeline')
    args = parser.parse_args()
    policy = load('.github/ce-merge-policy.json')
    github = GitHub(policy)
    github.verify_identity()
    directory = Path(args.directory)
    if args.phase == 'prepare':
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        number, actor = authorize(github, policy, event)
        github.comment(github.pull(number), 'pipeline',
                       'Integration pipeline: ' + os.environ['GITHUB_SERVER_URL'] + '/'
                       + policy['repository'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID']
                       + '\nAll affected versions must pass before merging. Conflicts or moved branches stop the run.')
        directory.mkdir(parents=True, exist_ok=True)
        steps = prepare(github, policy, number, directory, actor)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('versions=' + json.dumps(steps) + '\n')
    else:
        publish(github, policy, json.loads((directory / 'plan.json').read_text()))


if __name__ == '__main__':
    try:
        main()
    except Blocked as error:
        raise SystemExit(str(error)) from None
