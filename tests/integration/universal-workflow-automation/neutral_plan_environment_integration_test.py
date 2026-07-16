"""Filesystem integration evidence for STORY-UWA-004 AC-1."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_project_manifest import load_manifest
from vibe_workflow_plan import compile_plan


def canonical_bytes(value: object) -> bytes:
    """Serialize one document or result using the public canonical comparison form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def snapshot(path: Path) -> tuple[bytes, int]:
    """Capture exact file content and modification time."""

    return path.read_bytes(), path.stat().st_mtime_ns


class NeutralPlanEnvironmentIntegrationTest(unittest.TestCase):
    """Verify that project roots and unrelated files remain ambient inputs."""

    def write_manifest(self, root: Path, document: dict[str, object]) -> Path:
        target = root / "vibe-workflow.json"
        target.write_bytes(canonical_bytes(document))
        return target

    def test_ac1_project_roots_and_unreferenced_files_are_read_only(self) -> None:
        document = {
            "schema_version": "1.0",
            "steps": [
                {"id": "prepare", "argv": ["neutral-runner", "prepare"]},
                {"id": "verify", "argv": ["neutral-runner", "verify"], "depends_on": ["prepare"]},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            alpha, beta = Path(temporary) / "consumer-alpha", Path(temporary) / "consumer-beta"
            alpha.mkdir()
            beta.mkdir()
            files = (
                self.write_manifest(alpha, document),
                self.write_manifest(beta, document),
                alpha / "unreferenced-alpha.txt",
                beta / "unreferenced-beta.txt",
            )
            files[2].write_text("alpha", encoding="utf-8")
            files[3].write_text("beta", encoding="utf-8")
            before = {path: snapshot(path) for path in files}

            alpha_plan = compile_plan(load_manifest(alpha, ROOT, for_execution=True))
            beta_plan = compile_plan(load_manifest(beta, ROOT, for_execution=True))

            self.assertEqual(canonical_bytes(alpha_plan), canonical_bytes(beta_plan))
            self.assertEqual(before, {path: snapshot(path) for path in files})


if __name__ == "__main__":
    unittest.main()
