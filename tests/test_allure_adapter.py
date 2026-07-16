"""Batch 5B tests for unified Allure test and Closeout Evidence reports."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"
ALLURE_ADAPTER_ROOT = ROOT / "integrations" / "allure"
sys.path.insert(0, str(ROOT / ".ccgs-core" / "scripts"))
sys.path.insert(0, str(ALLURE_ADAPTER_ROOT))
from ccgs_allure_adapter import (  # noqa: E402
    AllureAdapterError,
    build_neutral_allure_bundle,
    preflight_neutral_allure_target,
    validate_neutral_allure_target_path,
    write_neutral_allure_bundle,
)
from ccgs_allure_port import (  # noqa: E402
    allure_capability_document,
    build_allure_reporting_adapter,
    build_allure_reporting_data,
)
from vibe_reporting import build_reporting_request, invoke_reporting  # noqa: E402
STORY = "ccgs-data/production/epics/sample/story-001.md"
EVIDENCE = "ccgs-data/production/qa/evidence/story-001.json"
NORMALIZED = "ccgs-data/production/qa/test-results/story-001-tests.json"
JUNIT = "ccgs-data/production/qa/test-results/story-001-junit.xml"
ALLURE_ROOT = Path("ccgs-data/production/qa/reports")
STATE_ROOTS = (
    "ccgs-data/production/plans",
    "ccgs-data/production/results",
    "ccgs-data/production/qa/evidence",
    "ccgs-data/production/replay",
    "ccgs-data/production/epics",
    "ccgs-data/production/observability/events",
)


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
        for path in sorted(directory.rglob("*-result.json"))
    ]


def result_by_full_name(directory: Path) -> dict[str, dict[str, object]]:
    return {str(item["fullName"]): item for item in load_results(directory)}


def neutral_results() -> list[dict[str, object]]:
    return [
        {
            "id": "unit-001",
            "name": "Neutral unit result",
            "status": "passed",
            "duration_ms": 12,
            "source_ref": "ccgs-data/production/qa/test-results/unit.json",
            "suite": "Unit",
            "start_ms": 100,
        },
        {
            "id": "integration-001",
            "name": "Neutral integration result",
            "status": "failed",
            "duration_ms": 21,
            "source_ref": "ccgs-data/production/qa/test-results/integration.json",
            "failure_code": "ASSERTION_FAILED",
        },
    ]


def neutral_evidence() -> dict[str, object]:
    return {
        "story_id": "STORY-UWA-013",
        "result": "pass",
        "acceptance_criteria": [{
            "id": "AC-1",
            "status": "pass",
            "source_refs": ["ccgs-data/production/qa/evidence/story-013.json"],
        }],
        "checks": [{
            "id": "check-001",
            "type": "integration",
            "status": "pass",
            "source_refs": ["ccgs-data/production/qa/test-results/unit.json"],
        }],
        "source_ref": "ccgs-data/production/qa/evidence/story-013.json",
    }


def state_snapshot(project: Path) -> dict[str, tuple[bytes, int]]:
    """Capture every core lifecycle byte and mtime relevant to reporting isolation."""

    snapshot: dict[str, tuple[bytes, int]] = {}
    for relative_root in STATE_ROOTS:
        root = project / relative_root
        if root.is_dir():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                snapshot[path.relative_to(project).as_posix()] = (
                    path.read_bytes(), path.stat().st_mtime_ns,
                )
    return snapshot


def seed_state_snapshot(project: Path) -> dict[str, tuple[bytes, int]]:
    """Add missing neutral lifecycle sentinels and return their complete snapshot."""

    for relative in (
        "ccgs-data/production/plans/run-plan.json",
        "ccgs-data/production/results/run-result.json",
        "ccgs-data/production/replay/run-replay.json",
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"sentinel":true}\n', encoding="utf-8", newline="\n")
    return state_snapshot(project)


class AllureAdapterTests(unittest.TestCase):
    def test_report_export_and_allure_alias_share_the_public_port_route(self) -> None:
        with materialized_fixture("mature-project") as project:
            commands = []
            for entry in ("report-export", "allure-export"):
                commands.append(subprocess.run(
                    [
                        sys.executable, str(CLI), entry,
                        "--project-root", str(project), "--story", STORY,
                        "--report-id", "alias-001", "--test-result", NORMALIZED,
                        "--dry-run",
                    ],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
                ))
            self.assertEqual([item.returncode for item in commands], [0, 0])
            self.assertEqual(commands[0].stdout, commands[1].stdout)
            report = json.loads(commands[0].stdout)
            self.assertFalse(report["called"])
            self.assertTrue(all(
                item.startswith("ccgs-data/production/qa/reports/alias-001/")
                for item in report["data"]["artifact_refs"]
            ))

        cli = CLI.read_text(encoding="utf-8")
        command_body = cli[cli.index("def command_report_export"):cli.index("def _qdrant_store")]
        self.assertNotIn("subprocess", command_body)
        self.assertNotIn("build_allure_bundle", command_body)
        self.assertNotIn("write_allure_bundle", command_body)
        adapter_source = (ALLURE_ADAPTER_ROOT / "ccgs_allure_adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("def build_allure_bundle", adapter_source)
        self.assertNotIn("def write_allure_bundle", adapter_source)
        self.assertNotIn("project: Path", adapter_source)

    def test_neutral_builder_is_deterministic_and_contains_no_free_logs(self) -> None:
        first = build_neutral_allure_bundle(
            "report-013", neutral_results(), neutral_evidence()
        )
        second = build_neutral_allure_bundle(
            "report-013", neutral_results(), neutral_evidence()
        )
        self.assertEqual(first, second)
        self.assertEqual(first.summary["total_results"], 3)
        self.assertEqual(first.summary["statuses"]["passed"], 2)
        self.assertEqual(first.summary["statuses"]["failed"], 1)
        encoded = b"".join(first.files.values()).lower()
        for forbidden in (b"stdout", b"stderr", b"traceback", b"prompt", b"completion"):
            self.assertNotIn(forbidden, encoded)

    def test_neutral_builder_rejects_old_free_text_and_source_fields(self) -> None:
        for field in ("stdout", "stderr", "trace", "command", "source_code", "metadata"):
            with self.subTest(field=field):
                results = neutral_results()
                results[0][field] = "do-not-retain"
                with self.assertRaises(AllureAdapterError):
                    build_neutral_allure_bundle(
                        "report-013", results, neutral_evidence()
                    )

    def test_blocked_evidence_uses_deferred_item_statuses(self) -> None:
        evidence = neutral_evidence()
        evidence["result"] = "blocked"
        for key in ("acceptance_criteria", "checks"):
            for item in evidence[key]:
                item["status"] = "deferred"
        bundle = build_neutral_allure_bundle(
            "report-blocked", neutral_results(), evidence,
        )
        evidence_result = next(
            json.loads(content)
            for relative, content in bundle.files.items()
            if relative.endswith("-result.json")
            and json.loads(content)["fullName"].startswith("report.evidence.")
        )
        self.assertEqual(evidence_result["status"], "skipped")
        self.assertTrue(all(step["status"] == "skipped" for step in evidence_result["steps"]))

    def test_neutral_writer_reuses_exact_content_and_rejects_conflicts(self) -> None:
        with materialized_fixture("mature-project") as project:
            target = project / "ccgs-data/production/qa/reports/report-013"
            bundle = build_neutral_allure_bundle(
                "report-013", neutral_results(), neutral_evidence()
            )
            self.assertTrue(write_neutral_allure_bundle(target, bundle))
            before = tree_digest(target)
            mtimes = {
                path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                for path in target.rglob("*") if path.is_file()
            }
            self.assertFalse(write_neutral_allure_bundle(target, bundle))
            self.assertEqual(tree_digest(target), before)
            self.assertEqual(
                {
                    path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                    for path in target.rglob("*") if path.is_file()
                },
                mtimes,
            )
            changed = build_neutral_allure_bundle(
                "report-013",
                [{**neutral_results()[0], "duration_ms": 13}],
                neutral_evidence(),
            )
            with self.assertRaises(AllureAdapterError):
                write_neutral_allure_bundle(target, changed)
            self.assertEqual(tree_digest(target), before)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_dry_run_preflights_existing_reuse_and_conflict_without_writes(self) -> None:
        with materialized_fixture("mature-project") as project:
            first = run_export(project, "preflight-001", write=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            target = output_dir(project, "preflight-001")
            before = tree_digest(target)
            mtimes = {
                path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                for path in target.rglob("*") if path.is_file()
            }

            reused = run_export(project, "preflight-001")
            reused_report = json.loads(reused.stdout)
            self.assertEqual(reused.returncode, 0, reused.stderr)
            self.assertTrue(reused_report["data"]["reused"])
            self.assertFalse(reused_report["called"])
            self.assertEqual(tree_digest(target), before)
            self.assertEqual(
                {
                    path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                    for path in target.rglob("*") if path.is_file()
                },
                mtimes,
            )

            artifact = next(target.rglob("*-result.json"))
            artifact.write_text("conflict\n", encoding="utf-8", newline="\n")
            conflict_before = tree_digest(target)
            conflict_mtimes = {
                path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                for path in target.rglob("*") if path.is_file()
            }
            conflict = run_export(project, "preflight-001")
            conflict_report = json.loads(conflict.stdout)
            self.assertEqual(conflict.returncode, 2)
            self.assertTrue(conflict_report["ok"])
            self.assertFalse(conflict_report["called"])
            self.assertEqual(conflict_report["data"]["outcome"], "failed")
            self.assertEqual(
                conflict_report["data"]["failures"][0]["code"],
                "REPORT_OUTPUT_CONFLICT",
            )
            self.assertEqual(tree_digest(target), conflict_before)
            self.assertEqual(
                {
                    path.relative_to(target).as_posix(): path.stat().st_mtime_ns
                    for path in target.rglob("*") if path.is_file()
                },
                conflict_mtimes,
            )
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_neutral_target_preflight_is_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            target = output_dir(project, "preflight-api-001")
            bundle = build_neutral_allure_bundle(
                "preflight-api-001", neutral_results(), neutral_evidence(),
            )
            self.assertFalse(preflight_neutral_allure_target(target, bundle))
            self.assertFalse(target.exists())
            self.assertTrue(write_neutral_allure_bundle(target, bundle))
            before = tree_digest(target)
            self.assertTrue(preflight_neutral_allure_target(target, bundle))
            self.assertEqual(tree_digest(target), before)

    def test_neutral_target_preflight_rejects_symbolic_links(self) -> None:
        with materialized_fixture("mature-project") as project:
            target = output_dir(project, "preflight-symlink-001")
            target.mkdir(parents=True)
            external = project / "ccgs-data/production/qa/evidence/story-001.json"
            link = target / "external-result.json"
            try:
                link.symlink_to(external)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable on this platform")
            bundle = build_neutral_allure_bundle(
                "preflight-symlink-001", neutral_results(), neutral_evidence(),
            )
            with self.assertRaises(AllureAdapterError):
                preflight_neutral_allure_target(target, bundle)

    def test_neutral_target_root_symbolic_link_is_rejected_before_resolution(self) -> None:
        with materialized_fixture("mature-project") as project:
            real_target = output_dir(project, "real-report-001")
            bundle = build_neutral_allure_bundle(
                "root-symlink-001", neutral_results(), neutral_evidence(),
            )
            self.assertTrue(write_neutral_allure_bundle(real_target, bundle))
            linked_target = output_dir(project, "root-symlink-001")
            try:
                linked_target.symlink_to(real_target, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable on this platform")

            with self.assertRaises(AllureAdapterError):
                validate_neutral_allure_target_path(linked_target)
            with self.assertRaises(AllureAdapterError):
                preflight_neutral_allure_target(linked_target, bundle)
            with self.assertRaises(AllureAdapterError):
                write_neutral_allure_bundle(linked_target, bundle)

            process = run_export(project, "root-symlink-001", write=True)
            report = json.loads(process.stdout)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(report["error"]["code"], "PORT_ADAPTER_FAILED")
            self.assertTrue(report["called"])
            self.assertTrue(linked_target.is_symlink())

    def test_neutral_writer_cleans_staging_after_mid_write_failure(self) -> None:
        with materialized_fixture("mature-project") as project:
            target = output_dir(project, "atomic-failure-001")
            bundle = build_neutral_allure_bundle(
                "atomic-failure-001", neutral_results(), neutral_evidence(),
            )
            with mock.patch.object(Path, "write_bytes", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    write_neutral_allure_bundle(target, bundle)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_reporting_port_only_passes_output_ref_and_neutral_bundle_to_writer(self) -> None:
        calls: list[tuple[str, object]] = []

        def writer(output_ref, bundle):
            calls.append((output_ref, bundle))
            return True

        request = {
            "contract_version": "1.0",
            "request_id": "request-013",
            "project_id": "fixture-project",
            "port": "reporting",
            "operation": "export_report",
            "capability": "evidence_report",
            "payload": {
                "contract_version": "1.0",
                "report_id": "report-013",
                "results": neutral_results(),
                "evidence": neutral_evidence(),
                "output_ref": "ccgs-data/production/qa/reports/report-013",
            },
            "references": [],
        }
        response = build_allure_reporting_adapter(writer)(request, 5)
        self.assertEqual(
            allure_capability_document()["capabilities"][0],
            {
                "port": "reporting",
                "operation": "export_report",
                "capability": "evidence_report",
                "contract_versions": ["1.0"],
            },
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], request["payload"]["output_ref"])
        self.assertNotIn("project_root", response)
        self.assertTrue(response["data"]["outcome"] == "generated")
        self.assertEqual(response["data"]["total_results"], 3)
        self.assertEqual(response["data"]["failures"], [])
        reused = build_allure_reporting_adapter(lambda output_ref, bundle: False)(
            request, 5
        )
        self.assertTrue(reused["data"]["reused"])
        self.assertTrue(all(
            item.startswith(request["payload"]["output_ref"] + "/")
            for item in response["data"]["artifact_refs"]
        ))

    def test_dry_run_is_read_only_and_lists_exact_files(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = run_export(
                project, "dry-run-001", results=(NORMALIZED, JUNIT),
            )
            report = json.loads(process.stdout)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report["ok"])
            self.assertFalse(report["called"])
            self.assertEqual(report["data"]["total_results"], 4)
            self.assertEqual(report["data"]["status_counts"]["passed"], 4)
            self.assertEqual(tree_digest(project), before)
            self.assertFalse(output_dir(project, "dry-run-001").exists())
            self.assertNotIn(str(project), process.stdout)

            written = run_export(
                project, "dry-run-001", results=(NORMALIZED, JUNIT), write=True,
            )
            written_report = json.loads(written.stdout)
            actual = sorted(
                path.relative_to(project).as_posix()
                for path in output_dir(project, "dry-run-001").rglob("*")
                if path.is_file()
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(report["data"]["artifact_refs"], actual)
            self.assertEqual(
                report["data"]["artifact_refs"],
                written_report["data"]["artifact_refs"],
            )

    def test_write_combines_normalized_junit_and_closeout_evidence(self) -> None:
        with materialized_fixture("mature-project") as project:
            process = run_export(
                project, "combined-001", results=(NORMALIZED, JUNIT), write=True,
            )
            report = json.loads(process.stdout)
            directory = output_dir(project, "combined-001")
            results = load_results(directory)
            evidence = next(item for item in results if item["fullName"] == "report.evidence.STORY-001")

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report["called"])
            self.assertEqual(report["data"]["outcome"], "generated")
            self.assertEqual(len(results), 4)
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(len(evidence["steps"]), 4)
            self.assertEqual(evidence["attachments"][0]["type"], "application/json")
            attachment = directory / str(evidence["attachments"][0]["source"])
            self.assertEqual(json.loads(attachment.read_text(encoding="utf-8"))["story_id"], "STORY-001")
            categories = json.loads((directory / "categories.json").read_text(encoding="utf-8"))
            self.assertEqual(categories[0]["name"], "CCGS Evidence failures")

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
            self.assertTrue(report["data"]["reused"])
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
            artifact = next(output_dir(project, "immutable-001").rglob("*-result.json"))
            artifact.write_text("conflict\n", encoding="utf-8", newline="\n")
            before = tree_digest(output_dir(project, "immutable-001"))
            second = run_export(project, "immutable-001", write=True)

            self.assertEqual(second.returncode, 2)
            conflict = json.loads(second.stdout)
            self.assertEqual(conflict["error"]["code"], "PORT_ADAPTER_FAILED")
            self.assertTrue(conflict["called"])
            self.assertEqual(tree_digest(output_dir(project, "immutable-001")), before)
            self.assertEqual(list(output_dir(project, "immutable-001").parent.glob("*.tmp")), [])

    def test_write_failures_preserve_all_core_lifecycle_state_bytes_and_mtimes(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = seed_state_snapshot(project)
            request = build_reporting_request(
                neutral_results(), neutral_evidence(), data_dir="ccgs-data",
                report_id="isolation-port-001", request_id="isolation-request-001",
                project_id="fixture-project",
            )
            bundle = build_neutral_allure_bundle(
                "isolation-port-001", neutral_results(), neutral_evidence(),
            )

            def timed_out(_request, _timeout):
                raise TimeoutError("injected")

            def unavailable(_request, _timeout):
                raise OSError("injected")

            def business_failure(value, _timeout):
                data = build_allure_reporting_data(
                    value, bundle, failures=[{
                        "code": "REPORT_RENDER_FAILED",
                        "message": "Report rendering failed",
                        "retryable": False,
                    }],
                )
                return {
                    "contract_version": "1.0",
                    **{
                        key: value[key]
                        for key in (
                            "request_id", "project_id", "port", "operation", "capability",
                        )
                    },
                    "ok": True, "status": "success", "action": "invoke",
                    "called": True, "data": data, "error": None,
                }

            for adapter in (
                timed_out,
                unavailable,
                lambda _request, _timeout: {"malformed": True},
                business_failure,
            ):
                response = invoke_reporting(
                    request, allure_capability_document(), adapter,
                    data_dir="ccgs-data", dry_run=False,
                )
                self.assertTrue(
                    not response["ok"] or response["data"]["outcome"] == "failed"
                )
                self.assertEqual(state_snapshot(project), before)

            escaped = run_export(project, "../escaped-state", write=True)
            self.assertEqual(escaped.returncode, 2)
            self.assertEqual(state_snapshot(project), before)

            invalid_config = run_export(
                project, "invalid-config-state", "--engine", "forbidden", write=True,
            )
            self.assertEqual(invalid_config.returncode, 2)
            self.assertFalse(json.loads(invalid_config.stdout)["called"])
            self.assertEqual(state_snapshot(project), before)

            normalized = project / NORMALIZED
            normalized.write_text(
                '{"schema_version":"1.0","tests":[{"id":"invalid"}]}\n',
                encoding="utf-8", newline="\n",
            )
            invalid = run_export(project, "invalid-state", write=True)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(state_snapshot(project), before)

            normalized.write_bytes(
                (
                    ROOT / "tests/fixtures/projects/mature-project/project" / NORMALIZED
                ).read_bytes()
            )
            conflict_target = output_dir(project, "conflict-state")
            conflict_target.mkdir(parents=True)
            (conflict_target / "foreign.txt").write_text(
                "foreign\n", encoding="utf-8", newline="\n",
            )
            conflict = run_export(project, "conflict-state", write=True)
            self.assertEqual(conflict.returncode, 2)
            self.assertEqual(state_snapshot(project), before)

    def test_failed_and_blocked_evidence_map_to_allure_statuses(self) -> None:
        for evidence_status, expected in (("fail", "failed"), ("blocked", "skipped")):
            with self.subTest(evidence_status=evidence_status):
                with materialized_fixture("mature-project") as project:
                    evidence_path = project / EVIDENCE
                    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                    evidence["result"] = evidence_status
                    if evidence_status == "fail":
                        evidence["acceptance_criteria"][0]["status"] = "fail"
                    else:
                        for item in evidence["acceptance_criteria"]:
                            item["status"] = "deferred"
                        for item in evidence["checks"]:
                            item["status"] = "deferred"
                    evidence_path.write_text(
                        json.dumps(evidence, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    process = run_export(project, f"evidence-{evidence_status}", write=True)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    result = result_by_full_name(
                        output_dir(project, f"evidence-{evidence_status}")
                    )["report.evidence.STORY-001"]
                    self.assertEqual(result["status"], expected)

    def test_junit_failures_errors_are_mapped_and_free_output_is_dropped(self) -> None:
        with materialized_fixture("mature-project") as project:
            junit = project / "ccgs-data/production/qa/test-results/mixed.xml"
            junit.write_text(
                """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<testsuite name=\"mixed\" tests=\"3\">
  <testcase classname=\"pkg.Sample\" name=\"passes\" time=\"0.01\"><system-out>PRIVATE_STDOUT_VALUE</system-out></testcase>
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
            results = [item for item in load_results(output_dir(project, "junit-mixed")) if str(item["fullName"]).startswith("report.tests.")]
            self.assertEqual(sorted(item["status"] for item in results), ["broken", "failed", "passed"])
            encoded = json.dumps(results)
            for forbidden in ("trace-a", "trace-b", "assertion", "setup", "PRIVATE_STDOUT_VALUE"):
                self.assertNotIn(forbidden, encoded)

    def test_junit_negative_or_non_finite_duration_fails_closed(self) -> None:
        for duration in ("-1", "nan", "inf"):
            with self.subTest(duration=duration):
                with materialized_fixture("mature-project") as project:
                    junit = project / "ccgs-data/production/qa/test-results/duration.xml"
                    junit.write_text(
                        f'<testsuite name="duration"><testcase name="bad" time="{duration}"/></testsuite>\n',
                        encoding="utf-8",
                        newline="\n",
                    )
                    process = run_export(
                        project,
                        f"duration-{duration}",
                        results=("ccgs-data/production/qa/test-results/duration.xml",),
                    )
                    report = json.loads(process.stdout)
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(report["error"]["code"], "PORT_REQUEST_INVALID")
                    self.assertFalse(report["called"])

    def test_paths_and_numeric_inputs_are_guarded(self) -> None:
        with materialized_fixture("mature-project") as project:
            escaped = run_export(project, "../escaped")
            wrong_scope = run_export(project, "wrong-scope", results=(EVIDENCE,))
            negative_time = run_export(project, "negative-time", "--start-ms", "-1")

            self.assertEqual(escaped.returncode, 2)
            self.assertFalse(json.loads(escaped.stdout)["called"])
            self.assertEqual(wrong_scope.returncode, 2)
            self.assertFalse(json.loads(wrong_scope.stdout)["called"])
            self.assertEqual(negative_time.returncode, 2)
            self.assertFalse(json.loads(negative_time.stdout)["called"])
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
            report = json.loads(process.stdout)
            self.assertEqual(report["error"]["code"], "PORT_REQUEST_INVALID")
            self.assertFalse(report["called"])

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
