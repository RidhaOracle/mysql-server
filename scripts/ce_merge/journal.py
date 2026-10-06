# Copyright (c) 2026, Oracle and/or its affiliates.
"""Local JSON manifests and audit records; Git provides publication atomicity.

One service host, local disk only. The OS worker lock is released on process exit;
there is no persistent merge-lock owner and no database. Publication receipts live
in Git and are created in the same transaction as the target branch updates.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .policy import require


class Journal:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("operations", "settings", "bundles"):
            (self.path / name).mkdir(exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def lock(self, name, nonblocking=False):
        with (self.path / name).open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def exclusive(self):
        return self.lock("worker.lock", nonblocking=True)

    def write(self, path, value):
        fd, temp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def audit(self, operation, event, data):
        with self.lock("audit.lock"):
            with (self.path / "audit.jsonl").open("a") as stream:
                stream.write(json.dumps({"timestamp": time.time(), "operation": operation,
                                         "event": event, "data": data}, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def setting_path(self, key):
        return self.path / "settings" / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def setting(self, key, default=None):
        path = self.setting_path(key)
        return json.loads(path.read_text())["value"] if path.exists() else default

    def settings(self, prefix):
        return [row["value"] for path in (self.path / "settings").glob("*.json")
                if (row := json.loads(path.read_text()))["key"].startswith(prefix)]

    def set_setting(self, key, value, operator):
        self.write(self.setting_path(key), {"key": key, "value": value})
        self.audit(None, "setting:" + key, {"operator": operator, "value": value})

    def promotion_key(self, manifest):
        identity = json.dumps([manifest["release_tag"], manifest["base"]])
        return "promotion-active:" + hashlib.sha256(identity.encode()).hexdigest()

    def active_promotions(self, release_tag):
        active = {m["base"]: m for m in self.settings("promotion-active:")
                  if m["release_tag"] == release_tag}
        # Read old journals without guessing which of several legacy PRs is current.
        legacy = {}
        for manifest in self.settings("promotion:"):
            if manifest["release_tag"] == release_tag and manifest["base"] not in active:
                legacy.setdefault(manifest["base"], []).append(manifest)
        for base, manifests in legacy.items():
            require(len(manifests) == 1,
                    "Multiple legacy promotions for target; register the approved replacement manifest")
            active[base] = manifests[0]
        # Retirement tombstones suppress the legacy fallback without deleting history.
        return [m for m in active.values() if not m.get("retired")]

    def check_release_mutation(self, release_tag):
        require(not self.uncertain(), "Reconcile uncertain CE publication before changing a promotion")
        for op in self.operations():
            if op["state"] == "aborted":
                continue
            root = self.setting(f'promotion:{op["pr"]}')
            evidence = [root] + [s.get("promotion") for s in op["data"]["steps"]]
            require(not any(m and m["release_tag"] == release_tag for m in evidence),
                    "Abort unpublished release operations before replacing a promotion; completed releases cannot be replaced")

    def retire_promotion(self, release_tag, target, reason, operator):
        """Caller holds the worker mutex; retain a tombstone and all PR manifests."""
        require(reason.strip(), "Promotion retirement requires a reason")
        self.check_release_mutation(release_tag)
        hold = self.setting("hold")
        require(hold and hold.get("release_tag") == release_tag and target not in hold.get("targets", []),
                "Remove the target from the matching release hold before retirement")
        identity = {"release_tag": release_tag, "base": target}
        key = self.promotion_key(identity)
        selected = self.setting(key)
        require(selected or any(m["release_tag"] == release_tag and m["base"] == target
                                for m in self.settings("promotion:")), "Promotion target is not registered")
        if selected and selected.get("retired"):
            return
        self.set_setting(key, dict(identity, retired=True, reason=reason), operator)

    def check_promotion_replacement(self, manifest):
        registered = self.setting(f'promotion:{manifest["pr"]}') if manifest.get("pr") else None
        require(not registered or (registered["release_tag"], registered["base"]) ==
                (manifest["release_tag"], manifest["base"]),
                "A promotion PR cannot be reassigned to another release or target")
        active = self.setting(self.promotion_key(manifest))
        if active == dict(manifest, pr=manifest.get("pr", (active or {}).get("pr"))):
            return  # Exact replay does not change the approved batch.
        self.check_release_mutation(manifest["release_tag"])

    def activate_promotion(self, manifest, operator):
        """Caller holds the worker mutex. The active snapshot is the commit point."""
        self.check_promotion_replacement(manifest)
        key = self.promotion_key(manifest)
        if self.setting(key) == manifest and self.setting(f'promotion:{manifest["pr"]}') == manifest:
            return
        # Keep previous PR entries and setting audit events. Write history before
        # switching the release/target snapshot, preserving an existing active selection.
        self.set_setting(f'promotion:{manifest["pr"]}', manifest, operator)
        self.set_setting(key, manifest, operator)

    def request(self, check_id, pr, actor):
        operation = hashlib.sha256(str(check_id).encode()).hexdigest()[:32]
        with self.lock("requests.lock"):
            for op in self.operations():
                if op["id"] == operation or (op["pr"] == pr["number"] and
                        op["state"] not in ("aborted", "complete")):
                    return op["id"]
            op = {"id": operation, "pr": pr["number"], "head": pr["head"]["sha"],
                  "state": "queued", "created": time.time(), "data": {
                      "actor": actor, "body": pr.get("body") or "", "base": pr["base"]["ref"],
                      "steps": [], "intent": False}}
            self.save(op, "queued", "authorized")
        return operation

    def operations(self):
        return sorted((json.loads(p.read_text()) for p in (self.path / "operations").glob("*.json")),
                      key=lambda op: op["created"])

    def bundle(self, operation, generation=0):
        require(len(operation) == 32 and all(c in "0123456789abcdef" for c in operation), "Invalid operation ID")
        require(isinstance(generation, int) and generation >= 0, "Invalid bundle generation")
        return self.path / "bundles" / (operation + (f"-{generation}" if generation else "") + ".bundle")

    def save(self, op, state, event):
        self.bundle(op["id"])  # Validate before using an identifier as a filename.
        op["state"] = state
        self.write(self.path / "operations" / (op["id"] + ".json"), op)
        self.audit(op["id"], event, {"state": state})

    def uncertain(self):
        return [op for op in self.operations() if op["data"]["intent"] and op["state"] != "complete"]
