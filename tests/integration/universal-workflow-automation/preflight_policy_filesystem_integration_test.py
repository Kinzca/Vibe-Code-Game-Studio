"""Real-filesystem acceptance evidence for STORY-UWA-005 preflight policy."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_workflow_preflight import (
    PATH_MESSAGE,
    PREFLIGHT_PATH_INVALID,
    PreflightError,
    preflight_plan,
)


def plan(**fields: object) -> dict[str, object]:
    """Build one valid compiled plan with a stable identity."""

    step: dict[str, object] = {"id": "prepare", "argv": ["neutral-runner"]}
    step.update(fields)
    return {"plan_id": "sha256:" + "a" * 64, "steps": [step]}


def create_fixture(base: Path, root_name: str, outside_name: str) -> Path:
    """Create one neutral project root and one external symlink target."""

    root = base / root_name
    outside = base / outside_name
    (root / "work").mkdir(parents=True)
    outside.mkdir()
    (root / "marker.txt").write_text("project fixture", encoding="utf-8")
    (outside / "sentinel.txt").write_text("outside fixture", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    return root


def snapshot_tree(base: Path) -> tuple[tuple[object, ...], ...]:
    """Capture fixture type, size, mtime and content without following links."""

    records = []
    for path in sorted((base, *base.rglob("*")), key=lambda item: str(item)):
        stat = path.lstat()
        if path.is_symlink():
            payload: object = os.readlink(path)
        elif path.is_file():
            payload = path.read_bytes()
        else:
            payload = None
        records.append(
            (
                path.relative_to(base).as_posix(),
                stat.st_mode,
                stat.st_size,
                stat.st_mtime_ns,
                payload,
            )
        )
    return tuple(records)


class PreflightFilesystemIntegrationTest(unittest.TestCase):
    """Verify the production path inspector against real filesystem semantics."""

    def test_ac1_two_roots_are_equivalent_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            roots = (
                create_fixture(base, "project-alpha", "outside-alpha"),
                create_fixture(base, "project-beta", "outside-beta"),
            )
            before = snapshot_tree(base)
            valid = plan(
                working_directory="work",
                artifacts=["future/output.bin"],
            )
            results = [preflight_plan(valid, root) for root in roots]
            self.assertEqual(results[0], results[1])

            reports = []
            for root in roots:
                with self.assertRaises(PreflightError) as caught:
                    preflight_plan(plan(artifacts=["linked/future.bin"]), root)
                reports.append(caught.exception.report())
            self.assertEqual(reports[0], reports[1])
            reason = reports[0]["error"]["details"]["reason"]
            self.assertEqual("SYMLINK_ESCAPE", reason)
            self.assertNotIn(str(base), json.dumps((results, reports)))
            self.assertEqual(before, snapshot_tree(base))

    def test_ac1_symlink_loop_is_stable_sanitized_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project-loop"
            root.mkdir()
            loop = root / "loop"
            loop.symlink_to(loop)
            before = snapshot_tree(base)

            with self.assertRaises(PreflightError) as caught:
                preflight_plan(plan(working_directory="loop"), root)

            expected = {
                "contract_version": "1.0",
                "ok": False,
                "error": {
                    "code": PREFLIGHT_PATH_INVALID,
                    "message": PATH_MESSAGE,
                    "details": {
                        "step_id": "prepare",
                        "field": "working_directory",
                        "reason": "RESOLUTION_FAILED",
                    },
                },
            }
            self.assertEqual(expected, caught.exception.report())
            self.assertNotIn(str(base), json.dumps(caught.exception.report()))
            self.assertEqual(before, snapshot_tree(base))


if __name__ == "__main__":
    unittest.main()
