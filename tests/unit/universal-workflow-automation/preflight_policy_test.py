"""Acceptance coverage for STORY-UWA-005's workflow preflight policy."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping as AbstractMapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Mapping, NoReturn
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_workflow_preflight import (
    ARGUMENT_MESSAGE,
    ENVIRONMENT_MESSAGE,
    PATH_MESSAGE,
    PREFLIGHT_ARGUMENT_INVALID,
    PREFLIGHT_ENVIRONMENT_INVALID,
    PREFLIGHT_PATH_INVALID,
    PreflightError,
    preflight_plan,
)


PROJECT = Path("/virtual/project")


def step(step_id: str = "prepare", **fields: object) -> dict[str, object]:
    """Build one neutral, Schema-valid plan step."""

    result: dict[str, object] = {"id": step_id, "argv": ["neutral-runner"]}
    result.update(fields)
    return result


def plan(*steps: dict[str, object]) -> dict[str, object]:
    """Build one successful Story 003 plan result."""

    return {
        "contract_version": "1.0",
        "ok": True,
        "plan_id": "sha256:" + "a" * 64,
        "step_order": [item["id"] for item in steps],
        "steps": list(steps),
    }


class FakePathInspector:
    """Pure in-memory canonical-path and directory seam."""

    def __init__(
        self,
        *,
        resolutions: Mapping[Path, Path] | None = None,
        directories: set[Path] | None = None,
    ) -> None:
        self.resolutions = dict(resolutions or {})
        self.directories = set(directories or {PROJECT})

    def resolve(self, path: Path) -> Path:
        return self.resolutions.get(path, path)

    def is_dir(self, path: Path) -> bool:
        return path in self.directories


class FailingPathInspector(FakePathInspector):
    """Raise a path-bearing filesystem error for one declared candidate."""

    def __init__(self, failed_path: Path) -> None:
        super().__init__()
        self.failed_path = failed_path

    def resolve(self, path: Path) -> Path:
        if path == self.failed_path:
            raise RuntimeError(f"symlink loop from {path}")
        return super().resolve(path)


class FailingDirectoryInspector(FakePathInspector):
    """Raise a path-bearing filesystem error during directory inspection."""

    def is_dir(self, path: Path) -> bool:
        raise OSError(f"permission denied for {path}")


class ForbiddenEnvironment(AbstractMapping[str, str]):
    """Fail if preflight reads host environment state."""

    def _fail(self) -> NoReturn:
        raise AssertionError("preflight must not read host environment")

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
def forbid_processes_and_writes() -> Iterator[None]:
    """Fail on process launch or filesystem mutation."""

    targets = (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "call"),
        (subprocess, "check_call"),
        (subprocess, "check_output"),
        (os, "system"),
        (os, "popen"),
        (Path, "write_text"),
        (Path, "write_bytes"),
        (Path, "touch"),
        (Path, "mkdir"),
        (Path, "unlink"),
        (Path, "rename"),
        (Path, "replace"),
    )
    with ExitStack() as stack:
        operations: list[Mock] = [
            stack.enter_context(patch.object(owner, name)) for owner, name in targets
        ]
        operations.append(stack.enter_context(patch("builtins.open")))
        yield
        for operation in operations:
            operation.assert_not_called()


def expected_error(
    code: str,
    message: str,
    details: dict[str, object],
) -> dict[str, object]:
    """Build one exact versioned preflight error report."""

    return {
        "contract_version": "1.0",
        "ok": False,
        "error": {"code": code, "message": message, "details": details},
    }


class PreflightPolicyTest(unittest.TestCase):
    """Verify the complete Story 005 preflight contract."""

    def assert_preflight_error(
        self,
        candidate: dict[str, object],
        expected: dict[str, object],
        inspector: FakePathInspector | None = None,
    ) -> PreflightError:
        error: PreflightError | None = None
        reports = []
        for _ in range(2):
            with forbid_processes_and_writes():
                with self.assertRaises(PreflightError) as caught:
                    preflight_plan(
                        candidate,
                        PROJECT,
                        inspector=inspector or FakePathInspector(),
                    )
            error = caught.exception
            reports.append(error.report())
        self.assertEqual([expected, expected], reports)
        assert error is not None
        self.assertFalse(error.retryable)
        return error

    def assert_priority_scenario(
        self,
        candidate: dict[str, object],
        expected: dict[str, object],
    ) -> None:
        """Assert deterministic first-error selection without side effects."""

        before = copy.deepcopy(candidate)
        error = self.assert_preflight_error(candidate, expected)
        self.assertEqual(before, candidate)
        self.assertNotIn(str(PROJECT), json.dumps(error.report()))

    def test_ac1_paths_normalize_without_mutating_the_plan(self) -> None:
        candidate = plan(
            step(
                working_directory="workspace\\nested\\..",
                artifacts=["output\\report.json", "future/result.bin"],
            )
        )
        original = copy.deepcopy(candidate)
        inspector = FakePathInspector(directories={PROJECT, PROJECT / "workspace"})

        with forbid_processes_and_writes():
            result = preflight_plan(candidate, PROJECT, inspector=inspector)

        self.assertEqual("workspace", result["steps"][0]["working_directory"])
        self.assertEqual(
            ["output/report.json", "future/result.bin"],
            result["steps"][0]["artifacts"],
        )
        self.assertEqual(original, candidate)
        self.assertNotIn(str(PROJECT), json.dumps(result))

    def test_ac1_cross_platform_escapes_fail_closed(self) -> None:
        cases = (
            ("/outside", "ABSOLUTE"),
            ("C:\\outside", "ABSOLUTE"),
            ("\\\\server\\share", "ABSOLUTE"),
            ("../../outside", "OUTSIDE_PROJECT"),
            ("bad\x00path", "INVALID_CHARACTER"),
        )
        for value, reason in cases:
            self.assert_preflight_error(
                plan(step(working_directory=value)),
                expected_error(
                    PREFLIGHT_PATH_INVALID,
                    PATH_MESSAGE,
                    {
                        "step_id": "prepare",
                        "field": "working_directory",
                        "reason": reason,
                    },
                ),
            )

    def test_ac1_working_directory_symlinks_and_missing_paths_fail_closed(self) -> None:
        working_directory_cases = (
            (
                "linked",
                "SYMLINK_ESCAPE",
                FakePathInspector(resolutions={PROJECT / "linked": Path("/outside")}),
            ),
            ("missing", "NOT_DIRECTORY", FakePathInspector()),
        )
        for value, reason, inspector in working_directory_cases:
            self.assert_preflight_error(
                plan(step(working_directory=value)),
                expected_error(
                    PREFLIGHT_PATH_INVALID,
                    PATH_MESSAGE,
                    {
                        "step_id": "prepare",
                        "field": "working_directory",
                        "reason": reason,
                    },
                ),
                inspector,
            )

    def test_ac1_artifact_symlink_escape_fails_closed(self) -> None:
        self.assert_preflight_error(
            plan(step(artifacts=["linked/output.bin"])),
            expected_error(
                PREFLIGHT_PATH_INVALID,
                PATH_MESSAGE,
                {
                    "step_id": "prepare",
                    "field": "artifacts",
                    "index": 0,
                    "reason": "SYMLINK_ESCAPE",
                },
            ),
            FakePathInspector(
                resolutions={
                    PROJECT / "linked" / "output.bin": Path("/outside/output.bin")
                }
            ),
        )

    def test_ac1_filesystem_failures_are_stable_and_sanitized(self) -> None:
        expected = expected_error(
            PREFLIGHT_PATH_INVALID,
            PATH_MESSAGE,
            {
                "step_id": "prepare",
                "field": "working_directory",
                "reason": "RESOLUTION_FAILED",
            },
        )
        cases = (
            ("loop", FailingPathInspector(PROJECT / "loop")),
            ("denied", FailingDirectoryInspector()),
        )
        for working_directory, inspector in cases:
            error = self.assert_preflight_error(
                plan(step(working_directory=working_directory)),
                expected,
                inspector,
            )
            self.assertNotIn(str(PROJECT), json.dumps(error.report()))

    def test_ac1_default_working_directory_and_artifact_index_are_stable(self) -> None:
        result = preflight_plan(
            plan(step(artifacts=["future/output.bin"])),
            PROJECT,
            inspector=FakePathInspector(),
        )
        self.assertEqual(".", result["steps"][0]["working_directory"])

        self.assert_preflight_error(
            plan(step(artifacts=["valid.bin", "../outside.bin"])),
            expected_error(
                PREFLIGHT_PATH_INVALID,
                PATH_MESSAGE,
                {
                    "step_id": "prepare",
                    "field": "artifacts",
                    "index": 1,
                    "reason": "OUTSIDE_PROJECT",
                },
            ),
        )

    def test_ac2_argv_is_literal_and_invalid_values_never_start_processes(self) -> None:
        literal = ["neutral-runner", "值 with space", ";", "|", "$HOME"]
        with forbid_processes_and_writes():
            result = preflight_plan(
                plan(step(argv=literal)),
                PROJECT,
                inspector=FakePathInspector(),
            )
        self.assertEqual(literal, result["steps"][0]["argv"])

        invalid_arguments = (
            ("bad\x00arg", "NUL"),
            ("${{unknown}}", "UNDECLARED_INTERPOLATION"),
        )
        for argument, reason in invalid_arguments:
            self.assert_preflight_error(
                plan(step(argv=["neutral-runner", argument])),
                expected_error(
                    PREFLIGHT_ARGUMENT_INVALID,
                    ARGUMENT_MESSAGE,
                    {"step_id": "prepare", "argument_index": 1, "reason": reason},
                ),
            )

    def test_ac3_environment_is_portable_literal_and_host_independent(self) -> None:
        candidate = plan(step(environment={"EMPTY": "", "UNICODE": "值"}))
        with patch.dict(os.environ, {"HOST_ONLY": "first"}, clear=True):
            first = preflight_plan(candidate, PROJECT, inspector=FakePathInspector())
        with patch.dict(os.environ, {"OTHER_HOST": "second"}, clear=True):
            second = preflight_plan(candidate, PROJECT, inspector=FakePathInspector())
        first_bytes = json.dumps(first, ensure_ascii=False, sort_keys=True).encode()
        second_bytes = json.dumps(second, ensure_ascii=False, sort_keys=True).encode()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            {"EMPTY": "", "UNICODE": "值"},
            first["steps"][0]["environment"],
        )
        empty = preflight_plan(
            plan(step(environment={})),
            PROJECT,
            inspector=FakePathInspector(),
        )
        self.assertEqual({}, empty["steps"][0]["environment"])

        with patch.object(os, "environ", ForbiddenEnvironment()):
            preflight_plan(candidate, PROJECT, inspector=FakePathInspector())

        cases = (
            ({"BAD-NAME": "value"}, "BAD-NAME", "INVALID_NAME"),
            ({"VALUE": "bad\x00value"}, "VALUE", "NUL"),
            ({"VALUE": "${{unknown}}"}, "VALUE", "UNDECLARED_INTERPOLATION"),
        )
        for environment, key, reason in cases:
            self.assert_preflight_error(
                plan(step(environment=environment)),
                expected_error(
                    PREFLIGHT_ENVIRONMENT_INVALID,
                    ENVIRONMENT_MESSAGE,
                    {"step_id": "prepare", "key": key, "reason": reason},
                ),
            )

    def test_ac4_paths_precede_later_fields_without_side_effects(self) -> None:
        candidate = plan(
            step(
                artifacts=["../outside"],
                argv=["bad\x00arg"],
                environment={"BAD-NAME": "value"},
            )
        )
        expected = expected_error(
            PREFLIGHT_PATH_INVALID,
            PATH_MESSAGE,
            {
                "step_id": "prepare",
                "field": "artifacts",
                "index": 0,
                "reason": "OUTSIDE_PROJECT",
            },
        )
        self.assert_priority_scenario(candidate, expected)

    def test_ac4_working_directory_precedes_artifacts(self) -> None:
        candidate = plan(
            step(working_directory="../outside", artifacts=["../artifact"])
        )
        expected = expected_error(
            PREFLIGHT_PATH_INVALID,
            PATH_MESSAGE,
            {
                "step_id": "prepare",
                "field": "working_directory",
                "reason": "OUTSIDE_PROJECT",
            },
        )
        self.assert_priority_scenario(candidate, expected)

    def test_ac4_arguments_precede_environment_without_side_effects(self) -> None:
        candidate = plan(
            step(argv=["bad\x00arg"], environment={"BAD-NAME": "value"})
        )
        expected = expected_error(
            PREFLIGHT_ARGUMENT_INVALID,
            ARGUMENT_MESSAGE,
            {"step_id": "prepare", "argument_index": 0, "reason": "NUL"},
        )
        self.assert_priority_scenario(candidate, expected)

    def test_ac4_environment_and_step_order_are_deterministic(self) -> None:
        environment_candidate = plan(
            step(environment={"Z-BAD": "value", "A-BAD": "value"})
        )
        environment_expected = expected_error(
            PREFLIGHT_ENVIRONMENT_INVALID,
            ENVIRONMENT_MESSAGE,
            {"step_id": "prepare", "key": "A-BAD", "reason": "INVALID_NAME"},
        )
        self.assert_priority_scenario(environment_candidate, environment_expected)

        step_candidate = plan(
            step("first", environment={"BAD-NAME": "value"}),
            step("second", artifacts=["../outside"]),
        )
        step_expected = expected_error(
            PREFLIGHT_ENVIRONMENT_INVALID,
            ENVIRONMENT_MESSAGE,
            {"step_id": "first", "key": "BAD-NAME", "reason": "INVALID_NAME"},
        )
        self.assert_priority_scenario(step_candidate, step_expected)


if __name__ == "__main__":
    unittest.main()
