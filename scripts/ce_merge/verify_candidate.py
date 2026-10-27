# Copyright (c) 2026, Oracle and/or its affiliates.
"""Read-only runner verification before executing any candidate build code."""
import json
import os
import re
import subprocess


def verify():
    def env(name, pattern):
        value = os.environ[name]
        if not re.fullmatch(pattern, value):
            raise ValueError('Invalid ' + name)
        return value

    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', *args],
                                       env=dict(os.environ, GIT_NO_REPLACE_OBJECTS='1')).decode().strip()

    env('MERGE_REQUEST', r'[0-9a-f]{64}')
    candidate = env('MERGE_CANDIDATE', r'[0-9a-f]{40}')
    base = env('MERGE_BASE', r'[0-9a-f]{40}')
    tree = env('MERGE_TREE', r'[0-9a-f]{40}')
    env('MERGE_MTR_TEST', r'(?:main\.[A-Za-z0-9_-]+)?')
    checks = json.loads(os.environ['MERGE_CHECKS'])
    if not isinstance(checks, list) or not checks or len(checks) != len(set(checks)) or not set(checks) <= {
            'pr-build.yml', 'mtr.yml', 'clang-format.yml'}:
        raise ValueError('Invalid requested checks')
    suites = json.loads(os.environ['MERGE_SUITES'])
    if not isinstance(suites, dict) or set(suites) != {'replication', 'storage', 'core', 'services'} or not all(
            isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*', v) for v in suites.values()):
        raise ValueError('Invalid suite profile')
    if git('rev-parse', 'HEAD') != candidate or git('rev-parse', 'HEAD^{tree}') != tree:
        raise ValueError('Candidate identity or file tree changed')
    git('merge-base', '--is-ancestor', base, candidate)
    if git('-C', '../trusted', 'rev-parse', 'HEAD') != base:
        raise ValueError('Version test scripts do not match target snapshot')


if __name__ == '__main__':
    verify()
