"""Acceptance coverage for STORY-UWA-003's deterministic DAG compiler."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vibe_workflow_plan
from vibe_workflow_plan import (
    PLAN_CYCLE_DETECTED,
    PLAN_DEPENDENCY_NOT_FOUND,
    PLAN_SELF_DEPENDENCY,
    PlanCompileError,
    compile_plan,
)


def step(step_id: str, *dependencies: str, **fields: object) -> dict[str, object]:
    """Build one neutral, Schema-valid workflow step fixture."""

    result: dict[str, object] = {"id": step_id, "argv": ["tool", step_id]}
    if dependencies:
        result["depends_on"] = list(dependencies)
    result.update(fields)
    return result


def execution_manifest(
    steps: list[dict[str, object]],
    *,
    schema_version: str = "1.0",
    manifest_path: str = "vibe-workflow.json",
) -> dict[str, object]:
    """Build the relevant shape returned by load_manifest(..., for_execution=True)."""

    return {
        "contract_version": "1.0",
        "ok": True,
        "mode": "execution-request",
        "schema_version": schema_version,
        "schema_path": "schemas/project-workflow-manifest.schema.json",
        "manifest_path": manifest_path,
        "steps": steps,
    }


class DagPlanCompilerTest(unittest.TestCase):
    def assert_plan_error(
        self,
        manifest: dict[str, object],
        expected: dict[str, object],
    ) -> PlanCompileError:
        error: PlanCompileError | None = None
        reports = []
        for _ in range(3):
            with self.assertRaises(PlanCompileError) as caught:
                compile_plan(manifest)
            error = caught.exception
            reports.append(error.report())
        self.assertEqual([expected, expected, expected], reports)
        assert error is not None
        return error

    def test_ac1_single_parallel_and_join_use_declaration_order_tie_break(self) -> None:
        single = execution_manifest([step("only")])
        branches = execution_manifest(
            [step("second"), step("first"), step("join", "first", "second")]
        )

        self.assertEqual(["only"], compile_plan(single)["step_order"])
        for _ in range(3):
            self.assertEqual(
                ["second", "first", "join"],
                compile_plan(branches)["step_order"],
            )

        reordered = execution_manifest(
            [step("first"), step("second"), step("join", "first", "second")]
        )
        self.assertEqual(["first", "second", "join"], compile_plan(reordered)["step_order"])

    def test_ac1_newly_ready_earlier_declaration_wins_next_selection(self) -> None:
        manifest = execution_manifest(
            [step("early", "last"), step("middle"), step("last")]
        )

        self.assertEqual(["middle", "last", "early"], compile_plan(manifest)["step_order"])

    def test_ac2_plan_result_and_identity_are_byte_stable(self) -> None:
        steps = [
            step(
                "prepare",
                environment={"LANGUAGE": "neutral"},
                artifacts=["output/report.json"],
                acceptance_mapping=["AC-1"],
            ),
            step("verify", "prepare", working_directory="workspace"),
        ]
        manifest = execution_manifest(steps)

        first = compile_plan(manifest)
        second = compile_plan(manifest)
        machine_first = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
        machine_second = json.dumps(second, ensure_ascii=False, separators=(",", ":"))

        self.assertEqual(machine_first.encode("utf-8"), machine_second.encode("utf-8"))
        self.assertRegex(first["plan_id"], re.compile(r"^sha256:[0-9a-f]{64}$"))
        self.assertEqual("1.0", first["contract_version"])
        self.assertTrue(first["ok"])
        self.assertEqual(steps, first["steps"])

        identity = {
            "contract_version": "1.0",
            "schema_version": "1.0",
            "steps": steps,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(f"sha256:{hashlib.sha256(canonical).hexdigest()}", first["plan_id"])

    def test_ac2_every_execution_field_schema_and_contract_version_affect_identity(self) -> None:
        base_step = step(
            "prepare",
            environment={"MODE": "check"},
            artifacts=["output/report.json"],
            acceptance_mapping=["AC-1"],
            working_directory="workspace",
        )
        baseline_manifest = execution_manifest([base_step])
        baseline = compile_plan(baseline_manifest)["plan_id"]

        mutations = (
            {**base_step, "id": "prepare-other"},
            {**base_step, "argv": ["tool", "changed"]},
            {**base_step, "depends_on": []},
            {**base_step, "working_directory": "other"},
            {**base_step, "environment": {"MODE": "apply"}},
            {**base_step, "artifacts": ["output/other.json"]},
            {**base_step, "acceptance_mapping": ["AC-2"]},
        )
        for changed_step in mutations:
            self.assertNotEqual(
                baseline,
                compile_plan(execution_manifest([changed_step]))["plan_id"],
            )

        self.assertNotEqual(
            baseline,
            compile_plan(execution_manifest([base_step], schema_version="1.1"))["plan_id"],
        )
        with patch.object(vibe_workflow_plan, "CONTRACT_VERSION", "1.1"):
            changed_contract = compile_plan(baseline_manifest)
        self.assertEqual("1.1", changed_contract["contract_version"])
        self.assertNotEqual(baseline, changed_contract["plan_id"])

    def test_ac2_machine_paths_are_excluded_from_identity_and_result(self) -> None:
        steps = [step("prepare")]
        first = execution_manifest(steps, manifest_path="/machine-one/private/workflow.json")
        second = execution_manifest(steps, manifest_path="/machine-two/private/workflow.json")
        first["project_root"] = "/machine-one/private/project"
        first["framework_root"] = "/machine-one/private/framework"
        second["project_root"] = "/machine-two/private/project"
        second["framework_root"] = "/machine-two/private/framework"

        first_plan = compile_plan(first)
        second_plan = compile_plan(second)

        self.assertEqual(first_plan, second_plan)
        serialized = json.dumps(first_plan, ensure_ascii=False)
        self.assertNotIn("/machine-one", serialized)
        self.assertNotIn("/machine-two", serialized)

    def test_ac2_unicode_scalar_values_compile_to_utf8_identity(self) -> None:
        steps = [
            step(
                "准备",
                environment={"语言": "中性"},
                artifacts=["输出/😀.json"],
            )
        ]

        plan = compile_plan(execution_manifest(steps))
        encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        self.assertIn("准备".encode("utf-8"), encoded)
        self.assertRegex(plan["plan_id"], re.compile(r"^sha256:[0-9a-f]{64}$"))

    def test_ac3_missing_dependency_precedes_self_dependency_and_uses_stable_order(self) -> None:
        manifest = execution_manifest(
            [
                step("first", "unknown-first", "unknown-second", "first"),
                step("second", "unknown-third"),
            ]
        )
        expected = {
            "contract_version": "1.0",
            "ok": False,
            "error": {
                "code": PLAN_DEPENDENCY_NOT_FOUND,
                "message": "workflow plan references an unknown dependency",
                "details": {"step_id": "first", "dependency_id": "unknown-first"},
            },
        }

        error = self.assert_plan_error(manifest, expected)
        self.assertEqual(expected, error.report())

        later_missing = execution_manifest(
            [step("self-first", "self-first"), step("later", "unknown")]
        )
        self.assertEqual(
            PLAN_DEPENDENCY_NOT_FOUND,
            self.assert_plan_error(
                later_missing,
                {
                    "contract_version": "1.0",
                    "ok": False,
                    "error": {
                        "code": PLAN_DEPENDENCY_NOT_FOUND,
                        "message": "workflow plan references an unknown dependency",
                        "details": {"step_id": "later", "dependency_id": "unknown"},
                    },
                },
            ).code,
        )

    def test_ac3_self_dependency_uses_step_declaration_order(self) -> None:
        manifest = execution_manifest(
            [step("first"), step("second", "second"), step("third", "third")]
        )

        self.assert_plan_error(
            manifest,
            {
                "contract_version": "1.0",
                "ok": False,
                "error": {
                    "code": PLAN_SELF_DEPENDENCY,
                    "message": "workflow step may not depend on itself",
                    "details": {"step_id": "second"},
                },
            },
        )

    def test_ac4_cycle_is_closed_rotated_and_uses_execution_edge_direction(self) -> None:
        manifest = execution_manifest(
            [step("c", "b"), step("a", "c"), step("b", "a")]
        )

        self.assert_plan_error(
            manifest,
            {
                "contract_version": "1.0",
                "ok": False,
                "error": {
                    "code": PLAN_CYCLE_DETECTED,
                    "message": "workflow plan contains a dependency cycle",
                    "details": {"cycle": ["a", "b", "c", "a"], "cycle_length": 3},
                },
            },
        )

    def test_ac4_shortest_then_lexicographically_smallest_cycle_is_stable(self) -> None:
        declarations = (
            [
                step("long-c", "long-b"),
                step("z", "y"),
                step("long-a", "long-c"),
                step("a", "b"),
                step("y", "z"),
                step("long-b", "long-a"),
                step("b", "a"),
            ],
            [
                step("b", "a"),
                step("long-b", "long-a"),
                step("y", "z"),
                step("a", "b"),
                step("long-a", "long-c"),
                step("z", "y"),
                step("long-c", "long-b"),
            ],
        )
        expected_details = {"cycle": ["a", "b", "a"], "cycle_length": 2}

        for steps in declarations:
            with self.assertRaises(PlanCompileError) as caught:
                compile_plan(execution_manifest(steps))
            self.assertEqual(PLAN_CYCLE_DETECTED, caught.exception.code)
            self.assertEqual(expected_details, caught.exception.details)

    def test_ac4_overlapping_equal_cycles_choose_smallest_normalized_sequence(self) -> None:
        manifest = execution_manifest(
            [
                step("d", "b"),
                step("c", "b"),
                step("a", "d", "c"),
                step("b", "a"),
            ]
        )

        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(manifest)

        self.assertEqual(
            {"cycle": ["a", "b", "c", "a"], "cycle_length": 3},
            caught.exception.details,
        )

    def test_ac4_unicode_code_point_order_selects_canonical_cycle(self) -> None:
        manifest = execution_manifest(
            [
                step("😀", "中"),
                step("β", "é"),
                step("中", "😀"),
                step("é", "β"),
            ]
        )

        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(manifest)

        self.assertEqual(
            {"cycle": ["é", "β", "é"], "cycle_length": 2},
            caught.exception.details,
        )

    def test_success_and_every_failure_path_are_file_and_process_free(self) -> None:
        scenarios = (
            execution_manifest([step("valid")]),
            execution_manifest([step("missing", "unknown")]),
            execution_manifest([step("self", "self")]),
            execution_manifest([step("a", "b"), step("b", "a")]),
        )

        targets = (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (os, "system"),
            (os, "open"),
            (Path, "open"),
        )
        with ExitStack() as stack:
            operations = [stack.enter_context(patch.object(owner, name)) for owner, name in targets]
            operations.append(stack.enter_context(patch("builtins.open")))
            compile_plan(scenarios[0])
            for manifest in scenarios[1:]:
                with self.assertRaises(PlanCompileError):
                    compile_plan(manifest)
            for operation in operations:
                operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
