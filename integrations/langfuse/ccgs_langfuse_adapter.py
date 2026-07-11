"""Privacy-bounded Langfuse observability for CCGS workflow events."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCHEMA_VERSION = "1.0"
ADAPTER_VERSION = "1.0"
DEFAULT_HOST = "https://cloud.langfuse.com"
MAX_EVENT_BYTES = 1_000_000
MAX_SUMMARY_CHARS = 4000
MAX_QUERY_CHARS = 4000
MAX_REFERENCES = 50
MAX_FAILURES = 50
MAX_TAGS = 20
MAX_METADATA = 32
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
METADATA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
ENVIRONMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SECRET_KEY_RE = re.compile(
    r"(?:secret|token|password|authorization|credential|api[_-]?key)", re.IGNORECASE
)
WINDOWS_ABSOLUTE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
URL_CREDENTIAL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/@\s]+@", re.IGNORECASE)
TRACE_NAMESPACE = uuid.UUID("92cf9674-8c1b-49a5-956a-a7f23bfaef5d")
SCORE_NAMESPACE = uuid.UUID("c70b9e20-62b3-4d41-aa24-460c91f56872")
STATUS_LEVEL = {
    "pass": "DEFAULT",
    "fail": "ERROR",
    "blocked": "WARNING",
    "error": "ERROR",
    "unknown": "WARNING",
}


class LangfuseAdapterError(ValueError):
    """Raised when an event or configuration violates the contract."""


class LangfuseTransportError(LangfuseAdapterError):
    """Raised for transient Langfuse transport failures that may be retried."""


@dataclass(frozen=True)
class WorkflowScore:
    score_id: str
    name: str
    value: float | str
    data_type: str
    comment: str
    metadata: dict[str, str | int | float | bool]


@dataclass(frozen=True)
class WorkflowEvent:
    relative_path: str
    event_id: str
    trace_key: str
    timestamp: str
    end_timestamp: str
    project_id: str
    operation: str
    status: str
    environment: str
    session_id: str
    story_id: str
    surface: str
    workflow_version: str
    tags: tuple[str, ...]
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    metadata: dict[str, str | int | float | bool]
    scores: tuple[WorkflowScore, ...]


@dataclass(frozen=True)
class LangfuseBundle:
    event: WorkflowEvent
    trace_id: str
    span_id: str
    start_time_ns: int
    end_time_ns: int
    span_name: str
    attributes: dict[str, Any]
    score_payloads: tuple[dict[str, Any], ...]
    manifest_sha256: str


class TraceExporter(Protocol):
    def export(self, bundle: LangfuseBundle) -> dict[str, Any]: ...


class ScoreSender(Protocol):
    def send_scores(self, payloads: Sequence[dict[str, Any]]) -> int: ...


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_string(
    value: Any,
    path: str,
    *,
    minimum: int = 1,
    maximum: int = 128,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise LangfuseAdapterError(f"{path} must be a string")
    stripped = value.strip()
    if len(stripped) < minimum or len(stripped) > maximum:
        raise LangfuseAdapterError(
            f"{path} must contain {minimum}-{maximum} characters"
        )
    if pattern is not None and not pattern.fullmatch(stripped):
        raise LangfuseAdapterError(f"{path} uses unsupported characters")
    return stripped


def _parse_timestamp(value: Any, path: str) -> tuple[str, datetime]:
    text = _require_string(value, path, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LangfuseAdapterError(f"{path} must use ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LangfuseAdapterError(f"{path} must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    rendered = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return rendered, utc


def _reject_sensitive_string(value: str, path: str) -> None:
    if WINDOWS_ABSOLUTE_RE.search(value):
        raise LangfuseAdapterError(f"{path} must not contain an absolute Windows path")
    if URL_CREDENTIAL_RE.search(value):
        raise LangfuseAdapterError(f"{path} must not contain URL credentials")


def _metadata(value: Any, path: str) -> dict[str, str | int | float | bool]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_METADATA:
        raise LangfuseAdapterError(f"{path} must be an object with at most {MAX_METADATA} keys")
    result: dict[str, str | int | float | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not METADATA_KEY_RE.fullmatch(key):
            raise LangfuseAdapterError(f"{path} contains an invalid metadata key")
        if SECRET_KEY_RE.search(key):
            raise LangfuseAdapterError(f"{path}.{key} looks sensitive and is not allowed")
        if not isinstance(item, (str, int, float, bool)) or item is None:
            raise LangfuseAdapterError(f"{path}.{key} must be a scalar")
        if isinstance(item, float) and not math.isfinite(item):
            raise LangfuseAdapterError(f"{path}.{key} must be finite")
        if isinstance(item, str):
            if len(item) > 512:
                raise LangfuseAdapterError(f"{path}.{key} exceeds 512 characters")
            _reject_sensitive_string(item, f"{path}.{key}")
        result[key] = item
    return dict(sorted(result.items()))


def _references(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_REFERENCES:
        raise LangfuseAdapterError(
            f"$.input.references must be an array with at most {MAX_REFERENCES} entries"
        )
    result: list[str] = []
    for index, item in enumerate(value):
        reference = _require_string(
            item, f"$.input.references[{index}]", maximum=512
        ).replace("\\", "/")
        path = Path(reference)
        if path.is_absolute() or ".." in path.parts:
            raise LangfuseAdapterError(
                f"$.input.references[{index}] must be project-relative"
            )
        _reject_sensitive_string(reference, f"$.input.references[{index}]")
        result.append(reference)
    if len(set(result)) != len(result):
        raise LangfuseAdapterError("$.input.references must be unique")
    return result


def _input_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LangfuseAdapterError("$.input must be an object")
    allowed = {"summary", "query", "references", "context_manifest"}
    extras = sorted(set(value) - allowed)
    if extras:
        raise LangfuseAdapterError(f"$.input contains unsupported fields: {', '.join(extras)}")
    summary = _require_string(value.get("summary"), "$.input.summary", maximum=MAX_SUMMARY_CHARS)
    _reject_sensitive_string(summary, "$.input.summary")
    query = value.get("query", "")
    if query:
        query = _require_string(query, "$.input.query", maximum=MAX_QUERY_CHARS)
        _reject_sensitive_string(query, "$.input.query")
    manifest = value.get("context_manifest", "")
    if manifest:
        manifest = _require_string(
            manifest, "$.input.context_manifest", minimum=64, maximum=64
        )
        if not re.fullmatch(r"[0-9a-f]{64}", manifest):
            raise LangfuseAdapterError("$.input.context_manifest must be a SHA-256 hash")
    return {
        "summary": summary,
        "query": query,
        "references": _references(value.get("references", [])),
        "context_manifest": manifest,
    }


def _output_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LangfuseAdapterError("$.output must be an object")
    allowed = {"summary", "decision", "failure_reasons"}
    extras = sorted(set(value) - allowed)
    if extras:
        raise LangfuseAdapterError(f"$.output contains unsupported fields: {', '.join(extras)}")
    summary = _require_string(value.get("summary"), "$.output.summary", maximum=MAX_SUMMARY_CHARS)
    _reject_sensitive_string(summary, "$.output.summary")
    decision = _require_string(value.get("decision"), "$.output.decision", maximum=32)
    if decision not in {"pass", "fail", "blocked", "unknown"}:
        raise LangfuseAdapterError("$.output.decision must be pass, fail, blocked, or unknown")
    reasons = value.get("failure_reasons", [])
    if not isinstance(reasons, list) or len(reasons) > MAX_FAILURES:
        raise LangfuseAdapterError(
            f"$.output.failure_reasons must have at most {MAX_FAILURES} entries"
        )
    rendered: list[str] = []
    for index, item in enumerate(reasons):
        reason = _require_string(
            item, f"$.output.failure_reasons[{index}]", maximum=500
        )
        _reject_sensitive_string(reason, f"$.output.failure_reasons[{index}]")
        rendered.append(reason)
    return {"summary": summary, "decision": decision, "failure_reasons": rendered}


def _score_value(value: Any, data_type: str, path: str) -> float | str:
    if data_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise LangfuseAdapterError(f"{path}.value must be boolean")
        return 1.0 if value else 0.0
    if data_type == "NUMERIC":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LangfuseAdapterError(f"{path}.value must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise LangfuseAdapterError(f"{path}.value must be finite")
        return numeric
    if not isinstance(value, str):
        raise LangfuseAdapterError(f"{path}.value must be a string")
    maximum = 100 if data_type == "CATEGORICAL" else 500
    return _require_string(value, f"{path}.value", maximum=maximum)


def _scores(
    value: Any, project_id: str, event_id: str
) -> tuple[WorkflowScore, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise LangfuseAdapterError("$.scores must be an array with at most 20 entries")
    result: list[WorkflowScore] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.scores[{index}]"
        if not isinstance(item, dict):
            raise LangfuseAdapterError(f"{path} must be an object")
        extras = sorted(set(item) - {"name", "value", "data_type", "comment", "metadata"})
        if extras:
            raise LangfuseAdapterError(f"{path} contains unsupported fields: {', '.join(extras)}")
        name = _require_string(item.get("name"), f"{path}.name", maximum=128, pattern=IDENTIFIER_RE)
        if name in names:
            raise LangfuseAdapterError("$.scores names must be unique")
        names.add(name)
        data_type = _require_string(item.get("data_type"), f"{path}.data_type", maximum=16)
        if data_type not in {"NUMERIC", "BOOLEAN", "CATEGORICAL", "TEXT"}:
            raise LangfuseAdapterError(
                f"{path}.data_type must be NUMERIC, BOOLEAN, CATEGORICAL, or TEXT"
            )
        score_value = _score_value(item.get("value"), data_type, path)
        comment = item.get("comment", "")
        if comment:
            comment = _require_string(comment, f"{path}.comment", maximum=500)
            _reject_sensitive_string(comment, f"{path}.comment")
        score_id = str(uuid.uuid5(SCORE_NAMESPACE, f"{project_id}:{event_id}:{name}"))
        result.append(
            WorkflowScore(
                score_id,
                name,
                score_value,
                data_type,
                comment,
                _metadata(item.get("metadata", {}), f"{path}.metadata"),
            )
        )
    return tuple(result)


def validate_event_document(document: Any, relative_path: str) -> WorkflowEvent:
    if not isinstance(document, dict):
        raise LangfuseAdapterError("workflow event must be a JSON object")
    allowed = {
        "schema_version",
        "event_id",
        "trace_key",
        "timestamp",
        "end_timestamp",
        "project_id",
        "operation",
        "status",
        "environment",
        "session_id",
        "story_id",
        "surface",
        "workflow_version",
        "tags",
        "input",
        "output",
        "metadata",
        "scores",
    }
    extras = sorted(set(document) - allowed)
    if extras:
        raise LangfuseAdapterError(f"workflow event contains unsupported fields: {', '.join(extras)}")
    required = {
        "schema_version",
        "event_id",
        "trace_key",
        "timestamp",
        "project_id",
        "operation",
        "status",
        "environment",
        "surface",
        "input",
        "output",
    }
    missing = sorted(required - set(document))
    if missing:
        raise LangfuseAdapterError(f"workflow event is missing required fields: {', '.join(missing)}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise LangfuseAdapterError(f"workflow event schema_version must be {SCHEMA_VERSION}")
    event_id = _require_string(document["event_id"], "$.event_id", maximum=128, pattern=IDENTIFIER_RE)
    trace_key = _require_string(document["trace_key"], "$.trace_key", maximum=128, pattern=IDENTIFIER_RE)
    project_id = _require_string(document["project_id"], "$.project_id", maximum=128, pattern=IDENTIFIER_RE)
    operation = _require_string(document["operation"], "$.operation", maximum=128, pattern=IDENTIFIER_RE)
    status = _require_string(document["status"], "$.status", maximum=16)
    if status not in STATUS_LEVEL:
        raise LangfuseAdapterError("$.status must be pass, fail, blocked, error, or unknown")
    environment = _require_string(
        document["environment"], "$.environment", maximum=64, pattern=ENVIRONMENT_RE
    )
    if environment.startswith("langfuse"):
        raise LangfuseAdapterError("$.environment must not start with 'langfuse'")
    timestamp, start = _parse_timestamp(document["timestamp"], "$.timestamp")
    end_value = document.get("end_timestamp", timestamp)
    end_timestamp, end = _parse_timestamp(end_value, "$.end_timestamp")
    if end < start:
        raise LangfuseAdapterError("$.end_timestamp must not be before $.timestamp")
    tags = document.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise LangfuseAdapterError(f"$.tags must have at most {MAX_TAGS} entries")
    rendered_tags = tuple(
        _require_string(item, f"$.tags[{index}]", maximum=64, pattern=IDENTIFIER_RE)
        for index, item in enumerate(tags)
    )
    if len(set(rendered_tags)) != len(rendered_tags):
        raise LangfuseAdapterError("$.tags must be unique")
    session_id = document.get("session_id", "")
    if session_id:
        session_id = _require_string(session_id, "$.session_id", maximum=128, pattern=IDENTIFIER_RE)
    story_id = document.get("story_id", "")
    if story_id:
        story_id = _require_string(story_id, "$.story_id", maximum=128, pattern=IDENTIFIER_RE)
    surface = _require_string(document["surface"], "$.surface", maximum=64, pattern=IDENTIFIER_RE)
    workflow_version = document.get("workflow_version", ADAPTER_VERSION)
    workflow_version = _require_string(
        workflow_version, "$.workflow_version", maximum=64, pattern=IDENTIFIER_RE
    )
    return WorkflowEvent(
        relative_path=relative_path,
        event_id=event_id,
        trace_key=trace_key,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
        project_id=project_id,
        operation=operation,
        status=status,
        environment=environment,
        session_id=session_id,
        story_id=story_id,
        surface=surface,
        workflow_version=workflow_version,
        tags=rendered_tags,
        input_data=_input_data(document["input"]),
        output_data=_output_data(document["output"]),
        metadata=_metadata(document.get("metadata", {}), "$.metadata"),
        scores=_scores(document.get("scores", []), project_id, event_id),
    )


def load_workflow_event(
    project: Path, data_dir: str, raw_path: str
) -> tuple[Path, WorkflowEvent]:
    project = project.resolve()
    root = (project / data_dir / "production" / "observability" / "events").resolve()
    candidate = Path(raw_path)
    candidate = candidate if candidate.is_absolute() else project / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
        relative = candidate.relative_to(project).as_posix()
    except ValueError as exc:
        raise LangfuseAdapterError(
            f"workflow event must stay under {root.relative_to(project).as_posix()}"
        ) from exc
    if candidate.suffix.casefold() != ".json":
        raise LangfuseAdapterError("workflow event must use the .json extension")
    if not candidate.is_file():
        raise LangfuseAdapterError(f"workflow event not found: {relative}")
    if candidate.stat().st_size > MAX_EVENT_BYTES:
        raise LangfuseAdapterError(
            f"workflow event exceeds the {MAX_EVENT_BYTES} byte limit"
        )
    try:
        document = json.loads(candidate.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LangfuseAdapterError(f"invalid workflow event JSON: {relative}: {exc}") from exc
    return candidate, validate_event_document(document, relative)


def _time_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def _trace_id(project_id: str, trace_key: str) -> str:
    digest = hashlib.sha256(f"trace:{project_id}:{trace_key}".encode("utf-8")).hexdigest()[:32]
    return digest if int(digest, 16) else "1".zfill(32)


def _span_id(project_id: str, event_id: str) -> str:
    digest = hashlib.sha256(f"span:{project_id}:{event_id}".encode("utf-8")).hexdigest()[:16]
    return digest if int(digest, 16) else "1".zfill(16)


def build_langfuse_bundle(event: WorkflowEvent) -> LangfuseBundle:
    trace_id = _trace_id(event.project_id, event.trace_key)
    span_id = _span_id(event.project_id, event.event_id)
    input_json = _json_text(event.input_data)
    output_json = _json_text(event.output_data)
    tags = tuple(dict.fromkeys(("ccgs", event.operation, event.project_id, *event.tags)))
    attributes: dict[str, Any] = {
        "langfuse.trace.name": f"ccgs.{event.operation}",
        "langfuse.trace.input": input_json,
        "langfuse.trace.output": output_json,
        "langfuse.trace.tags": tags,
        "langfuse.trace.metadata.project_id": event.project_id,
        "langfuse.trace.metadata.event_id": event.event_id,
        "langfuse.trace.metadata.surface": event.surface,
        "langfuse.trace.metadata.status": event.status,
        "langfuse.observation.type": "span",
        "langfuse.observation.level": STATUS_LEVEL[event.status],
        "langfuse.observation.input": input_json,
        "langfuse.observation.output": output_json,
        "langfuse.version": event.workflow_version,
        "ccgs.event_id": event.event_id,
        "ccgs.trace_key": event.trace_key,
        "ccgs.operation": event.operation,
        "ccgs.status": event.status,
        "ccgs.event_path": event.relative_path,
    }
    if event.session_id:
        attributes["langfuse.session.id"] = event.session_id
    if event.story_id:
        attributes["langfuse.trace.metadata.story_id"] = event.story_id
    for key, value in event.metadata.items():
        attributes[f"langfuse.trace.metadata.{key}"] = value
    status_message = event.output_data["summary"]
    if event.output_data["failure_reasons"]:
        status_message = "; ".join(event.output_data["failure_reasons"])
    attributes["langfuse.observation.status_message"] = status_message[:1000]
    score_payloads = tuple(
        {
            "id": score.score_id,
            "traceId": trace_id,
            "observationId": span_id,
            "name": score.name,
            "value": score.value,
            "comment": score.comment or None,
            "metadata": {
                "ccgs_event_id": event.event_id,
                "ccgs_operation": event.operation,
                **score.metadata,
            },
            "environment": event.environment,
            "dataType": score.data_type,
            "source": "API",
        }
        for score in event.scores
    )
    manifest = {
        "event": event.relative_path,
        "trace_id": trace_id,
        "span_id": span_id,
        "attributes": attributes,
        "scores": score_payloads,
    }
    return LangfuseBundle(
        event=event,
        trace_id=trace_id,
        span_id=span_id,
        start_time_ns=_time_ns(event.timestamp),
        end_time_ns=_time_ns(event.end_timestamp),
        span_name=f"ccgs.{event.operation}",
        attributes=attributes,
        score_payloads=score_payloads,
        manifest_sha256=_sha256_text(_json_text(manifest)),
    )


def validate_host(host: str, allow_insecure_http: bool = False) -> str:
    parsed = urlparse(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LangfuseAdapterError("Langfuse host must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LangfuseAdapterError(
            "Langfuse host must not contain credentials, query parameters, or fragments"
        )
    if parsed.path not in {"", "/"}:
        raise LangfuseAdapterError("Langfuse host must not contain an API path")
    loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise LangfuseAdapterError(
            "remote Langfuse HTTP requires --allow-insecure-http or an https URL"
        )
    return host.rstrip("/")


def validate_env_name(name: str, label: str) -> str:
    if not ENV_NAME_RE.fullmatch(name):
        raise LangfuseAdapterError(f"{label} must use uppercase shell variable syntax")
    return name


def credentials_from_environment(
    public_key_env: str, secret_key_env: str
) -> tuple[str, str]:
    public_name = validate_env_name(public_key_env, "public key environment name")
    secret_name = validate_env_name(secret_key_env, "secret key environment name")
    public_key = os.environ.get(public_name, "")
    secret_key = os.environ.get(secret_name, "")
    if not public_key or not secret_key:
        raise LangfuseAdapterError(
            f"Langfuse credentials are required in {public_name} and {secret_name}"
        )
    return public_key, secret_key


def _basic_auth(public_key: str, secret_key: str) -> str:
    encoded = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


class OtelLangfuseExporter:
    """Lazy OpenTelemetry OTLP exporter for the current Langfuse endpoint."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = 30.0,
        allow_insecure_http: bool = False,
    ) -> None:
        self.host = validate_host(host, allow_insecure_http)
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise LangfuseAdapterError("Langfuse timeout must be between 0 and 300 seconds")
        self.public_key = public_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        return f"{self.host}/api/public/otel/v1/traces"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": _basic_auth(self.public_key, self.secret_key),
            "x-langfuse-ingestion-version": "4",
        }

    def export(self, bundle: LangfuseBundle) -> dict[str, Any]:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                SimpleSpanProcessor,
                SpanExportResult,
                SpanExporter,
            )
            from opentelemetry.sdk.trace.id_generator import IdGenerator
            from opentelemetry.trace import Status, StatusCode
        except ImportError as exc:
            raise LangfuseAdapterError(
                "OpenTelemetry export requires 'opentelemetry-sdk' and "
                "'opentelemetry-exporter-otlp-proto-http'"
            ) from exc

        trace_id_value = int(bundle.trace_id, 16)
        span_id_value = int(bundle.span_id, 16)

        class FixedIdGenerator(IdGenerator):
            def generate_span_id(self) -> int:
                return span_id_value

            def generate_trace_id(self) -> int:
                return trace_id_value

        class RecordingExporter(SpanExporter):
            def __init__(self, delegate: Any) -> None:
                self.delegate = delegate
                self.result: Any = None

            def export(self, spans: Sequence[Any]) -> Any:
                self.result = self.delegate.export(spans)
                return self.result

            def shutdown(self) -> None:
                self.delegate.shutdown()

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return True

        delegate = OTLPSpanExporter(
            endpoint=self.endpoint,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        recording = RecordingExporter(delegate)
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "ccgs-workflow",
                    "service.version": ADAPTER_VERSION,
                    "deployment.environment.name": bundle.event.environment,
                }
            ),
            id_generator=FixedIdGenerator(),
        )
        provider.add_span_processor(SimpleSpanProcessor(recording))
        tracer = provider.get_tracer("ccgs.langfuse", ADAPTER_VERSION)
        span = tracer.start_span(
            bundle.span_name,
            start_time=bundle.start_time_ns,
            attributes=bundle.attributes,
        )
        if bundle.event.status in {"fail", "error"}:
            span.set_status(
                Status(
                    StatusCode.ERROR,
                    str(bundle.attributes["langfuse.observation.status_message"]),
                )
            )
        span.end(end_time=bundle.end_time_ns)
        provider.force_flush()
        success = recording.result == SpanExportResult.SUCCESS
        provider.shutdown()
        if not success:
            raise LangfuseTransportError("Langfuse OTLP exporter did not acknowledge the span")
        return {"endpoint": self.endpoint, "trace_sent": True}


class LangfuseScoreClient:
    """Compatibility client for the current public Score POST endpoint."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = 30.0,
        allow_insecure_http: bool = False,
    ) -> None:
        self.host = validate_host(host, allow_insecure_http)
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise LangfuseAdapterError("Langfuse timeout must be between 0 and 300 seconds")
        self.authorization = _basic_auth(public_key, secret_key)
        self.timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        return f"{self.host}/api/public/scores"

    def _post_score(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.endpoint,
            data=_json_bytes(payload),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": self.authorization,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:1000]
            error = f"Langfuse Score HTTP {exc.code}: {message or exc.reason}"
            if exc.code in {408, 425, 429} or exc.code >= 500:
                raise LangfuseTransportError(error) from exc
            raise LangfuseAdapterError(error) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LangfuseTransportError(f"Langfuse Score request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LangfuseAdapterError("Langfuse Score endpoint returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise LangfuseAdapterError("Langfuse Score response must be an object")
        return document

    def send_scores(self, payloads: Sequence[dict[str, Any]]) -> int:
        sent = 0
        for payload in payloads:
            self._post_score(payload)
            sent += 1
        return sent


def send_bundle(
    bundle: LangfuseBundle,
    trace_exporter: TraceExporter,
    score_sender: ScoreSender,
) -> dict[str, Any]:
    trace_result = trace_exporter.export(bundle)
    if not trace_result.get("trace_sent"):
        raise LangfuseTransportError("Langfuse trace export was not acknowledged")
    scores_sent = score_sender.send_scores(bundle.score_payloads)
    return {
        "trace_sent": True,
        "trace_endpoint": str(trace_result.get("endpoint", "")),
        "scores_sent": scores_sent,
    }


def bundle_report(
    bundle: LangfuseBundle,
    host: str,
    mode: str,
    send_result: dict[str, Any] | None = None,
    allow_insecure_http: bool = False,
) -> dict[str, Any]:
    host = validate_host(host, allow_insecure_http=allow_insecure_http)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter": "langfuse",
        "adapter_version": ADAPTER_VERSION,
        "mode": mode,
        "event": bundle.event.relative_path,
        "event_id": bundle.event.event_id,
        "project_id": bundle.event.project_id,
        "operation": bundle.event.operation,
        "status": bundle.event.status,
        "surface": bundle.event.surface,
        "trace_id": bundle.trace_id,
        "span_id": bundle.span_id,
        "span_name": bundle.span_name,
        "score_count": len(bundle.score_payloads),
        "manifest_sha256": bundle.manifest_sha256,
        "otel_endpoint": f"{host}/api/public/otel/v1/traces",
        "score_endpoint": f"{host}/api/public/scores",
        "credentials": "environment-only",
        "content_policy": "bounded-summary-only",
        "sent": send_result is not None,
        "preview": {
            "attributes": bundle.attributes,
            "scores": list(bundle.score_payloads),
        },
    }
    if send_result is not None:
        report["send"] = send_result
    return report