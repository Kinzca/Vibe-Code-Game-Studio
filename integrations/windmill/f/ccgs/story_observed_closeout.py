"""Windmill entrypoint for the observed CCGS Story Closeout loop."""

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
        run_observed_story_closeout,
    )
    return run_observed_story_closeout, raise_for_windmill, WindmillAdapterError


def main(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str,
    project_id: str,
    event_id: str,
    trace_key: str,
    session_id: str,
    environment: str = "automation",
    query: str = "",
    apply: bool = True,
    qdrant_url: str = "http://127.0.0.1:6333",
    qdrant_collection: str = "ccgs-context",
    qdrant_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    qdrant_limit: int = 8,
    langfuse_host: str = "https://cloud.langfuse.com",
    langfuse_send: bool = True,
    allow_insecure_http: bool = False,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
) -> dict:
    run, raise_for_windmill, adapter_error = _adapter(framework_root)
    try:
        result = run(
            framework_root,
            project_root,
            story,
            evidence,
            project_id,
            event_id,
            trace_key,
            session_id,
            environment,
            query,
            apply,
            qdrant_url,
            qdrant_collection,
            qdrant_embedding_model,
            qdrant_limit,
            langfuse_host,
            langfuse_send,
            allow_insecure_http,
            max_attempts,
            retry_delay_seconds,
            timeout_seconds,
        )
    except adapter_error as exc:
        raise RuntimeError(f"[CCGS_PERMANENT]{exc}") from exc
    return raise_for_windmill(result)
