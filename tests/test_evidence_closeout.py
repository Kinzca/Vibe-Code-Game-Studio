"""Expose STORY-UWA-007 integration evidence to repository discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = (
    Path(__file__).parent
    / "integration"
    / "universal-workflow-automation"
    / "evidence_closeout_test.py"
)
SPEC = importlib.util.spec_from_file_location(
    "story_uwa_007_evidence_closeout",
    TARGET,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load Evidence Closeout tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

EvidenceCloseoutTest = MODULE.EvidenceCloseoutTest
