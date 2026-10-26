# Copyright (c) 2026, Oracle and/or its affiliates.
"""Hidden bypass actors require a full, current administrator attestation."""
import copy
import tempfile
import unittest
from unittest.mock import MagicMock

from scripts.ce_merge.coordinator import Coordinator
from scripts.ce_merge.journal import Journal
from scripts.ce_merge.policy import Blocked, rulesets
from test_coordinator import POLICY


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.api = MagicMock()
        self.store = Journal(temp.name)
        self.coordinator = Coordinator(POLICY, self.store, self.api)
        self.full = [dict(r, id=i, source_type="Repository", source=POLICY["repository"],
                          updated_at="2026-10-01T10:00:00Z") for i, r in enumerate(rulesets(POLICY), 1)]
        self.visible = copy.deepcopy(self.full)
        for rule in self.visible:
            rule.pop("bypass_actors")
        self.api.pages.return_value = [{"id": r["id"], "name": r["name"]} for r in self.full]
        self.api.repo.side_effect = self.repo

    def repo(self, path):
        if not path:
            return {"allow_rebase_merge": False, "allow_auto_merge": False,
                    "allow_merge_commit": True, "allow_squash_merge": True}
        return self.visible[int(path.rsplit("/", 1)[1]) - 1]

    def attest(self):
        self.coordinator.attest_deployment({"repository": POLICY["repository"], "rulesets": self.full}, "owner")

    def test_current_full_attestation_allows_read_only_verification(self):
        with self.assertRaisesRegex(Blocked, "must attest"):
            self.coordinator.deployment()
        self.attest()
        self.coordinator.deployment()
        self.assertTrue(all(call.args[0].startswith("/rulesets/") or call.args[0] == ""
                            for call in self.api.repo.call_args_list))

    def test_ruleset_revision_or_policy_drift_invalidates_attestation(self):
        self.attest()
        self.visible[0]["updated_at"] = "2026-10-01T10:00:01Z"
        with self.assertRaisesRegex(Blocked, "changed"):
            self.coordinator.deployment()
        self.visible[0]["updated_at"] = "2026-10-01T10:00:00Z"
        self.coordinator.policy = dict(POLICY, maintainers=["different"])
        with self.assertRaisesRegex(Blocked, "must attest"):
            self.coordinator.deployment()

    def test_attestation_cannot_add_an_extra_bypass_actor(self):
        self.full[0]["bypass_actors"].append({"actor_id": 99, "actor_type": "User", "bypass_mode": "always"})
        with self.assertRaisesRegex(Blocked, "differs"):
            self.attest()
        self.assertIsNone(self.store.setting("deployment-attestation"))

    def test_equivalent_timezone_revision_is_accepted(self):
        for rule in self.full:
            rule["updated_at"] = "2026-10-01T12:00:00+02:00"
        self.attest()
        self.coordinator.deployment()
