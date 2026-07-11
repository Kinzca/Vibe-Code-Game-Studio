"""Tests for immutable, disposable CCGS fixture workspaces."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import (
    FixtureError,
    OVERLAYS_ROOT,
    PROJECTS_ROOT,
    fixture_catalog,
    load_fixture_manifest,
    materialized_fixture,
    tree_digest,
)


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"


class FixtureCatalogTests(unittest.TestCase):
    """Committed fixture inputs must cover every approved Batch 2 dimension."""

    def test_catalog_contains_all_lifecycle_and_engine_inputs(self) -> None:
        self.assertEqual(
            fixture_catalog(),
            {
                "projects": [
                    "empty-project",
                    "malformed-project",
                    "mature-project",
                    "minimal-project",
                ],
                "engine_overlays": ["cocos", "godot", "unity"],
            },
        )


class FixtureSourceTests(unittest.TestCase):
    """Committed fixture inputs must remain synthetic and structurally valid."""

    def test_repository_has_no_generated_test_artifacts(self) -> None:
        generated = list(ROOT.rglob("__pycache__"))
        self.assertEqual(generated, [])

    def test_engine_overlay_json_markers_are_valid(self) -> None:
        for path in OVERLAYS_ROOT.rglob("*.json"):
            with self.subTest(path=path):
                json.loads(path.read_text(encoding="utf-8"))

    def test_fixture_sources_do_not_reference_consumer_paths(self) -> None:
        forbidden = ("InterwovenSpace", "D:\\", "E:\\")
        for path in (PROJECTS_ROOT.parent).rglob("*"):
            if not path.is_file():
                continue
            with self.subTest(path=path):
                content = path.read_text(encoding="utf-8", errors="strict")
                self.assertFalse(any(token in content for token in forbidden))


class FixtureWorkspaceTests(unittest.TestCase):
    """Generated workflow artifacts must exist only in disposable workspaces."""

    def test_lifecycle_fixtures_materialize_and_cleanup(self) -> None:
        for name in fixture_catalog()["projects"]:
            with self.subTest(fixture=name):
                manifest = load_fixture_manifest(PROJECTS_ROOT, name, "project")
                with materialized_fixture(name) as workspace:
                    temporary_path = workspace
                    for relative in manifest["expected_paths"]:
                        self.assertTrue((workspace / relative).exists(), relative)
                    if name == "empty-project":
                        self.assertEqual(list(workspace.iterdir()), [])
                self.assertFalse(temporary_path.exists())

    def test_engine_overlays_compose_without_changing_sources(self) -> None:
        for engine in fixture_catalog()["engine_overlays"]:
            with self.subTest(engine=engine):
                overlay_source = OVERLAYS_ROOT / engine
                before = tree_digest(overlay_source)
                manifest = load_fixture_manifest(OVERLAYS_ROOT, engine, "engine-overlay")
                with materialized_fixture("minimal-project", engine) as workspace:
                    temporary_path = workspace
                    for relative in manifest["expected_paths"]:
                        self.assertTrue((workspace / relative).exists(), relative)
                    self.assertTrue((workspace / "ccgs-data").is_dir())
                self.assertFalse(temporary_path.exists())
                self.assertEqual(tree_digest(overlay_source), before)

    def test_generated_outputs_are_deleted_with_workspace(self) -> None:
        source = PROJECTS_ROOT / "mature-project"
        before = tree_digest(source)
        with materialized_fixture("mature-project") as workspace:
            generated = workspace / "ccgs-data" / "production" / "context" / "generated.json"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text('{"temporary": true}\n', encoding="utf-8")
            temporary_path = workspace
            self.assertTrue(generated.is_file())

        self.assertFalse(temporary_path.exists())
        self.assertEqual(tree_digest(source), before)
        self.assertFalse((source / "project" / "ccgs-data" / "production" / "context").exists())

    def test_fixture_name_rejects_path_traversal(self) -> None:
        with self.assertRaises(FixtureError):
            with materialized_fixture("../mature-project"):
                self.fail("invalid fixture unexpectedly materialized")

    def test_malformed_fixture_exposes_expected_doctor_error(self) -> None:
        with materialized_fixture("malformed-project") as workspace:
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "doctor",
                    "--project-root",
                    str(workspace),
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            report = json.loads(process.stdout)
            data_check = next(check for check in report["checks"] if check["key"] == "project.data")

            self.assertEqual(process.returncode, 1)
            self.assertEqual(data_check["status"], "error")
            self.assertIn("case mismatch", data_check["message"])


if __name__ == "__main__":
    unittest.main()
