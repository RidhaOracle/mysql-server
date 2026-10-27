# Copyright (c) 2026, Oracle and/or its affiliates.
"""Read-only runner verification before executing any candidate build code."""
import json
import os
from pathlib import Path
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
    state = env('MERGE_STATE', r'[0-9a-f]{40}')
    name = env('MERGE_BUNDLE', r'[0-9a-f]{32}(?:-[0-9]+)?\.bundle')
    env('MERGE_MTR_TEST', r'(?:main\.[A-Za-z0-9_-]+)?')
    checks = json.loads(os.environ['MERGE_CHECKS'])
    if not isinstance(checks, list) or not checks or len(checks) != len(set(checks)) or not set(checks) <= {
            'pr-build.yml', 'mtr.yml', 'clang-format.yml'}:
        raise ValueError('Invalid requested checks')
    suites = json.loads(os.environ['MERGE_SUITES'])
    if not isinstance(suites, dict) or set(suites) != {'replication', 'storage', 'core', 'services'} or not all(
            isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*', v) for v in suites.values()):
        raise ValueError('Invalid suite profile')
    if git('-C', '../trusted', 'rev-parse', 'HEAD') != base:
        raise ValueError('Version test scripts do not match target snapshot')
    if git('-C', '../state', 'rev-parse', 'HEAD') != state:
        raise ValueError('Public state checkpoint changed')
    bundle = Path('../state/journal/bundles') / name
    if bundle.is_symlink() or not bundle.is_file():
        raise ValueError('Missing public candidate bundle')
    recorded = git('-C', '../state', 'rev-parse', 'HEAD:journal/bundles/' + name)
    if git('hash-object', str(bundle)) != recorded:
        raise ValueError('Public bundle changed after checkout')
    if candidate not in {line.split()[0] for line in git('bundle', 'list-heads', str(bundle)).splitlines()}:
        raise ValueError('Candidate is not a saved bundle head')
    prerequisites = []
    with bundle.open('rb') as stream:
        if stream.readline() not in (b'# v2 git bundle\n', b'# v3 git bundle\n'):
            raise ValueError('Invalid candidate bundle header')
        for line in stream:
            if line == b'\n':
                break
            if line.startswith(b'-'):
                prerequisite = line[1:].split(b' ', 1)[0].decode('ascii')
                if not re.fullmatch(r'[0-9a-f]{40}', prerequisite):
                    raise ValueError('Invalid bundle prerequisite')
                prerequisites.append(prerequisite)
    if prerequisites:
        git('fetch', '--no-tags', '--depth=1', 'origin', *prerequisites)
    git('fetch', '--no-tags', str(bundle), 'refs/ce-candidates/*:refs/ce-candidates/*')
    if git('rev-parse', candidate + '^{tree}') != tree:
        raise ValueError('Candidate identity or file tree changed')
    git('merge-base', '--is-ancestor', base, candidate)
    git('checkout', '--detach', candidate)


if __name__ == '__main__':
    verify()
