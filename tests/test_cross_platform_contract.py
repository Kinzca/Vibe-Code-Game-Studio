"""Expose STORY-UWA-015 integration evidence to repository discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = Path(__file__).parent / "integration" / "universal-workflow-automation" / "cross_platform_contract_test.py"
SPEC = importlib.util.spec_from_file_location("story_uwa_015_cross_platform_contract", TARGET)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load cross-platform integration tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CrossPlatformContractTest = MODULE.CrossPlatformContractTest
