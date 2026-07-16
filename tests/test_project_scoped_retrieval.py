"""Discovery bridge for STORY-UWA-010 integration evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SOURCE = Path(__file__).parent / "integration/universal-workflow-automation/project_scoped_retrieval_test.py"
SPEC = importlib.util.spec_from_file_location("project_scoped_retrieval_test", SOURCE)
if SPEC is None or SPEC.loader is None: raise RuntimeError("unable to load retrieval tests")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ProjectScopedRetrievalTest = MODULE.ProjectScopedRetrievalTest
