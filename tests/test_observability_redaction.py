"""Expose STORY-UWA-012 integration evidence to unittest discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SOURCE = (
    Path(__file__).parent
    / "integration"
    / "universal-workflow-automation"
    / "observability_redaction_test.py"
)
SPEC = importlib.util.spec_from_file_location("observability_redaction_test", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load observability redaction tests")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ObservabilityRedactionTest = MODULE.ObservabilityRedactionTest
