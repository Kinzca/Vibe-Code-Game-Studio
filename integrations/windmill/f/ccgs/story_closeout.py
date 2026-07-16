"""Windmill entrypoint for Orchestration Port ``story_closeout``."""

from __future__ import annotations

import sys
from pathlib import Path


def _port(framework_root: str):
    adapter_dir = Path(framework_root).expanduser().resolve() / "integrations" / "windmill"
    core_dir = Path(framework_root).expanduser().resolve() / ".ccgs-core" / "scripts"
    if not (adapter_dir / "ccgs_windmill_port.py").is_file() or not core_dir.is_dir():
        raise RuntimeError("[CCGS_PERMANENT] Windmill orchestration port is unavailable")
    for path in (str(core_dir), str(adapter_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from ccgs_windmill_port import (
        build_windmill_orchestration_adapter,
        raise_port_error_for_windmill,
        stable_request_id,
        windmill_capability_document,
    )
    from vibe_orchestration import orchestration_request_envelope, invoke_orchestration
    return (
        orchestration_request_envelope,
        invoke_orchestration,
        build_windmill_orchestration_adapter,
        windmill_capability_document,
        raise_port_error_for_windmill,
        stable_request_id,
    )


def main(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str = "",
    data_dir: str = "ccgs-data",
    project_id: str = "windmill-project",
    request_id: str = "",
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
) -> dict:
    (
        build_request,
        invoke,
        build_adapter,
        capability_document,
        raise_for_windmill,
        stable_id,
    ) = _port(framework_root)
    identity = request_id or stable_id(project_id, "story_closeout", story, evidence)
    request = build_request(
        request_id=identity,
        project_id=project_id,
        action="story_closeout",
        story=story,
        evidence=evidence or None,
    )
    adapter = build_adapter(
        framework_root,
        project_root,
        data_dir=data_dir,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    return raise_for_windmill(invoke(
        request,
        capability_document(),
        adapter,
        data_dir=data_dir,
        timeout_seconds=timeout_seconds,
    ))
