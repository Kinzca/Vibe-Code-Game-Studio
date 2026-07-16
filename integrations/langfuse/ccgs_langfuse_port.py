"""Langfuse adapter boundary for the neutral Observability Port 1.0."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Callable, Mapping, Sequence

from vibe_observability import (
    OBSERVABILITY_CAPABILITY,
    OBSERVABILITY_OPERATION,
    OBSERVABILITY_PORT,
    stable_observability_identity,
    validate_neutral_event,
)


def langfuse_capability_document() -> dict[str, Any]:
    """Return the static, versioned capability advertised by this adapter."""

    return {
        "contract_version": "1.0",
        "adapter_id": "vibe-langfuse-observability",
        "capabilities": [
            {
                "port": OBSERVABILITY_PORT,
                "operation": OBSERVABILITY_OPERATION,
                "capability": OBSERVABILITY_CAPABILITY,
                "contract_versions": ["1.0"],
            }
        ],
    }


def _metric_id(project_id: str, event_id: str, name: str) -> str:
    return hashlib.sha256(
        f"metric:{project_id}:{event_id}:{name}".encode("utf-8")
    ).hexdigest()


def build_langfuse_payload(event: Mapping[str, Any], event_ref: str) -> dict[str, Any]:
    """Map a validated neutral event without prompt, output, log, or source text."""

    neutral = validate_neutral_event(dict(event))
    trace_id, span_id = stable_observability_identity(neutral)
    attributes: dict[str, Any] = {
        "vibe.event_id": neutral["event_id"],
        "vibe.trace_key": neutral["trace_key"],
        "vibe.project_id": neutral["project_id"],
        "vibe.operation": neutral["operation"],
        "vibe.status": neutral["status"],
        "vibe.environment": neutral["environment"],
        "vibe.surface": neutral["surface"],
        "vibe.event_ref": event_ref,
        "vibe.context_manifest": neutral["context_manifest"],
        "vibe.references": tuple(neutral["references"]),
        "vibe.failure_codes": tuple(neutral["failure_codes"]),
    }
    for name in ("session_id", "story_id", "workflow_version", "tags"):
        if name in neutral:
            value = neutral[name]
            attributes[f"vibe.{name}"] = tuple(value) if type(value) is list else value
    metrics = [
        {
            "id": _metric_id(neutral["project_id"], neutral["event_id"], item["name"]),
            "trace_id": trace_id,
            "span_id": span_id,
            "name": item["name"],
            "value": item["value"],
            "data_type": item["data_type"],
        }
        for item in neutral["metrics"]
    ]
    return {
        "trace": {
            "trace_id": trace_id,
            "span_id": span_id,
            "name": neutral["operation"],
            "start_timestamp": neutral["timestamp"],
            "end_timestamp": neutral["end_timestamp"],
            "attributes": attributes,
        },
        "metrics": metrics,
    }


def _port_response(
    request: Mapping[str, Any], *, data: Mapping[str, Any] | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    ok = code is None
    return {
        "contract_version": "1.0",
        "request_id": request["request_id"],
        "project_id": request["project_id"],
        "port": OBSERVABILITY_PORT,
        "operation": OBSERVABILITY_OPERATION,
        "capability": OBSERVABILITY_CAPABILITY,
        "ok": ok,
        "status": "success" if ok else "degraded",
        "action": "invoke" if ok else "degraded",
        "called": True,
        "data": copy.deepcopy(dict(data or {})),
        "error": None if ok else {
            "code": code,
            "message": "Observability adapter did not complete",
            "retryable": code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"},
            "details": {},
        },
    }


def build_langfuse_observability_adapter(
    trace_exporter: Callable[[Mapping[str, Any], float], Any],
    metric_sender: Callable[[Sequence[Mapping[str, Any]], float], Any] | None = None,
) -> Callable[[dict[str, Any], float], dict[str, Any]]:
    """Build one injected adapter; no credentials or transport are loaded here.

    ``trace_exporter`` must confirm the trace by returning a truthy value.  Only
    then are metrics sent.  ``TimeoutError`` and ``OSError`` intentionally pass
    through to the generic Port boundary for the sole retryable classifications.
    """

    if not callable(trace_exporter):
        raise TypeError("trace_exporter must be callable")

    def adapter(request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        payload = request["payload"]
        event = validate_neutral_event(payload["event"])
        outbound = build_langfuse_payload(event, payload["event_ref"])
        confirmed = trace_exporter(copy.deepcopy(outbound["trace"]), timeout_seconds)
        if not confirmed:
            return _port_response(request, code="PORT_ADAPTER_FAILED")

        failures: list[dict[str, Any]] = []
        metric_count = len(outbound["metrics"])
        if metric_count:
            if not callable(metric_sender):
                failures.append({
                    "code": "OBSERVABILITY_METRIC_FAILED",
                    "message": "Declared metrics were not accepted",
                    "retryable": False,
                })
            else:
                accepted = metric_sender(copy.deepcopy(outbound["metrics"]), timeout_seconds)
                if accepted is not True and accepted != metric_count:
                    failures.append({
                        "code": "OBSERVABILITY_METRIC_FAILED",
                        "message": "Declared metrics were not accepted",
                        "retryable": False,
                    })

        trace_id, span_id = stable_observability_identity(event)
        exported = not failures
        return _port_response(
            request,
            data={
                "contract_version": "1.0",
                "outcome": "exported" if exported else "failed",
                "event_ref": payload["event_ref"],
                "event_id": event["event_id"],
                "trace_id": trace_id,
                "span_id": span_id,
                "exported": exported,
                "metric_count": metric_count,
                "failures": failures,
            },
        )

    return adapter
