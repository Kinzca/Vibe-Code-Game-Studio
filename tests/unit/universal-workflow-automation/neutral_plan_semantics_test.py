"""Pure acceptance coverage for STORY-UWA-004's neutral plan semantics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, NoReturn
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_workflow_plan import (
    PLAN_CYCLE_DETECTED,
    PLAN_DEPENDENCY_NOT_FOUND,
    PLAN_SELF_DEPENDENCY,
    PlanCompileError,
    compile_plan,
)


def step(step_id: str, *dependencies: str, **fields: object) -> dict[str, object]:
    """Build one artificial, Schema-valid workflow step fixture."""

    result: dict[str, object] = {"id": step_id, "argv": ["neutral-runner", step_id]}
    if dependencies:
        result["depends_on"] = list(dependencies)
    result.update(fields)
    return result


def execution_manifest(steps: list[dict[str, object]]) -> dict[str, object]:
    """Build the relevant in-memory shape returned by the public loader."""

    return {
        "contract_version": "1.0",
        "ok": True,
        "mode": "execution-request",
        "schema_version": "1.0",
        "schema_path": "schemas/project-workflow-manifest.schema.json",
        "manifest_path": "vibe-workflow.json",
        "steps": steps,
    }


def canonical_bytes(value: object) -> bytes:
    """Serialize one machine result through the stable canonical JSON form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ForbiddenEnvironment(Mapping[str, str]):
    """Fail if plan compilation tries to inspect process environment state."""

    def _fail(self) -> NoReturn:
        raise AssertionError("compile_plan must not inspect the process environment")

    def __getitem__(self, key: str) -> str:
        self._fail()

    def __contains__(self, key: object) -> bool:
        self._fail()

    def __iter__(self) -> Iterator[str]:
        self._fail()

    def __len__(self) -> int:
        self._fail()

    def get(self, key: str, default: object = None) -> NoReturn:
        self._fail()

    def keys(self) -> NoReturn:
        self._fail()

    def items(self) -> NoReturn:
        self._fail()

    def values(self) -> NoReturn:
        self._fail()

    def copy(self) -> NoReturn:
        self._fail()


@contextmanager
def forbid_runtime_probes() -> Iterator[None]:
    """Reject process, environment, file, metadata, and directory probes."""

    targets = (
        (subprocess, "run"), (subprocess, "Popen"), (subprocess, "call"),
        (subprocess, "check_call"), (subprocess, "check_output"),
        (os, "system"), (os, "open"), (os, "popen"), (os, "getenv"),
        (os, "getcwd"), (os, "access"), (os, "listdir"), (os, "scandir"),
        (os, "walk"), (os.path, "exists"),
        (os.path, "isfile"), (os.path, "isdir"), (shutil, "which"),
        (Path, "open"), (Path, "read_text"), (Path, "read_bytes"),
        (Path, "cwd"), (Path, "home"), (Path, "exists"),
        (Path, "is_file"), (Path, "is_dir"),
        (Path, "stat"), (Path, "iterdir"), (Path, "glob"), (Path, "rglob"),
    )
    with ExitStack() as stack:
        operations: list[Mock] = [
            stack.enter_context(patch.object(owner, name)) for owner, name in targets
        ]
        operations.append(stack.enter_context(patch("builtins.open")))
        stack.enter_context(patch.object(os, "environ", ForbiddenEnvironment()))
        yield
        for operation in operations:
            operation.assert_not_called()


def expected_error(code: str, message: str, details: dict[str, object]) -> dict[str, object]:
    """Build one exact versioned plan error report."""

    return {
        "contract_version": "1.0",
        "ok": False,
        "error": {"code": code, "message": message, "details": details},
    }


def priority_scenarios(prefix: str) -> tuple[tuple[dict[str, object], dict[str, object]], ...]:
    """Build compound graphs proving missing, self, then cycle priority."""

    node, unknown = f"{prefix}-node", f"{prefix}-unknown"
    alpha, beta = f"{prefix}-alpha", f"{prefix}-beta"
    cycle = [step(alpha, beta), step(beta, alpha)]
    self_step, missing_step = step(node, node), step(f"{prefix}-missing", unknown)
    missing = expected_error(
        PLAN_DEPENDENCY_NOT_FOUND,
        "workflow plan references an unknown dependency",
        {"step_id": f"{prefix}-missing", "dependency_id": unknown},
    )
    self_error = expected_error(
        PLAN_SELF_DEPENDENCY,
        "workflow step may not depend on itself",
        {"step_id": node},
    )
    cycle_error = expected_error(
        PLAN_CYCLE_DETECTED,
        "workflow plan contains a dependency cycle",
        {"cycle": [alpha, beta, alpha], "cycle_length": 2},
    )
    return (
        (execution_manifest(cycle + [self_step, missing_step]), missing),
        (execution_manifest(cycle + [self_step]), self_error),
        (execution_manifest(cycle), cycle_error),
    )


class NeutralPlanSemanticsTest(unittest.TestCase):
    """Verify that pure plan compilation has no project or engine semantics."""

    def assert_stable_error(
        self,
        manifest: dict[str, object],
        expected: dict[str, object],
    ) -> None:
        reports = []
        for _ in range(2):
            with self.assertRaises(PlanCompileError) as caught:
                compile_plan(manifest)
            reports.append(caught.exception.report())
        self.assertEqual([expected, expected], reports)

    def test_ac1_environment_metadata_does_not_change_plan(self) -> None:
        steps = [step("prepare"), step("verify", "prepare")]
        first = execution_manifest(steps)
        second = execution_manifest(steps)
        first.update(project_root="/machine-a/project", framework_root="/machine-a/framework")
        second.update(project_root="/machine-b/project", framework_root="/machine-b/framework")
        first["manifest_path"] = "alpha/vibe-workflow.json"
        second["manifest_path"] = "beta/vibe-workflow.json"

        with forbid_runtime_probes():
            first_plan = compile_plan(first)
            second_plan = compile_plan(second)

        self.assertEqual(canonical_bytes(first_plan), canonical_bytes(second_plan))

    def test_ac2_project_and_engine_style_strings_remain_declared_data(self) -> None:
        first_steps = [
            step("project-style-prepare", environment={"STYLE_MODE": "engine-style"}),
            step("engine-style-verify", "project-style-prepare"),
        ]
        second_steps = json.loads(json.dumps(first_steps))
        second_steps[0]["environment"] = {"STYLE_MODE": "project-style"}

        with forbid_runtime_probes():
            first_plan = compile_plan(execution_manifest(first_steps))
            second_plan = compile_plan(execution_manifest(second_steps))

        expected_keys = {"contract_version", "ok", "plan_id", "step_order", "steps"}
        self.assertEqual(expected_keys, set(first_plan))
        self.assertEqual(first_steps, first_plan["steps"])
        self.assertEqual(second_steps, second_plan["steps"])
        self.assertEqual(first_plan["step_order"], second_plan["step_order"])
        self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])

    def test_ac3_language_style_runner_names_share_generic_plan_semantics(self) -> None:
        alpha = [
            step("prepare", argv=["language-alpha-runner", "prepare"]),
            step("verify", "prepare", argv=["language-alpha-runner", "verify"]),
        ]
        beta = [
            step("prepare", argv=["language-beta-runner", "prepare"]),
            step("verify", "prepare", argv=["language-beta-runner", "verify"]),
        ]

        with forbid_runtime_probes():
            alpha_plan = compile_plan(execution_manifest(alpha))
            beta_plan = compile_plan(execution_manifest(beta))

        self.assertEqual(set(alpha_plan), set(beta_plan))
        self.assertEqual(alpha_plan["step_order"], beta_plan["step_order"])
        self.assertEqual(alpha, alpha_plan["steps"])
        self.assertEqual(beta, beta_plan["steps"])
        self.assertNotEqual(alpha_plan["plan_id"], beta_plan["plan_id"])

    def test_ac4_dependency_errors_use_stable_graph_only_priority(self) -> None:
        with forbid_runtime_probes():
            for prefix in ("plain", "project-style", "engine-style", "language-style"):
                for manifest, expected in priority_scenarios(prefix):
                    self.assert_stable_error(manifest, expected)


if __name__ == "__main__":
    unittest.main()
