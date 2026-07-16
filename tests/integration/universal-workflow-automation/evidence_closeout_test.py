"""Integration evidence for STORY-UWA-007 Evidence-driven Closeout."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ccgs_cli import atomic_write_text
from ccgs_story_workflow import (
    BEGIN,
    END,
    apply_closeout,
    closeout_report,
    parse_story,
    validate_evidence,
)
from vibe_workflow_evidence import (
    EVIDENCE_INPUT_INVALID,
    EVIDENCE_PLAN_INVALID,
    EVIDENCE_RESULT_INVALID,
    EvidenceBuildError,
    build_evidence,
)


PLAN_ID = "sha256:" + "7" * 64


def artifact_id(plan_id: str, step_id: str, path: str) -> str:
    """Return the Story 006 deterministic artifact identity."""

    canonical = json.dumps(
        [plan_id, step_id, path], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def plan() -> dict[str, Any]:
    """Return a valid neutral Plan Contract 1.0 fixture."""

    return {
        "contract_version": "1.0",
        "ok": True,
        "plan_id": PLAN_ID,
        "step_order": ["compile", "verify"],
        "steps": [
            {
                "id": "compile",
                "argv": ["neutral-tool", "compile"],
                "acceptance_mapping": ["AC-1"],
                "artifacts": ["output/result.bin"],
            },
            {
                "id": "verify",
                "argv": ["neutral-tool", "verify"],
                "depends_on": ["compile"],
                "acceptance_mapping": ["AC-1", "AC-2"],
            },
        ],
    }


def result(step_id: str, status: str = "passed") -> dict[str, Any]:
    """Return one complete Story 006 Result Contract 1.0 fixture."""

    exit_category = "success"
    exit_code: int | None = 0
    error: dict[str, Any] | None = None
    if status == "failed":
        exit_category, exit_code = "command_failed", 5
        error = {
            "code": "EXECUTION_COMMAND_FAILED",
            "message": "workflow step exited with a non-zero code",
            "details": {},
        }
    elif status == "cancelled":
        exit_category, exit_code = "cancelled", None
        error = {
            "code": "EXECUTION_CANCELLED",
            "message": "workflow step was cancelled",
            "details": {},
        }
    payload: dict[str, Any] = {
        "contract_version": "1.0",
        "ok": status == "passed",
        "plan_id": PLAN_ID,
        "step_id": step_id,
        "status": status,
        "exit_category": exit_category,
        "exit_code": exit_code,
        "duration_ms": 3,
        "retryable": False,
        "stdout": {"text": "bounded", "byte_count": 7, "truncated": False},
        "stderr": {"text": "", "byte_count": 0, "truncated": False},
        "artifacts": [
            {
                "artifact_id": artifact_id(PLAN_ID, step_id, "output/result.bin"),
                "path": "output/result.bin",
                "present": True,
            }
        ] if step_id == "compile" else [],
    }
    if error is not None:
        payload["error"] = error
    return payload


def checks(status: str = "pass") -> list[dict[str, str]]:
    """Return ordered, distinct checks without command inference."""

    return [
        {
            "id": "integration-suite",
            "type": "automated-test",
            "status": status,
            "summary": "declared integration suite result",
        },
        {
            "id": "contract-review",
            "type": "review",
            "status": status,
            "summary": "declared contract review result",
        },
    ]


def story_text(status: str = "review") -> str:
    """Return a parseable synthetic Story with explicit criterion IDs."""

    return f"""---
id: STORY-NEUTRAL-007
title: Neutral evidence closeout
status: {status}
---
# Neutral evidence closeout

## Acceptance Criteria

- [ ] AC-1: first observable condition
- [ ] AC-2: second observable condition
"""


def passing_evidence() -> dict[str, Any]:
    """Build passing Evidence through the production aggregation API."""

    return build_evidence(
        "STORY-NEUTRAL-007",
        ["AC-1", "AC-2"],
        plan(),
        [result("compile"), result("verify")],
        checks(),
    )


class EvidenceCloseoutTest(unittest.TestCase):
    """Verify every acceptance criterion of STORY-UWA-007."""

    def assert_build_error(self, code: str, callback: Any) -> None:
        """Assert that invalid input returns only the stable failure contract."""

        with self.assertRaises(EvidenceBuildError) as captured:
            callback()
        report = captured.exception.report()
        self.assertEqual("1.0", report["contract_version"])
        self.assertFalse(report["ok"])
        self.assertEqual(code, report["error"]["code"])
        self.assertEqual({"contract_version", "ok", "error"}, set(report))

    def test_ac1_valid_inputs_generate_schema_exact_ordered_evidence(self) -> None:
        evidence = passing_evidence()

        self.assertEqual(
            {"schema_version", "story_id", "result", "acceptance_criteria", "checks"},
            set(evidence),
        )
        self.assertEqual([], validate_evidence(evidence))
        self.assertEqual(["AC-1", "AC-2"], [item["id"] for item in evidence["acceptance_criteria"]])
        self.assertEqual(
            [("integration-suite", "automated-test"), ("contract-review", "review")],
            [(item["id"], item["type"]) for item in evidence["checks"]],
        )
        for criterion in evidence["acceptance_criteria"]:
            references = json.loads(criterion["evidence"])
            self.assertTrue(all(set(item) == {"plan_id", "step_id"} for item in references))
            self.assertTrue(all(item["plan_id"] == PLAN_ID for item in references))

    def test_ac1_invalid_input_plan_and_result_use_stable_errors(self) -> None:
        invalid_plan = plan()
        invalid_plan["steps"][0]["acceptance_mapping"] = ["AC-99"]
        mismatched = result("compile")
        mismatched["plan_id"] = "sha256:" + "9" * 64
        unknown_step = result("unknown")
        unsupported_plan = plan()
        unsupported_plan["contract_version"] = "2.0"
        unsupported_result = result("compile")
        unsupported_result["contract_version"] = "2.0"
        duplicate_criteria = ["AC-1", "AC-1"]
        invalid_check = checks()
        invalid_check[0]["type"] = "guessed-from-command"

        self.assert_build_error(
            EVIDENCE_INPUT_INVALID,
            lambda: build_evidence("STORY-NEUTRAL-007", duplicate_criteria, plan(), [], checks()),
        )
        self.assert_build_error(
            EVIDENCE_INPUT_INVALID,
            lambda: build_evidence("STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(), [], invalid_check),
        )
        self.assert_build_error(
            EVIDENCE_PLAN_INVALID,
            lambda: build_evidence("STORY-NEUTRAL-007", ["AC-1", "AC-2"], invalid_plan, [], checks()),
        )
        self.assert_build_error(
            EVIDENCE_RESULT_INVALID,
            lambda: build_evidence("STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(), [mismatched], checks()),
        )
        self.assert_build_error(
            EVIDENCE_RESULT_INVALID,
            lambda: build_evidence(
                "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
                [result("compile"), result("compile")], checks(),
            ),
        )
        for invalid in (unknown_step, unsupported_result):
            self.assert_build_error(
                EVIDENCE_RESULT_INVALID,
                lambda invalid=invalid: build_evidence(
                    "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(), [invalid], checks()
                ),
            )
        self.assert_build_error(
            EVIDENCE_PLAN_INVALID,
            lambda: build_evidence(
                "STORY-NEUTRAL-007", ["AC-1", "AC-2"], unsupported_plan, [], checks()
            ),
        )

    def test_ac1_result_contract_rejects_semantic_and_artifact_path_drift(self) -> None:
        inconsistent = result("compile")
        inconsistent["exit_category"] = "timed_out"
        cancelled_as_success = result("verify", "cancelled")
        cancelled_as_success["exit_category"] = "success"
        cancelled_as_success["exit_code"] = 0
        bad_artifact_id = result("compile")
        bad_artifact_id["artifacts"][0]["artifact_id"] = "unstable"
        escaped_artifact = result("compile")
        escaped_artifact["artifacts"][0]["path"] = "../outside.bin"
        omitted_artifact = result("compile")
        omitted_artifact["artifacts"] = []
        changed_artifact = result("compile")
        changed_artifact["artifacts"][0]["path"] = "output/other.bin"
        wrong_stable_id = result("compile")
        wrong_stable_id["artifacts"][0]["artifact_id"] = "sha256:" + "8" * 64
        backslash_artifact = result("compile")
        backslash_artifact["artifacts"][0]["path"] = "output\\result.bin"

        for invalid in (
            inconsistent,
            cancelled_as_success,
            bad_artifact_id,
            escaped_artifact,
            omitted_artifact,
            changed_artifact,
            wrong_stable_id,
            backslash_artifact,
        ):
            self.assert_build_error(
                EVIDENCE_RESULT_INVALID,
                lambda invalid=invalid: build_evidence(
                    "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(), [invalid], checks()
                ),
            )

        two_artifact_plan = plan()
        declarations = ["output/first.bin", "output/second.bin"]
        two_artifact_plan["steps"][0]["artifacts"] = declarations
        two_artifact_result = result("compile")
        two_artifact_result["artifacts"] = [
            {
                "artifact_id": artifact_id(PLAN_ID, "compile", path),
                "path": path,
                "present": True,
            }
            for path in declarations
        ]
        accepted = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], two_artifact_plan,
            [two_artifact_result], checks(),
        )
        self.assertEqual("blocked", accepted["result"])
        swapped = copy.deepcopy(two_artifact_result)
        swapped["artifacts"].reverse()
        self.assert_build_error(
            EVIDENCE_RESULT_INVALID,
            lambda: build_evidence(
                "STORY-NEUTRAL-007", ["AC-1", "AC-2"], two_artifact_plan,
                [swapped], checks(),
            ),
        )

        for exit_category, error_code in (
            ("start_failed", "EXECUTION_START_FAILED"),
            ("policy_rejected", "EXECUTION_POLICY_INVALID"),
        ):
            pre_execution = result("compile", "failed")
            pre_execution["exit_category"] = exit_category
            pre_execution["exit_code"] = None
            pre_execution["artifacts"] = []
            pre_execution["error"]["code"] = error_code
            evidence = build_evidence(
                "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
                [pre_execution], checks(),
            )
            self.assertEqual("fail", evidence["result"])

        unexpected_policy_artifact = result("compile", "failed")
        unexpected_policy_artifact["exit_category"] = "policy_rejected"
        unexpected_policy_artifact["exit_code"] = None
        unexpected_policy_artifact["error"]["code"] = "EXECUTION_POLICY_INVALID"
        self.assert_build_error(
            EVIDENCE_RESULT_INVALID,
            lambda: build_evidence(
                "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
                [unexpected_policy_artifact], checks(),
            ),
        )

    def test_ac2_aggregation_pass_fail_and_deferred_are_deterministic(self) -> None:
        all_pass = passing_evidence()
        one_failed = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
            [result("compile"), result("verify", "failed")], checks(),
        )
        missing = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(), [result("compile")], checks(),
        )
        check_deferred = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
            [result("compile"), result("verify")], checks("deferred"),
        )
        cancelled = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
            [result("compile"), result("verify", "cancelled")], checks(),
        )
        empty_mapping_plan = plan()
        empty_mapping_plan["steps"][1]["acceptance_mapping"] = ["AC-1"]
        empty_mapping = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], empty_mapping_plan,
            [result("compile"), result("verify")], checks(),
        )
        check_failed = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], plan(),
            [result("compile"), result("verify")], checks("fail"),
        )

        self.assertEqual(("pass", ["pass", "pass"]), (
            all_pass["result"], [item["status"] for item in all_pass["acceptance_criteria"]]
        ))
        self.assertEqual(("fail", ["fail", "fail"]), (
            one_failed["result"], [item["status"] for item in one_failed["acceptance_criteria"]]
        ))
        self.assertEqual(("blocked", ["deferred", "deferred"]), (
            missing["result"], [item["status"] for item in missing["acceptance_criteria"]]
        ))
        self.assertEqual("blocked", check_deferred["result"])
        self.assertEqual(("fail", ["fail", "fail"]), (
            cancelled["result"], [item["status"] for item in cancelled["acceptance_criteria"]]
        ))
        self.assertEqual(("blocked", ["pass", "deferred"]), (
            empty_mapping["result"], [item["status"] for item in empty_mapping["acceptance_criteria"]]
        ))
        self.assertEqual("fail", check_failed["result"])

    def test_ac2_logs_commands_and_artifact_content_fields_do_not_change_semantics(self) -> None:
        changed_plan = plan()
        changed_plan["steps"][0]["argv"] = ["different-neutral-tool", "opaque"]
        # A changed valid plan has its own stable identity; update its Results accordingly.
        changed_plan["plan_id"] = "sha256:" + "a" * 64
        changed_results = [result("compile"), result("verify")]
        for item in changed_results:
            item["plan_id"] = changed_plan["plan_id"]
            item["stdout"]["text"] = "different bounded log body"
            item["stdout"]["byte_count"] = len(item["stdout"]["text"])
            for artifact in item["artifacts"]:
                artifact["present"] = not artifact["present"]
                artifact["artifact_id"] = artifact_id(
                    changed_plan["plan_id"], item["step_id"], artifact["path"]
                )
        evidence = build_evidence(
            "STORY-NEUTRAL-007", ["AC-1", "AC-2"], changed_plan, changed_results, checks()
        )

        self.assertEqual("pass", evidence["result"])
        self.assertEqual(["pass", "pass"], [item["status"] for item in evidence["acceptance_criteria"]])
        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("different-neutral-tool", serialized)
        self.assertNotIn("different bounded log body", serialized)
        self.assertNotIn("output/result.bin", serialized)

    def test_ac3_closeout_uses_exact_state_ownership_criteria_and_check_failures(self) -> None:
        passing = passing_evidence()
        review = parse_story("ccgs-data/production/epics/neutral/story.md", story_text("review"))
        accepted = closeout_report(review, "ccgs-data/production/qa/evidence/neutral.json", passing, [])
        self.assertEqual(("pass", "done", []), (
            accepted["verdict"], accepted["target_state"], accepted["failures"]
        ))
        self.assertFalse(accepted["written"])
        done = parse_story(review.relative_path, story_text("done"))
        self.assertEqual("pass", closeout_report(
            done, "ccgs-data/production/qa/evidence/neutral.json", passing, []
        )["verdict"])

        cases: list[tuple[str, Any, Any, list[dict[str, str]]]] = []
        cases.append(("story.state", parse_story(review.relative_path, story_text("in-progress")), passing, []))
        wrong_story = copy.deepcopy(passing)
        wrong_story["story_id"] = "STORY-OTHER"
        cases.append(("evidence.story", review, wrong_story, []))
        bad_result = copy.deepcopy(passing)
        bad_result["result"] = "fail"
        cases.append(("evidence.result", review, bad_result, []))
        bad_acceptance = copy.deepcopy(passing)
        bad_acceptance["acceptance_criteria"][0]["status"] = "deferred"
        cases.append(("evidence.acceptance", review, bad_acceptance, []))
        bad_checks = copy.deepcopy(passing)
        bad_checks["checks"][0]["status"] = "fail"
        cases.append(("evidence.checks", review, bad_checks, []))
        deferred_checks = copy.deepcopy(passing)
        deferred_checks["checks"][0]["status"] = "deferred"
        cases.append(("evidence.checks", review, deferred_checks, []))
        missing_criterion = copy.deepcopy(passing)
        missing_criterion["acceptance_criteria"] = missing_criterion["acceptance_criteria"][:1]
        cases.append(("evidence.acceptance", review, missing_criterion, []))
        empty_checks = copy.deepcopy(passing)
        empty_checks["checks"] = []
        cases.append(("evidence.checks", review, empty_checks, validate_evidence(empty_checks)))
        invalid_check_id = copy.deepcopy(passing)
        invalid_check_id["checks"][0]["id"] = []
        invalid_check_id["checks"][0]["status"] = "fail"
        cases.append(
            ("evidence.schema", review, invalid_check_id, validate_evidence(invalid_check_id))
        )
        for collection, field in (
            ("root", "result"),
            ("criterion", "status"),
            ("check", "type"),
            ("check", "status"),
        ):
            invalid_scalar = copy.deepcopy(passing)
            if collection == "root":
                invalid_scalar[field] = []
            elif collection == "criterion":
                invalid_scalar["acceptance_criteria"][0][field] = []
            else:
                invalid_scalar["checks"][0][field] = []
            cases.append(
                ("evidence.schema", review, invalid_scalar, validate_evidence(invalid_scalar))
            )
        cases.append(("evidence.schema", review, {}, [{"path": "$", "message": "invalid"}]))
        for invalid_root in (
            [],
            None,
            "invalid",
            7,
            {"acceptance_criteria": None, "checks": None},
        ):
            cases.append(("evidence.schema", review, invalid_root, validate_evidence(invalid_root)))

        for expected_code, story, evidence, errors in cases:
            report = closeout_report(story, "ccgs-data/production/qa/evidence/neutral.json", evidence, errors)
            self.assertEqual("fail", report["verdict"])
            self.assertEqual(story.status, report["target_state"])
            self.assertIn(expected_code, [item["code"] for item in report["failures"]])

    def test_ac4_dry_run_success_and_failure_write_share_decision_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "story.md"
            path.write_text(story_text("review"), encoding="utf-8", newline="\n")
            story = parse_story("ccgs-data/production/epics/neutral/story.md", path.read_text(encoding="utf-8"))
            report = closeout_report(story, "ccgs-data/production/qa/evidence/neutral.json", passing_evidence(), [])
            before = path.read_bytes()
            mtime = path.stat().st_mtime_ns

            # Report construction is the dry-run path and does not touch the Story.
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(mtime, path.stat().st_mtime_ns)
            written = apply_closeout(path, story, report, atomic_write_text)
            self.assertTrue(written)
            self.assertTrue(report["written"])
            content = path.read_text(encoding="utf-8")
            self.assertIn("status: done", content)
            self.assertEqual(1, content.count(BEGIN))
            self.assertEqual(1, content.count(END))

            failed_evidence = passing_evidence()
            failed_evidence["result"] = "fail"
            failed_story = parse_story(story.relative_path, story_text("review"))
            path.write_text(failed_story.text, encoding="utf-8", newline="\n")
            failed_report = closeout_report(
                failed_story, "ccgs-data/production/qa/evidence/neutral.json", failed_evidence, []
            )
            self.assertTrue(apply_closeout(path, failed_story, failed_report, atomic_write_text))
            failed_content = path.read_text(encoding="utf-8")
            self.assertIn("status: review", failed_content)
            self.assertEqual(1, failed_content.count(BEGIN))
            self.assertIn("evidence.result", failed_content)

            changed_failure = passing_evidence()
            changed_failure["acceptance_criteria"][0]["status"] = "deferred"
            current_story = parse_story(failed_story.relative_path, failed_content)
            changed_report = closeout_report(
                current_story,
                "ccgs-data/production/qa/evidence/neutral.json",
                changed_failure,
                [],
            )
            self.assertTrue(apply_closeout(path, current_story, changed_report, atomic_write_text))
            changed_content = path.read_text(encoding="utf-8")
            self.assertEqual(1, changed_content.count(BEGIN))
            self.assertEqual(1, changed_content.count(END))
            self.assertIn("evidence.acceptance", changed_content)

    def test_ac4_atomic_replace_failure_returns_closeout_write_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "story.md"
            path.write_text(story_text("review"), encoding="utf-8", newline="\n")
            story = parse_story("ccgs-data/production/epics/neutral/story.md", path.read_text(encoding="utf-8"))
            report = closeout_report(story, "ccgs-data/production/qa/evidence/neutral.json", passing_evidence(), [])
            report["written"] = False
            before = path.read_bytes()
            mtime = path.stat().st_mtime_ns

            with mock.patch.object(Path, "replace", side_effect=OSError("private host path")):
                written = apply_closeout(path, story, report, atomic_write_text)

            self.assertFalse(written)
            self.assertFalse(report["written"])
            self.assertEqual("fail", report["verdict"])
            self.assertEqual("review", report["target_state"])
            self.assertIn("closeout.write", [item["code"] for item in report["failures"]])
            self.assertNotIn("private host path", json.dumps(report))
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(mtime, path.stat().st_mtime_ns)
            self.assertEqual([], list(path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
