"""Build idempotent Langfuse events from bounded CCGS workflow artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ccgs_context_pack import ContextPackError, build_context_pack
from ccgs_story_workflow import StoryWorkflowError, load_evidence, load_story

from ccgs_langfuse_adapter import (
    build_langfuse_bundle,
    validate_event_document,
)

SCHEMA_VERSION = "1.0"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
STATUS_MAP = {"passed": "pass", "failed": "fail", "error": "error"}
MAX_RETRIEVAL_REFERENCES = 20


class WorkflowObserverError(ValueError):
    """Raised when an observation cannot be built inside the CCGS boundary."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise WorkflowObserverError(
            f"{label} must be 1-128 characters using identifier-safe characters"
        )
    return value


def _timestamp(value: str) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowObserverError("timestamp must use ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowObserverError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _relative_reference(value: str, label: str) -> str:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise WorkflowObserverError(f"{label} must be project-relative")
    return normalized


def event_relative_path(data_dir: str, event_id: str) -> str:
    event_id = _identifier(event_id, "event_id")
    return (
        Path(data_dir)
        / "production"
        / "observability"
        / "events"
        / f"{event_id}.json"
    ).as_posix()


def _coverage(document: dict[str, Any]) -> float:
    criteria = document.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or not criteria:
        return 0.0
    passed = sum(
        1
        for item in criteria
        if isinstance(item, dict) and item.get("status") == "pass"
    )
    return round(passed / len(criteria), 6)


def build_workflow_event(
    project: Path,
    data_dir: str,
    *,
    story_path: str,
    evidence_path: str,
    project_id: str,
    event_id: str,
    trace_key: str,
    session_id: str,
    environment: str,
    surface: str,
    operation: str,
    status: str,
    query: str,
    retrieval_references: Sequence[str] = (),
    failure_codes: Sequence[str] = (),
    timestamp: str = "",
    workflow_version: str = "0.8.1",
) -> dict[str, Any]:
    """Create one privacy-bounded event from CCGS-owned inputs."""

    project = project.resolve()
    project_id = _identifier(project_id, "project_id")
    event_id = _identifier(event_id, "event_id")
    trace_key = _identifier(trace_key, "trace_key")
    session_id = _identifier(session_id, "session_id")
    surface = _identifier(surface, "surface")
    operation = _identifier(operation, "operation")
    if status not in {"pass", "fail", "blocked", "error", "unknown"}:
        status = STATUS_MAP.get(status, "")
    if not status:
        raise WorkflowObserverError("status is not supported")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", environment):
        raise WorkflowObserverError("environment uses unsupported characters")
    if len(query) > 4000:
        raise WorkflowObserverError("query exceeds 4000 characters")

    try:
        _, story = load_story(project, story_path, data_dir)
        evidence_relative, evidence, evidence_errors = load_evidence(
            project, evidence_path, data_dir
        )
        pack = build_context_pack(project, story_path, data_dir)
    except (StoryWorkflowError, ContextPackError) as exc:
        raise WorkflowObserverError(str(exc)) from exc

    if len(retrieval_references) > MAX_RETRIEVAL_REFERENCES:
        raise WorkflowObserverError(
            f"retrieval references exceed {MAX_RETRIEVAL_REFERENCES} entries"
        )
    retrieval = [
        _relative_reference(item, "retrieval reference")
        for item in retrieval_references
    ]
    pack_references = [source.path for source in pack.sources]
    references = list(
        dict.fromkeys([*pack_references, evidence_relative, *retrieval])
    )[:50]
    failures = list(
        dict.fromkeys(_identifier(item, "failure code") for item in failure_codes)
    )
    if evidence_errors and not failures:
        failures = ["evidence.invalid"]
    decision = (
        "blocked"
        if status == "blocked"
        else "fail"
        if status in {"fail", "error"}
        else "pass"
    )
    summary = (
        "Story workflow completed with passing Closeout Evidence."
        if decision == "pass"
        else "Story workflow did not pass; inspect the bounded failure reasons."
    )
    observed_at = _timestamp(timestamp)
    context_manifest = hashlib.sha256(pack.markdown.encode("utf-8")).hexdigest()
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "trace_key": trace_key,
        "timestamp": observed_at,
        "end_timestamp": observed_at,
        "project_id": project_id,
        "operation": operation,
        "status": status,
        "environment": environment,
        "session_id": session_id,
        "story_id": story.story_id,
        "surface": surface,
        "workflow_version": workflow_version,
        "tags": ["windmill", "closeout", "qdrant" if retrieval else "context-pack"],
        "input": {
            "summary": f"Evaluate {story.story_id} with bounded context and Evidence.",
            "query": query or f"Is {story.story_id} ready to close?",
            "references": references,
            "context_manifest": context_manifest,
        },
        "output": {
            "summary": summary,
            "decision": decision,
            "failure_reasons": failures,
        },
        "metadata": {
            "engine": "agnostic",
            "context_source": "qdrant" if retrieval else "context-pack",
            "evidence_schema": str(evidence.get("schema_version", "unknown")),
            "retrieval_count": len(retrieval),
        },
        "scores": [
            {
                "name": "closeout_pass",
                "value": decision == "pass",
                "data_type": "BOOLEAN",
                "comment": "Derived from the CCGS Closeout verdict.",
            },
            {
                "name": "evidence_coverage",
                "value": _coverage(evidence),
                "data_type": "NUMERIC",
                "comment": "Passing acceptance criteria divided by total criteria.",
            },
        ],
    }
    relative = event_relative_path(data_dir, event_id)
    validate_event_document(document, relative)
    return document


def materialize_workflow_event(
    project: Path,
    data_dir: str,
    document: dict[str, Any],
    *,
    write: bool,
    atomic_write: Callable[[Path, str], bool],
) -> tuple[str, bool, dict[str, Any]]:
    """Write a new event once, or reuse the existing event for retries."""

    relative = event_relative_path(data_dir, str(document.get("event_id", "")))
    target = (project.resolve() / relative).resolve(strict=False)
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowObserverError(f"existing event is invalid JSON: {relative}") from exc
        event = validate_event_document(existing, relative)
        expected = {
            "event_id": document["event_id"],
            "trace_key": document["trace_key"],
            "project_id": document["project_id"],
            "operation": document["operation"],
            "story_id": document["story_id"],
        }
        actual = {key: getattr(event, key) for key in expected}
        if actual != expected:
            raise WorkflowObserverError(
                "existing event identity conflicts with the requested observation"
            )
        return relative, False, existing
    if not write:
        return relative, False, document
    rendered = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    atomic_write(target, rendered)
    return relative, True, document


def workflow_event_report(
    relative: str,
    document: dict[str, Any],
    *,
    mode: str,
    written: bool,
) -> dict[str, Any]:
    event = validate_event_document(document, relative)
    bundle = build_langfuse_bundle(event)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "workflow-observe",
        "mode": mode,
        "event": relative,
        "event_id": event.event_id,
        "trace_key": event.trace_key,
        "status": event.status,
        "written": written,
        "score_count": len(event.scores),
        "trace_id": bundle.trace_id,
        "span_id": bundle.span_id,
        "manifest_sha256": bundle.manifest_sha256,
    }
