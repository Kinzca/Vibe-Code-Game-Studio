"""End-to-end tests for the repository-safe Context Pack command."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from fixture_workspace import materialized_fixture, tree_digest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"
GOLDEN_PATH = ROOT / "tests" / "golden" / "context-packs" / "story-001-context-pack.md"
STORY_PATH = "ccgs-data/production/epics/sample/story-001.md"
DEFAULT_OUTPUT = "ccgs-data/production/context/packs/story-001-context-pack.md"


def run_context_pack(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public CLI exactly as an external orchestrator would."""

    return subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "context-pack",
            "--project-root",
            str(project),
            "--story",
            STORY_PATH,
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class ContextPackTests(unittest.TestCase):
    """Context Packs must stay bounded, deterministic, and repository-safe."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.golden = GOLDEN_PATH.read_text(encoding="utf-8")

    def test_preview_matches_golden_and_is_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = run_context_pack(project)
            after = tree_digest(project)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout, self.golden)
            self.assertEqual(process.stderr, "")
            self.assertEqual(after, before)
            self.assertFalse((project / DEFAULT_OUTPUT).exists())

    def test_preview_is_engine_agnostic(self) -> None:
        for engine in ("unity", "godot", "cocos"):
            with self.subTest(engine=engine):
                with materialized_fixture("mature-project", engine) as project:
                    process = run_context_pack(project)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(process.stdout, self.golden)

    def test_dry_run_validates_custom_output_without_writing(self) -> None:
        output = "ccgs-data/production/context/custom/story-001.md"
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            process = run_context_pack(project, "--dry-run", "--output", output)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout, self.golden)
            self.assertEqual(tree_digest(project), before)
            self.assertFalse((project / output).exists())

    def test_write_persists_exact_golden_atomically(self) -> None:
        with materialized_fixture("mature-project") as project:
            process = run_context_pack(project, "--write")
            output = project / DEFAULT_OUTPUT

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), DEFAULT_OUTPUT)
            self.assertEqual(output.read_text(encoding="utf-8"), self.golden)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_write_rejects_output_outside_context_directory(self) -> None:
        output = "ccgs-data/design/unsafe-context-pack.md"
        with materialized_fixture("mature-project") as project:
            process = run_context_pack(project, "--write", "--output", output)

            self.assertEqual(process.returncode, 2)
            self.assertIn("production/context", process.stderr)
            self.assertFalse((project / output).exists())

    def test_missing_explicit_reference_refuses_write(self) -> None:
        with materialized_fixture("mature-project") as project:
            story = project / STORY_PATH
            text = story.read_text(encoding="utf-8")
            story.write_text(
                text.replace("design/gdd/core-loop.md", "design/gdd/missing.md"),
                encoding="utf-8",
            )
            process = run_context_pack(project, "--write")

            self.assertEqual(process.returncode, 1)
            self.assertIn("explicit references are missing", process.stderr)
            self.assertFalse((project / DEFAULT_OUTPUT).exists())

    def test_limits_truncate_and_omit_sources_deterministically(self) -> None:
        with materialized_fixture("mature-project") as project:
            process = run_context_pack(
                project,
                "--max-files",
                "8",
                "--max-chars-per-file",
                "200",
                "--max-total-chars",
                "500",
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("| story |", process.stdout)
            self.assertIn("| 200 | 343 | yes |", process.stdout)
            self.assertIn("[truncated by Context Pack limits]", process.stdout)
            self.assertIn(
                "ccgs-data/production/qa/evidence/story-001.md",
                process.stdout,
            )

    def test_legacy_body_references_and_mixed_case_keys_are_resolved(self) -> None:
        with materialized_fixture("mature-project") as project:
            story = project / STORY_PATH
            story.write_text(
                """---
ID: STORY-001
Name: Legacy Fixture Story
Status: ready
---

# References

- CCGS-Data/design/gdd/core-loop.md
- ADR-0001
""",
                encoding="utf-8",
            )
            process = run_context_pack(project)

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("Title: Legacy Fixture Story", process.stdout)
            self.assertIn("| gdd | ccgs-data/design/gdd/core-loop.md |", process.stdout)
            self.assertIn(
                "| adr | ccgs-data/project-docs/architecture/"
                "ADR-0001-deterministic-loop.md |",
                process.stdout,
            )
            self.assertIn("- None.", process.stdout)

    def test_story_outside_epic_tree_is_rejected(self) -> None:
        with materialized_fixture("mature-project") as project:
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "context-pack",
                    "--project-root",
                    str(project),
                    "--story",
                    "ccgs-data/design/gdd/core-loop.md",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(process.returncode, 2)
            self.assertIn("production/epics", process.stderr)


if __name__ == "__main__":
    unittest.main()
