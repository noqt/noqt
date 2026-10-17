"""Frozen contract cases for the standalone Yumi continuity resolver.

These tests deliberately use only synthetic ledgers.  They are written before
the implementation so the resolver contract is explicit and repeatable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "yumi_continuity.py"


def load_module():
    spec = importlib.util.spec_from_file_location("yumi_continuity", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("resolver module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ledger(revisions, *, prior_heads=None, heads=None):
    value = {
        "schema": "yumi.continuity.ledger.v1",
        "scope": "synthetic-session",
        "revisions": revisions,
    }
    if prior_heads is not None:
        value["prior_heads"] = prior_heads
    if heads is not None:
        value["heads"] = heads
    return value


def revision(identifier, parent=None, *, sequence=None, assertions=None, **extra):
    result = {"id": identifier, "parent": parent, "assertions": assertions or []}
    if sequence is not None:
        result["sequence"] = sequence
    result.update(extra)
    return result


def assertion(key, value=None, *, identifier=None, state="asserted", **extra):
    result = {"key": key, "state": state}
    if value is not None:
        result["value"] = value
    if identifier is not None:
        result["id"] = identifier
    result.update(extra)
    return result


class FrozenContinuityCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = load_module()

    def resolve(self, value):
        return self.resolver.resolve_ledger(value)

    def test_case_01_control_assertion_and_unverified_source(self):
        result = self.resolve(
            ledger(
                [
                    revision(
                        "r1",
                        sequence=1,
                        assertions=[
                            assertion(
                                "status",
                                "ready",
                                identifier="a1",
                                source_label="operator",
                                source_ref="opaque://note/1",
                            )
                        ],
                    )
                ]
            )
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["current_assertions"][0]["key"], "status")
        self.assertEqual(result["current_assertions"][0]["provenance"]["verification"], "unverified")
        self.assertEqual(result["current_assertions"][0]["provenance"]["source_ref"], "opaque://note/1")
        self.assertFalse(result["ambiguity"]["is_ambiguous"])

    def test_case_02_revision_supersession_chain(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("mode", "draft", identifier="a1")]),
                    revision("r2", "r1", sequence=2, assertions=[assertion("mode", "ready", identifier="a2")]),
                    revision("r3", "r2", sequence=3, assertions=[assertion("mode", "sent", identifier="a3")]),
                ]
            )
        )
        self.assertEqual(result["heads"], ["r3"])
        self.assertEqual(result["current_assertions"][0]["value"], "sent")
        self.assertEqual({item["assertion_id"] for item in result["superseded_assertions"]}, {"a1", "a2"})
        self.assertEqual(result["revision_chains"]["r3"], ["r1", "r2", "r3"])

    def test_case_03_assertion_retraction(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("flag", True, identifier="a1")]),
                    revision("r2", "r1", sequence=2, assertions=[assertion("flag", state="retracted", target="a1")]),
                ]
            )
        )
        self.assertEqual(result["current_assertions"], [])
        self.assertEqual(result["retractions"][0]["target"], "a1")
        self.assertEqual(result["retractions"][0]["status"], "retracted")

    def test_case_04_revision_retraction_chain(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("flag", True, identifier="a1")]),
                    revision("r2", "r1", sequence=2, retracts=["a1"]),
                    revision("r3", "r2", sequence=3, assertions=[assertion("flag", False, identifier="a3")]),
                    revision("r4", "r3", sequence=4, assertions=[assertion("flag", state="retracted", target="a1")]),
                ]
            )
        )
        self.assertEqual(result["current_assertions"][0]["value"], False)
        self.assertEqual([item["target"] for item in result["retractions"]], ["a1", "a1"])
        self.assertEqual([item["revision_id"] for item in result["retractions"]], ["r2", "r4"])
        self.assertEqual([item["effective"] for item in result["retractions"]], [True, False])

    def test_case_05_fork_is_explicitly_ambiguous(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("status", "base", identifier="a1")]),
                    revision("r2a", "r1", sequence=2, assertions=[assertion("status", "left", identifier="a2")]),
                    revision("r2b", "r1", sequence=3, assertions=[assertion("status", "right", identifier="a3")]),
                ]
            )
        )
        self.assertTrue(result["ambiguity"]["is_ambiguous"])
        self.assertEqual(result["heads"], ["r2a", "r2b"])
        self.assertEqual(result["forks"][0]["parent"], "r1")
        self.assertEqual(result["current_assertions"], [])
        self.assertEqual(len(result["head_results"]), 2)

        converged = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("status", "base", identifier="a1")]),
                    revision("r2", "r1", sequence=2, assertions=[assertion("status", "left", identifier="a2")]),
                    revision("r3", "r1", sequence=3, assertions=[assertion("status", "right", identifier="a3")]),
                    revision("r4", "r2", sequence=4, supersedes=["r3"], assertions=[assertion("status", "merged", identifier="a4")]),
                ]
            )
        )
        self.assertEqual(converged["status"], "invalid")
        self.assertTrue(any("convergent_fork_requires_explicit_merge" in item for item in converged["diagnostics"]["errors"]))

    def test_case_06_missing_predecessor_is_failed_closed(self):
        result = self.resolve(
            ledger([revision("r2", "does-not-exist", sequence=2, assertions=[assertion("x", 1)])])
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("does-not-exist", result["diagnostics"]["missing_references"])
        self.assertEqual(result["current_assertions"], [])

    def test_case_07_parent_cycle_is_explicit(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", "r2", sequence=1),
                    revision("r2", "r1", sequence=2),
                ]
            )
        )
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["diagnostics"]["cycles"])

    def test_case_08_supersession_cycle_is_explicit(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, supersedes=["r2"]),
                    revision("r2", sequence=2, supersedes=["r1"]),
                ]
            )
        )
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["diagnostics"]["cycles"])

    def test_case_09_duplicate_replay_is_deduplicated(self):
        r1 = revision("r1", sequence=1, assertions=[assertion("x", 1, identifier="a1")])
        result = self.resolve(ledger([r1, json.loads(json.dumps(r1))]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["diagnostics"]["duplicate_revisions"], ["r1"])
        self.assertEqual(len(result["current_assertions"]), 1)

    def test_case_10_shuffle_stability_and_fresh_cli_equality(self):
        revisions = [
            revision("r2", "r1", sequence=2, assertions=[assertion("x", 2, identifier="a2")]),
            revision("r1", sequence=1, assertions=[assertion("x", 1, identifier="a1")]),
        ]
        first = self.resolve(ledger(revisions))
        second = self.resolve(ledger(list(reversed(revisions))))
        self.assertEqual(first, second)
        payload = json.dumps(ledger(revisions), sort_keys=True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        outputs = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-S", str(SCRIPT), "resolve", "--input", "-"],
                input=payload,
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            self.assertEqual(completed.stderr, "")
            outputs.append(completed.stdout)
        self.assertEqual(outputs[0], outputs[1])

    def test_case_11_delta_from_supplied_prior_head(self):
        result = self.resolve(
            ledger(
                [
                    revision("r1", sequence=1, assertions=[assertion("x", 1, identifier="a1")]),
                    revision("r2", "r1", sequence=2, assertions=[assertion("x", 2, identifier="a2"), assertion("y", "new", identifier="a3")]),
                ],
                prior_heads=["r1"],
            )
        )
        self.assertEqual(result["delta"]["from_prior_heads"], ["r1"])
        self.assertEqual(result["delta"]["changed"][0]["key"], "x")
        self.assertEqual(result["delta"]["added"][0]["key"], "y")
        self.assertEqual(result["delta"]["removed"], [])

    def test_case_12_control_bounds_and_malformed_input(self):
        malformed = self.resolve({"scope": "synthetic-session", "revisions": "not-a-list"})
        self.assertEqual(malformed["status"], "invalid")
        too_many = ledger([revision(f"r{i}", sequence=i) for i in range(129)])
        bounded = self.resolve(too_many)
        self.assertEqual(bounded["status"], "invalid")
        self.assertIn("revision_count", bounded["diagnostics"]["limits"])
        oversized = self.resolve({"scope": "synthetic-session", "revisions": [], "padding": "x" * 70000})
        self.assertEqual(oversized["status"], "invalid")
        self.assertIn("input_bytes", oversized["diagnostics"]["limits"])


if __name__ == "__main__":
    unittest.main()
