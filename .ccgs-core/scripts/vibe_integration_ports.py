"""Versioned, fail-closed integration-port contract helpers.

Adapters are injected by callers.  This standard-library-only module never
imports concrete integrations or gives them ownership of core project state.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


CONTRACT_VERSION = "1.0"
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_REFERENCES = 100
MAX_TIMEOUT_SECONDS = 3600.0
MAX_JSON_DEPTH = 64

_PORT_OPERATIONS = {
    "orchestration": "trigger",
    "retrieval": "retrieve",
    "observability": "export_trace",
    "reporting": "export_report",
}
_REQUEST_FIELDS = {
    "contract_version", "request_id", "project_id", "port", "operation",
    "capability", "payload", "references",
}
_CAPABILITY_DOCUMENT_FIELDS = {"contract_version", "adapter_id", "capabilities"}
_CAPABILITY_FIELDS = {"port", "operation", "capability", "contract_versions"}
_RESPONSE_FIELDS = {
    "contract_version", "request_id", "project_id", "port", "operation",
    "capability", "ok", "status", "action", "called", "data", "error",
}
_ERROR_FIELDS = {"code", "message", "retryable", "details"}
_ERROR_CODES = {
    "PORT_VERSION_UNSUPPORTED",
    "PORT_REQUEST_INVALID",
    "PORT_CAPABILITY_UNAVAILABLE",
    "PORT_ADAPTER_UNAVAILABLE",
    "PORT_ADAPTER_TIMEOUT",
    "PORT_ADAPTER_FAILED",
    "PORT_PROTOCOL_INVALID",
    "PORT_PAYLOAD_UNSAFE",
}
_REJECTED_ERROR_CODES = {
    "PORT_VERSION_UNSUPPORTED",
    "PORT_REQUEST_INVALID",
    "PORT_PROTOCOL_INVALID",
    "PORT_PAYLOAD_UNSAFE",
}
_DEGRADED_ERROR_CODES = _ERROR_CODES - _REJECTED_ERROR_CODES
_RETRYABLE_ERROR_CODES = {
    "PORT_ADAPTER_UNAVAILABLE",
    "PORT_ADAPTER_TIMEOUT",
}
_ADAPTER_RESPONSE_ERROR_CODES = {
    "PORT_ADAPTER_UNAVAILABLE",
    "PORT_ADAPTER_TIMEOUT",
    "PORT_ADAPTER_FAILED",
}
_ERROR_CALLED_REQUIREMENTS = {
    "PORT_REQUEST_INVALID": False,
    "PORT_CAPABILITY_UNAVAILABLE": False,
    "PORT_ADAPTER_UNAVAILABLE": True,
    "PORT_ADAPTER_TIMEOUT": True,
    "PORT_ADAPTER_FAILED": True,
}
_SENSITIVE_KEYS = {
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "api_key",
    "private_prompt",
    "source_text",
    "source_code",
    "state_transition",
    "policy_override",
    "evidence_override",
    "project_writes",
    "commands",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REFERENCE = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_EMBEDDED_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9:/])/(?!/)[^\s/]+(?:/[^\s/]+)*")
_EMBEDDED_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s]+")
_EMBEDDED_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9])file:")
_EMBEDDED_TILDE_PATH = re.compile(r"(?<![A-Za-z0-9])~/[^\s]+")
_URL_CANDIDATE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")


class IntegrationPortContractError(ValueError):
    """Public validation error carrying a stable integration-port error code."""

    def __init__(self, code: str) -> None:
        super().__init__("Integration port contract validation failed")
        self.code = code


def _fail(code: str) -> None:
    raise IntegrationPortContractError(code)


def _require_exact_fields(value: Mapping[str, Any], fields: set[str], code: str) -> None:
    if set(value) != fields:
        _fail(code)


def _require_identifier(value: Any, code: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _url_contains_credentials(value: str) -> bool:
    """Return whether any URL-like fragment contains user information."""

    if "://" not in value:
        return False
    try:
        for match in _URL_CANDIDATE.finditer(value):
            parsed = urlsplit(match.group(0).rstrip(".,;:!?)]}"))
            if parsed.netloc and (parsed.username is not None or parsed.password is not None):
                return True
        return False
    except ValueError:
        return True


def _contains_unsafe_location(value: str) -> bool:
    """Detect standalone or embedded absolute locations and credential URLs."""

    return bool(
        value.startswith(("/", "\\\\", "~/"))
        or _WINDOWS_ABSOLUTE_PATH.match(value)
        or _EMBEDDED_POSIX_PATH.search(value)
        or _EMBEDDED_WINDOWS_PATH.search(value)
        or _EMBEDDED_FILE_URI.search(value)
        or _EMBEDDED_TILDE_PATH.search(value)
        or _url_contains_credentials(value)
    )


def _validate_json_scalar(value: Any, invalid_code: str) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(invalid_code)
        return
    if type(value) is str:
        if _contains_unsafe_location(value):
            _fail("PORT_PAYLOAD_UNSAFE")
        return
    _fail(invalid_code)


def _validate_safe_json(value: Any, invalid_code: str) -> None:
    """Validate finite JSON iteratively, rejecting cycles and excessive depth."""

    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()
    while stack:
        current, depth, exiting = stack.pop()
        if type(current) not in {dict, list}:
            _validate_json_scalar(current, invalid_code)
            continue
        identity = id(current)
        if exiting:
            active_containers.remove(identity)
            continue
        if depth > MAX_JSON_DEPTH or identity in active_containers:
            _fail(invalid_code)
        active_containers.add(identity)
        stack.append((current, depth, True))
        children = (
            current
            if type(current) is list
            else _validated_object_values(current, invalid_code)
        )
        stack.extend((item, depth + 1, False) for item in reversed(children))


def _validated_object_values(value: dict[Any, Any], invalid_code: str) -> list[Any]:
    children: list[Any] = []
    for key, item in value.items():
        if type(key) is not str:
            _fail(invalid_code)
        if key.casefold() in _SENSITIVE_KEYS:
            _fail("PORT_PAYLOAD_UNSAFE")
        children.append(item)
    return children


def _validate_canonical_size(value: Any, invalid_code: str) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(invalid_code)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        _fail(invalid_code)


def _validate_references(value: Any) -> None:
    if type(value) is not list or len(value) > MAX_REFERENCES:
        _fail("PORT_REQUEST_INVALID")
    if not all(type(item) is str for item in value):
        _fail("PORT_REQUEST_INVALID")
    if len(set(value)) != len(value):
        _fail("PORT_REQUEST_INVALID")
    for reference in value:
        if _contains_unsafe_location(reference):
            _fail("PORT_PAYLOAD_UNSAFE")
        if len(reference) > 1024 or _REFERENCE.fullmatch(reference) is None:
            _fail("PORT_REQUEST_INVALID")


def _validate_details(value: Any) -> None:
    if type(value) is not dict or len(value) > 16:
        _fail("PORT_PROTOCOL_INVALID")
    for key, item in value.items():
        _require_identifier(key, "PORT_PROTOCOL_INVALID")
        if key.casefold() in _SENSITIVE_KEYS:
            _fail("PORT_PAYLOAD_UNSAFE")
        _validate_detail_value(item)


def _validate_detail_value(value: Any) -> None:
    if type(value) is bool:
        return
    if type(value) in {int, float}:
        if type(value) is float and not math.isfinite(value):
            _fail("PORT_PROTOCOL_INVALID")
        return
    if type(value) is str and len(value) <= 512:
        if _contains_unsafe_location(value):
            _fail("PORT_PAYLOAD_UNSAFE")
        return
    _fail("PORT_PROTOCOL_INVALID")


def _validate_contract_versions(value: Any) -> tuple[str, ...]:
    if type(value) is not list or not value or not all(type(item) is str for item in value):
        _fail("PORT_PROTOCOL_INVALID")
    if len(set(value)) != len(value):
        _fail("PORT_PROTOCOL_INVALID")
    if any(version != CONTRACT_VERSION for version in value):
        _fail("PORT_VERSION_UNSUPPORTED")
    return tuple(value)


def _validate_capability_entry(item: Any) -> tuple[str, str, str, tuple[str, ...]]:
    if type(item) is not dict:
        _fail("PORT_PROTOCOL_INVALID")
    _require_exact_fields(item, _CAPABILITY_FIELDS, "PORT_PROTOCOL_INVALID")
    port = item["port"]
    operation = item["operation"]
    capability = _require_identifier(item["capability"], "PORT_PROTOCOL_INVALID")
    if type(port) is not str or type(operation) is not str:
        _fail("PORT_PROTOCOL_INVALID")
    if _PORT_OPERATIONS.get(port) != operation:
        _fail("PORT_PROTOCOL_INVALID")
    versions = _validate_contract_versions(item["contract_versions"])
    return port, operation, capability, versions


def validate_capability_document(value: Any) -> dict[str, Any]:
    """Validate Capability Document 1.0 and return an isolated deep copy.

    Invalid values raise :class:`IntegrationPortContractError` with a stable
    code and a generic message that never includes rejected input.
    """

    if type(value) is not dict:
        _fail("PORT_PROTOCOL_INVALID")
    _require_exact_fields(value, _CAPABILITY_DOCUMENT_FIELDS, "PORT_PROTOCOL_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    _require_identifier(value["adapter_id"], "PORT_PROTOCOL_INVALID")
    capabilities = value["capabilities"]
    if type(capabilities) is not list or not capabilities:
        _fail("PORT_PROTOCOL_INVALID")
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for item in capabilities:
        identity = _validate_capability_entry(item)
        if identity in seen:
            _fail("PORT_PROTOCOL_INVALID")
        seen.add(identity)
    return copy.deepcopy(value)


def validate_port_request(value: Any) -> dict[str, Any]:
    """Validate Integration Port Request 1.0 and return a deep copy.

    Payloads are finite safe JSON objects whose canonical encoding is at most
    one MiB.  References are unique project-relative paths, at most 100 items.
    """

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _require_exact_fields(value, _REQUEST_FIELDS, "PORT_REQUEST_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    for field in ("request_id", "project_id", "capability"):
        _require_identifier(value[field], "PORT_REQUEST_INVALID")
    port = value["port"]
    operation = value["operation"]
    if type(port) is not str or type(operation) is not str or _PORT_OPERATIONS.get(port) != operation:
        _fail("PORT_REQUEST_INVALID")
    if type(value["payload"]) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _validate_safe_json(value["payload"], "PORT_REQUEST_INVALID")
    _validate_canonical_size(value["payload"], "PORT_REQUEST_INVALID")
    _validate_references(value["references"])
    return copy.deepcopy(value)


def _validate_response_header(request: dict[str, Any], value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("PORT_PROTOCOL_INVALID")
    _require_exact_fields(value, _RESPONSE_FIELDS, "PORT_PROTOCOL_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    _validate_response_identity(request, value)
    _validate_response_field_types(value)
    return value


def _validate_response_identity(
    request: dict[str, Any], response: dict[str, Any],
) -> None:
    for field in ("request_id", "project_id", "port", "operation", "capability"):
        if response[field] != request[field]:
            _fail("PORT_PROTOCOL_INVALID")


def _validate_response_field_types(value: dict[str, Any]) -> None:
    if type(value["ok"]) is not bool or type(value["called"]) is not bool:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["status"]) is not str or value["status"] not in {"success", "degraded", "rejected"}:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["action"]) is not str or value["action"] not in {"invoke", "validate", "degraded", "reject"}:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["data"]) is not dict:
        _fail("PORT_PROTOCOL_INVALID")


def _validate_success_response(value: dict[str, Any]) -> None:
    if not value["ok"] or value["error"] is not None:
        _fail("PORT_PROTOCOL_INVALID")
    if (value["action"], value["called"]) not in {("validate", False), ("invoke", True)}:
        _fail("PORT_PROTOCOL_INVALID")


def _validate_error_semantics(value: dict[str, Any], code: str) -> None:
    expected_status = "rejected" if code in _REJECTED_ERROR_CODES else "degraded"
    expected_action = "reject" if expected_status == "rejected" else "degraded"
    if value["status"] != expected_status or value["action"] != expected_action:
        _fail("PORT_PROTOCOL_INVALID")
    expected_called = _ERROR_CALLED_REQUIREMENTS.get(code)
    if expected_called is not None and value["called"] is not expected_called:
        _fail("PORT_PROTOCOL_INVALID")


def _validate_error_response(value: dict[str, Any]) -> None:
    error = value["error"]
    if value["ok"] or type(error) is not dict:
        _fail("PORT_PROTOCOL_INVALID")
    _require_exact_fields(error, _ERROR_FIELDS, "PORT_PROTOCOL_INVALID")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        _fail("PORT_PROTOCOL_INVALID")
    message = error["message"]
    if type(message) is not str or not 1 <= len(message) <= 512:
        _fail("PORT_PROTOCOL_INVALID")
    _validate_safe_json(message, "PORT_PROTOCOL_INVALID")
    if type(error["retryable"]) is not bool:
        _fail("PORT_PROTOCOL_INVALID")
    if error["retryable"] is not (code in _RETRYABLE_ERROR_CODES):
        _fail("PORT_PROTOCOL_INVALID")
    _validate_error_semantics(value, code)
    _validate_details(error["details"])


def validate_port_response(request: Any, value: Any) -> dict[str, Any]:
    """Validate Integration Port Response 1.0 against request identity."""

    request_copy = validate_port_request(request)
    response = _validate_response_header(request_copy, value)
    _validate_safe_json(response["data"], "PORT_PROTOCOL_INVALID")
    _validate_canonical_size(response["data"], "PORT_PROTOCOL_INVALID")
    if response["status"] == "success":
        _validate_success_response(response)
    else:
        _validate_error_response(response)
    return copy.deepcopy(response)


def _safe_identity(request: Any) -> dict[str, str]:
    source = request if type(request) is dict else {}
    identity = {
        "contract_version": CONTRACT_VERSION,
        "request_id": "invalid",
        "project_id": "invalid",
        "port": "orchestration",
        "operation": "trigger",
        "capability": "invalid",
    }
    for field in ("request_id", "project_id", "capability"):
        candidate = source.get(field)
        if type(candidate) is str and _IDENTIFIER.fullmatch(candidate):
            identity[field] = candidate
    port = source.get("port")
    operation = source.get("operation")
    if type(port) is str and _PORT_OPERATIONS.get(port) == operation:
        identity["port"] = port
        identity["operation"] = operation
    return identity


def _response(
    request: Any,
    *,
    ok: bool,
    status: str,
    action: str,
    called: bool,
    code: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        **_safe_identity(request),
        "ok": ok,
        "status": status,
        "action": action,
        "called": called,
        "data": {},
        "error": None,
    }
    if code is not None:
        result["error"] = {
            "code": code,
            "message": "Integration port operation did not complete",
            "retryable": retryable,
            "details": {},
        }
    return result


def _reject(request: Any, code: str, *, called: bool = False) -> dict[str, Any]:
    return _response(
        request, ok=False, status="rejected", action="reject",
        called=called, code=code, retryable=False,
    )


def _degrade(
    request: Any, code: str, *, called: bool, retryable: bool,
) -> dict[str, Any]:
    return _response(
        request, ok=False, status="degraded", action="degraded",
        called=called, code=code, retryable=retryable,
    )


def _validate_timeout(value: Any) -> float:
    if type(value) not in {int, float}:
        _fail("PORT_REQUEST_INVALID")
    try:
        timeout = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail("PORT_REQUEST_INVALID")
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        _fail("PORT_REQUEST_INVALID")
    return timeout


def _has_capability(request: dict[str, Any], document: dict[str, Any]) -> bool:
    return any(
        item["port"] == request["port"]
        and item["operation"] == request["operation"]
        and item["capability"] == request["capability"]
        and request["contract_version"] in item["contract_versions"]
        for item in document["capabilities"]
    )


def _call_adapter(
    request: dict[str, Any],
    adapter_call: Callable[[dict[str, Any], float], Any],
    timeout: float,
) -> tuple[Any, dict[str, Any] | None]:
    try:
        return adapter_call(copy.deepcopy(request), timeout), None
    except TimeoutError:
        return None, _degrade(request, "PORT_ADAPTER_TIMEOUT", called=True, retryable=True)
    except OSError:
        return None, _degrade(request, "PORT_ADAPTER_UNAVAILABLE", called=True, retryable=True)
    except Exception:
        return None, _degrade(request, "PORT_ADAPTER_FAILED", called=True, retryable=False)


def _validate_adapter_value(request: dict[str, Any], value: Any) -> dict[str, Any]:
    try:
        response = validate_port_response(request, value)
        if response["called"] is not True:
            _fail("PORT_PROTOCOL_INVALID")
        if response["status"] == "success" and response["action"] != "invoke":
            _fail("PORT_PROTOCOL_INVALID")
        if response["status"] != "success":
            if response["error"]["code"] not in _ADAPTER_RESPONSE_ERROR_CODES:
                _fail("PORT_PROTOCOL_INVALID")
        return response
    except IntegrationPortContractError as exc:
        code = exc.code if exc.code in {"PORT_VERSION_UNSUPPORTED", "PORT_PAYLOAD_UNSAFE"} else "PORT_PROTOCOL_INVALID"
        return _reject(request, code, called=True)


def _request_preflight(
    request: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return validate_port_request(request), None
    except IntegrationPortContractError as exc:
        allowed = {"PORT_VERSION_UNSUPPORTED", "PORT_PAYLOAD_UNSAFE"}
        code = exc.code if exc.code in allowed else "PORT_REQUEST_INVALID"
        return None, _reject(request, code)


def _capability_preflight(
    request: dict[str, Any], capability_document: Any, timeout_seconds: Any,
) -> tuple[dict[str, Any] | None, float | None, dict[str, Any] | None]:
    try:
        document = validate_capability_document(capability_document)
        timeout = _validate_timeout(timeout_seconds)
        return document, timeout, None
    except IntegrationPortContractError as exc:
        code = exc.code if exc.code == "PORT_VERSION_UNSUPPORTED" else "PORT_PROTOCOL_INVALID"
        if exc.code == "PORT_REQUEST_INVALID":
            code = exc.code
        return None, None, _reject(request, code)


def _dispatch_port(
    request: dict[str, Any],
    document: dict[str, Any],
    adapter_call: Callable[[dict[str, Any], float], Any] | None,
    *,
    write: bool,
    timeout: float,
) -> dict[str, Any]:
    if type(write) is not bool:
        return _reject(request, "PORT_REQUEST_INVALID")
    if not _has_capability(request, document):
        return _degrade(
            request, "PORT_CAPABILITY_UNAVAILABLE", called=False, retryable=False,
        )
    if not write:
        return _response(
            request, ok=True, status="success", action="validate", called=False,
        )
    if not callable(adapter_call):
        return _degrade(
            request, "PORT_CAPABILITY_UNAVAILABLE", called=False, retryable=False,
        )
    adapter_value, failure = _call_adapter(request, adapter_call, timeout)
    return failure if failure is not None else _validate_adapter_value(request, adapter_value)


def invoke_port(
    request: Any,
    capability_document: Any,
    adapter_call: Callable[[dict[str, Any], float], Any] | None,
    *,
    write: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Validate and optionally invoke one injected integration adapter.

    In write mode the adapter is called exactly once as
    ``adapter_call(deep_copied_request, timeout_seconds)`` after all preflight
    checks.  The adapter owns timeout enforcement and signals expiry by raising
    ``TimeoutError``.  ``timeout_seconds`` must be in ``(0, 3600]``.
    """

    request_copy, failure = _request_preflight(request)
    if failure is not None:
        return failure
    if request_copy is None:
        return _reject(request, "PORT_REQUEST_INVALID")
    document_copy, timeout, failure = _capability_preflight(
        request_copy, capability_document, timeout_seconds,
    )
    if failure is not None:
        return failure
    if document_copy is None or timeout is None:
        return _reject(request_copy, "PORT_PROTOCOL_INVALID")
    return _dispatch_port(
        request_copy,
        document_copy,
        adapter_call,
        write=write,
        timeout=timeout,
    )
