"""Expose STORY-UWA-006 integration evidence to repository discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = (
    Path(__file__).parent
    / "integration"
    / "universal-workflow-automation"
    / "secure_execution_result_test.py"
)
SPEC = importlib.util.spec_from_file_location(
    "story_uwa_006_secure_execution_result",
    TARGET,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load secure execution tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SecureExecutionResultTest = MODULE.SecureExecutionResultTest
