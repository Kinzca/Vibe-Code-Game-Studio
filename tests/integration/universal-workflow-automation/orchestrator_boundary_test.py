"""Acceptance evidence for STORY-UWA-011 orchestration ownership boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
WINDMILL = ROOT / "integrations" / "windmill"
sys.path[:0] = [str(SCRIPTS), str(WINDMILL), str(ROOT / "tests")]

from ccgs_windmill_port import (
    build_windmill_orchestration_adapter,
    stable_request_id,
    windmill_capability_document,
)
from ccgs_cli import atomic_write_text
from ccgs_story_workflow import apply_closeout, closeout_report, parse_story
from vibe_integration_ports import (
    IntegrationPortContractError,
    MAX_PAYLOAD_BYTES,
    validate_port_response,
)
from vibe_orchestration import (
    build_orchestration_request,
    invoke_orchestration,
    orchestration_request_envelope,
    validate_orchestration_data,
    validate_orchestration_request,
)
from vibe_workflow_evidence import build_evidence
from vibe_workflow_execute import execute_step
from vibe_workflow_plan import compile_plan
from vibe_workflow_preflight import preflight_plan


STORY = "ccgs-data/production/epics/neutral/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"
DATA_DIR = "ccgs-data"


class FakeCoreExecutor:
    """Act as the fixed core CLI while recording structured argv calls."""

    def __init__(
        self, story_path: Path | None = None, *, invalid_json: bool = False,
        business_fail: bool = False, failure_message: str = "Evidence did not pass",
        failure_messages: tuple[str, ...] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.story_path = story_path
        self.invalid_json = invalid_json
        self.business_fail = business_fail
        self.failure_messages = failure_messages or (failure_message,)

    def __call__(self, command: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        rendered = list(command)
        self.calls.append(rendered)
        operation = rendered[1]
        if self.invalid_json:
            return subprocess.CompletedProcess(command, 0, "not-json", "")
        if operation == "doctor":
            payload = {
                "cli_version": "1.0",
                "repository_mode": "external",
                "data_dir": "ccgs-data",
                "read_only": True,
                "engine_agnostic": True,
                "summary": {"pass": 4, "warn": 0, "error": 0, "info": 0},
            }
        elif operation == "evidence-validate":
            payload = {"valid": True, "errors": []}
        elif operation == "closeout":
            if self.business_fail:
                payload = {
                    "verdict": "fail",
                    "failures": [
                        {"code": "evidence.failed", "message": message}
                        for message in self.failure_messages
                    ],
                    "written": False,
                }
                return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")
            written = False
            if "--write" in rendered and self.story_path is not None:
                current = self.story_path.read_text(encoding="utf-8")
                if "status: review" in current:
                    self.story_path.write_text(
                        current.replace("status: review", "status: done"),
                        encoding="utf-8",
                    )
                    written = True
            payload = {"verdict": "pass", "failures": [], "written": written}
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


class OrchestratorBoundaryTest(unittest.TestCase):
    def request(self, action: str = "story_check", **changes: Any) -> dict[str, Any]:
        values = {
            "request_id": "request-011",
            "project_id": "neutral-project",
            "action": action,
            "story": STORY,
            "evidence": EVIDENCE,
            "data_dir": DATA_DIR,
        }
        values.update(changes)
        return build_orchestration_request(**values)

    def invoke(self, project: Path, request: dict[str, Any], executor: FakeCoreExecutor,
               *, max_attempts: int = 1) -> dict[str, Any]:
        adapter = build_windmill_orchestration_adapter(
            str(ROOT), str(project), data_dir=DATA_DIR, max_attempts=max_attempts,
            retry_delay_seconds=0, executor=executor, sleeper=lambda _: None,
            platform="posix",
        )
        return invoke_orchestration(
            request, windmill_capability_document(), adapter,
            data_dir=DATA_DIR, timeout_seconds=30,
        )

    @staticmethod
    def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
        """Capture the complete file set, bytes, and modification times."""

        return {
            path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()
        }

    def project(self, root: Path) -> Path:
        story = root / STORY
        evidence = root / EVIDENCE
        story.parent.mkdir(parents=True)
        evidence.parent.mkdir(parents=True)
        story.write_text("---\nstatus: review\n---\n", encoding="utf-8")
        evidence.write_text('{"result":"pass"}\n', encoding="utf-8")
        for relative in (
            "ccgs-data/production/plans/plan.json",
            "ccgs-data/production/results/result.json",
            "ccgs-data/production/replay/replay.json",
            "ccgs-data/production/closeout/closeout.json",
            "src/neutral.txt",
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("stable\n", encoding="utf-8")
        return story

    def test_ac1_versioned_allowlist_paths_and_references_fail_closed(self) -> None:
        for action in ("story_check", "story_closeout"):
            request = self.request(action)
            self.assertEqual(
                validate_orchestration_request(request, data_dir=DATA_DIR), request,
            )
            self.assertEqual(request["port"], "orchestration")
            self.assertEqual(request["operation"], "trigger")

        called = 0
        def adapter(_request: dict[str, Any], _timeout: float) -> dict[str, Any]:
            nonlocal called
            called += 1
            raise AssertionError("invalid request reached adapter")

        candidates = []
        candidates.append(orchestration_request_envelope(
            request_id="request-wrong-data", project_id="neutral-project",
            action="story_check", story="wrong-data/production/epics/story.md",
            evidence=None,
        ))
        unknown = orchestration_request_envelope(
            request_id="request-unknown", project_id="neutral-project",
            action="unknown", story=STORY, evidence=None,
        )
        candidates.append(unknown)
        mismatch = self.request()
        mismatch["payload"]["action"] = "story_closeout"
        candidates.append(mismatch)
        extra = self.request()
        extra["payload"]["command"] = "whoami"
        candidates.append(extra)
        null_evidence = self.request()
        null_evidence["payload"]["evidence"] = None
        null_evidence["references"] = [STORY]
        null_response = invoke_orchestration(
            null_evidence, windmill_capability_document(), adapter,
            data_dir=DATA_DIR, timeout_seconds=30,
        )
        self.assertFalse(null_response["called"])
        self.assertEqual(null_response["error"]["code"], "PORT_REQUEST_INVALID")
        refs = self.request()
        refs["references"] = [EVIDENCE, STORY]
        candidates.append(refs)
        for unsafe in (
            "/tmp/story.md", "../story.md", "ccgs-data/production/epics/./story.md",
            r"ccgs-data\production\epics\story.md", "C:/story.md", r"\\host\story.md",
            "file:///tmp/story.md", "ccgs-data/production/epics/story.md;whoami",
        ):
            envelope = orchestration_request_envelope(
                request_id="request-unsafe", project_id="neutral-project",
                action="story_check", story=unsafe, evidence=None,
            )
            candidates.append(envelope)
        for unsafe_evidence in (
            "/tmp/evidence.json", "../evidence.json", r"ccgs-data\production\qa\evidence\x.json",
            "ccgs-data/production/qa/evidence/x.json;whoami",
        ):
            candidates.append(orchestration_request_envelope(
                request_id="request-evidence", project_id="neutral-project",
                action="story_check", story=STORY, evidence=unsafe_evidence,
            ))
        for candidate in candidates:
            response = invoke_orchestration(
                candidate, windmill_capability_document(), adapter,
                data_dir=DATA_DIR, timeout_seconds=30,
            )
            self.assertFalse(response["ok"])
            self.assertFalse(response["called"])
            self.assertFalse(response["error"]["retryable"])
        bad_config = invoke_orchestration(
            self.request(), windmill_capability_document(), adapter,
            data_dir="../wrong-data", timeout_seconds=30,
        )
        self.assertFalse(bad_config["ok"])
        self.assertFalse(bad_config["called"])
        self.assertEqual(called, 0)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema dependency")
    def test_ac1_public_data_schemas_are_draft_2020_12_valid(self) -> None:
        from jsonschema import Draft202012Validator

        request_schema = json.loads(
            (ROOT / "schemas/orchestration-request-data.schema.json").read_text(encoding="utf-8")
        )
        response_schema = json.loads(
            (ROOT / "schemas/orchestration-response-data.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(response_schema)
        request = self.request()
        data = {
            "contract_version": "1.0", "action": "story_check", "outcome": "passed",
            "story": STORY, "evidence": EVIDENCE,
            "checks": [{"name": "doctor", "status": "passed", "attempt_count": 1, "summary": {}}],
            "closeout_applied": False, "failures": [],
        }
        self.assertFalse(list(Draft202012Validator(request_schema).iter_errors(request["payload"])))
        self.assertFalse(list(Draft202012Validator(response_schema).iter_errors(data)))

    def test_ac2_story_check_is_read_only_and_calls_fixed_core_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.project(project)
            before = self.snapshot(project)
            executor = FakeCoreExecutor()
            response = self.invoke(project, self.request(), executor)
            after = self.snapshot(project)

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["outcome"], "passed")
        self.assertEqual([call[1] for call in executor.calls], [
            "doctor", "evidence-validate", "closeout",
        ])
        self.assertIn("--dry-run", executor.calls[-1])
        self.assertEqual(before, after)
        for wrapper in ("story_check.py", "story_closeout.py"):
            source = (WINDMILL / "f/ccgs" / wrapper).read_text(encoding="utf-8")
            self.assertNotIn("read_text(", source)
            self.assertNotIn("write_text(", source)
            self.assertNotIn("subprocess", source)

    def test_ac3_only_core_closeout_write_changes_state_and_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            story_path = self.project(project)
            executor = FakeCoreExecutor(story_path)
            request = self.request("story_closeout")
            first = self.invoke(project, request, executor)
            first_bytes = story_path.read_bytes()
            first_mtime = story_path.stat().st_mtime_ns
            second = self.invoke(project, request, executor)

            self.assertEqual(first["data"]["outcome"], "passed")
            self.assertTrue(first["data"]["closeout_applied"])
            self.assertFalse(second["data"]["closeout_applied"])
            self.assertEqual(story_path.read_bytes(), first_bytes)
            self.assertEqual(story_path.stat().st_mtime_ns, first_mtime)
            self.assertEqual(list(project.rglob("*.tmp")), [])
        self.assertEqual([call[1] for call in executor.calls].count("closeout"), 4)
        writes = [call for call in executor.calls if call[1] == "closeout" and "--write" in call]
        self.assertEqual(len(writes), 2)

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            story_path = self.project(project)
            before = story_path.read_bytes()
            executor = FakeCoreExecutor(story_path, business_fail=True)
            failed = self.invoke(project, self.request("story_closeout"), executor)
            self.assertEqual(failed["data"]["outcome"], "failed")
            self.assertFalse(failed["data"]["closeout_applied"])
            self.assertEqual(failed["data"]["failures"][0]["code"], "evidence.failed")
            self.assertEqual(failed["data"]["failures"][0]["message"], "Evidence did not pass")
            self.assertEqual(story_path.read_bytes(), before)
            self.assertEqual(
                len([call for call in executor.calls if call[1] == "closeout" and "--write" in call]),
                1,
            )

    def test_ac4_transport_is_retryable_but_protocol_and_business_are_not(self) -> None:
        request = self.request()
        for exception, code in (
            (OSError("offline"), "PORT_ADAPTER_UNAVAILABLE"),
            (TimeoutError("late"), "PORT_ADAPTER_TIMEOUT"),
        ):
            def failing(_request: dict[str, Any], _timeout: float, error=exception):
                raise error
            response = invoke_orchestration(
                request, windmill_capability_document(), failing,
                data_dir=DATA_DIR, timeout_seconds=30,
            )
            self.assertEqual(response["error"]["code"], code)
            self.assertTrue(response["called"])
            self.assertTrue(response["error"]["retryable"])

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.project(project)
            executor = FakeCoreExecutor(invalid_json=True)
            response = self.invoke(project, request, executor, max_attempts=5)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "PORT_PROTOCOL_INVALID")
        self.assertTrue(response["called"])
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(len(executor.calls), 1)

        flow = json.loads((WINDMILL / "f/ccgs/story_closeout__flow/flow.yaml").read_text(encoding="utf-8"))
        module = flow["value"]["modules"][0]
        self.assertEqual(
            module["value"]["input_transforms"]["data_dir"]["expr"],
            "flow_input.data_dir",
        )
        self.assertEqual(module["value"]["input_transforms"]["max_attempts"]["value"], 1)
        self.assertEqual(module["retry"]["constant"]["attempts"], 2)
        self.assertIn("CCGS_RETRYABLE", module["retry"]["retry_if"]["expr"])
        self.assertEqual(
            stable_request_id("p", "story_check", STORY, EVIDENCE),
            stable_request_id("p", "story_check", STORY, EVIDENCE),
        )

        script_path = WINDMILL / "f/ccgs/story_closeout.py"
        spec = importlib.util.spec_from_file_location("story_closeout_retry_probe", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        request_ids: list[str] = []
        adapter_attempts: list[int] = []

        def build_request(**values: Any) -> dict[str, Any]:
            return values

        def build_adapter(*_args: Any, **values: Any) -> object:
            adapter_attempts.append(values["max_attempts"])
            return object()

        def invoke(request: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            request_ids.append(request["request_id"])
            if len(request_ids) <= module["retry"]["constant"]["attempts"]:
                return {"ok": False}
            return {"ok": True}

        def raise_for_windmill(response: dict[str, Any]) -> dict[str, Any]:
            if not response["ok"]:
                raise RuntimeError("[CCGS_RETRYABLE]PORT_ADAPTER_UNAVAILABLE")
            return response

        entrypoint._port = lambda _root: (
            build_request, invoke, build_adapter, lambda: {}, raise_for_windmill,
            stable_request_id,
        )
        for attempt in range(module["retry"]["constant"]["attempts"] + 1):
            try:
                result = entrypoint.main(
                    str(ROOT), "/worker/project", STORY, EVIDENCE,
                    data_dir=DATA_DIR, project_id="p",
                    max_attempts=module["value"]["input_transforms"]["max_attempts"]["value"],
                )
            except RuntimeError as exc:
                self.assertLess(attempt, module["retry"]["constant"]["attempts"])
                self.assertIn("CCGS_RETRYABLE", str(exc))
            else:
                self.assertEqual(attempt, module["retry"]["constant"]["attempts"])
                self.assertTrue(result["ok"])
        self.assertEqual(adapter_attempts, [1, 1, 1])
        self.assertEqual(len(set(request_ids)), 1)

    def test_ac5_response_is_bounded_project_relative_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.project(project)
            response = self.invoke(project, self.request(), FakeCoreExecutor())
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_PAYLOAD_BYTES)
        rendered = encoded.decode("utf-8")
        self.assertNotIn(str(ROOT), rendered)
        self.assertNotIn(str(project), rendered)
        for forbidden in ("environment", "credential", "command", "stdout", "stderr", "source_text"):
            self.assertNotIn(f'"{forbidden}"', rendered)
        self.assertEqual(response["data"]["story"], STORY)
        self.assertEqual(response["data"]["evidence"], EVIDENCE)

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            story_path = self.project(project)
            failed = self.invoke(
                project, self.request("story_closeout"),
                FakeCoreExecutor(
                    story_path, business_fail=True,
                    failure_message="/private/worker secret=do-not-return",
                ),
            )
        failed_text = json.dumps(failed, sort_keys=True)
        self.assertNotIn("/private/worker", failed_text)
        self.assertNotIn("do-not-return", failed_text)

        invalid = dict(response["data"])
        invalid["failures"] = [
            {"code": "x", "message": "same", "retryable": False},
            {"code": "x", "message": "same", "retryable": False},
        ]
        with self.assertRaises(IntegrationPortContractError):
            validate_orchestration_data(
                self.request(), invalid, data_dir=DATA_DIR,
            )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            story_path = self.project(project)
            distinct_response = self.invoke(
                project, self.request("story_closeout"),
                FakeCoreExecutor(
                    story_path, business_fail=True,
                    failure_messages=("first reason", "second reason", "first reason"),
                ),
            )
        distinct = distinct_response["data"]["failures"]
        self.assertEqual(
            [(item["code"], item["message"], item["retryable"]) for item in distinct],
            [
                ("evidence.failed", "first reason", False),
                ("evidence.failed", "second reason", False),
            ],
        )

        oversized = dict(response["data"])
        oversized["outcome"] = "failed"
        oversized["failures"] = [{
            "code": "x", "message": "x" * (MAX_PAYLOAD_BYTES + 1),
            "retryable": False,
        }]
        with self.assertRaises(IntegrationPortContractError):
            validate_orchestration_data(
                self.request(), oversized, data_dir=DATA_DIR,
            )

        request = self.request()
        identity = {key: request[key] for key in (
            "contract_version", "request_id", "project_id", "port", "operation", "capability",
        )}
        envelope = {
            **identity,
            "ok": True,
            "status": "success",
            "action": "invoke",
            "called": True,
            "data": {},
            "error": None,
        }
        empty_size = len(json.dumps(
            {"blob": ""}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        envelope["data"] = {"blob": "x" * (MAX_PAYLOAD_BYTES - empty_size)}
        self.assertEqual(
            MAX_PAYLOAD_BYTES,
            len(json.dumps(
                envelope["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")),
        )
        self.assertEqual(envelope, validate_port_response(request, envelope))
        envelope["data"]["blob"] += "x"
        with self.assertRaises(IntegrationPortContractError):
            validate_port_response(request, envelope)

    def test_ac6_contract_and_base_flow_are_offline_and_engine_neutral(self) -> None:
        files = [
            SCRIPTS / "vibe_orchestration.py",
            WINDMILL / "ccgs_windmill_adapter.py",
            WINDMILL / "ccgs_windmill_port.py",
            WINDMILL / "f/ccgs/story_check.py",
            WINDMILL / "f/ccgs/story_closeout.py",
            WINDMILL / "f/ccgs/story_closeout__flow/flow.yaml",
            ROOT / "schemas/orchestration-request-data.schema.json",
            ROOT / "schemas/orchestration-response-data.schema.json",
        ]
        fixture_source = Path(__file__).read_text(encoding="utf-8").split(
            "    def test_ac6_", maxsplit=1,
        )[0]
        text = "\n".join([
            *(path.read_text(encoding="utf-8") for path in files),
            fixture_source,
        ]).casefold()
        for forbidden in (
            "import requests", "import windmill", "unity", "godot", "unreal",
            "cocos", "client/assets", "server/", "marblegame",
        ):
            self.assertNotIn(forbidden, text)

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            story_path = project / STORY
            story_path.parent.mkdir(parents=True)
            story_path.write_text(
                """---
id: STORY-NEUTRAL-011
title: Neutral offline loop
status: review
---
# Neutral offline loop

## Acceptance Criteria

- [ ] AC-1: local core loop completes
""",
                encoding="utf-8",
            )
            manifest = {
                "contract_version": "1.0",
                "ok": True,
                "mode": "execution-request",
                "schema_version": "1.0",
                "schema_path": "schemas/project-workflow-manifest.schema.json",
                "manifest_path": "vibe-workflow.json",
                "steps": [{
                    "id": "verify",
                    "argv": [sys.executable, "-c", "pass"],
                    "acceptance_mapping": ["AC-1"],
                }],
            }
            plan = compile_plan(manifest)
            preflight = preflight_plan(plan, project)
            result = execute_step(
                preflight,
                "verify",
                project,
                {
                    "contract_version": "1.0",
                    "timeout_seconds": 5,
                    "max_log_bytes": 1024,
                    "termination_grace_seconds": 1,
                },
            )
            self.assertTrue(result["ok"])
            evidence = build_evidence(
                "STORY-NEUTRAL-011",
                ["AC-1"],
                plan,
                [result],
                [{
                    "id": "offline-loop",
                    "type": "automated-test",
                    "status": "pass",
                    "summary": "local core path completed without an external service",
                }],
            )
            story = parse_story(STORY, story_path.read_text(encoding="utf-8"))
            report = closeout_report(story, EVIDENCE, evidence, [])
            self.assertEqual("pass", report["verdict"])
            self.assertTrue(apply_closeout(
                story_path, story, report, atomic_write_text,
            ))
            self.assertIn("status: done", story_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
