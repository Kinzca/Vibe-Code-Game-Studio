"""Expose STORY-UWA-014 integration evidence to repository discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path


TARGET = Path(__file__).parent / "integration" / "universal-workflow-automation" / "upgrade_migration_preview_test.py"
SPEC = importlib.util.spec_from_file_location("story_uwa_014_upgrade_migration_preview", TARGET)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load upgrade integration tests from {TARGET}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

UpgradeMigrationPreviewTest = MODULE.UpgradeMigrationPreviewTest
