"""Expose Story 004 unit and integration evidence to the repository runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Type
from unittest import TestCase


def load_test_class(relative: str, module_name: str, class_name: str) -> Type[TestCase]:
    """Load one Story-mandated test class from its canonical evidence path."""

    target = Path(__file__).parent / relative
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Story 004 tests from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


NeutralPlanSemanticsTest = load_test_class(
    "unit/universal-workflow-automation/neutral_plan_semantics_test.py",
    "story_uwa_004_neutral_plan_semantics",
    "NeutralPlanSemanticsTest",
)
NeutralPlanEnvironmentIntegrationTest = load_test_class(
    "integration/universal-workflow-automation/neutral_plan_environment_integration_test.py",
    "story_uwa_004_neutral_environment",
    "NeutralPlanEnvironmentIntegrationTest",
)
