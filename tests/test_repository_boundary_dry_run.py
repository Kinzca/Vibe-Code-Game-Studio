"""Expose Story-mandated repository-boundary tests to the full test runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = (
    Path(__file__).parent
    / "integration"
    / "universal-workflow-automation"
    / "repository_boundary_dry_run_test.py"
)
SPEC = importlib.util.spec_from_file_location(
    "story_uwa_002_repository_boundary_dry_run",
    TARGET,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load repository-boundary tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

RepositoryBoundaryDryRunTest = MODULE.RepositoryBoundaryDryRunTest
