"""End-to-end tests for the project-local Codex Bridge bootstrap."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"
GOLDEN_ROOT = ROOT / "tests" / "golden" / "codex-bootstrap"
TARGETS = {
    "AGENTS.md": "AGENTS.golden.md",
    ".agents/skills/ccgs-context/SKILL.md": "ccgs-context-SKILL.md",
    ".agents/skills/ccgs-workflow/SKILL.md": "ccgs-workflow-SKILL.md",
}


def run_bootstrap(
    project: Path,
    mode: str,
    *,
    json_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the public bootstrap command against one disposable project."""

    arguments = [
        sys.executable,
        str(CLI_PATH),
        "bootstrap",
        "--project-root",
        str(project),
        "--codex",
        mode,
    ]
    if json_output:
        arguments.append("--json")
    return subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class CodexBridgeTests(unittest.TestCase):
    """Codex bootstrap must be deterministic, non-destructive, and idempotent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.golden_manifest = (GOLDEN_ROOT / "dry-run.json").read_text(
            encoding="utf-8"
        )
        cls.golden_files = {
            relative: (GOLDEN_ROOT / golden).read_text(encoding="utf-8")
            for relative, golden in TARGETS.items()
        }

    def test_dry_run_matches_manifest_and_is_read_only(self) -> None:
        with materialized_fixture("minimal-project") as project:
            before = tree_digest(project)
            process = run_bootstrap(project, "--dry-run")

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout, self.golden_manifest)
            self.assertEqual(process.stderr, "")
            self.assertEqual(tree_digest(project), before)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / ".agents").exists())

    def test_write_creates_exact_manifest_and_golden_files_atomically(self) -> None:
        with materialized_fixture("minimal-project") as project:
            process = run_bootstrap(project, "--write")
            manifest = json.loads(process.stdout)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(
                [item["path"] for item in manifest["files"]],
                list(TARGETS),
            )
            self.assertEqual(
                manifest["summary"],
                {"create": 3, "update": 0, "unchanged": 0},
            )
            for relative, expected in self.golden_files.items():
                actual = (project / relative).read_text(encoding="utf-8")
                self.assertEqual(actual, expected)
            self.assertEqual(
                (project / ".agents/skills/ccgs-context/SKILL.md")
                .read_text(encoding="utf-8")
                .splitlines()[0],
                "---",
            )
            self.assertEqual(list(project.rglob("*.tmp")), [])

    def test_second_write_is_idempotent_and_preserves_mtime(self) -> None:
        with materialized_fixture("minimal-project") as project:
            first = run_bootstrap(project, "--write")
            self.assertEqual(first.returncode, 0, first.stderr)
            before_digest = tree_digest(project)
            before_mtimes = {
                relative: (project / relative).stat().st_mtime_ns
                for relative in TARGETS
            }

            second = run_bootstrap(project, "--write")
            manifest = json.loads(second.stdout)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                manifest["summary"],
                {"create": 0, "update": 0, "unchanged": 3},
            )
            self.assertFalse(manifest["would_write"])
            self.assertEqual(tree_digest(project), before_digest)
            self.assertEqual(
                {
                    relative: (project / relative).stat().st_mtime_ns
                    for relative in TARGETS
                },
                before_mtimes,
            )

    def test_existing_agents_content_is_preserved_outside_managed_block(self) -> None:
        original = (
            "# Project Instructions\r\n\r\n"
            "Keep this consumer-owned instruction.\r\n"
        )
        with materialized_fixture("minimal-project") as project:
            agents = project / "AGENTS.md"
            agents.write_bytes(original.encode("utf-8"))
            first = run_bootstrap(project, "--write")
            content = agents.read_bytes().decode("utf-8")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(content.startswith(original))
            self.assertEqual(content.count("CCGS CODEX BRIDGE:BEGIN"), 1)
            self.assertEqual(content.count("CCGS CODEX BRIDGE:END"), 1)

            second = run_bootstrap(project, "--write")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                json.loads(second.stdout)["summary"],
                {"create": 0, "update": 0, "unchanged": 3},
            )

    def test_unmanaged_skill_collision_refuses_all_writes(self) -> None:
        relative = ".agents/skills/ccgs-context/SKILL.md"
        with materialized_fixture("minimal-project") as project:
            collision = project / relative
            collision.parent.mkdir(parents=True)
            collision.write_text(
                "# Consumer Skill\n\n"
                "<!-- CCGS CODEX BRIDGE:MANAGED -->\n",
                encoding="utf-8",
            )
            before = tree_digest(project)

            process = run_bootstrap(project, "--write")

            self.assertEqual(process.returncode, 2)
            self.assertIn("unmanaged Codex Skill", process.stderr)
            self.assertEqual(tree_digest(project), before)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse(
                (project / ".agents/skills/ccgs-workflow/SKILL.md").exists()
            )

    def test_malformed_agents_markers_are_rejected_without_writes(self) -> None:
        with materialized_fixture("minimal-project") as project:
            agents = project / "AGENTS.md"
            agents.write_text(
                "# Existing\n\n<!-- CCGS CODEX BRIDGE:BEGIN -->\n",
                encoding="utf-8",
            )
            before = tree_digest(project)
            process = run_bootstrap(project, "--dry-run")

            self.assertEqual(process.returncode, 2)
            self.assertIn("malformed CCGS managed markers", process.stderr)
            self.assertEqual(tree_digest(project), before)
            self.assertFalse((project / ".agents").exists())

    def test_human_dry_run_lists_every_planned_file(self) -> None:
        with materialized_fixture("minimal-project") as project:
            process = run_bootstrap(
                project,
                "--dry-run",
                json_output=False,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("CCGS Codex Bridge (dry-run)", process.stdout)
            for relative in TARGETS:
                self.assertIn(relative, process.stdout)
            self.assertIn("Summary: 3 create, 0 update, 0 unchanged", process.stdout)

    def test_cross_engine_manifests_and_outputs_are_identical(self) -> None:
        for engine in ("unity", "godot", "cocos"):
            with self.subTest(engine=engine):
                with materialized_fixture("minimal-project", engine) as project:
                    dry_run = run_bootstrap(project, "--dry-run")
                    self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
                    self.assertEqual(dry_run.stdout, self.golden_manifest)

                    write = run_bootstrap(project, "--write")
                    self.assertEqual(write.returncode, 0, write.stderr)
                    for relative, expected in self.golden_files.items():
                        self.assertEqual(
                            (project / relative).read_text(encoding="utf-8"),
                            expected,
                        )
                    self.assertEqual(list(project.rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
