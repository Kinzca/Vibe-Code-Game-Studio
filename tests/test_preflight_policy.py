"""Expose Story 005 unit evidence to the repository test runner."""

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
        raise RuntimeError(f"cannot load Story 005 tests from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


PreflightPolicyTest = load_test_class(
    "unit/universal-workflow-automation/preflight_policy_test.py",
    "story_uwa_005_preflight_policy",
    "PreflightPolicyTest",
)

PreflightFilesystemIntegrationTest = load_test_class(
    "integration/universal-workflow-automation/preflight_policy_filesystem_integration_test.py",
    "story_uwa_005_preflight_filesystem_integration",
    "PreflightFilesystemIntegrationTest",
)
