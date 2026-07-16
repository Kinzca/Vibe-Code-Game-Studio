"""Run the repository-fast Batch 1 and Batch 2 Python test suites."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Discover both public fixture tests and internal CLI contract tests."""

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.discover(str(ROOT / "tests"), pattern="test_*.py"))
    suite.addTests(
        loader.discover(
            str(ROOT / ".ccgs-core" / "tests" / "python"),
            pattern="test_*.py",
        )
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
