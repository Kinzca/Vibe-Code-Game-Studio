"""Batch 5A tests for the repository-safe Windmill adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
WINDMILL_ROOT = ROOT / "integrations" / "windmill"
sys.path.insert(0, str(WINDMILL_ROOT))

from ccgs_windmill_adapter import (
    CcgsCmdRunner,
    RetryPolicy,
    WindmillAdapterError,
    raise_for_windmill,
    run_story_check,
    run_story_closeout,
    validate_relative_path,
)

STORY = "ccgs-data/production/epics/sample/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"


def set_review(project: Path) -> None:
    path = project / STORY
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: ready", "status: review"),
        encoding="utf-8",
        newline="\n",
    )


def fail_evidence(project: Path) -> None:
    path = project / EVIDENCE
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["result"] = "fail"
    evidence["acceptance_criteria"][0]["status"] = "fail"
    evidence["checks"][0]["status"] = "fail"
    path.write_text(
        json.dumps(evidence, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


@unittest.skipUnless(os.name == "nt", "ccgs.cmd integration requires Windows")
class WindmillAdapterIntegrationTests(unittest.TestCase):
    def test_story_check_is_read_only_and_hides_absolute_project_root(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            before = tree_digest(project)
            result = run_story_check(
                str(ROOT),
                str(project),
                STORY,
                max_attempts=1,
                retry_delay_seconds=0,
            )

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["ok"])
            self.assertFalse(result["retryable"])
            self.assertEqual(
                [item["command"] for item in result["commands"]],
                ["doctor", "evidence-validate", "closeout"],
            )
            self.assertEqual(tree_digest(project), before)
            self.assertNotIn(str(project), json.dumps(result))

    def test_closeout_advances_only_through_cli_and_is_idempotent(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            story = project / STORY
            first = run_story_closeout(
                str(ROOT),
                str(project),
                STORY,
                max_attempts=1,
                retry_delay_seconds=0,
            )

            self.assertEqual(first["status"], "passed")
            self.assertTrue(first["advance"]["payload"]["written"])
            self.assertIn("status: done", story.read_text(encoding="utf-8"))
            before = tree_digest(project)
            mtime = story.stat().st_mtime_ns

            second = run_story_closeout(
                str(ROOT),
                str(project),
                STORY,
                max_attempts=1,
                retry_delay_seconds=0,
            )
            self.assertEqual(second["status"], "passed")
            self.assertFalse(second["advance"]["payload"]["written"])
            self.assertEqual(tree_digest(project), before)
            self.assertEqual(story.stat().st_mtime_ns, mtime)
            self.assertEqual(list(project.rglob("*.tmp")), [])

    def test_failed_closeout_collects_and_persists_failure_report(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            fail_evidence(project)
            result = run_story_closeout(
                str(ROOT),
                str(project),
                STORY,
                max_attempts=1,
                retry_delay_seconds=0,
            )
            story_text = (project / STORY).read_text(encoding="utf-8")

            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["ok"])
            codes = {item["code"] for item in result["failures"]}
            self.assertIn("evidence.result", codes)
            self.assertIn("evidence.acceptance", codes)
            self.assertIn("evidence.checks", codes)
            self.assertIn("status: review", story_text)
            self.assertIn("- Verdict: FAIL", story_text)
            self.assertTrue(result["advance"]["payload"]["written"])

    def test_apply_false_is_a_read_only_closeout_check(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            before = tree_digest(project)
            result = run_story_closeout(
                str(ROOT),
                str(project),
                STORY,
                apply=False,
                max_attempts=1,
                retry_delay_seconds=0,
            )

            self.assertEqual(result["status"], "passed")
            self.assertFalse(result["apply"])
            self.assertIsNone(result["advance"])
            self.assertEqual(tree_digest(project), before)

    def test_reports_are_identical_across_engines(self) -> None:
        reports = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                set_review(project)
                reports.append(
                    run_story_check(
                        str(ROOT),
                        str(project),
                        STORY,
                        max_attempts=1,
                        retry_delay_seconds=0,
                    )
                )
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])

    def test_windmill_entrypoint_delegates_to_adapter(self) -> None:
        script_path = WINDMILL_ROOT / "f/ccgs/story_check.py"
        spec = importlib.util.spec_from_file_location("ccgs_wm_story_check", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with materialized_fixture("mature-project") as project:
            set_review(project)
            result = module.main(
                str(ROOT),
                str(project),
                STORY,
                max_attempts=1,
                retry_delay_seconds=0,
            )
            self.assertEqual(result["status"], "passed")
            with self.assertRaisesRegex(RuntimeError, r"^\[CCGS_PERMANENT\]"):
                module.main(
                    str(ROOT),
                    str(project),
                    "../unsafe-story.md",
                    max_attempts=1,
                    retry_delay_seconds=0,
                )


class WindmillAdapterRetryTests(unittest.TestCase):
    def _runner(self, project: Path, executor, sleeper=lambda _: None) -> CcgsCmdRunner:
        return CcgsCmdRunner(
            str(ROOT),
            str(project),
            retry_policy=RetryPolicy(3, 0, 10),
            executor=executor,
            sleeper=sleeper,
            platform="nt",
            comspec="cmd.exe",
        )

    def test_timeout_is_retried_then_succeeds(self) -> None:
        calls = []
        sleeps = []

        def executor(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(command, 10)
            payload = {
                "cli_version": "0.3.0",
                "repository_mode": "external",
                "data_dir": "ccgs-data",
                "read_only": True,
                "engine_agnostic": True,
                "summary": {"pass": 15, "warn": 0, "error": 0, "info": 0},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        with materialized_fixture("mature-project") as project:
            result = self._runner(project, executor, sleeps.append).invoke(
                "doctor", ["--json"]
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["attempts"][0]["outcome"], "timeout")
        self.assertEqual(result["attempts"][1]["outcome"], "success")
        self.assertEqual(sleeps, [0])

    def test_business_failure_is_not_retried(self) -> None:
        calls = []

        def executor(command, **kwargs):
            calls.append(command)
            payload = {"valid": False, "errors": [{"path": "$", "message": "bad"}]}
            return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")

        with materialized_fixture("mature-project") as project:
            result = self._runner(project, executor).invoke(
                "evidence-validate", ["--evidence", EVIDENCE]
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["retryable"])

    def test_invocation_error_is_not_retried(self) -> None:
        calls = []

        def executor(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 2, "", "invalid path")

        with materialized_fixture("mature-project") as project:
            result = self._runner(project, executor).invoke("doctor", ["--json"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["retryable"])

    def test_exhausted_transport_error_is_marked_for_windmill_retry(self) -> None:
        def executor(command, **kwargs):
            raise OSError("worker transport unavailable")

        with materialized_fixture("mature-project") as project:
            result = self._runner(project, executor).invoke("doctor", ["--json"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["attempt_count"], 3)
        self.assertTrue(result["retryable"])
        wrapped = {
            "status": "error",
            "retryable": True,
            "failures": [{"code": "adapter.doctor", "message": "unavailable"}],
        }
        with self.assertRaisesRegex(RuntimeError, r"^\[CCGS_RETRYABLE\]"):
            raise_for_windmill(wrapped)


class WindmillAdapterSafetyTests(unittest.TestCase):
    def test_relative_path_policy_blocks_escape_and_shell_characters(self) -> None:
        self.assertEqual(validate_relative_path(STORY, "story"), STORY)
        for value in (
            "../story.md",
            "ccgs-data/../story.md",
            "C:/project/story.md",
            "/project/story.md",
            "story.md&whoami",
            "story.md|more",
            "story.md!value",
        ):
            with self.subTest(value=value):
                with self.assertRaises(WindmillAdapterError):
                    validate_relative_path(value, "story")

    def test_retry_policy_is_bounded(self) -> None:
        for policy in (
            RetryPolicy(0, 1, 10),
            RetryPolicy(6, 1, 10),
            RetryPolicy(1, -1, 10),
            RetryPolicy(1, 61, 10),
            RetryPolicy(1, 1, 0),
            RetryPolicy(1, 1, 3601),
        ):
            with self.subTest(policy=policy):
                with self.assertRaises(WindmillAdapterError):
                    policy.validate()

    def test_runner_allows_only_stable_cli_commands(self) -> None:
        with materialized_fixture("mature-project") as project:
            runner = CcgsCmdRunner(
                str(ROOT),
                str(project),
                retry_policy=RetryPolicy(1, 0, 10),
                executor=lambda *args, **kwargs: None,
                platform="nt",
            )
            with self.assertRaises(WindmillAdapterError):
                runner.invoke("context-pack", [])

    def test_windmill_assets_are_strict_json_compatible_yaml(self) -> None:
        config = json.loads((WINDMILL_ROOT / "wmill.yaml").read_text(encoding="utf-8"))
        check_meta = json.loads(
            (WINDMILL_ROOT / "f/ccgs/story_check.script.yaml").read_text(
                encoding="utf-8"
            )
        )
        closeout_meta = json.loads(
            (WINDMILL_ROOT / "f/ccgs/story_closeout.script.yaml").read_text(
                encoding="utf-8"
            )
        )
        flow = json.loads(
            (WINDMILL_ROOT / "f/ccgs/story_closeout__flow/flow.yaml").read_text(
                encoding="utf-8"
            )
        )
        folder = json.loads(
            (WINDMILL_ROOT / "f/ccgs/folder.meta.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["includes"], ["f/**"])
        for key in (
            "skipVariables",
            "skipResources",
            "skipResourceTypes",
            "skipSecrets",
            "skipApps",
        ):
            self.assertTrue(config[key])
        self.assertEqual(folder["display_name"], "CCGS Automation")
        self.assertEqual(check_meta["kind"], "script")
        self.assertEqual(closeout_meta["kind"], "script")
        self.assertEqual(
            check_meta["schema"]["required"],
            ["framework_root", "project_root", "story"],
        )
        module = flow["value"]["modules"][0]
        self.assertEqual(module["value"]["path"], "f/ccgs/story_closeout")
        self.assertEqual(
            module["value"]["input_transforms"]["max_attempts"],
            {"type": "static", "value": 1},
        )
        self.assertNotIn("max_attempts", flow["schema"]["properties"])
        self.assertEqual(module["retry"]["constant"], {"attempts": 2, "seconds": 5})
        self.assertIn("CCGS_RETRYABLE", module["retry"]["retry_if"]["expr"])

    def test_windmill_scripts_do_not_implement_project_workflow_logic(self) -> None:
        wrappers = [
            WINDMILL_ROOT / "f/ccgs/story_check.py",
            WINDMILL_ROOT / "f/ccgs/story_closeout.py",
        ]
        for path in wrappers:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("subprocess", text)
            self.assertNotIn("Client/Assets", text)
            self.assertNotIn("Server/", text)
            self.assertIn("ccgs_windmill_adapter", text)

        adapter = (WINDMILL_ROOT / "ccgs_windmill_adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Client/Assets", adapter)
        self.assertNotIn("Server/", adapter)
        self.assertIn('ALLOWED_COMMANDS = {"doctor", "evidence-validate", "closeout"}', adapter)


if __name__ == "__main__":
    unittest.main()