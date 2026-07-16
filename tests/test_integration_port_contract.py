"""Expose integration port contract evidence to test discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent


def _load(name: str, target: Path):
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load integration port contract tests from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTEGRATION = _load(
    "integration_port_contract",
    ROOT
    / "integration"
    / "universal-workflow-automation"
    / "integration_port_contract_test.py",
)

IntegrationPortContractTest = INTEGRATION.IntegrationPortContractTest
