"""Integration coverage for repository roots and equivalent dry-run planning."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ccgs_codex_bridge import build_codex_plan
from vibe_repository_boundary import (
    RepositoryBoundaryError,
    resolve_repository_boundary,
)


CLI_PATH = SCRIPT_DIR / "ccgs_cli.py"
FRAMEWORK_MARKERS = (
    Path(".ccgs-core/scripts/ccgs_cli.py"),
    Path("ccgs.workflow.yaml"),
)


def make_framework(path: Path, *, git_boundary: bool = True) -> Path:
    """Create one minimal, engine-neutral framework-root fixture."""

    for marker in FRAMEWORK_MARKERS:
        target = path / marker
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    if git_boundary:
        initialize_git_repository(path)
    return path


def initialize_git_repository(path: Path) -> None:
    """Create a real Git repository boundary for integration validation."""

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def make_doctor_framework(path: Path) -> Path:
    """Create every framework artifact required by the public Doctor command."""

    make_framework(path)
    required_files = (
        ".ccgs-core/ccgs.env",
        "README.md",
        ".ccgs-core/scripts/workflow/ccgs-context-router.py",
        "ccgs.deps.lock",
        "ccgs.cmd",
        "ccgs.ps1",
        "ccgs.sh",
    )
    for relative in required_files:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    return path


def run_bootstrap(project: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Run the public bootstrap command with JSON output."""

    return subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "bootstrap",
            "--project-root",
            str(project),
            "--codex",
            mode,
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def run_doctor(project: Path, framework: Path) -> subprocess.CompletedProcess[str]:
    """Run the same public Doctor command against a selected framework root."""

    environment = os.environ.copy()
    environment["CCGS_FRAMEWORK_ROOT"] = str(framework)
    return subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "doctor",
            "--project-root",
            str(project),
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def prepare_mixed_project(path: Path) -> None:
    """Create update, unchanged, and create cases for one bootstrap plan."""

    path.mkdir()
    (path / "AGENTS.md").write_text(
        "# Consumer Instructions\n\nKeep this line.\n",
        encoding="utf-8",
    )
    initial_plan = build_codex_plan(ROOT, path, "ccgs-data")
    unchanged = next(
        item
        for item in initial_plan.files
        if item.path == ".agents/skills/ccgs-context/SKILL.md"
    )
    target = path / unchanged.path
    target.parent.mkdir(parents=True)
    target.write_text(unchanged.content, encoding="utf-8")


def tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """Capture project-relative content hashes-bytes and exact mtimes."""

    snapshot: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes().hex() if path.is_file() else "<directory>"
        snapshot[relative] = (content, path.stat().st_mtime_ns)
    return snapshot


class RepositoryBoundaryDryRunTest(unittest.TestCase):
    """The public contract must validate roots before producing write plans."""

    def test_resolves_standalone_embedded_and_external_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)

            standalone = make_framework(fixture_root / "standalone")
            standalone_result = resolve_repository_boundary(standalone, standalone)
            self.assertEqual(standalone_result.repository_mode, "standalone")
            self.assertEqual(standalone_result.project.location, ".")
            self.assertEqual(standalone_result.framework.location, ".")

            embedded_project = fixture_root / "embedded-project"
            embedded_project.mkdir()
            initialize_git_repository(embedded_project)
            embedded_framework = make_framework(embedded_project / "framework-kit")
            embedded_result = resolve_repository_boundary(
                embedded_project, embedded_framework
            )
            self.assertEqual(
                embedded_result.repository_mode,
                "embedded-submodule",
            )
            self.assertEqual(embedded_result.project.location, ".")
            self.assertEqual(embedded_result.framework.location, "framework-kit")

            external_project = fixture_root / "external-project"
            external_project.mkdir()
            external_framework = make_framework(fixture_root / "external-framework")
            external_result = resolve_repository_boundary(
                external_project, external_framework
            )
            self.assertEqual(external_result.repository_mode, "external")
            self.assertEqual(external_result.project.location, ".")
            self.assertEqual(external_result.framework.location, "<external>")

            for result in (
                standalone_result,
                embedded_result,
                external_result,
            ):
                payload = json.dumps(result.public_result(), sort_keys=True)
                self.assertNotIn(str(fixture_root), payload)
                self.assertEqual(result.contract_version, "1.0")

    def test_relative_project_root_is_valid_from_embedded_framework_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            project = fixture_root / "project"
            project.mkdir()
            initialize_git_repository(project)
            framework = make_framework(project / "framework-kit")

            previous_cwd = Path.cwd()
            try:
                os.chdir(framework)
                result = resolve_repository_boundary(Path(".."), framework)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result.repository_mode, "embedded-submodule")
            self.assertEqual(result.project.location, ".")
            self.assertEqual(result.framework.location, "framework-kit")
            payload = json.dumps(result.public_result(), sort_keys=True)
            self.assertNotIn(str(fixture_root), payload)

    def test_invalid_roots_return_stable_sanitized_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            valid_framework = make_framework(fixture_root / "valid-framework")
            project = fixture_root / "project"
            project.mkdir()

            cases = (
                (
                    project / "missing",
                    valid_framework,
                    "PROJECT_ROOT_NOT_FOUND",
                ),
                (
                    project,
                    fixture_root / "invalid-framework",
                    "FRAMEWORK_ROOT_INVALID",
                ),
                (
                    valid_framework / "nested-project",
                    valid_framework,
                    "ROOT_BOUNDARY_INVALID",
                ),
            )
            (valid_framework / "nested-project").mkdir()

            embedded_project = fixture_root / "embedded-without-boundary"
            embedded_project.mkdir()
            embedded_framework = make_framework(
                embedded_project / "framework-kit",
                git_boundary=False,
            )
            cases += (
                (
                    embedded_project,
                    embedded_framework,
                    "ROOT_BOUNDARY_INVALID",
                ),
            )

            missing_project_boundary = fixture_root / "missing-project-boundary"
            missing_project_boundary.mkdir()
            framework_with_boundary = make_framework(
                missing_project_boundary / "framework-kit"
            )
            cases += (
                (
                    missing_project_boundary,
                    framework_with_boundary,
                    "ROOT_BOUNDARY_INVALID",
                ),
            )

            shared_boundary_project = fixture_root / "shared-boundary"
            shared_boundary_project.mkdir()
            initialize_git_repository(shared_boundary_project)
            shared_boundary_framework = make_framework(
                shared_boundary_project / "framework-kit",
                git_boundary=False,
            )
            (shared_boundary_framework / ".git").write_text(
                "gitdir: ../.git\n",
                encoding="utf-8",
            )
            cases += (
                (
                    shared_boundary_project,
                    shared_boundary_framework,
                    "ROOT_BOUNDARY_INVALID",
                ),
            )

            fake_boundary_project = fixture_root / "fake-boundary-project"
            initialize_git_repository(fake_boundary_project)
            fake_boundary_framework = make_framework(
                fake_boundary_project / "framework-kit",
                git_boundary=False,
            )
            fake_git = fake_boundary_framework / ".git"
            fake_git.mkdir()
            (fake_git / "HEAD").write_text(
                "ref: refs/heads/fixture\n",
                encoding="utf-8",
            )
            cases += (
                (
                    fake_boundary_project,
                    fake_boundary_framework,
                    "ROOT_BOUNDARY_INVALID",
                ),
            )

            for project_root, framework_root, expected_code in cases:
                with self.subTest(code=expected_code, project=project_root.name):
                    with self.assertRaises(RepositoryBoundaryError) as caught:
                        resolve_repository_boundary(project_root, framework_root)
                    report = caught.exception.report("diagnostic")
                    serialized = json.dumps(report, sort_keys=True)
                    self.assertEqual(report["error"]["code"], expected_code)
                    self.assertEqual(report["repository_mode"], "invalid")
                    self.assertEqual(report["roots"]["project"]["location"], ".")
                    self.assertIn(
                        report["roots"]["framework"]["location"],
                        {".", "framework-kit", "<external>"},
                    )
                    self.assertNotIn(str(fixture_root), serialized)
                    self.assertFalse(report["validation"]["valid"])

    def test_framework_symlink_cannot_escape_requested_project_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            project = fixture_root / "project"
            project.mkdir()
            initialize_git_repository(project)
            external_framework = make_framework(fixture_root / "framework")
            framework_link = project / "framework-link"
            framework_link.symlink_to(external_framework, target_is_directory=True)

            with self.assertRaises(RepositoryBoundaryError) as caught:
                resolve_repository_boundary(project, framework_link)

            report = caught.exception.report("diagnostic")
            serialized = json.dumps(report, sort_keys=True)
            self.assertEqual(
                report["error"]["code"],
                "ROOT_BOUNDARY_INVALID",
            )
            self.assertEqual(report["error"]["location"], "framework-link")
            self.assertNotIn(str(fixture_root), serialized)

            relative_escape = project / "nested" / ".." / ".." / "framework"
            with self.assertRaises(RepositoryBoundaryError) as relative_caught:
                resolve_repository_boundary(project, relative_escape)
            relative_report = relative_caught.exception.report("diagnostic")
            self.assertEqual(
                relative_report["error"]["code"],
                "ROOT_BOUNDARY_INVALID",
            )
            self.assertEqual(
                relative_report["error"]["location"],
                "nested/../../framework",
            )
            self.assertNotIn(
                str(fixture_root),
                json.dumps(relative_report, sort_keys=True),
            )

    def test_relative_framework_paths_cannot_escape_requested_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            project = fixture_root / "project"
            project.mkdir()
            initialize_git_repository(project)
            external_framework = make_framework(fixture_root / "framework")
            relative_link = project / "framework-link"
            relative_link.symlink_to(external_framework, target_is_directory=True)

            previous_cwd = Path.cwd()
            try:
                os.chdir(fixture_root)
                relative_cases = (
                    Path("project/nested/../../framework"),
                    Path("project/framework-link"),
                )
                for framework_root in relative_cases:
                    with self.subTest(framework=framework_root):
                        with self.assertRaises(RepositoryBoundaryError) as caught:
                            resolve_repository_boundary(Path("project"), framework_root)
                        report = caught.exception.report("diagnostic")
                        self.assertEqual(
                            report["error"]["code"],
                            "ROOT_BOUNDARY_INVALID",
                        )
                        self.assertEqual(report["repository_mode"], "invalid")
                        self.assertNotIn(
                            str(fixture_root),
                            json.dumps(report, sort_keys=True),
                        )
            finally:
                os.chdir(previous_cwd)

    def test_public_doctor_command_supports_all_repository_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            standalone = make_doctor_framework(fixture_root / "standalone")

            embedded_project = fixture_root / "embedded-project"
            initialize_git_repository(embedded_project)
            embedded_framework = make_doctor_framework(
                embedded_project / "framework-kit"
            )

            external_project = fixture_root / "external-project"
            external_project.mkdir()
            external_framework = make_doctor_framework(
                fixture_root / "external-framework"
            )

            cases = (
                (standalone, standalone, "standalone", "."),
                (
                    embedded_project,
                    embedded_framework,
                    "embedded-submodule",
                    "framework-kit",
                ),
                (external_project, external_framework, "external", "<external>"),
            )
            for project, framework, expected_mode, expected_location in cases:
                with self.subTest(mode=expected_mode):
                    process = run_doctor(project, framework)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    report = json.loads(process.stdout)
                    self.assertEqual(report["repository_mode"], expected_mode)
                    self.assertEqual(report["root_contract_version"], "1.0")
                    self.assertEqual(report["roots"]["project"]["location"], ".")
                    self.assertEqual(
                        report["roots"]["framework"]["location"],
                        expected_location,
                    )
                    self.assertNotIn(
                        str(fixture_root),
                        process.stdout + process.stderr,
                    )

    def test_dry_run_and_write_share_validation_and_normalized_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            dry_project = fixture_root / "dry-project"
            write_project = fixture_root / "write-project"
            prepare_mixed_project(dry_project)
            prepare_mixed_project(write_project)

            before = tree_snapshot(dry_project)
            temporary_files_before = sorted(
                path.relative_to(dry_project).as_posix()
                for path in dry_project.rglob("*.tmp")
            )
            dry_process = run_bootstrap(dry_project, "--dry-run")
            write_process = run_bootstrap(write_project, "--write")

            self.assertEqual(dry_process.returncode, 0, dry_process.stderr)
            self.assertEqual(write_process.returncode, 0, write_process.stderr)
            dry_report = json.loads(dry_process.stdout)
            write_report = json.loads(write_process.stdout)

            self.assertEqual(dry_report["validation"], write_report["validation"])
            self.assertEqual(
                dry_report["planned_writes"],
                write_report["planned_writes"],
            )
            self.assertEqual(
                [item["action"] for item in dry_report["planned_writes"]],
                ["update", "unchanged", "create"],
            )
            self.assertEqual(dry_report["mode"], "dry-run")
            self.assertFalse(dry_report["written"])
            self.assertEqual(write_report["mode"], "write")
            self.assertTrue(write_report["written"])
            self.assertEqual(tree_snapshot(dry_project), before)
            self.assertEqual(
                sorted(
                    path.relative_to(dry_project).as_posix()
                    for path in dry_project.rglob("*.tmp")
                ),
                temporary_files_before,
            )

            serialized = dry_process.stdout + write_process.stdout
            self.assertNotIn(str(fixture_root), serialized)
            self.assertEqual(list(write_project.rglob("*.tmp")), [])

    def test_invalid_write_request_has_equivalent_read_only_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            outside = fixture_root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            dry_project = fixture_root / "dry-invalid"
            write_project = fixture_root / "write-invalid"

            for project in (dry_project, write_project):
                project.mkdir()
                collision = project / ".agents/skills/ccgs-context/SKILL.md"
                collision.parent.mkdir(parents=True)
                collision.symlink_to(outside)

            dry_before = tree_snapshot(dry_project)
            write_before = tree_snapshot(write_project)
            dry_process = run_bootstrap(dry_project, "--dry-run")
            write_process = run_bootstrap(write_project, "--write")
            dry_report = json.loads(dry_process.stdout)
            write_report = json.loads(write_process.stdout)

            self.assertEqual(dry_process.returncode, 2)
            self.assertEqual(write_process.returncode, 2)
            self.assertEqual(dry_report["validation"], write_report["validation"])
            self.assertEqual(
                dry_report["planned_writes"],
                write_report["planned_writes"],
            )
            self.assertEqual(dry_report["error"], write_report["error"])
            self.assertEqual(
                dry_report["error"]["code"],
                "WRITE_POLICY_DENIED",
            )
            self.assertEqual(
                dry_report["error"]["location"],
                ".agents/skills/ccgs-context/SKILL.md",
            )
            self.assertFalse(dry_report["written"])
            self.assertFalse(write_report["written"])
            self.assertEqual(tree_snapshot(dry_project), dry_before)
            self.assertEqual(tree_snapshot(write_project), write_before)
            self.assertEqual(list(dry_project.rglob("*.tmp")), [])
            self.assertEqual(list(write_project.rglob("*.tmp")), [])
            self.assertNotIn(
                str(fixture_root),
                dry_process.stdout + dry_process.stderr,
            )
            self.assertNotIn(
                str(fixture_root),
                write_process.stdout + write_process.stderr,
            )


if __name__ == "__main__":
    unittest.main()
