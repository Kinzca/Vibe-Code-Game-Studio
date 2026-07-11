"""Batch 5B tests for unified Allure test and Closeout Evidence reports."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"
STORY = "ccgs-data/production/epics/sample/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"
NORMALIZED = "ccgs-data/production/qa/test-results/story-001-tests.json"
JUNIT = "ccgs-data/production/qa/test-results/story-001-junit.xml"
ALLURE_ROOT = Path("ccgs-data/production/qa/allure-results")


def run_export(
    project: Path,
    run_id: str,
    *extra: str,
    write: bool = False,
    results: tuple[str, ...] = (NORMALIZED,),
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(CLI),
        "allure-export",
        "--project-root",
        str(project),
        "--story",
        STORY,
        "--run-id",
        run_id,
    ]
    for result in results:
        command.extend(("--test-result", result))
    command.extend(extra)
    command.append("--write" if write else "--dry-run")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def output_dir(project: Path, run_id: str) -> Path:
    return project / ALLURE_ROOT / run_id


def load_results(directory: Path) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*-result.json"))
    ]


def result_by_full_name(directory: Path) -> dict[str, dict[str, object]]:
    return {str(item["fullName"]): item for item in load_results(directory)}


class AllureAdapterTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_lists_exact_files(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = run_export(
                project,
                "dry-run-001",
                "--engine",
                "agnostic",
                "--environment",
                "fixture",
                results=(NORMALIZED, JUNIT),
            )
            report = json.loads(process.stdout)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(report["total_results"], 4)
            self.assertEqual(report["statuses"]["passed"], 4)
            self.assertEqual(report["mode"], "dry-run")
            self.assertFalse(report["written"])
            self.assertEqual(len(report["files"]), 10)
            self.assertEqual(tree_digest(project), before)
            self.assertFalse(output_dir(project, "dry-run-001").exists())
            self.assertNotIn(str(project), process.stdout)

    def test_write_combines_normalized_junit_and_closeout_evidence(self) -> None:
        with materialized_fixture("mature-project") as project:
            process = run_export(
                project,
                "combined-001",
                "--engine",
                "godot",
                "--environment",
                "ci",
                "--build-name",
                "fixture-build",
                "--build-url",
                "https://ci.example/build/1",
                results=(NORMALIZED, JUNIT),
                write=True,
            )
            report = json.loads(process.stdout)
            directory = output_dir(project, "combined-001")
            results = load_results(directory)
            evidence = next(item for item in results if item["fullName"] == "ccgs.closeout.STORY-001")

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report["written"])
            self.assertEqual(len(results), 4)
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(len(evidence["steps"]), 4)
            self.assertEqual(evidence["attachments"][0]["type"], "application/json")
            attachment = directory / str(evidence["attachments"][0]["source"])
            self.assertEqual(json.loads(attachment.read_text(encoding="utf-8"))["story_id"], "STORY-001")
            executor = json.loads((directory / "executor.json").read_text(encoding="utf-8"))
            self.assertEqual(executor["buildName"], "fixture-build")
            self.assertEqual(executor["buildUrl"], "https://ci.example/build/1")
            categories = json.loads((directory / "categories.json").read_text(encoding="utf-8"))
            self.assertEqual(categories[0]["name"], "CCGS Evidence failures")
            environment = (directory / "environment.properties").read_text(encoding="utf-8")
            self.assertIn("Engine=godot", environment)
            self.assertIn("Environment=ci", environment)

    def test_identifiers_are_stable_across_runs_but_uuids_are_unique(self) -> None:
        with materialized_fixture("mature-project") as project:
            first = run_export(project, "history-001", write=True)
            second = run_export(project, "history-002", write=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            left = result_by_full_name(output_dir(project, "history-001"))
            right = result_by_full_name(output_dir(project, "history-002"))
            self.assertEqual(set(left), set(right))
            for name in left:
                self.assertEqual(left[name]["historyId"], right[name]["historyId"])
                self.assertEqual(left[name]["testCaseId"], right[name]["testCaseId"])
                self.assertNotEqual(left[name]["uuid"], right[name]["uuid"])

    def test_repeated_write_is_idempotent(self) -> None:
        with materialized_fixture("mature-project") as project:
            first = run_export(project, "idempotent-001", write=True)
            directory = output_dir(project, "idempotent-001")
            before = tree_digest(directory)
            mtimes = {path.name: path.stat().st_mtime_ns for path in directory.iterdir()}
            second = run_export(project, "idempotent-001", write=True)
            report = json.loads(second.stdout)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(report["written"])
            self.assertEqual(tree_digest(directory), before)
            self.assertEqual(
                {path.name: path.stat().st_mtime_ns for path in directory.iterdir()},
                mtimes,
            )
            self.assertEqual(list(directory.parent.glob("*.tmp")), [])

    def test_conflicting_run_directory_is_never_overwritten(self) -> None:
        with materialized_fixture("mature-project") as project:
            first = run_export(project, "immutable-001", write=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            executor = output_dir(project, "immutable-001") / "executor.json"
            executor.write_text("conflict\n", encoding="utf-8", newline="\n")
            before = tree_digest(output_dir(project, "immutable-001"))
            second = run_export(project, "immutable-001", write=True)

            self.assertEqual(second.returncode, 2)
            self.assertIn("different content", second.stderr)
            self.assertEqual(tree_digest(output_dir(project, "immutable-001")), before)
            self.assertEqual(list(output_dir(project, "immutable-001").parent.glob("*.tmp")), [])

    def test_failed_and_blocked_evidence_map_to_allure_statuses(self) -> None:
        for evidence_status, expected in (("fail", "failed"), ("blocked", "skipped")):
            with self.subTest(evidence_status=evidence_status):
                with materialized_fixture("mature-project") as project:
                    evidence_path = project / EVIDENCE
                    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                    evidence["result"] = evidence_status
                    if evidence_status == "fail":
                        evidence["acceptance_criteria"][0]["status"] = "fail"
                    evidence_path.write_text(
                        json.dumps(evidence, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    process = run_export(project, f"evidence-{evidence_status}", write=True)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    result = result_by_full_name(
                        output_dir(project, f"evidence-{evidence_status}")
                    )["ccgs.closeout.STORY-001"]
                    self.assertEqual(result["status"], expected)
                    self.assertIn("[CCGS Evidence]", result["statusDetails"]["message"])

    def test_junit_failures_errors_and_output_are_preserved(self) -> None:
        with materialized_fixture("mature-project") as project:
            junit = project / "ccgs-data/production/qa/test-results/mixed.xml"
            junit.write_text(
                """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<testsuite name=\"mixed\" tests=\"3\">
  <testcase classname=\"pkg.Sample\" name=\"passes\" time=\"0.01\"><system-out>ok</system-out></testcase>
  <testcase classname=\"pkg.Sample\" name=\"fails\" time=\"0.02\"><failure message=\"assertion\">trace-a</failure></testcase>
  <testcase classname=\"pkg.Sample\" name=\"breaks\" time=\"0.03\"><error message=\"setup\">trace-b</error></testcase>
</testsuite>
""",
                encoding="utf-8",
                newline="\n",
            )
            process = run_export(
                project,
                "junit-mixed",
                results=("ccgs-data/production/qa/test-results/mixed.xml",),
                write=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            results = result_by_full_name(output_dir(project, "junit-mixed"))
            self.assertEqual(results["pkg.Sample.passes"]["status"], "passed")
            self.assertEqual(results["pkg.Sample.fails"]["status"], "failed")
            self.assertEqual(results["pkg.Sample.breaks"]["status"], "broken")
            self.assertEqual(results["pkg.Sample.passes"]["attachments"][0]["name"], "stdout")
            labels = {
                item["name"]: item["value"]
                for item in results["pkg.Sample.passes"]["labels"]
            }
            self.assertEqual(labels["framework"], "junit")

    def test_paths_and_numeric_inputs_are_guarded(self) -> None:
        with materialized_fixture("mature-project") as project:
            escaped = run_export(project, "../escaped")
            wrong_scope = run_export(project, "wrong-scope", results=(EVIDENCE,))
            negative_time = run_export(project, "negative-time", "--start-ms", "-1")

            self.assertEqual(escaped.returncode, 2)
            self.assertIn("run_id", escaped.stderr)
            self.assertEqual(wrong_scope.returncode, 2)
            self.assertIn("test result must stay under", wrong_scope.stderr)
            self.assertEqual(negative_time.returncode, 2)
            self.assertIn("--start-ms", negative_time.stderr)
            self.assertFalse((project / "escaped").exists())

    def test_normalized_schema_and_invalid_document_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/automated-test-results.schema.json").read_text(encoding="utf-8")
        )
        fixture = json.loads(
            (
                ROOT
                / "tests/fixtures/projects/mature-project/project"
                / NORMALIZED
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(fixture["schema_version"], schema["properties"]["schema_version"]["const"])
        self.assertTrue(fixture["tests"])

        with materialized_fixture("mature-project") as project:
            invalid = project / NORMALIZED
            invalid.write_text('{"schema_version":"1.0","tests":[{"id":"x"}]}\n', encoding="utf-8")
            process = run_export(project, "invalid-normalized")
            self.assertEqual(process.returncode, 2)
            self.assertIn("tests[0].name must be a non-empty string", process.stderr)

    def test_reports_are_identical_across_engine_overlays(self) -> None:
        reports = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                process = run_export(project, "cross-engine-001")
                self.assertEqual(process.returncode, 0, process.stderr)
                reports.append(json.loads(process.stdout))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])


if __name__ == "__main__":
    unittest.main()