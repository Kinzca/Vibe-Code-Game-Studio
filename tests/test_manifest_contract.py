"""Expose the Story-mandated manifest tests to the repository test runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = Path(__file__).parent / "unit" / "universal-workflow-automation" / "manifest_contract_test.py"
SPEC = importlib.util.spec_from_file_location("story_uwa_001_manifest_contract", TARGET)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load manifest contract tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ManifestContractTest = MODULE.ManifestContractTest
