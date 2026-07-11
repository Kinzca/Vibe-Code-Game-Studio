"""Tests for the repository-safe CCGS command entrypoint."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ccgs_cli.py"
SPEC = importlib.util.spec_from_file_location("ccgs_cli", CLI_PATH)
assert SPEC and SPEC.loader
CCGS_CLI = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CCGS_CLI
SPEC.loader.exec_module(CCGS_CLI)


class WritePolicyTests(unittest.TestCase):
    """The CLI must never authorize writes to consumer runtime code."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_allows_ccgs_owned_paths(self) -> None:
        allowed = [
            Path("ccgs-data/production/context/index.json"),
            Path(".agents/skills/ccgs/SKILL.md"),
            Path("AGENTS.md"),
        ]
        for target in allowed:
            with self.subTest(target=target):
                result = CCGS_CLI.validate_write_target(self.project, target, "ccgs-data")
                self.assertTrue(result.is_relative_to(self.project.resolve()))

    def test_denies_non_ccgs_paths_across_engines(self) -> None:
        denied = [
            # Unity
            Path("Client/Assets/Game.cs"),
            Path("Assets/Scripts/Game.cs"),
            # Godot
            Path("project.godot"),
            Path("addons/ccgs_test/plugin.gd"),
            Path("scenes/main.tscn"),
            # Cocos Creator
            Path("assets/scripts/main.ts"),
            Path("settings/v2/packages/project.json"),
            # Generic runtime and repository files
            Path("src/main.py"),
            Path("Server/config.json"),
            Path("README.md"),
            self.project.parent / "outside.txt",
        ]
        for target in denied:
            with self.subTest(target=target):
                with self.assertRaises(CCGS_CLI.PolicyError):
                    CCGS_CLI.validate_write_target(self.project, target, "ccgs-data")


class RepositoryModeTests(unittest.TestCase):
    """Repository topology labels must remain stable for automation callers."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_standalone_layout(self) -> None:
        framework = self.root / "standalone"
        framework.mkdir()
        self.assertEqual(CCGS_CLI.repository_mode(framework, framework), "standalone")

    def test_embedded_submodule_layout(self) -> None:
        project = self.root / "consumer"
        framework = project / ".ccgs-upstream"
        framework.mkdir(parents=True)
        self.assertEqual(
            CCGS_CLI.repository_mode(project, framework),
            "embedded-submodule",
        )

    def test_external_layout(self) -> None:
        project = self.root / "consumer"
        framework = self.root / "framework"
        project.mkdir()
        framework.mkdir()
        self.assertEqual(CCGS_CLI.repository_mode(project, framework), "external")


class DoctorTests(unittest.TestCase):
    """Doctor output must be structured and read-only."""

    def test_json_report_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "ccgs-data").mkdir()
            before = sorted(path.relative_to(project) for path in project.rglob("*"))
            process = subprocess.run(
                [sys.executable, str(CLI_PATH), "doctor", "--project-root", str(project), "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            after = sorted(path.relative_to(project) for path in project.rglob("*"))
            report = json.loads(process.stdout)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report["read_only"])
            self.assertTrue(report["engine_agnostic"])
            self.assertEqual(report["write_policy"], "allowlist")
            self.assertEqual(report["data_dir"], "ccgs-data")
            self.assertEqual(before, after)

    def test_case_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "CCGS-Data").mkdir()
            report = CCGS_CLI.build_doctor_report(project)
            data_check = next(check for check in report["checks"] if check["key"] == "project.data")
            self.assertEqual(data_check["status"], "error")


if __name__ == "__main__":
    unittest.main()
