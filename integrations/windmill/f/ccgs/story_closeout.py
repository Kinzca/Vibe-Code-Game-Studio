"""Windmill entrypoint for CCGS Closeout automation."""

from __future__ import annotations

import sys
from pathlib import Path


def _adapter(framework_root: str):
    adapter_dir = Path(framework_root).expanduser().resolve() / "integrations" / "windmill"
    adapter_file = adapter_dir / "ccgs_windmill_adapter.py"
    if not adapter_file.is_file():
        raise RuntimeError("[CCGS_PERMANENT] Windmill adapter is missing from framework_root")
    sys.path.insert(0, str(adapter_dir))
    from ccgs_windmill_adapter import (
        WindmillAdapterError,
        raise_for_windmill,
        run_story_closeout,
    )
    return run_story_closeout, raise_for_windmill, WindmillAdapterError


def main(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str = "",
    apply: bool = True,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
) -> dict:
    run_story_closeout, raise_for_windmill, adapter_error = _adapter(framework_root)
    try:
        result = run_story_closeout(
            framework_root,
            project_root,
            story,
            evidence,
            apply,
            max_attempts,
            retry_delay_seconds,
            timeout_seconds,
        )
    except adapter_error as exc:
        raise RuntimeError(f"[CCGS_PERMANENT]{exc}") from exc
    return raise_for_windmill(result)
