"""Expose STORY-UWA-011 integration evidence to test discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SOURCE = Path(__file__).parent / "integration/universal-workflow-automation/orchestrator_boundary_test.py"
SPEC = importlib.util.spec_from_file_location("orchestrator_boundary_test", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load orchestration boundary tests from {SOURCE}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
OrchestratorBoundaryTest = MODULE.OrchestratorBoundaryTest
