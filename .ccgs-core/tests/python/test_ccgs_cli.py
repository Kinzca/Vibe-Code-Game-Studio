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


def initialize_git_boundary(root: Path) -> None:
    """Create a real Git repository boundary."""

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def initialize_framework(root: Path, *, git_boundary: bool = False) -> None:
    """Create the required framework markers for boundary classification."""

    cli = root / ".ccgs-core/scripts/ccgs_cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("fixture\n", encoding="utf-8")
    (root / "ccgs.workflow.yaml").write_text("fixture\n", encoding="utf-8")
    if git_boundary:
        initialize_git_boundary(root)


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

    def test_policy_outputs_only_public_project_relative_locations(self) -> None:
        allowed_target = self.project / "ccgs-data/production/result.json"
        allowed = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "policy",
                "--project-root",
                str(self.project),
                "--target",
                str(allowed_target),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        allowed_report = json.loads(allowed.stdout)

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed_report["project_root"], ".")
        self.assertEqual(
            allowed_report["target"],
            "ccgs-data/production/result.json",
        )
        self.assertNotIn(str(self.project), allowed.stdout)

        external_target = self.project.parent / "outside.txt"
        denied = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "policy",
                "--project-root",
                str(self.project),
                "--target",
                str(external_target),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        denied_report = json.loads(denied.stdout)

        self.assertEqual(denied.returncode, 1)
        self.assertEqual(denied_report["project_root"], ".")
        self.assertEqual(denied_report["target"], "<external>")
        self.assertNotIn(str(self.project.parent), denied.stdout + denied.stderr)

        human = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "policy",
                "--project-root",
                str(self.project),
                "--target",
                str(external_target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(human.returncode, 1)
        self.assertIn("DENY: <external>", human.stdout)
        self.assertNotIn(str(self.project.parent), human.stdout + human.stderr)


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
        initialize_framework(framework)
        self.assertEqual(CCGS_CLI.repository_mode(framework, framework), "standalone")

    def test_embedded_submodule_layout(self) -> None:
        project = self.root / "consumer"
        framework = project / ".ccgs-upstream"
        framework.mkdir(parents=True)
        initialize_git_boundary(project)
        initialize_framework(framework, git_boundary=True)
        self.assertEqual(
            CCGS_CLI.repository_mode(project, framework),
            "embedded-submodule",
        )

    def test_external_layout(self) -> None:
        project = self.root / "consumer"
        framework = self.root / "framework"
        project.mkdir()
        framework.mkdir()
        initialize_framework(framework)
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
            self.assertEqual(report["roots"]["project"]["location"], ".")
            self.assertEqual(report["roots"]["framework"]["location"], "<external>")
            self.assertNotIn(str(project), process.stdout)
            self.assertEqual(before, after)

    def test_missing_project_returns_stable_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "missing"
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "doctor",
                    "--project-root",
                    str(project),
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            report = json.loads(process.stdout)

            self.assertEqual(process.returncode, 1)
            self.assertEqual(report["error"]["code"], "PROJECT_ROOT_NOT_FOUND")
            self.assertEqual(report["error"]["location"], ".")
            self.assertFalse(report["validation"]["valid"])
            self.assertNotIn(temp_dir, process.stdout)

    def test_case_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "CCGS-Data").mkdir()
            report = CCGS_CLI.build_doctor_report(project)
            data_check = next(check for check in report["checks"] if check["key"] == "project.data")
            self.assertEqual(data_check["status"], "error")


if __name__ == "__main__":
    unittest.main()
