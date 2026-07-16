"""Expose STORY-UWA-013 integration evidence to unittest discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SOURCE = Path(__file__).parent / "integration" / "universal-workflow-automation" / "reporting_adapter_boundary_test.py"
SPEC = importlib.util.spec_from_file_location("reporting_adapter_boundary_test", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load reporting adapter boundary tests")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ReportingAdapterBoundaryTests = MODULE.ReportingAdapterBoundaryTests
