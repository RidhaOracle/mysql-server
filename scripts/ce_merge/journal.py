# Copyright (c) 2026, Oracle and/or its affiliates.
"""Durable operation manifests, audit records, and the cooperative CE merge lock."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .policy import require


class PersistenceError(Exception):
    """Durable state could not be confirmed; stop all further remote writes."""


class Journal:
    """Runner-local files; GitJournal checkpoints reject competing remote writers."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("operations", "settings", "bundles"):
            (self.path / name).mkdir(exist_ok=True, mode=0o700)

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

    def set_setting(self, key, value, operator):
        self.write(self.setting_path(key), {"key": key, "value": value})
        self.audit(None, "setting:" + key, {"operator": operator, "value": value})

    def ensure_healthy(self):
        """Local journals are used by tests; Actions uses state.GitJournal."""

    def checkpoint(self):
        self.ensure_healthy()

    def request(self, comment_id, pr, actor):
        operation = hashlib.sha256(str(comment_id).encode()).hexdigest()[:32]
        for op in self.operations():
            if op["id"] == operation or (op["pr"] == pr["number"] and
                    op["state"] not in ("aborted", "complete")):
                return op["id"]
        op = {"id": operation, "pr": pr["number"], "head": pr["head"]["sha"],
              "state": "queued", "created": time.time(), "data": {
                  "actor": actor, "body": pr.get("body") or "", "base": pr["base"]["ref"],
                  "protocol": "direct-versions-v1", "steps": [], "intent": False}}
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
