"""Acceptance coverage for STORY-UWA-001's public manifest contract."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ccgs_cli import main
from vibe_project_manifest import (
    MANIFEST_EMPTY_STEPS,
    MANIFEST_INVALID_JSON,
    MANIFEST_NOT_FOUND,
    MANIFEST_SCHEMA_INVALID,
    MANIFEST_SCHEMA_UNSUPPORTED,
    ManifestError,
    load_manifest,
)


def valid_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "steps": [
            {
                "id": "prepare",
                "argv": ["tool", "prepare"],
                "environment": {"MODE": "check"},
                "artifacts": ["output/summary.json"],
                "acceptance_mapping": ["AC-1"],
            },
            {
                "id": "verify",
                "argv": ["tool", "verify"],
                "depends_on": ["prepare"],
                "working_directory": ".",
                "acceptance_mapping": ["AC-2", "AC-3"],
            },
        ],
    }


class ManifestContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, document: object, relative: str = "vibe-workflow.json") -> Path:
        target = self.project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        return target

    def assert_error(self, code: str, *, for_execution: bool = False, path: str | None = None) -> ManifestError:
        with self.assertRaises(ManifestError) as caught:
            load_manifest(
                self.project,
                ROOT,
                path,
                for_execution=for_execution,
            )
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_ac1_returns_only_declared_steps_without_adding_commands(self) -> None:
        document = valid_manifest()
        self.write_manifest(document)

        result = load_manifest(self.project, ROOT, for_execution=True)

        self.assertEqual(document["steps"], result["steps"])
        self.assertEqual(["prepare", "verify"], [step["id"] for step in result["steps"]])
        self.assertNotIn("commands", result)
        self.assertNotIn("engine", json.dumps(result).lower())

    def test_ac2_default_path_uses_draft_2020_12_schema_and_versioned_result(self) -> None:
        self.write_manifest(valid_manifest())

        result = load_manifest(self.project, ROOT)
        schema = json.loads((ROOT / result["schema_path"]).read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual("1.0", result["contract_version"])
        self.assertEqual("1.0", result["schema_version"])
        self.assertEqual("vibe-workflow.json", result["manifest_path"])
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])

    def test_ac2_explicit_path_must_be_project_relative(self) -> None:
        self.write_manifest(valid_manifest(), "config/workflow.json")
        result = load_manifest(self.project, ROOT, "config/workflow.json")
        self.assertEqual("config/workflow.json", result["manifest_path"])

        absolute = str(self.project / "config/workflow.json")
        error = self.assert_error(MANIFEST_SCHEMA_INVALID, path=absolute)
        report = json.dumps(error.report("diagnostic"))
        self.assertNotIn(absolute, report)
        self.assertEqual("<absolute>", error.details["manifest_path"])
        self.assert_error(MANIFEST_SCHEMA_INVALID, path="../workflow.json")

    def test_ac2_compatibility_yaml_is_never_used_as_consumer_default(self) -> None:
        (self.project / "ccgs.workflow.yaml").write_text("schema_version: 1.0\n", encoding="utf-8")
        self.assert_error(MANIFEST_NOT_FOUND)

    def test_ac3_stable_preflight_errors(self) -> None:
        self.assert_error(MANIFEST_NOT_FOUND, for_execution=True)

        (self.project / "vibe-workflow.json").write_text("{not-json", encoding="utf-8")
        self.assert_error(MANIFEST_INVALID_JSON, for_execution=True)

        self.write_manifest({"schema_version": "1.0"})
        self.assert_error(MANIFEST_SCHEMA_INVALID, for_execution=True)

        self.write_manifest({"schema_version": "1.0", "steps": []})
        self.assert_error(MANIFEST_EMPTY_STEPS, for_execution=True)

    def test_ac3_non_standard_json_constants_are_invalid_json(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            (self.project / "vibe-workflow.json").write_text(
                f'{{"schema_version": {constant}, "steps": []}}',
                encoding="utf-8",
            )
            self.assert_error(MANIFEST_INVALID_JSON, for_execution=True)

    def test_ac3_invalid_utf8_is_invalid_json(self) -> None:
        (self.project / "vibe-workflow.json").write_bytes(b"\xff\xfe\x00")
        self.assert_error(MANIFEST_INVALID_JSON, for_execution=True)

    def test_ac3_every_execution_failure_precedes_subprocess_start(self) -> None:
        scenarios = (
            (None, MANIFEST_NOT_FOUND),
            ("{not-json", MANIFEST_INVALID_JSON),
            ({"schema_version": "1.0"}, MANIFEST_SCHEMA_INVALID),
            ({"schema_version": "2.0", "steps": []}, MANIFEST_SCHEMA_UNSUPPORTED),
            ({"schema_version": "1.0", "steps": []}, MANIFEST_EMPTY_STEPS),
        )
        with patch.object(subprocess, "run") as run:
            for index, (document, expected_code) in enumerate(scenarios):
                project = self.project / str(index)
                project.mkdir()
                if isinstance(document, str):
                    (project / "vibe-workflow.json").write_text(document, encoding="utf-8")
                elif document is not None:
                    (project / "vibe-workflow.json").write_text(json.dumps(document), encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main(["workflow-request", "--project-root", str(project)])
                self.assertEqual(1, exit_code)
                self.assertEqual(expected_code, json.loads(output.getvalue())["error"]["code"])
            run.assert_not_called()

    def test_ac3_empty_steps_remain_available_for_diagnostics(self) -> None:
        self.write_manifest({"schema_version": "1.0", "steps": []})
        result = load_manifest(self.project, ROOT)
        self.assertEqual([], result["steps"])
        self.assertEqual("diagnostic", result["mode"])

    def test_ac3_unknown_schema_reports_supported_version_and_migration_hint(self) -> None:
        self.write_manifest({"schema_version": "2.0", "steps": []})
        error = self.assert_error(MANIFEST_SCHEMA_UNSUPPORTED, for_execution=True)
        self.assertEqual(["1.0"], error.details["supported_versions"])
        self.assertIn("1.0", error.details["migration_hint"])

    def test_unknown_fields_and_duplicate_step_ids_are_rejected(self) -> None:
        document = valid_manifest()
        document["extra"] = True
        self.write_manifest(document)
        self.assert_error(MANIFEST_SCHEMA_INVALID)

        document = valid_manifest()
        document["steps"][1]["id"] = "prepare"
        self.write_manifest(document)
        self.assert_error(MANIFEST_SCHEMA_INVALID)

        document = valid_manifest()
        document["steps"][0]["shell"] = "tool prepare"
        self.write_manifest(document)
        self.assert_error(MANIFEST_SCHEMA_INVALID)

        document = valid_manifest()
        del document["steps"][0]["argv"]
        self.write_manifest(document)
        self.assert_error(MANIFEST_SCHEMA_INVALID)

    def test_schema_constraints_are_applied_to_nested_values(self) -> None:
        document = valid_manifest()
        document["steps"][0]["environment"] = {"MODE": True}
        self.write_manifest(document)
        self.assert_error(MANIFEST_SCHEMA_INVALID)

        document = valid_manifest()
        document["steps"][0]["argv"] = ["tool", "same", "same"]
        self.write_manifest(document)
        result = load_manifest(self.project, ROOT)
        self.assertEqual(["tool", "same", "same"], result["steps"][0]["argv"])

    def test_schema_rejects_non_scalar_unicode_without_leaking_surrogates(self) -> None:
        documents = []

        invalid_version = valid_manifest()
        invalid_version["schema_version"] = "\ud800"
        documents.append((invalid_version, "$.schema_version"))

        invalid_value = valid_manifest()
        invalid_value["steps"][0]["id"] = "\ud800"
        documents.append((invalid_value, "$.steps[0].id"))

        invalid_key = valid_manifest()
        invalid_key["steps"][0]["environment"] = {"\udfff": "value"}
        documents.append((invalid_key, "$.steps[0].environment"))

        for document, expected_path in documents:
            with self.subTest(path=expected_path):
                self.write_manifest(document)
                error = self.assert_error(MANIFEST_SCHEMA_INVALID, for_execution=True)
                self.assertEqual(expected_path, error.details["path"])
                self.assertIn("Unicode scalar values", error.details["reason"])
                json.dumps(error.report("execution-request"), ensure_ascii=False).encode("utf-8")

    def test_schema_rejects_empty_wrong_type_and_duplicate_list_values(self) -> None:
        invalid_documents = (
            {"schema_version": "1.0", "steps": "prepare"},
            {"schema_version": "1.0", "steps": [{"id": "", "argv": ["tool"]}]},
            {"schema_version": "1.0", "steps": [{"id": "prepare", "argv": []}]},
            {
                "schema_version": "1.0",
                "steps": [{"id": "prepare", "argv": ["tool"], "depends_on": ["x", "x"]}],
            },
            {
                "schema_version": "1.0",
                "steps": [{"id": "prepare", "argv": ["tool"], "artifacts": ["x", "x"]}],
            },
            {
                "schema_version": "1.0",
                "steps": [
                    {"id": "prepare", "argv": ["tool"], "acceptance_mapping": ["AC-1", "AC-1"]}
                ],
            },
        )
        for document in invalid_documents:
            self.write_manifest(document)
            self.assert_error(MANIFEST_SCHEMA_INVALID)

    def test_explicit_manifest_symlink_cannot_escape_project_root(self) -> None:
        consumer = self.project / "consumer"
        consumer.mkdir()
        outside = self.project / "outside.json"
        outside.write_text(json.dumps(valid_manifest()), encoding="utf-8")
        link = consumer / "workflow.json"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises(ManifestError) as caught:
            load_manifest(consumer, ROOT, "workflow.json")
        self.assertEqual(MANIFEST_SCHEMA_INVALID, caught.exception.code)

    def test_cli_success_paths_are_read_only_and_start_no_subprocess(self) -> None:
        manifest = self.write_manifest(valid_manifest())
        original_content = manifest.read_bytes()
        original_mtime = manifest.stat().st_mtime_ns

        with patch.object(subprocess, "run") as run:
            for command, expected_mode in (
                ("manifest-load", "diagnostic"),
                ("workflow-request", "execution-request"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main([command, "--project-root", str(self.project)])
                payload = json.loads(output.getvalue())
                self.assertEqual(0, exit_code)
                self.assertTrue(payload["ok"])
                self.assertEqual("1.0", payload["contract_version"])
                self.assertEqual(expected_mode, payload["mode"])
            run.assert_not_called()

        self.assertEqual(original_content, manifest.read_bytes())
        self.assertEqual(original_mtime, manifest.stat().st_mtime_ns)

    def test_workflow_request_emits_machine_error_without_subprocess(self) -> None:
        self.write_manifest({"schema_version": "1.0", "steps": []})
        output = io.StringIO()
        with patch.object(subprocess, "run") as run, redirect_stdout(output):
            exit_code = main(["workflow-request", "--project-root", str(self.project)])

        self.assertEqual(1, exit_code)
        run.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(MANIFEST_EMPTY_STEPS, payload["error"]["code"])
        self.assertEqual("1.0", payload["contract_version"])


if __name__ == "__main__":
    unittest.main()
