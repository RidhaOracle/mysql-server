# Copyright (c) 2026, Oracle and/or its affiliates.
"""One bounded Actions reconciliation pass; credentials come from CE_MERGE_TOKEN."""
import argparse
import hashlib
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile

from .coordinator import Coordinator
from .github import GitHub, authorization_identity
from .journal import PersistenceError
from .policy import Blocked, load, require, resolution_number, rulesets
from .state import GitJournal


COMMANDS = ("once", "init-state", "status", "audit", "snapshot", "rulesets", "verify-deployment",
            "attest-deployment", "resume", "abort", "reconcile", "retry",
            "use-resolution", "refresh-checks", "hold", "unhold")


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Event timestamp must include a timezone")
    return parsed


def authorize(payload, coordinator):
    """Only newly created, live, unedited /integrate comments can authorize a revision."""
    policy, store, github = coordinator.policy, coordinator.store, coordinator.github
    require(payload.get("action") == "created" and payload.get("issue", {}).get("pull_request"),
            "Integration requires a newly created PR comment")
    require(payload.get("repository", {}).get("full_name") == policy["repository"], "Wrong repository")
    event = payload["comment"]
    require(event.get("body", "").strip() == "/integrate", "Not an integration command")
    live = github.repo(f'/issues/comments/{int(event["id"])}')
    require(live["body"] == event["body"] and live["created_at"] == live["updated_at"]
            and live["created_at"] == event["created_at"] and live["user"]["id"] == event["user"]["id"],
            "Integration command changed after submission")
    require(live["issue_url"].endswith(f'/repos/{policy["repository"]}/issues/{int(payload["issue"]["number"])}'),
            "Comment belongs to another PR")
    actor = live["user"]["login"]
    require(github.can_integrate(actor), "Only configured maintainers may authorize integration")
    # An exact replay returns the original operation, including after completion.
    identity = hashlib.sha256(str(event["id"]).encode()).hexdigest()[:32]
    existing = next((o for o in store.operations() if o["id"] == identity), None)
    if existing:
        return existing["id"]
    pr = github.pull(int(payload["issue"]["number"]))
    ready = store.setting(f'ready:{pr["number"]}')
    require(ready and ready["eligible"] and ready["identity"] == authorization_identity(pr)
            and ready["policy"] == coordinator.policy_identity()
            and timestamp(ready["created_at"]) < timestamp(live["created_at"]),
            "No matching readiness evidence predates this command; wait for readiness and post /integrate again")
    coordinator.source(pr)
    return store.request(event["id"], pr, actor)


def operate(coordinator, command, args, actor):
    store = coordinator.store
    if command == "once":
        coordinator.tick()
    elif command == "init-state":
        print("Public state branch is initialized")
    elif command == "status":
        value = {"repository": coordinator.policy["repository"], "hold": store.setting("hold"),
                 "merge_lock": store.setting("merge-lock"), "operations": store.operations()}
        print(json.dumps(value, indent=2))
        coordinator.github.summary("```json\n" + json.dumps(value, indent=2) + "\n```")
    elif command == "audit":
        path = store.path / "audit.jsonl"
        print(path.read_text() if path.exists() else "", end="")
    elif command == "attest-deployment":
        # The workflow accepts only this reviewed default-branch file, never a user-supplied path.
        coordinator.attest_deployment(json.loads(Path(".github/ce-merge-rulesets.json").read_text()), actor)
    elif command == "verify-deployment":
        coordinator.deployment()
        print("CE settings and rulesets match deployment policy")
    elif command == "snapshot":
        require(not store.uncertain() and not store.setting("hold") and not store.setting("merge-lock"), "Publication is uncertain or on hold")
        print(json.dumps({"repository": coordinator.policy["repository"], "heads": coordinator.verify_chain()}, indent=2))
    elif command in ("hold", "unhold"):
        require(not store.uncertain(), "Reconcile uncertain publication before changing holds")
        require(args.record.strip(), "A public operator evidence record is required")
        if command == "unhold" and not store.setting("merge-lock"):
            coordinator.verify_chain()
        store.set_setting("hold", {"reason": args.record} if command == "hold" else None, actor)
    else:
        op = next((o for o in store.operations() if o["id"] == args.operation), None)
        require(op, "Unknown operation")
        if command == "use-resolution":
            coordinator.repair(op, resolution_number(args.pr, coordinator.policy['repository']))
        elif command == 'refresh-checks':
            require(not op['data']['intent'] and op['state'] not in ('complete', 'aborted'),
                    'Reconcile published or uncertain operations before refreshing checks')
            pending = [i for i, step in enumerate(op['data']['steps']) if not step.get('merged_sha')]
            require(pending, 'No prepared PRs to refresh')
            for index in pending:
                step = op['data']['steps'][index]
                step.pop('test_request', None)
                step['test_attempt'] = step.get('test_attempt', 0) + 1
                step['fresh_tests'] = True
            store.save(op, 'prepared', 'fresh-checks-requested:' + actor)
        elif command == "reconcile":
            require(op["data"]["intent"], "No publication intent")
            coordinator.reconcile(op)
        elif command == "retry":
            coordinator.retry(op)
        else:
            require(not op["data"]["intent"] and op["state"] != "complete", "Reconcile published or uncertain operations")
            require(command != 'abort' or not any(s.get('merged_sha') for s in op['data']['steps']),
                    'Serial integration already started; resume propagation instead of aborting published history')
            state = "aborted" if command == "abort" else ("prepared" if op["data"]["steps"] else "queued")
            store.save(op, state, command + ":" + actor)
            if command == "abort":
                coordinator.release(op)
                store.set_setting(f'published:{op["pr"]}', None, actor)
                store.set_setting(f'ready:{op["pr"]}', None, actor)
        coordinator.report(op)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=".github/ce-merge-policy.json")
    parser.add_argument("command", choices=COMMANDS, nargs="?", default="once")
    parser.add_argument("--operation", default="")
    parser.add_argument("--pr", default="")
    parser.add_argument("--record", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy = load(args.policy)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()) if os.environ.get("GITHUB_EVENT_PATH") else {}
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event:
        require(os.environ.get("GITHUB_REPOSITORY") == policy["repository"], "Workflow repository differs from policy")
    if event_name == "workflow_dispatch":
        require(os.environ.get("GITHUB_REF") == "refs/heads/" + event["repository"]["default_branch"],
                "Manual operations must use the trusted default branch")
        inputs = event.get("inputs", {})
        args.command = inputs.get("command", "once")
        require(args.command in COMMANDS, "Unknown operator command")
        args.operation, args.pr, args.record = inputs.get("operation", ""), inputs.get("pr", ""), inputs.get("record", "")
    if args.command == "rulesets" and event_name != "workflow_dispatch":
        print(json.dumps({"repository": {"allow_merge_commit": True, "allow_squash_merge": True,
                                        "allow_rebase_merge": False, "allow_auto_merge": False},
                          "rulesets": rulesets(policy)}, indent=2))
        return
    github = GitHub(policy)
    github.verify_identity()
    actor = event.get("sender", {}).get("login") or os.environ.get("GITHUB_ACTOR", "")
    if args.command != "once" or event_name == "workflow_dispatch":
        require(actor and github.can_integrate(actor), "Operator commands require a configured maintainer")
    if args.command == "rulesets":
        print(json.dumps({"repository": {"allow_merge_commit": True, "allow_squash_merge": True,
                                        "allow_rebase_merge": False, "allow_auto_merge": False},
                          "rulesets": rulesets(policy)}, indent=2))
        return
    if not os.environ.get("CE_GIT_CACHE"):
        os.environ["CE_GIT_CACHE"] = str(Path(tempfile.gettempdir()) / ("ce-public-" + policy["repository"].replace("/", "-") + ".git"))
    logging.info("Public Git cache: %s", os.environ["CE_GIT_CACHE"])
    with tempfile.TemporaryDirectory(prefix="ce-state-") as directory:
        store = GitJournal(directory, policy, github.token(), initialize=args.command == "init-state")
        coordinator = Coordinator(policy, store, github)
        if event_name == "issue_comment":
            authorize(event, coordinator)
        operate(coordinator, args.command, args, actor)


if __name__ == "__main__":
    try:
        main()
    except (Blocked, PersistenceError) as error:
        raise SystemExit(str(error)) from None
