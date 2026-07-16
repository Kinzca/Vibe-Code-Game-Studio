"""Expose the Story-mandated DAG compiler tests to the repository test runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = Path(__file__).parent / "unit" / "universal-workflow-automation" / "dag_plan_compiler_test.py"
SPEC = importlib.util.spec_from_file_location("story_uwa_003_dag_plan_compiler", TARGET)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load DAG plan compiler tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

DagPlanCompilerTest = MODULE.DagPlanCompilerTest
