"""Neutral, privacy-bounded observability integration-port helpers.

The local Workflow Event remains a compatibility artifact owned by the core.
Only the strict neutral projection produced here may cross an observability
adapter boundary.  This module is standard-library only and performs no I/O.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from vibe_integration_ports import (
    CONTRACT_VERSION,
    IntegrationPortContractError,
    invoke_port,
)


OBSERVABILITY_PORT = "observability"
OBSERVABILITY_OPERATION = "export_trace"
OBSERVABILITY_CAPABILITY = "workflow_trace"
MAX_EVENT_BYTES = 1_000_000
MAX_PORT_BYTES = 1024 * 1024
MAX_REFERENCES = 50
MAX_FAILURES = 50
MAX_TAGS = 20
MAX_METRICS = 20

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_16 = re.compile(r"^[0-9a-f]{16}$")
_REFERENCE = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)
_WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
_POSIX_ABSOLUTE = re.compile(r"(?:^|[\s\"'(=])/(?!/)[^\s\"']+")
_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9])file:")
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:api[_ -]?key|secret|token|password|authorization|credential)\s*[:=]"
)
_FORBIDDEN_TEXT = re.compile(
    r"(?i)(?:prompt|completion|generation|source[_ -]?(?:code|text)|raw[_ -]?log|exception)\s*[:=]"
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_SHELL_META = re.compile(r"[;&|`$<>\r\n\x00]")

_EVENT_REQUIRED = {
    "schema_version", "event_id", "trace_key", "timestamp", "end_timestamp",
    "project_id", "operation", "status", "environment", "surface",
    "references", "context_manifest", "failure_codes", "metrics",
}
_EVENT_OPTIONAL = {"session_id", "story_id", "workflow_version", "tags"}
_REQUEST_PAYLOAD_FIELDS = {"contract_version", "event_ref", "event"}
_RESPONSE_FIELDS = {
    "contract_version", "outcome", "event_ref", "event_id", "trace_id",
    "span_id", "exported", "metric_count", "failures",
}
_FAILURE_FIELDS = {"code", "message", "retryable"}
_METRIC_FIELDS = {"name", "value", "data_type"}
_STATUSES = {"pass", "fail", "blocked", "error", "unknown"}
_METRIC_TYPES = {"NUMERIC", "BOOLEAN", "CATEGORICAL", "TEXT"}
_SENSITIVE_KEYS = {
    "apikey", "authorization", "completion", "credential", "exception",
    "generation", "log", "logs", "password", "prompt", "secret",
    "sourcecode", "sourcetext", "token",
}


def _fail(code: str) -> None:
    raise IntegrationPortContractError(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail("PORT_REQUEST_INVALID")


def _credential_url(value: str) -> bool:
    try:
        for match in _URL.finditer(value):
            parsed = urlsplit(match.group(0).rstrip(".,;:!?)]}"))
            if parsed.username is not None or parsed.password is not None:
                return True
    except ValueError:
        return True
    return False


def _unsafe_text(value: str) -> bool:
    return bool(
        _WINDOWS_ABSOLUTE.search(value)
        or _POSIX_ABSOLUTE.search(value)
        or _FILE_URI.search(value)
        or _CREDENTIAL_TEXT.search(value)
        or _FORBIDDEN_TEXT.search(value)
        or _credential_url(value)
        or value.startswith(("~/", "/", "\\\\"))
    )


def _reject_sensitive_keys(value: Mapping[Any, Any]) -> None:
    for key in value:
        if type(key) is not str:
            _fail("PORT_REQUEST_INVALID")
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized in _SENSITIVE_KEYS:
            _fail("PORT_PAYLOAD_UNSAFE")


def _identifier(value: Any, code: str = "PORT_REQUEST_INVALID") -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or not 1 <= len(value) <= 64:
        _fail("PORT_REQUEST_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("PORT_REQUEST_INVALID")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("PORT_REQUEST_INVALID")
    return parsed


def _relative_reference(value: Any, *, maximum: int = 512) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        _fail("PORT_REQUEST_INVALID")
    if _unsafe_text(value):
        _fail("PORT_PAYLOAD_UNSAFE")
    if _SHELL_META.search(value) or _REFERENCE.fullmatch(value) is None:
        _fail("PORT_REQUEST_INVALID")
    return value


def validate_event_reference(value: Any, data_dir: str) -> str:
    """Validate an event JSON path under the configured data directory."""

    reference = _relative_reference(value)
    configured = _relative_reference(data_dir)
    prefix = f"{configured}/production/observability/events/"
    if not reference.startswith(prefix) or not reference.endswith(".json"):
        _fail("PORT_REQUEST_INVALID")
    leaf = reference[len(prefix):]
    if not leaf or "/" in leaf or leaf == ".json":
        _fail("PORT_REQUEST_INVALID")
    return reference


def _unique_strings(
    value: Any, *, maximum_items: int, maximum_length: int,
    references: bool = False,
) -> list[str]:
    if type(value) is not list or len(value) > maximum_items:
        _fail("PORT_REQUEST_INVALID")
    result: list[str] = []
    for item in value:
        rendered = (
            _relative_reference(item, maximum=maximum_length)
            if references else _identifier(item)
        )
        result.append(rendered)
    if len(set(result)) != len(result):
        _fail("PORT_REQUEST_INVALID")
    return result


def _tags(value: Any) -> list[str]:
    if type(value) is not list or len(value) > MAX_TAGS:
        _fail("PORT_REQUEST_INVALID")
    result: list[str] = []
    for item in value:
        if type(item) is not str or not 1 <= len(item) <= 64:
            _fail("PORT_REQUEST_INVALID")
        if _IDENTIFIER.fullmatch(item) is None or _unsafe_text(item):
            _fail("PORT_PAYLOAD_UNSAFE" if _unsafe_text(item) else "PORT_REQUEST_INVALID")
        result.append(item)
    if len(set(result)) != len(result):
        _fail("PORT_REQUEST_INVALID")
    return result


def _metric_value(value: Any, data_type: str) -> Any:
    if data_type == "BOOLEAN":
        if type(value) is not bool:
            _fail("PORT_REQUEST_INVALID")
        return value
    if data_type == "NUMERIC":
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            _fail("PORT_REQUEST_INVALID")
        return value
    if type(value) is not str or not 1 <= len(value) <= 500:
        _fail("PORT_REQUEST_INVALID")
    if _unsafe_text(value):
        _fail("PORT_PAYLOAD_UNSAFE")
    return value


def _metrics(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > MAX_METRICS:
        _fail("PORT_REQUEST_INVALID")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if type(item) is not dict:
            _fail("PORT_REQUEST_INVALID")
        _reject_sensitive_keys(item)
        if set(item) != _METRIC_FIELDS:
            _fail("PORT_REQUEST_INVALID")
        name = _identifier(item["name"])
        if name in names:
            _fail("PORT_REQUEST_INVALID")
        names.add(name)
        data_type = item["data_type"]
        if type(data_type) is not str or data_type not in _METRIC_TYPES:
            _fail("PORT_REQUEST_INVALID")
        result.append({
            "name": name,
            "value": _metric_value(item["value"], data_type),
            "data_type": data_type,
        })
    return result


def validate_neutral_event(value: Any) -> dict[str, Any]:
    """Validate and isolate Neutral Workflow Observation Event 1.0."""

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(value)
    fields = set(value)
    if not _EVENT_REQUIRED <= fields or fields - _EVENT_REQUIRED - _EVENT_OPTIONAL:
        _fail("PORT_REQUEST_INVALID")
    if value["schema_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    for name in ("event_id", "trace_key", "project_id", "operation", "surface"):
        _identifier(value[name])
    if type(value["status"]) is not str or value["status"] not in _STATUSES:
        _fail("PORT_REQUEST_INVALID")
    if type(value["environment"]) is not str or _ENVIRONMENT.fullmatch(value["environment"]) is None:
        _fail("PORT_REQUEST_INVALID")
    started = _timestamp(value["timestamp"])
    ended = _timestamp(value["end_timestamp"])
    if ended < started:
        _fail("PORT_REQUEST_INVALID")
    if type(value["context_manifest"]) is not str or _HASH.fullmatch(value["context_manifest"]) is None:
        _fail("PORT_REQUEST_INVALID")

    result: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "event_id": value["event_id"],
        "trace_key": value["trace_key"],
        "timestamp": value["timestamp"],
        "end_timestamp": value["end_timestamp"],
        "project_id": value["project_id"],
        "operation": value["operation"],
        "status": value["status"],
        "environment": value["environment"],
        "surface": value["surface"],
        "references": _unique_strings(
            value["references"], maximum_items=MAX_REFERENCES,
            maximum_length=512, references=True,
        ),
        "context_manifest": value["context_manifest"],
        "failure_codes": _unique_strings(
            value["failure_codes"], maximum_items=MAX_FAILURES,
            maximum_length=128,
        ),
        "metrics": _metrics(value["metrics"]),
    }
    for name in ("session_id", "story_id", "workflow_version"):
        if name in value:
            result[name] = _identifier(value[name])
    if "tags" in value:
        result["tags"] = _tags(value["tags"])
    if len(_canonical_bytes(result)) > MAX_EVENT_BYTES:
        _fail("PORT_REQUEST_INVALID")
    return copy.deepcopy(result)


def project_workflow_event(local_event: Any, project_id: str) -> dict[str, Any]:
    """Project compatible local Workflow Event 1.0 into the neutral allowlist."""

    if type(local_event) is not dict:
        _fail("PORT_REQUEST_INVALID")
    project = _identifier(project_id)
    if local_event.get("project_id") != project:
        _fail("PORT_REQUEST_INVALID")
    input_data = local_event.get("input")
    output_data = local_event.get("output")
    scores = local_event.get("scores", [])
    if type(input_data) is not dict or type(output_data) is not dict or type(scores) is not list:
        _fail("PORT_REQUEST_INVALID")
    event: dict[str, Any] = {
        "schema_version": local_event.get("schema_version"),
        "event_id": local_event.get("event_id"),
        "trace_key": local_event.get("trace_key"),
        "timestamp": local_event.get("timestamp"),
        "end_timestamp": local_event.get("end_timestamp", local_event.get("timestamp")),
        "project_id": project,
        "operation": local_event.get("operation"),
        "status": local_event.get("status"),
        "environment": local_event.get("environment"),
        "surface": local_event.get("surface"),
        "references": input_data.get("references"),
        "context_manifest": input_data.get("context_manifest"),
        "failure_codes": output_data.get("failure_reasons", []),
        "metrics": [
            {
                "name": item.get("name") if type(item) is dict else None,
                "value": item.get("value") if type(item) is dict else None,
                "data_type": item.get("data_type") if type(item) is dict else None,
            }
            for item in scores
        ],
    }
    for name in ("session_id", "story_id", "workflow_version", "tags"):
        if name in local_event:
            event[name] = local_event[name]
    return validate_neutral_event(event)


def build_observability_request(
    local_event: Any, *, data_dir: str, event_ref: str,
    request_id: str, project_id: str,
) -> dict[str, Any]:
    """Build Integration Port Request 1.0 for one local Workflow Event."""

    reference = validate_event_reference(event_ref, data_dir)
    request = {
        "contract_version": CONTRACT_VERSION,
        "request_id": _identifier(request_id),
        "project_id": _identifier(project_id),
        "port": OBSERVABILITY_PORT,
        "operation": OBSERVABILITY_OPERATION,
        "capability": OBSERVABILITY_CAPABILITY,
        "payload": {
            "contract_version": CONTRACT_VERSION,
            "event_ref": reference,
            "event": project_workflow_event(local_event, project_id),
        },
        "references": [reference],
    }
    return validate_observability_request(request, data_dir=data_dir)


def validate_observability_request(value: Any, *, data_dir: str) -> dict[str, Any]:
    """Validate the domain-specific request before invoking any adapter."""

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    expected = {
        "contract_version", "request_id", "project_id", "port", "operation",
        "capability", "payload", "references",
    }
    if set(value) != expected:
        _fail("PORT_REQUEST_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    if (
        value["port"] != OBSERVABILITY_PORT
        or value["operation"] != OBSERVABILITY_OPERATION
        or value["capability"] != OBSERVABILITY_CAPABILITY
    ):
        _fail("PORT_REQUEST_INVALID")
    _identifier(value["request_id"])
    project_id = _identifier(value["project_id"])
    payload = value["payload"]
    if type(payload) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(payload)
    if set(payload) != _REQUEST_PAYLOAD_FIELDS:
        _fail("PORT_REQUEST_INVALID")
    if payload["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    event_ref = validate_event_reference(payload["event_ref"], data_dir)
    if value["references"] != [event_ref]:
        _fail("PORT_REQUEST_INVALID")
    event = validate_neutral_event(payload["event"])
    if event["project_id"] != project_id:
        _fail("PORT_REQUEST_INVALID")
    result = copy.deepcopy(value)
    result["payload"]["event"] = event
    if len(_canonical_bytes(result["payload"])) > MAX_PORT_BYTES:
        _fail("PORT_REQUEST_INVALID")
    return result


def stable_observability_identity(event: Mapping[str, Any]) -> tuple[str, str]:
    """Return stable non-zero lower-case hexadecimal Trace and Span IDs."""

    neutral = validate_neutral_event(dict(event))
    trace_id = hashlib.sha256(
        f"trace:{neutral['project_id']}:{neutral['trace_key']}".encode("utf-8")
    ).hexdigest()[:32]
    span_id = hashlib.sha256(
        f"span:{neutral['project_id']}:{neutral['event_id']}".encode("utf-8")
    ).hexdigest()[:16]
    return trace_id if int(trace_id, 16) else "0" * 31 + "1", span_id if int(span_id, 16) else "0" * 15 + "1"


def validate_observability_response_data(
    value: Any, *, event: Mapping[str, Any], event_ref: str,
) -> dict[str, Any]:
    """Validate the only public success-data shape an adapter may return."""

    if type(value) is not dict or set(value) != _RESPONSE_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    neutral = validate_neutral_event(dict(event))
    if value["event_ref"] != event_ref or value["event_id"] != neutral["event_id"]:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["outcome"]) is not str or value["outcome"] not in {"exported", "failed"}:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["exported"]) is not bool or value["exported"] is not (value["outcome"] == "exported"):
        _fail("PORT_PROTOCOL_INVALID")
    expected_trace, expected_span = stable_observability_identity(neutral)
    if value["trace_id"] != expected_trace or _HEX_32.fullmatch(str(value["trace_id"])) is None:
        _fail("PORT_PROTOCOL_INVALID")
    if value["span_id"] != expected_span or _HEX_16.fullmatch(str(value["span_id"])) is None:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["metric_count"]) is not int or not 0 <= value["metric_count"] <= MAX_METRICS:
        _fail("PORT_PROTOCOL_INVALID")
    failures = value["failures"]
    if type(failures) is not list or len(failures) > MAX_FAILURES:
        _fail("PORT_PROTOCOL_INVALID")
    if value["metric_count"] != len(neutral["metrics"]):
        _fail("PORT_PROTOCOL_INVALID")
    rendered: list[dict[str, Any]] = []
    identities: set[tuple[str, str, bool]] = set()
    for failure in failures:
        if type(failure) is not dict:
            _fail("PORT_PROTOCOL_INVALID")
        _reject_sensitive_keys(failure)
        if set(failure) != _FAILURE_FIELDS:
            _fail("PORT_PROTOCOL_INVALID")
        code = _identifier(failure["code"], "PORT_PROTOCOL_INVALID")
        message = failure["message"]
        retryable = failure["retryable"]
        if type(message) is not str or not 1 <= len(message) <= 512 or type(retryable) is not bool:
            _fail("PORT_PROTOCOL_INVALID")
        if _unsafe_text(message):
            _fail("PORT_PAYLOAD_UNSAFE")
        if retryable is not (code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}):
            _fail("PORT_PROTOCOL_INVALID")
        identity = (code, message, retryable)
        if identity in identities:
            continue
        identities.add(identity)
        rendered.append({"code": code, "message": message, "retryable": retryable})
    if value["exported"] and rendered:
        _fail("PORT_PROTOCOL_INVALID")
    result = copy.deepcopy(value)
    result["failures"] = rendered
    if len(_canonical_bytes(result)) > MAX_PORT_BYTES:
        _fail("PORT_PROTOCOL_INVALID")
    return result


def _port_failure(request: Any, code: str, *, called: bool) -> dict[str, Any]:
    source = request if type(request) is dict else {}
    request_id = source.get("request_id")
    project_id = source.get("project_id")
    retryable = code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}
    rejected = code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID",
        "PORT_PROTOCOL_INVALID", "PORT_PAYLOAD_UNSAFE",
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id if type(request_id) is str and _IDENTIFIER.fullmatch(request_id) else "invalid",
        "project_id": project_id if type(project_id) is str and _IDENTIFIER.fullmatch(project_id) else "invalid",
        "port": OBSERVABILITY_PORT,
        "operation": OBSERVABILITY_OPERATION,
        "capability": OBSERVABILITY_CAPABILITY,
        "ok": False,
        "status": "rejected" if rejected else "degraded",
        "action": "reject" if rejected else "degraded",
        "called": called,
        "data": {},
        "error": {
            "code": code,
            "message": "Observability operation did not complete",
            "retryable": retryable,
            "details": {},
        },
    }


def invoke_observability(
    request: Any, capability_document: Any,
    adapter_call: Callable[[dict[str, Any], float], Any] | None,
    *, data_dir: str, dry_run: bool, timeout_seconds: float,
) -> dict[str, Any]:
    """Validate, optionally invoke once, then validate the public result."""

    if type(dry_run) is not bool:
        return _port_failure(request, "PORT_REQUEST_INVALID", called=False)
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(float(timeout_seconds))
        or not 1 <= float(timeout_seconds) <= 300
    ):
        return _port_failure(request, "PORT_REQUEST_INVALID", called=False)
    try:
        checked = validate_observability_request(request, data_dir=data_dir)
    except IntegrationPortContractError as exc:
        return _port_failure(request, exc.code, called=False)
    response = invoke_port(
        checked, capability_document, adapter_call,
        write=not dry_run, timeout_seconds=timeout_seconds,
    )
    if not response.get("ok"):
        return response
    event = checked["payload"]["event"]
    event_ref = checked["payload"]["event_ref"]
    if dry_run:
        trace_id, span_id = stable_observability_identity(event)
        response["data"] = {
            "contract_version": CONTRACT_VERSION,
            "outcome": "failed",
            "event_ref": event_ref,
            "event_id": event["event_id"],
            "trace_id": trace_id,
            "span_id": span_id,
            "exported": False,
            "metric_count": len(event["metrics"]),
            "failures": [],
        }
        return response
    try:
        response["data"] = validate_observability_response_data(
            response.get("data"), event=event, event_ref=event_ref,
        )
    except IntegrationPortContractError as exc:
        code = exc.code if exc.code in {"PORT_VERSION_UNSUPPORTED", "PORT_PAYLOAD_UNSAFE"} else "PORT_PROTOCOL_INVALID"
        return _port_failure(checked, code, called=True)
    return response
