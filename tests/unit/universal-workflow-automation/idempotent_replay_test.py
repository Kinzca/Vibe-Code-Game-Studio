"""Unit evidence for STORY-UWA-008 deterministic replay contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_workflow_replay import (
    REPLAY_IDENTITY_CONFLICT,
    REPLAY_INPUT_INVALID,
    REPLAY_RECORD_INVALID,
    REPLAY_RETRY_EXHAUSTED,
    REPLAY_RETRY_FORBIDDEN,
    ReplayContractError,
    build_replay_identity,
    decide_replay,
)


PLAN_ID = "sha256:" + "7" * 64


def plan(plan_id: str = PLAN_ID) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "ok": True,
        "plan_id": plan_id,
        "step_order": ["prepare", "verify"],
        "steps": [
            {
                "id": "prepare",
                "argv": ["neutral-tool", "prepare"],
                "acceptance_mapping": ["AC-1"],
                "artifacts": ["output/result.json"],
            },
            {
                "id": "verify",
                "argv": ["neutral-tool", "verify"],
                "depends_on": ["prepare"],
                "acceptance_mapping": ["AC-2"],
            },
        ],
    }


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def identity() -> dict[str, str]:
    return build_replay_identity(
        "request.received",
        "inputs-v1",
        {"z": [1, "中性"], "a": {"ready": True}},
        plan(),
    )


def failed_record(failure_class: str, attempt: int = 1) -> dict[str, Any]:
    value: dict[str, Any] = {
        **identity(),
        "attempt": attempt,
        "status": "failed",
        "failure_class": failure_class,
        "result": {"status": "failed", "step_id": "prepare"},
    }
    return value


class IdempotentReplayTest(unittest.TestCase):
    def test_ac1_identity_uses_exact_canonical_domain_contract(self) -> None:
        first = identity()
        reordered = build_replay_identity(
            "request.received",
            "inputs-v1",
            {"a": {"ready": True}, "z": [1, "中性"]},
            plan(),
        )
        fingerprint = canonical_digest(
            ["replay-input", "1.0", "inputs-v1", {"z": [1, "中性"], "a": {"ready": True}}]
        )
        event_id = canonical_digest(
            ["replay-event", "1.0", "request.received", fingerprint, PLAN_ID]
        )
        run_id = canonical_digest(["replay-run", "1.0", event_id])

        self.assertEqual(first, reordered)
        self.assertEqual(
            {
                "contract_version": "1.0",
                "event_id": event_id,
                "run_id": run_id,
                "input_fingerprint": fingerprint,
                "plan_id": PLAN_ID,
            },
            first,
        )
        self.assertEqual(["prepare", "verify"], [step["id"] for step in plan()["steps"]])

    def test_ac1_each_identity_input_changes_event_and_run(self) -> None:
        base = identity()
        variants = (
            build_replay_identity("other", "inputs-v1", {"z": [1, "中性"], "a": {"ready": True}}, plan()),
            build_replay_identity("request.received", "inputs-v2", {"z": [1, "中性"], "a": {"ready": True}}, plan()),
            build_replay_identity("request.received", "inputs-v1", {"z": [2, "中性"], "a": {"ready": True}}, plan()),
            build_replay_identity("request.received", "inputs-v1", {"z": [1, "中性"], "a": {"ready": True}}, plan("sha256:" + "8" * 64)),
        )
        for changed in variants:
            self.assertNotEqual(base["event_id"], changed["event_id"])
            self.assertNotEqual(base["run_id"], changed["run_id"])

    def test_ac1_invalid_json_and_plan_return_stable_input_error(self) -> None:
        invalid_values = ({1: "non-string"}, {"bad": math.nan}, {"bad": math.inf}, ("tuple",))
        for value in invalid_values:
            with self.assertRaises(ReplayContractError) as captured:
                build_replay_identity("event", "v1", value, plan())
            self.assertEqual(REPLAY_INPUT_INVALID, captured.exception.code)
        invalid_plan = plan()
        invalid_plan["contract_version"] = "2.0"
        with self.assertRaises(ReplayContractError) as captured:
            build_replay_identity("event", "v1", {}, invalid_plan)
        self.assertEqual(REPLAY_INPUT_INVALID, captured.exception.code)

    def test_ac3_record_conflict_and_invalid_record_are_rejected(self) -> None:
        conflicts = []
        for field, value in (
            ("plan_id", "sha256:" + "9" * 64),
            ("run_id", "sha256:" + "8" * 64),
            ("input_fingerprint", "sha256:" + "5" * 64),
        ):
            conflicted = failed_record("transient_transport_failure")
            conflicted[field] = value
            conflicts.append(
                decide_replay(
                    identity(), conflicted, "transient_transport_failure", {"max_attempts": 3}
                )
            )
        invalid = failed_record("transient_transport_failure")
        invalid["unknown"] = True
        invalid_report = decide_replay(
            identity(), invalid, "transient_transport_failure", {"max_attempts": 3}
        )
        self.assertTrue(
            all(report["error"]["code"] == REPLAY_IDENTITY_CONFLICT for report in conflicts)
        )
        self.assertEqual(REPLAY_RECORD_INVALID, invalid_report["error"]["code"])
        self.assertTrue(all(report["action"] == "reject" for report in conflicts))
        self.assertEqual("reject", invalid_report["action"])

    def test_ac4_retry_matrix_preserves_identity_and_enforces_limit(self) -> None:
        for failure_class in ("transient_transport_failure", "declared_worker_unavailable"):
            previous = failed_record(failure_class)
            report = decide_replay(identity(), previous, failure_class, {"max_attempts": 2})
            self.assertTrue(report["ok"])
            self.assertEqual("retry", report["action"])
            self.assertEqual(2, report["attempt"])
            for field in ("event_id", "run_id", "plan_id"):
                self.assertEqual(previous[field], report[field])
            exhausted = decide_replay(identity(), previous, failure_class, {"max_attempts": 1})
            self.assertEqual(REPLAY_RETRY_EXHAUSTED, exhausted["error"]["code"])

        for failure_class in (
            "business_failure",
            "configuration_error",
            "schema_error",
            "policy_rejected",
            "path_error",
        ):
            report = decide_replay(
                identity(), failed_record(failure_class), failure_class, {"max_attempts": 10}
            )
            self.assertEqual(REPLAY_RETRY_FORBIDDEN, report["error"]["code"])

        no_record_terminal = decide_replay(
            identity(), None, "business_failure", {"max_attempts": 1}
        )
        self.assertEqual(REPLAY_RETRY_FORBIDDEN, no_record_terminal["error"]["code"])
        no_record_transient = decide_replay(
            identity(), None, "transient_transport_failure", {"max_attempts": 10}
        )
        self.assertEqual(REPLAY_INPUT_INVALID, no_record_transient["error"]["code"])

    def test_ac4_retry_limit_accepts_tenth_attempt_and_rejects_eleventh(self) -> None:
        ninth = failed_record("transient_transport_failure", attempt=9)
        allowed = decide_replay(
            identity(), ninth, "transient_transport_failure", {"max_attempts": 10}
        )
        self.assertEqual("retry", allowed["action"])
        self.assertEqual(10, allowed["attempt"])

        tenth = failed_record("transient_transport_failure", attempt=10)
        exhausted = decide_replay(
            identity(), tenth, "transient_transport_failure", {"max_attempts": 10}
        )
        self.assertEqual(REPLAY_RETRY_EXHAUSTED, exhausted["error"]["code"])

        invalid = failed_record("transient_transport_failure", attempt=11)
        rejected = decide_replay(
            identity(), invalid, "transient_transport_failure", {"max_attempts": 10}
        )
        self.assertEqual(REPLAY_RECORD_INVALID, rejected["error"]["code"])

    def test_ac4_policy_bounds_and_success_replay_are_stable(self) -> None:
        succeeded = {
            **identity(),
            "attempt": 1,
            "status": "succeeded",
            "failure_class": None,
            "result": {"status": "passed"},
        }
        report = decide_replay(identity(), succeeded, None, {"max_attempts": 1})
        self.assertEqual("reuse", report["action"])
        self.assertEqual(succeeded, report["record"])
        execute = decide_replay(identity(), None, None, {"max_attempts": 1})
        self.assertEqual("execute", execute["action"])
        self.assertNotIn("record", execute)
        for maximum in (0, 11, True):
            with self.assertRaises(ReplayContractError) as captured:
                decide_replay(identity(), None, None, {"max_attempts": maximum})
            self.assertEqual(REPLAY_INPUT_INVALID, captured.exception.code)


if __name__ == "__main__":
    unittest.main()
