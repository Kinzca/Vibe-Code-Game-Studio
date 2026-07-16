"""Filesystem evidence for STORY-UWA-008 idempotent replay."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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

from ccgs_story_workflow import BEGIN, END, apply_closeout, closeout_report, parse_story
from vibe_workflow_replay import (
    REPLAY_IDENTITY_CONFLICT,
    REPLAY_INPUT_INVALID,
    REPLAY_RECORD_INVALID,
    REPLAY_RETRY_EXHAUSTED,
    REPLAY_RETRY_FORBIDDEN,
    REPLAY_WRITE_FAILED,
    ReplayContractError,
    build_replay_identity,
    materialize_replay_result,
)


PLAN_ID = "sha256:" + "6" * 64


def plan() -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "ok": True,
        "plan_id": PLAN_ID,
        "step_order": ["verify"],
        "steps": [
            {
                "id": "verify",
                "argv": ["neutral-tool", "verify"],
                "acceptance_mapping": ["AC-1"],
                "artifacts": ["output/report.json"],
            }
        ],
    }


def artifact_id(path: str = "output/report.json") -> str:
    encoded = json.dumps(
        [PLAN_ID, "verify", path], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def successful_result() -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "status": "passed",
        "step_id": "verify",
        "artifacts": [
            {"artifact_id": artifact_id(), "path": "output/report.json", "present": True}
        ],
    }


def replay_identity(payload: Any | None = None, version: str = "v1") -> dict[str, str]:
    return build_replay_identity(
        "neutral.event",
        version,
        {"value": 1, "labels": ["中性"]} if payload is None else payload,
        plan(),
    )


def record_path(project: Path, identity: dict[str, str]) -> Path:
    return (
        project
        / "neutral-data"
        / "production"
        / "workflow"
        / "replays"
        / f"{identity['event_id'][7:]}.json"
    )


class IdempotentReplayFilesystemTest(unittest.TestCase):
    def test_record_schema_matches_materialized_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "replay-record.schema.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity()
            report = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "transport-failed"},
                "transient_transport_failure",
                1,
                True,
            )
            record = json.loads(record_path(project, identity).read_text(encoding="utf-8"))

        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(schema["required"]), set(record))
        self.assertEqual(record, report["record"])
        self.assertIn(record["status"], schema["properties"]["status"]["enum"])
        failure_contract = schema["properties"]["failure_class"]["oneOf"]
        self.assertTrue(any(item.get("type") == "null" for item in failure_contract))
        failure_values = next(item["enum"] for item in failure_contract if "enum" in item)
        self.assertIn(record["failure_class"], failure_values)
        self.assertEqual(10, schema["properties"]["attempt"]["maximum"])

    def test_ac1_identity_is_stable_across_process_and_working_directory(self) -> None:
        expected = replay_identity()
        script = f"""
import json
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
from vibe_workflow_replay import build_replay_identity
plan = {plan()!r}
identity = build_replay_identity(
    "neutral.event", "v1", {{"labels": ["中性"], "value": 1}}, plan
)
print(json.dumps(identity, ensure_ascii=False, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            outputs = [
                subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=working_directory,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                ).stdout
                for working_directory in (first, second)
            ]
        self.assertEqual(expected, json.loads(outputs[0]))
        self.assertEqual(outputs[0], outputs[1])

    def test_ac2_first_success_and_replay_preserve_all_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity()
            story_path = project / "neutral-data" / "production" / "epics" / "story.md"
            evidence_path = project / "neutral-data" / "production" / "qa" / "evidence" / "story.json"
            story_path.parent.mkdir(parents=True)
            evidence_path.parent.mkdir(parents=True)
            story_text = f"""---
id: STORY-NEUTRAL-008
title: Neutral replay
status: done
---
# Neutral replay

## Acceptance Criteria

- [x] AC-1: replay remains stable

{BEGIN}
## CCGS Closeout
{END}
"""
            story_path.write_text(story_text, encoding="utf-8")
            evidence = {
                "schema_version": "1.0",
                "story_id": "STORY-NEUTRAL-008",
                "result": "pass",
                "acceptance_criteria": [
                    {"id": "AC-1", "status": "pass", "evidence": "stable reference"}
                ],
                "checks": [
                    {
                        "id": "unit",
                        "type": "automated-test",
                        "status": "pass",
                        "summary": "neutral check passed",
                    }
                ],
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            first = materialize_replay_result(
                project, "neutral-data", identity, successful_result(), None, 1, True
            )
            replay_path = record_path(project, identity)
            self.assertEqual("execute", first["action"])
            self.assertTrue(first["written"])

            managed = (replay_path, evidence_path, story_path)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in managed}
            writes: list[Path] = []
            second = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                successful_result(),
                None,
                1,
                True,
                lambda path, _content: writes.append(path),
            )
            parsed_story = parse_story("neutral-data/production/epics/story.md", story_path.read_text())
            closeout = closeout_report(parsed_story, "neutral-data/production/qa/evidence/story.json", evidence, [])
            self.assertFalse(
                apply_closeout(
                    story_path,
                    parsed_story,
                    closeout,
                    lambda _path, _content: self.fail("done Closeout replay wrote the Story"),
                )
            )

            self.assertEqual("reuse", second["action"])
            self.assertFalse(second["written"])
            self.assertEqual([], writes)
            self.assertEqual(successful_result(), second["record"]["result"])
            for path in managed:
                self.assertEqual(before[path], (path.read_bytes(), path.stat().st_mtime_ns))
            self.assertEqual([], list(replay_path.parent.glob(".*.tmp")))

    def test_ac3_changed_inputs_are_independent_and_conflicts_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            first_identity = replay_identity()
            changed_payload = replay_identity({"value": 2, "labels": ["中性"]})
            changed_version = replay_identity(version="v2")
            for current in (first_identity, changed_payload, changed_version):
                report = materialize_replay_result(
                    project, "neutral-data", current, successful_result(), None, 1, True
                )
                self.assertEqual("execute", report["action"])
            self.assertEqual(3, len(list(record_path(project, first_identity).parent.glob("*.json"))))

            target = record_path(project, first_identity)
            stored = json.loads(target.read_text(encoding="utf-8"))
            stored["plan_id"] = "sha256:" + "9" * 64
            target.write_text(json.dumps(stored), encoding="utf-8")
            snapshot = target.read_bytes(), target.stat().st_mtime_ns
            report = materialize_replay_result(
                project, "neutral-data", first_identity, successful_result(), None, 1, True
            )
            self.assertEqual(REPLAY_IDENTITY_CONFLICT, report["error"]["code"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

            target.write_text("{broken", encoding="utf-8")
            snapshot = target.read_bytes(), target.stat().st_mtime_ns
            report = materialize_replay_result(
                project, "neutral-data", first_identity, successful_result(), None, 1, True
            )
            self.assertEqual(REPLAY_RECORD_INVALID, report["error"]["code"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

    def test_ac4_terminal_records_and_attempt_jumps_cannot_be_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity()
            first = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "failed"},
                "business_failure",
                1,
                True,
            )
            self.assertTrue(first["written"])
            target = record_path(project, identity)
            snapshot = target.read_bytes(), target.stat().st_mtime_ns
            forbidden = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "passed"},
                None,
                2,
                True,
            )
            self.assertEqual(REPLAY_RETRY_FORBIDDEN, forbidden["error"]["code"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

            other = replay_identity({"transient": True})
            materialize_replay_result(
                project,
                "neutral-data",
                other,
                {"status": "transport-failed"},
                "transient_transport_failure",
                1,
                True,
            )
            jump = materialize_replay_result(
                project,
                "neutral-data",
                other,
                {"status": "passed"},
                None,
                3,
                True,
                retry_policy={"max_attempts": 10},
            )
            self.assertEqual(REPLAY_INPUT_INVALID, jump["error"]["code"])

    def test_ac4_materialization_requires_policy_and_enforces_its_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity({"retry": True})
            materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "transport-failed"},
                "transient_transport_failure",
                1,
                True,
            )
            target = record_path(project, identity)
            snapshot = target.read_bytes(), target.stat().st_mtime_ns

            dry_retry = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "passed"},
                None,
                2,
                False,
                retry_policy={"max_attempts": 10},
            )
            self.assertEqual("retry", dry_retry["action"])
            self.assertEqual(1, dry_retry["record"]["attempt"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

            missing_policy = materialize_replay_result(
                project, "neutral-data", identity, {"status": "passed"}, None, 2, True
            )
            self.assertEqual(REPLAY_INPUT_INVALID, missing_policy["error"]["code"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

            exhausted = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "passed"},
                None,
                2,
                True,
                retry_policy={"max_attempts": 1},
            )
            self.assertEqual(REPLAY_RETRY_EXHAUSTED, exhausted["error"]["code"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

            allowed = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "passed"},
                None,
                2,
                True,
                retry_policy={"max_attempts": 10},
            )
            self.assertEqual("retry", allowed["action"])
            self.assertTrue(allowed["written"])

            upper_identity = replay_identity({"retry": "upper-bound"})
            upper_target = record_path(project, upper_identity)
            upper_target.parent.mkdir(parents=True, exist_ok=True)
            upper_record = {
                **upper_identity,
                "attempt": 10,
                "status": "failed",
                "failure_class": "transient_transport_failure",
                "result": {"status": "transport-failed"},
            }
            upper_target.write_text(json.dumps(upper_record), encoding="utf-8")
            upper_snapshot = upper_target.read_bytes(), upper_target.stat().st_mtime_ns
            eleventh = materialize_replay_result(
                project,
                "neutral-data",
                upper_identity,
                {"status": "passed"},
                None,
                11,
                True,
                retry_policy={"max_attempts": 10},
            )
            self.assertEqual(REPLAY_RETRY_EXHAUSTED, eleventh["error"]["code"])
            self.assertEqual(
                upper_snapshot,
                (upper_target.read_bytes(), upper_target.stat().st_mtime_ns),
            )

    def test_ac4_same_failed_attempt_reuses_without_policy_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity({"same-attempt": True})
            result = {"status": "transport-failed"}
            first = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                result,
                "transient_transport_failure",
                1,
                True,
            )
            target = record_path(project, identity)
            snapshot = target.read_bytes(), target.stat().st_mtime_ns
            writes: list[Path] = []
            replay = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                result,
                "transient_transport_failure",
                1,
                True,
                lambda path, _content: writes.append(path),
            )
            self.assertTrue(first["written"])
            self.assertEqual("reuse", replay["action"])
            self.assertNotIn("error", replay)
            self.assertEqual([], writes)
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))

    def test_ac5_dry_run_matches_execute_decision_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity()
            dry = materialize_replay_result(
                project, "neutral-data", identity, successful_result(), None, 1, False
            )
            self.assertEqual("execute", dry["action"])
            self.assertFalse(dry["written"])
            self.assertNotIn("record", dry)
            self.assertFalse((project / "neutral-data").exists())
            written = materialize_replay_result(
                project, "neutral-data", identity, successful_result(), None, 1, True
            )
            for field in ("event_id", "run_id", "input_fingerprint", "plan_id", "action", "attempt"):
                self.assertEqual(dry[field], written[field])

            with self.assertRaises(ReplayContractError) as captured:
                materialize_replay_result(
                    project, "", identity, successful_result(), None, 1, False
                )
            self.assertEqual(REPLAY_INPUT_INVALID, captured.exception.code)

    def test_ac5_atomic_replace_failure_preserves_original_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            identity = replay_identity()
            materialize_replay_result(
                project,
                "neutral-data",
                identity,
                {"status": "transport-failed"},
                "transient_transport_failure",
                1,
                True,
            )
            target = record_path(project, identity)
            snapshot = target.read_bytes(), target.stat().st_mtime_ns
            with mock.patch("vibe_workflow_replay.os.replace", side_effect=OSError("private path")):
                failed = materialize_replay_result(
                    project,
                    "neutral-data",
                    identity,
                    {"status": "passed"},
                    None,
                    2,
                    True,
                    retry_policy={"max_attempts": 10},
                )
            self.assertEqual(REPLAY_WRITE_FAILED, failed["error"]["code"])
            self.assertFalse(failed["written"])
            self.assertEqual(1, failed["record"]["attempt"])
            self.assertEqual(snapshot, (target.read_bytes(), target.stat().st_mtime_ns))
            self.assertEqual([], list(target.parent.glob(".*.tmp")))

    def test_ac5_reader_injection_and_outside_data_directory_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            project = Path(temporary)
            identity = replay_identity({"reader": True})
            injected = materialize_replay_result(
                project,
                "neutral-data",
                identity,
                successful_result(),
                None,
                1,
                False,
                record_reader=lambda _path: (None, ("record", "UNREADABLE_RECORD")),
            )
            self.assertEqual(REPLAY_RECORD_INVALID, injected["error"]["code"])
            self.assertFalse((project / "neutral-data").exists())

            with self.assertRaises(ReplayContractError) as invalid_reader:
                materialize_replay_result(
                    project,
                    "neutral-data",
                    identity,
                    successful_result(),
                    None,
                    1,
                    False,
                    record_reader=lambda _path: (
                        None,
                        ("record", str(project / "private-record")),
                    ),
                )
            self.assertEqual(REPLAY_INPUT_INVALID, invalid_reader.exception.code)
            self.assertNotIn(str(project), str(invalid_reader.exception.report()))

            with self.assertRaises(ReplayContractError) as captured:
                materialize_replay_result(
                    project, outside, identity, successful_result(), None, 1, False
                )
            self.assertEqual(REPLAY_INPUT_INVALID, captured.exception.code)

            linked_data = project / "linked-data"
            try:
                linked_data.symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaises(ReplayContractError) as symlink_error:
                    materialize_replay_result(
                        project,
                        "linked-data",
                        identity,
                        successful_result(),
                        None,
                        1,
                        False,
                    )
                self.assertEqual(REPLAY_INPUT_INVALID, symlink_error.exception.code)


if __name__ == "__main__":
    unittest.main()
