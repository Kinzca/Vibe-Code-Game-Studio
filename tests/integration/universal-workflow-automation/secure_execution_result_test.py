"""Integration evidence for STORY-UWA-006 secure step execution."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_workflow_execute import (
    EXECUTION_ARTIFACT_INVALID,
    EXECUTION_BOUNDARY_INVALID,
    EXECUTION_CANCELLED,
    EXECUTION_COMMAND_FAILED,
    EXECUTION_NOT_AUTHORIZED,
    EXECUTION_POLICY_INVALID,
    EXECUTION_START_FAILED,
    EXECUTION_TIMED_OUT,
    _BoundedCapture,
    execute_step,
)
from vibe_workflow_preflight import preflight_plan


PLAN_ID = "sha256:" + "6" * 64


def execution_policy(**overrides: Any) -> dict[str, Any]:
    """Build a valid bounded execution policy."""

    policy: dict[str, Any] = {
        "contract_version": "1.0",
        "timeout_seconds": 5,
        "max_log_bytes": 64,
        "termination_grace_seconds": 1,
    }
    policy.update(overrides)
    return policy


def authorized_report(project_root: Path, **step_fields: Any) -> dict[str, Any]:
    """Create a Story 005 success report for one neutral step."""

    step: dict[str, Any] = {
        "id": "run-check",
        "argv": [sys.executable, "-c", "pass"],
    }
    step.update(step_fields)
    return preflight_plan({"plan_id": PLAN_ID, "steps": [step]}, project_root)


def serialized(payload: Any) -> str:
    """Serialize a result for leak assertions."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def process_is_alive(pid: int) -> bool:
    """Return whether a POSIX process remains observable."""

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class FinishedProcess:
    """Minimal completed-process seam used to inspect launch arguments."""

    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.pid = os.getpid()

    def poll(self) -> int:
        return 0


class SequenceClock:
    """Return a stable sequence and then repeat its final value."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class SecureExecutionResultTest(unittest.TestCase):
    """Verify every acceptance criterion of STORY-UWA-006."""

    def assert_result_contract(
        self,
        result: dict[str, Any],
        *,
        has_error: bool,
        plan_id: str | None = PLAN_ID,
        step_id: str = "run-check",
    ) -> None:
        """Assert the complete stable shape shared by every Result Contract outcome."""

        expected_keys = {
            "contract_version", "ok", "plan_id", "step_id", "status",
            "exit_category", "exit_code", "duration_ms", "retryable",
            "stdout", "stderr", "artifacts",
        }
        if has_error:
            expected_keys.add("error")
        self.assertEqual(expected_keys, set(result))
        self.assertEqual("1.0", result["contract_version"])
        self.assertEqual(plan_id, result["plan_id"])
        self.assertEqual(step_id, result["step_id"])
        self.assertIsInstance(result["ok"], bool)
        self.assertEqual(result["status"] == "passed", result["ok"])
        self.assertIn(result["status"], {"passed", "failed", "cancelled"})
        self.assertIn(
            result["exit_category"],
            {"success", "command_failed", "start_failed", "timed_out", "cancelled", "policy_rejected"},
        )
        self.assertTrue(result["exit_code"] is None or isinstance(result["exit_code"], int))
        self.assertIsInstance(result["duration_ms"], int)
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertIs(result["retryable"], False)
        self.assertIsInstance(result["artifacts"], list)
        for artifact in result["artifacts"]:
            self.assertEqual({"artifact_id", "path", "present"}, set(artifact))
            self.assertIsInstance(artifact["artifact_id"], str)
            self.assertRegex(artifact["artifact_id"], r"^sha256:[0-9a-f]{64}$")
            self.assertIsInstance(artifact["path"], str)
            self.assertIsInstance(artifact["present"], bool)
        for stream_name in ("stdout", "stderr"):
            stream = result[stream_name]
            self.assertEqual({"text", "byte_count", "truncated"}, set(stream))
            self.assertIsInstance(stream["text"], str)
            self.assertIsInstance(stream["byte_count"], int)
            self.assertGreaterEqual(stream["byte_count"], 0)
            self.assertIsInstance(stream["truncated"], bool)
        if has_error:
            self.assertEqual({"code", "message", "details"}, set(result["error"]))
            self.assertIsInstance(result["error"]["message"], str)
            self.assertIsInstance(result["error"]["details"], dict)

    def test_ac1_only_authorized_step_uses_structured_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir()
            report = authorized_report(
                root,
                argv=["neutral-runner", "literal;value", "$HOME"],
                working_directory="work",
                environment={"NEUTRAL_VALUE": "alpha"},
            )
            launches: list[tuple[list[str], dict[str, Any]]] = []

            def factory(argv: list[str], **options: Any) -> FinishedProcess:
                launches.append((argv, options))
                return FinishedProcess()

            result = execute_step(
                report,
                "run-check",
                root,
                execution_policy(),
                process_factory=factory,
                clock=SequenceClock(10, 10.125),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(125, result["duration_ms"])
            self.assertEqual(1, len(launches))
            argv, options = launches[0]
            self.assertEqual(["neutral-runner", "literal;value", "$HOME"], argv)
            self.assertIs(False, options["shell"])
            self.assertEqual((root / "work").resolve(), Path(options["cwd"]))
            self.assertEqual("alpha", options["env"]["NEUTRAL_VALUE"])

    def test_ac1_invalid_authorization_and_boundary_start_no_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            outside = base / "outside"
            work = root / "work"
            work.mkdir(parents=True)
            outside.mkdir()
            report = authorized_report(root, working_directory="work")
            work.rmdir()
            work.symlink_to(outside, target_is_directory=True)
            launches = 0

            def forbidden_factory(*_args: Any, **_kwargs: Any) -> FinishedProcess:
                nonlocal launches
                launches += 1
                return FinishedProcess()

            invalid_reports = (
                {"contract_version": "2.0", "ok": True},
                {"contract_version": "1.0", "ok": False},
                report,
            )
            step_ids = ("run-check", "run-check", "missing-step")
            expected_reasons = ("UNSUPPORTED_CONTRACT", "PREFLIGHT_FAILED", "STEP_NOT_FOUND")
            expected_plan_ids = (None, None, PLAN_ID)
            for invalid, step_id, expected_reason, expected_plan_id in zip(
                invalid_reports, step_ids, expected_reasons, expected_plan_ids
            ):
                result = execute_step(
                    invalid,
                    step_id,
                    root,
                    execution_policy(),
                    process_factory=forbidden_factory,
                )
                self.assertEqual(EXECUTION_NOT_AUTHORIZED, result["error"]["code"])
                self.assertEqual(
                    "workflow step is not authorized for execution",
                    result["error"]["message"],
                )
                self.assertEqual({"reason": expected_reason}, result["error"]["details"])
                self.assert_result_contract(
                    result,
                    has_error=True,
                    plan_id=expected_plan_id,
                    step_id=step_id,
                )

            boundary_result = execute_step(
                report,
                "run-check",
                root,
                execution_policy(),
                process_factory=forbidden_factory,
            )
            self.assertEqual(EXECUTION_BOUNDARY_INVALID, boundary_result["error"]["code"])
            self.assertEqual("SYMLINK_ESCAPE", boundary_result["error"]["details"]["reason"])
            self.assert_result_contract(boundary_result, has_error=True)
            self.assertEqual(0, launches)
            self.assertNotIn(str(base), serialized(boundary_result))

    def test_ac1_invalid_policy_values_start_no_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = authorized_report(root)
            invalid_values = (
                {"timeout_seconds": True},
                {"timeout_seconds": float("nan")},
                {"timeout_seconds": float("inf")},
                {"timeout_seconds": 0},
                {"timeout_seconds": 3601},
                {"max_log_bytes": 0},
                {"max_log_bytes": True},
                {"max_log_bytes": 1048577},
                {"termination_grace_seconds": 0},
                {"termination_grace_seconds": 11},
            )

            def forbidden_factory(*_args: Any, **_kwargs: Any) -> FinishedProcess:
                self.fail("invalid policy started a process")

            for overrides in invalid_values:
                result = execute_step(
                    report,
                    "run-check",
                    root,
                    execution_policy(**overrides),
                    process_factory=forbidden_factory,
                )
                self.assertEqual(EXECUTION_POLICY_INVALID, result["error"]["code"])
                field = next(iter(overrides))
                self.assertEqual({"field": field, "reason": "OUT_OF_RANGE"}, result["error"]["details"])
                self.assert_result_contract(result, has_error=True)
                self.assertFalse(result["retryable"])

            unsupported = execute_step(
                report,
                "run-check",
                root,
                execution_policy(contract_version="2.0"),
                process_factory=forbidden_factory,
            )
            self.assertEqual(
                {"field": "contract_version", "reason": "UNSUPPORTED_CONTRACT"},
                unsupported["error"]["details"],
            )
            self.assert_result_contract(unsupported, has_error=True)

    def test_ac2_success_command_failure_and_start_failure_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success = execute_step(
                authorized_report(root),
                "run-check",
                root,
                execution_policy(),
            )
            failed = execute_step(
                authorized_report(root, argv=[sys.executable, "-c", "raise SystemExit(7)"]),
                "run-check",
                root,
                execution_policy(),
            )

            secret_path = str(root / "private" / "runner")

            def fail_start(*_args: Any, **_kwargs: Any) -> FinishedProcess:
                raise OSError(secret_path)

            start_failed = execute_step(
                authorized_report(root),
                "run-check",
                root,
                execution_policy(),
                process_factory=fail_start,
            )

            self.assertEqual(("passed", "success", 0), (
                success["status"], success["exit_category"], success["exit_code"]
            ))
            self.assertNotIn("error", success)
            self.assertEqual(("failed", "command_failed", 7), (
                failed["status"], failed["exit_category"], failed["exit_code"]
            ))
            self.assertEqual(EXECUTION_COMMAND_FAILED, failed["error"]["code"])
            self.assertEqual("workflow step exited with a non-zero code", failed["error"]["message"])
            self.assertEqual({}, failed["error"]["details"])
            self.assertEqual(("failed", "start_failed", None), (
                start_failed["status"], start_failed["exit_category"], start_failed["exit_code"]
            ))
            self.assertEqual(EXECUTION_START_FAILED, start_failed["error"]["code"])
            self.assertEqual({"reason": "OS_ERROR"}, start_failed["error"]["details"])
            self.assertNotIn(secret_path, serialized(start_failed))

            def invalid_launch(*_args: Any, **_kwargs: Any) -> FinishedProcess:
                raise ValueError(secret_path)

            invalid_launch_result = execute_step(
                authorized_report(root),
                "run-check",
                root,
                execution_policy(),
                process_factory=invalid_launch,
            )
            self.assertEqual(EXECUTION_START_FAILED, invalid_launch_result["error"]["code"])
            self.assertEqual("start_failed", invalid_launch_result["exit_category"])
            self.assertNotIn(secret_path, serialized(invalid_launch_result))
            self.assert_result_contract(success, has_error=False)
            for result in (failed, start_failed, invalid_launch_result):
                self.assert_result_contract(result, has_error=True)

    def test_ac3_cancellation_wins_over_timeout_in_same_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancellation = threading.Event()
            cancellation.set()
            natural = execute_step(
                authorized_report(root),
                "run-check",
                root,
                execution_policy(timeout_seconds=0.01),
                cancellation,
                process_factory=lambda *_args, **_kwargs: FinishedProcess(),
                clock=SequenceClock(0, 1),
            )
            result = execute_step(
                authorized_report(
                    root,
                    argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                ),
                "run-check",
                root,
                execution_policy(timeout_seconds=0.01),
                cancellation,
                clock=SequenceClock(0, 10, 10),
                sleeper=lambda _seconds: None,
            )

            self.assertEqual("passed", natural["status"])
            self.assertEqual("cancelled", result["status"])
            self.assertEqual("cancelled", result["exit_category"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual(EXECUTION_CANCELLED, result["error"]["code"])
            self.assertEqual({}, result["error"]["details"])
            self.assert_result_contract(result, has_error=True)

    def test_ac3_timeout_terminates_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group signal evidence is POSIX-only; platform matrix is Story 015")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_signal = root / "child-signal.txt"
            child_ready = root / "child-ready.txt"
            child_code = (
                "import pathlib,signal,time,sys;"
                "target=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2]);"
                "signal.signal(signal.SIGTERM,lambda *_:(target.write_text('terminated'),sys.exit(0)));"
                "ready.write_text('ready');"
                "time.sleep(30)"
            )
            parent_code = (
                "import subprocess,sys,time,pathlib;"
                "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]);"
                "ready=pathlib.Path(sys.argv[3]);"
                "deadline=time.monotonic()+2;"
                "\nwhile not ready.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                "time.sleep(30)"
            )
            report = authorized_report(
                root,
                argv=[
                    sys.executable,
                    "-c",
                    parent_code,
                    child_code,
                    str(child_signal),
                    str(child_ready),
                ],
            )
            result = execute_step(
                report,
                "run-check",
                root,
                execution_policy(timeout_seconds=1, termination_grace_seconds=1),
            )

            self.assertEqual("timed_out", result["exit_category"])
            self.assertEqual(EXECUTION_TIMED_OUT, result["error"]["code"])
            self.assertEqual({"timeout_seconds": 1.0}, result["error"]["details"])
            self.assert_result_contract(result, has_error=True)
            self.assertTrue(child_ready.exists())
            self.assertEqual("terminated", child_signal.read_text(encoding="utf-8"))

    def test_ac3_natural_parent_exit_does_not_leave_pipe_holder_or_hang(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX process-group evidence; platform matrix is Story 015")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            parent_code = (
                "import pathlib,subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            )
            started = time.monotonic()
            result = execute_step(
                authorized_report(
                    root,
                    argv=[sys.executable, "-c", parent_code, str(pid_file)],
                ),
                "run-check",
                root,
                execution_policy(termination_grace_seconds=0.1),
            )
            elapsed = time.monotonic() - started

            self.assertEqual("passed", result["status"])
            self.assertLess(elapsed, 2)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while process_is_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(process_is_alive(child_pid))

    def test_ac3_timeout_reclaims_descendant_that_left_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX detached-descendant evidence; platform matrix is Story 015")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "detached.pid"
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                "start_new_session=True);"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
                "time.sleep(30)"
            )
            result = execute_step(
                authorized_report(
                    root,
                    argv=[sys.executable, "-c", parent_code, str(pid_file)],
                ),
                "run-check",
                root,
                execution_policy(timeout_seconds=0.5, termination_grace_seconds=0.2),
            )

            self.assertEqual("timed_out", result["exit_category"])
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while process_is_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(process_is_alive(child_pid))

    def test_ac3_timeout_forces_process_that_ignores_graceful_signal(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX forced-reclaim evidence; platform matrix is Story 015")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "stubborn.pid"
            script = (
                "import os,pathlib,signal,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                "time.sleep(30)"
            )
            result = execute_step(
                authorized_report(root, argv=[sys.executable, "-c", script, str(pid_file)]),
                "run-check",
                root,
                execution_policy(timeout_seconds=0.5, termination_grace_seconds=0.1),
            )

            self.assertEqual("timed_out", result["exit_category"])
            process_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertFalse(process_is_alive(process_pid))

    def test_ac4_streams_are_drained_bounded_and_decoded_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import os;"
                "os.write(1,b'\\xffabcdef');"
                "os.write(2,b'wxyz');"
                "os.write(1,b'q'*200000);"
                "os.write(2,b'r'*200000)"
            )
            result = execute_step(
                authorized_report(root, argv=[sys.executable, "-c", script]),
                "run-check",
                root,
                execution_policy(max_log_bytes=4),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(200007, result["stdout"]["byte_count"])
            self.assertEqual(200004, result["stderr"]["byte_count"])
            self.assertEqual("\ufffdabc", result["stdout"]["text"])
            self.assertEqual("wxyz", result["stderr"]["text"])
            self.assertTrue(result["stdout"]["truncated"])
            self.assertTrue(result["stderr"]["truncated"])
            self.assertLessEqual(len(result["stdout"]["text"]), 4)
            self.assertLessEqual(len(result["stderr"]["text"]), 4)
            capture = _BoundedCapture(4)
            capture.feed(b"z" * 200000)
            self.assertEqual(4, capture.retained_bytes)
            self.assertEqual(200000, capture.report()["byte_count"])

    def test_ac4_exact_limit_is_not_marked_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_step(
                authorized_report(
                    root,
                    argv=[sys.executable, "-c", "import os;os.write(1,b'abcd')"],
                ),
                "run-check",
                root,
                execution_policy(max_log_bytes=4),
            )
            self.assertEqual(
                {"text": "abcd", "byte_count": 4, "truncated": False},
                result["stdout"],
            )
            self.assertEqual(
                {"text": "", "byte_count": 0, "truncated": False},
                result["stderr"],
            )

    def test_ac5_artifacts_have_stable_identity_order_and_presence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_step(
                authorized_report(
                    root,
                    argv=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path;Path('present.bin').write_bytes(b'ok')",
                    ],
                    artifacts=["present.bin", "missing.bin"],
                ),
                "run-check",
                root,
                execution_policy(),
            )
            expected_ids = []
            for path in ("present.bin", "missing.bin"):
                canonical = json.dumps(
                    [PLAN_ID, "run-check", path],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                expected_ids.append(f"sha256:{hashlib.sha256(canonical).hexdigest()}")

            self.assertEqual(["present.bin", "missing.bin"], [item["path"] for item in result["artifacts"]])
            self.assertEqual(expected_ids, [item["artifact_id"] for item in result["artifacts"]])
            self.assertEqual([True, False], [item["present"] for item in result["artifacts"]])
            self.assert_result_contract(result, has_error=False)

    def test_ac5_artifact_symlink_escape_is_rejected_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            outside = base / "outside-secret"
            root.mkdir()
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("do-not-leak", encoding="utf-8")
            host_secret = "abc"
            script = (
                "import os,sys;"
                "print(os.environ['HOST_SECRET_TOKEN']);"
                "print(os.environ['NORMAL_LEVEL']);"
                "print(sys.argv[1]);"
                "os.symlink(sys.argv[1],'future',target_is_directory=True)"
            )
            report = authorized_report(
                root,
                argv=[sys.executable, "-c", script, str(outside)],
                artifacts=["future/secret.txt"],
            )
            with mock.patch.dict(
                os.environ,
                {"HOST_SECRET_TOKEN": host_secret, "NORMAL_LEVEL": "1"},
            ):
                result = execute_step(
                    report,
                    "run-check",
                    root,
                    execution_policy(),
                )

            self.assertEqual("policy_rejected", result["exit_category"])
            self.assertEqual(EXECUTION_ARTIFACT_INVALID, result["error"]["code"])
            self.assertEqual("future/secret.txt", result["error"]["details"]["path"])
            self.assertEqual("SYMLINK_ESCAPE", result["error"]["details"]["reason"])
            output = serialized(result)
            self.assertNotIn(str(base), output)
            self.assertNotIn("do-not-leak", output)
            self.assertNotIn(host_secret, output)
            self.assertIn("<redacted>", result["stdout"]["text"])
            self.assertIn("\n1\n", result["stdout"]["text"])
            self.assert_result_contract(result, has_error=True)


if __name__ == "__main__":
    unittest.main()
