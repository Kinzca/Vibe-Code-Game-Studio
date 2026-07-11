"""Batch 4 tests for Story automation and evidence closeout."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from ccgs_story_workflow import can_transition, normalize_state, validate_evidence

CLI = SCRIPTS / "ccgs_cli.py"
STORY = "ccgs-data/production/epics/sample/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"


def run_cli(project: Path, command: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), command, "--project-root", str(project), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def story_path(project: Path) -> Path:
    return project / STORY


def set_review(project: Path) -> None:
    path = story_path(project)
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: ready", "status: review"),
        encoding="utf-8",
        newline="\n",
    )


class StoryWorkflowTests(unittest.TestCase):
    def test_state_machine_contract(self) -> None:
        allowed = {
            ("draft", "ready"), ("draft", "blocked"),
            ("ready", "in-progress"), ("ready", "blocked"),
            ("in-progress", "review"), ("in-progress", "blocked"),
            ("review", "in-progress"), ("review", "blocked"), ("review", "done"),
            ("blocked", "ready"),
        }
        states = ("draft", "ready", "in-progress", "review", "blocked", "done")
        for current in states:
            for target in states:
                expected = current == target or (current, target) in allowed
                self.assertEqual(can_transition(current, target), expected)
        self.assertEqual(normalize_state("In Progress"), "in-progress")
        self.assertEqual(normalize_state("Complete"), "done")

    def test_schema_and_fixture_evidence_share_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/evidence.schema.json").read_text(encoding="utf-8")
        )
        evidence = json.loads(
            (
                ROOT / "tests/fixtures/projects/mature-project/project" / EVIDENCE
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0")
        self.assertEqual(validate_evidence(evidence), [])
        invalid = dict(evidence)
        invalid["unexpected"] = True
        self.assertEqual(
            validate_evidence(invalid)[0],
            {"path": "$.unexpected", "message": "is not allowed"},
        )

    def test_evidence_validate_is_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = run_cli(project, "evidence-validate", "--evidence", EVIDENCE)
            report = json.loads(process.stdout)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report["valid"])
            self.assertEqual(report["story_id"], "STORY-001")
            self.assertEqual(tree_digest(project), before)

    def test_transition_dry_run_and_invalid_write_are_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            preview = run_cli(
                project, "story-advance", "--story", STORY,
                "--to", "in-progress", "--reason", "start", "--dry-run",
            )
            blocked = run_cli(
                project, "story-advance", "--story", STORY, "--to", "done", "--write",
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertTrue(json.loads(preview.stdout)["changed"])
            self.assertEqual(blocked.returncode, 1, blocked.stderr)
            self.assertFalse(json.loads(blocked.stdout)["allowed"])
            self.assertEqual(tree_digest(project), before)

    def test_transition_write_is_atomic_and_idempotent(self) -> None:
        with materialized_fixture("mature-project") as project:
            path = story_path(project)
            first = run_cli(
                project, "story-advance", "--story", STORY,
                "--to", "in-progress", "--write",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["written"])
            self.assertIn("status: in-progress", path.read_text(encoding="utf-8"))
            before, mtime = tree_digest(project), path.stat().st_mtime_ns
            second = run_cli(
                project, "story-advance", "--story", STORY,
                "--to", "in-progress", "--write",
            )
            report = json.loads(second.stdout)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(report["changed"])
            self.assertFalse(report["written"])
            self.assertEqual(tree_digest(project), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(list(project.rglob("*.tmp")), [])

    def test_closeout_dry_run_passes_without_writing(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            before = tree_digest(project)
            process = run_cli(
                project, "closeout", "--story", STORY, "--dry-run"
            )
            report = json.loads(process.stdout)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(report["verdict"], "pass")
            self.assertEqual(report["target_state"], "done")
            self.assertFalse(report["written"])
            self.assertEqual(tree_digest(project), before)

    def test_closeout_write_and_retry_are_idempotent(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            path = story_path(project)
            first = run_cli(project, "closeout", "--story", STORY, "--write")
            self.assertEqual(first.returncode, 0, first.stderr)
            content = path.read_text(encoding="utf-8")
            self.assertIn("status: done", content)
            self.assertIn("- Verdict: PASS", content)
            before, mtime = tree_digest(project), path.stat().st_mtime_ns
            second = run_cli(project, "closeout", "--story", STORY, "--write")
            report = json.loads(second.stdout)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(report["would_write"])
            self.assertFalse(report["written"])
            self.assertEqual(tree_digest(project), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(list(project.rglob("*.tmp")), [])

    def test_failed_closeout_writes_reasons_without_advancing(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            evidence_path = project / EVIDENCE
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["result"] = "fail"
            evidence["acceptance_criteria"][1]["status"] = "fail"
            evidence["checks"][0]["status"] = "fail"
            evidence_path.write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            process = run_cli(project, "closeout", "--story", STORY, "--write")
            report = json.loads(process.stdout)
            content = story_path(project).read_text(encoding="utf-8")
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertEqual(report["verdict"], "fail")
            self.assertTrue(report["written"])
            self.assertIn("status: review", content)
            self.assertNotIn("status: done", content)
            self.assertIn("- Verdict: FAIL", content)
            self.assertIn("evidence.result", content)
            self.assertIn("evidence.acceptance", content)
            self.assertIn("evidence.checks", content)
            before, mtime = tree_digest(project), story_path(project).stat().st_mtime_ns
            retry = run_cli(project, "closeout", "--story", STORY, "--write")
            retry_report = json.loads(retry.stdout)
            self.assertEqual(retry.returncode, 1, retry.stderr)
            self.assertFalse(retry_report["written"])
            self.assertEqual(tree_digest(project), before)
            self.assertEqual(story_path(project).stat().st_mtime_ns, mtime)

    def test_missing_evidence_writes_stable_failure(self) -> None:
        with materialized_fixture("mature-project") as project:
            set_review(project)
            process = run_cli(
                project, "closeout", "--story", STORY,
                "--evidence", "ccgs-data/production/qa/evidence/missing.json",
                "--write",
            )
            content = story_path(project).read_text(encoding="utf-8")
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertIn("status: review", content)
            self.assertIn("evidence file not found", content)
            self.assertIn("evidence.schema", content)

    def test_owned_tree_boundaries_are_enforced(self) -> None:
        with materialized_fixture("mature-project") as project:
            story_result = run_cli(
                project, "story-advance",
                "--story", "ccgs-data/design/gdd/core-loop.md",
                "--to", "ready", "--dry-run",
            )
            evidence_result = run_cli(
                project, "evidence-validate",
                "--evidence", "ccgs-data/design/gdd/core-loop.md",
            )
            self.assertEqual(story_result.returncode, 2)
            self.assertIn("production/epics", story_result.stderr)
            self.assertEqual(evidence_result.returncode, 2)
            self.assertIn("production/qa/evidence", evidence_result.stderr)
            closeout_result = run_cli(
                project, "closeout", "--story", STORY,
                "--evidence", "ccgs-data/design/gdd/core-loop.json", "--write",
            )
            self.assertEqual(closeout_result.returncode, 2)
            self.assertIn("production/qa/evidence", closeout_result.stderr)

    def test_closeout_is_identical_across_engines(self) -> None:
        outputs = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                set_review(project)
                process = run_cli(
                    project, "closeout", "--story", STORY, "--dry-run"
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                outputs.append(process.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])


if __name__ == "__main__":
    unittest.main()