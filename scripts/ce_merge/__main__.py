# Copyright (c) 2026, Oracle and/or its affiliates.
"""Operator CLI and signed webhook endpoint. Secrets come only from the environment."""
import argparse
import hashlib
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .coordinator import Coordinator
from .github import GitHub, authorization_identity
from .policy import Blocked, load, require, rulesets, sha
from .journal import Journal


def authorize(payload, delivery, policy, store, github):
    require(policy["mode"] == "active", "Shadow mode cannot authorize merges")
    require(payload.get("action") == "requested_action" and
            payload.get("requested_action", {}).get("identifier") == "integrate",
            "Not an integration request")
    require(payload.get("repository", {}).get("full_name") == policy["repository"] and
            payload.get("installation", {}).get("id") == policy["installation_id"] and
            payload.get("check_run", {}).get("app", {}).get("id") == policy["app_id"],
            "Webhook source does not match deployment")
    actor = payload["sender"]["login"]
    require(github.can_integrate(actor), "Only configured maintainers may authorize integration")
    check = payload["check_run"]
    require(check.get("name") == "CE / policy" and check.get("conclusion") == "success",
            "Integration requires a successful App policy check")
    identity = check.get("external_id", "")
    number = identity.split(":", 1)[0]
    require(number.isdecimal(), "Invalid integration action identity")
    pr = github.pull(int(number))
    require(check["head_sha"] == pr["head"]["sha"] and authorization_identity(pr) == identity,
            "PR target, revision, or propagation instructions changed after the action was offered")
    require(pr["state"] == "open" and not pr["draft"], "PR is not ready")
    return store.request(check["id"], pr, actor)


def signed(secret, body, signature):
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def serve(coordinator, host, port):
    secret = os.environ["CE_WEBHOOK_SECRET"]
    require(len(secret) >= 32, "Webhook secret must be at least 32 characters")
    wake = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log webhook payloads or untrusted request paths.

        def do_POST(self):
            try:
                require(self.path == "/webhook", "Unknown route")
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= 2_000_000, "Invalid webhook size")
                self.connection.settimeout(10)
                body = self.rfile.read(size)
                require(signed(secret, body, self.headers.get("X-Hub-Signature-256")), "Invalid signature")
                payload = json.loads(body)
                event = self.headers.get("X-GitHub-Event")
                if event == "check_run" and payload.get("action") == "requested_action":
                    delivery = self.headers.get("X-GitHub-Delivery", "")
                    require(0 < len(delivery) < 200, "Missing delivery identity")
                    authorize(payload, delivery, coordinator.policy, coordinator.store, coordinator.github)
                    wake.set()
                elif event in ("pull_request", "pull_request_review", "workflow_run"):
                    wake.set()
                self.send_response(202)
            except (Blocked, ValueError, KeyError):
                self.send_response(400)
            except Exception:
                logging.exception("Webhook processing failed")
                self.send_response(503)
            self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            try:
                with coordinator.store.exclusive():
                    coordinator.tick()
            except BlockingIOError:
                pass
            except Exception:
                logging.exception("Coordinator tick failed; publication intent retained for receipt reconciliation")
            wake.wait(coordinator.policy.get("poll_seconds", 60))
            wake.clear()
    finally:
        server.shutdown()
        server.server_close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=".github/ce-merge-policy.json")
    parser.add_argument("--state-dir", default="/var/lib/mysql-ce-merge/journal")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("rulesets", "verify-deployment", "once", "status", "audit", "snapshot"):
        commands.add_parser(name)
    server = commands.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    hold = commands.add_parser("hold")
    hold.add_argument("--reason", required=True)
    hold.add_argument("--release-tag")
    hold.add_argument("--target", action="append", default=[])
    unhold = commands.add_parser("unhold")
    unhold.add_argument("--validation-record", required=True)
    unhold.add_argument("--sync-record", required=True)
    for name in ("resume", "reconcile", "retry", "abort"):
        commands.add_parser(name).add_argument("operation")
    repair = commands.add_parser("repair-upmerge")
    repair.add_argument("operation")
    repair.add_argument("--pr", required=True, type=int)
    rejected = commands.add_parser("resolve-rejected")
    rejected.add_argument("operation")
    rejected.add_argument("--fencing-record", required=True,
                          help="Evidence that the old push process/token can no longer complete")
    commands.add_parser("rerun-ci").add_argument("run_id", type=int)
    for name in ("register-promotion", "publish-promotion"):
        commands.add_parser(name).add_argument("manifest")
    args = parser.parse_args()
    policy = load(args.policy)
    if args.command == "rulesets":
        print(json.dumps({"repository": {"allow_merge_commit": True, "allow_squash_merge": True,
                                        "allow_rebase_merge": False, "allow_auto_merge": False},
                          "rulesets": rulesets(policy)}, indent=2))
        return
    store, github = Journal(args.state_dir), GitHub(policy)
    coordinator = Coordinator(policy, store, github)
    operator = str(os.getuid())
    if args.command == "serve":
        serve(coordinator, args.host, args.port)
        return
    with store.exclusive():
        if args.command == "once":
            coordinator.tick()
        elif args.command == "verify-deployment":
            coordinator.deployment()
            print("CE repository settings and rulesets match deployment policy")
        elif args.command == "status":
            print(json.dumps({"mode": policy["mode"], "hold": store.setting("hold"),
                              "operations": store.operations()}, indent=2))
        elif args.command == "audit":
            path = store.path / "audit.jsonl"
            print(path.read_text() if path.exists() else "", end="")
        elif args.command in ("resume", "reconcile", "retry", "abort", "resolve-rejected", "repair-upmerge"):
            op = next((o for o in store.operations() if o["id"] == args.operation), None)
            require(op, "Unknown operation")
            if args.command == "repair-upmerge":
                coordinator.repair(op, args.pr)
            elif args.command == "reconcile":
                require(op["data"]["intent"], "No publication intent")
                coordinator.reconcile(op)
            elif args.command == "retry":
                coordinator.retry(op)
            elif args.command == "resolve-rejected":
                require(op["data"]["intent"] and args.fencing_record.strip(), "Fenced publication intent is required")
                coordinator.reconcile(op)
                require(op["state"] != "complete", "Publication succeeded; preserve all published history")
                with coordinator.graph(policy) as graph:
                    heads = graph.remote_refs(["refs/heads/" + s["branch"] for s in op["data"]["steps"]])
                    require(all(heads.get("refs/heads/" + s["branch"]) == s["base_sha"] for s in op["data"]["steps"]),
                            "Target state differs from the original snapshot; investigate externally")
                store.audit(op["id"], "publication-fenced", {"operator": operator, "record": args.fencing_record})
                op["data"]["intent"] = False
                store.save(op, "aborted", "rejected-transaction-confirmed")
                store.set_setting(f'published:{op["pr"]}', None, operator)
            else:
                require(not op["data"]["intent"] and op["state"] != "complete",
                        "Published or uncertain operations must be reconciled")
                state = "aborted" if args.command == "abort" else ("prepared" if op["data"]["steps"] else "queued")
                store.save(op, state, "operator-" + args.command + ":" + operator)
                if args.command == "abort":
                    store.set_setting(f'published:{op["pr"]}', None, operator)
            coordinator.report(op)
        elif args.command == "rerun-ci":
            coordinator.rerun_ci(args.run_id, operator)
        elif args.command == "hold":
            require(not store.uncertain(), "Reconcile uncertain publication before changing holds")
            require(not args.release_tag or args.target, "Release holds must declare all target branches")
            require(len(set(args.target)) == len(args.target) and
                    all(t in policy["branches"] + policy.get("release_branches", []) for t in args.target),
                    "Invalid promotion targets")
            store.set_setting("hold", {"reason": args.reason, "release_tag": args.release_tag,
                                       "targets": args.target}, operator)
        elif args.command == "unhold":
            require(not store.uncertain(), "Reconcile publication before lifting the hold")
            hold = store.setting("hold")
            if hold and hold.get("release_tag"):
                completed = {s["branch"] for op in store.operations() if op["state"] == "complete"
                             for s in op["data"]["steps"] if s.get("promotion", {}).get("release_tag") == hold["release_tag"]}
                require(completed == set(hold["targets"]), "Release promotion batch is incomplete")
            coordinator.verify_chain()
            store.audit(None, "hold-release-evidence", {"operator": operator,
                        "validation": args.validation_record, "sync": args.sync_record})
            store.set_setting("hold", None, operator)
        elif args.command == "snapshot":
            require(not store.uncertain() and not store.setting("hold"), "CE publication requires reconciliation or is on hold")
            snapshot = {"repository": policy["repository"], "heads": coordinator.verify_chain()}
            store.audit(None, "snapshot", snapshot)
            print(json.dumps(snapshot, indent=2))
        elif args.command == "publish-promotion":
            from .release import publish
            print(publish(policy, store, github, json.loads(Path(args.manifest).read_text())))
        elif args.command == "register-promotion":
            value = json.loads(Path(args.manifest).read_text())
            require(value["security_approval"] and value["release_approval"] and value["validation_record"] and
                    value["security_approval"] != value["release_approval"], "Missing independent promotion signoffs")
            sha(value["head"])
            sha(value["base_sha"])
            require(value["commits"] and len(value["commits"]) == len(set(value["commits"])), "Invalid approved SC list")
            for commit in value["commits"]:
                sha(commit)
            require(value["head"] == value["commits"][-1], "Invalid promotion head")
            pr = github.pull(int(value["pr"]))
            coordinator.basic(pr)
            require(value["base"] == pr["base"]["ref"] and value["head"] == pr["head"]["sha"], "Manifest differs from PR")
            from .release import public_release
            public_release(github, value)
            hold = store.setting("hold")
            require(hold and hold.get("release_tag") == value["release_tag"] and value["base"] in hold["targets"],
                    "Promotion target is outside the declared release batch")
            store.activate_promotion(dict(value, pr=pr["number"]), operator)


if __name__ == "__main__":
    try:
        main()
    except Blocked as error:
        raise SystemExit(str(error)) from None
