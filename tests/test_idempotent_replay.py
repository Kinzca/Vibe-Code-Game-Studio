"""Expose STORY-UWA-008 unit and integration evidence to test discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent


def _load(name: str, target: Path):
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load replay tests from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UNIT = _load(
    "story_uwa_008_idempotent_replay_unit",
    ROOT / "unit" / "universal-workflow-automation" / "idempotent_replay_test.py",
)
INTEGRATION = _load(
    "story_uwa_008_idempotent_replay_filesystem",
    ROOT
    / "integration"
    / "universal-workflow-automation"
    / "idempotent_replay_filesystem_test.py",
)

IdempotentReplayTest = UNIT.IdempotentReplayTest
IdempotentReplayFilesystemTest = INTEGRATION.IdempotentReplayFilesystemTest
