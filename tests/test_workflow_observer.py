"""Tests for idempotent workflow-event materialization."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / ".ccgs-core" / "scripts"
LANGFUSE_ROOT = ROOT / "integrations" / "langfuse"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(LANGFUSE_ROOT))

from ccgs_langfuse_adapter import validate_event_document
from ccgs_workflow_observer import (
    WorkflowObserverError,
    build_workflow_event,
    materialize_workflow_event,
    workflow_event_report,
)
from vibe_observability import project_workflow_event

STORY = "ccgs-data/production/epics/sample/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"
STAMP = "2026-07-11T09:00:00Z"


def atomic_write(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return True


def build(project: Path, **overrides):
    values = {
        "story_path": STORY,
        "evidence_path": EVIDENCE,
        "project_id": "fixture-project",
        "event_id": "windmill-story-001-run-001",
        "trace_key": "story-001-workflow",
        "session_id": "fixture-session-001",
        "environment": "fixture",
        "surface": "windmill",
        "operation": "story-closeout",
        "status": "passed",
        "query": "Is STORY-001 ready to close?",
        "retrieval_references": ["ccgs-data/design/gdd/core-loop.md"],
        "failure_codes": [],
        "timestamp": STAMP,
    }
    values.update(overrides)
    return build_workflow_event(project, "ccgs-data", **values)


class WorkflowObserverTests(unittest.TestCase):
    def test_local_event_contract_is_preserved_while_remote_projection_is_neutral(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = build(project)
            before = json.dumps(document, sort_keys=True)
            neutral = project_workflow_event(document, "fixture-project")
        self.assertEqual(json.dumps(document, sort_keys=True), before)
        self.assertIn("input", document)
        self.assertIn("output", document)
        self.assertNotIn("input", neutral)
        self.assertNotIn("output", neutral)
        self.assertEqual(
            set(neutral["metrics"][0]), {"name", "value", "data_type"}
        )

    def test_event_uses_context_evidence_retrieval_and_exactly_two_scores(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = build(project)
            event = validate_event_document(
                document,
                "ccgs-data/production/observability/events/windmill-story-001-run-001.json",
            )

        self.assertEqual(event.status, "pass")
        self.assertEqual(event.story_id, "STORY-001")
        self.assertEqual(len(event.scores), 2)
        self.assertEqual(document["metadata"]["context_source"], "qdrant")
        self.assertEqual(document["metadata"]["retrieval_count"], 1)
        self.assertIn(STORY, document["input"]["references"])
        self.assertIn(EVIDENCE, document["input"]["references"])
        self.assertRegex(document["input"]["context_manifest"], r"^[0-9a-f]{64}$")

    def test_second_write_reuses_event_without_timestamp_or_mtime_churn(self) -> None:
        with materialized_fixture("mature-project") as project:
            first_document = build(project)
            relative, first_written, first = materialize_workflow_event(
                project,
                "ccgs-data",
                first_document,
                write=True,
                atomic_write=atomic_write,
            )
            target = project / relative
            first_mtime = target.stat().st_mtime_ns
            second_document = build(project, timestamp="2026-07-11T10:00:00Z")
            _, second_written, second = materialize_workflow_event(
                project,
                "ccgs-data",
                second_document,
                write=True,
                atomic_write=atomic_write,
            )

            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertEqual(first, second)
            self.assertEqual(target.stat().st_mtime_ns, first_mtime)
            self.assertEqual(list(project.rglob("*.tmp")), [])

    def test_existing_event_identity_conflict_fails_closed(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = build(project)
            materialize_workflow_event(
                project, "ccgs-data", document, write=True, atomic_write=atomic_write
            )
            conflict = build(project, project_id="another-project")
            with self.assertRaisesRegex(WorkflowObserverError, "identity conflicts"):
                materialize_workflow_event(
                    project,
                    "ccgs-data",
                    conflict,
                    write=True,
                    atomic_write=atomic_write,
                )

    def test_fixed_inputs_are_identical_across_engine_overlays(self) -> None:
        documents = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                documents.append(build(project))
        self.assertEqual(documents[0], documents[1])
        self.assertEqual(documents[1], documents[2])

    def test_report_exposes_stable_trace_span_and_manifest(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = build(project)
            report = workflow_event_report(
                "ccgs-data/production/observability/events/windmill-story-001-run-001.json",
                document,
                mode="dry-run",
                written=False,
            )
            repeated = workflow_event_report(
                report["event"], document, mode="dry-run", written=False
            )
        self.assertEqual(report["trace_id"], repeated["trace_id"])
        self.assertEqual(report["span_id"], repeated["span_id"])
        self.assertEqual(report["manifest_sha256"], repeated["manifest_sha256"])
        self.assertEqual(report["score_count"], 2)

    @unittest.skipUnless(os.name == "nt", "ccgs.cmd integration requires Windows")
    def test_cli_dry_run_is_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = subprocess.run(
                [
                    str(ROOT / "ccgs.cmd"),
                    "workflow-observe",
                    "--project-root",
                    str(project),
                    "--story",
                    STORY,
                    "--evidence",
                    EVIDENCE,
                    "--project-id",
                    "fixture-project",
                    "--event-id",
                    "windmill-story-001-run-001",
                    "--trace-key",
                    "story-001-workflow",
                    "--session-id",
                    "fixture-session-001",
                    "--environment",
                    "fixture",
                    "--status",
                    "pass",
                    "--timestamp",
                    STAMP,
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            report = json.loads(process.stdout)
            after = tree_digest(project)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(report["written"])
        self.assertEqual(report["score_count"], 2)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
