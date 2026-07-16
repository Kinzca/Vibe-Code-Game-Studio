"""Neutral, bounded Reporting Port contracts and projections.

This module is standard-library only and performs no project I/O.  Trusted
loaders may project local test and Evidence documents through the functions
below; concrete report renderers receive only the resulting public contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping, Sequence

from vibe_integration_ports import (
    CONTRACT_VERSION,
    MAX_PAYLOAD_BYTES,
    IntegrationPortContractError,
    invoke_port,
    validate_port_request,
)


REPORTING_PORT = "reporting"
REPORTING_OPERATION = "export_report"
REPORTING_CAPABILITY = "evidence_report"
MAX_RESULTS = 4_999
MAX_EVIDENCE_ITEMS = 200
MAX_ARTIFACTS = 10_000
MAX_FAILURES = 50
STATUS_ORDER = ("passed", "failed", "broken", "skipped", "unknown")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REFERENCE = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)
_SHELL_META = re.compile(r"[;&|`$<>\r\n\x00]")
_WINDOWS_ABSOLUTE = re.compile(r"(?i)^[A-Z]:[\\/]")
_FILE_URI = re.compile(r"(?i)^file:")
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:api[_ -]?key|secret|token|password|authorization|credential)\s*[:=]"
)
_SENSITIVE_KEYS = {
    "command", "commands", "environment", "env", "exception", "frameworkroot",
    "log", "logs", "metadata", "password", "policy", "privateprompt", "projectroot",
    "prompt", "completion", "secret", "source", "sourcecode", "sourcetext", "stderr",
    "stdout", "token", "trace",
}
_PAYLOAD_FIELDS = {"contract_version", "report_id", "results", "evidence", "output_ref"}
_RESULT_REQUIRED = {"id", "name", "status", "duration_ms", "source_ref"}
_RESULT_OPTIONAL = {"suite", "start_ms", "failure_code"}
_EVIDENCE_FIELDS = {"story_id", "result", "acceptance_criteria", "checks", "source_ref"}
_AC_FIELDS = {"id", "status", "source_refs"}
_CHECK_FIELDS = {"id", "type", "status", "source_refs"}
_RESPONSE_FIELDS = {
    "contract_version", "outcome", "report_id", "output_ref", "artifact_refs",
    "total_results", "status_counts", "reused", "failures",
}
_FAILURE_FIELDS = {"code", "message", "retryable"}
_LOCAL_TEST_FIELDS = {
    "id", "name", "status", "duration_ms", "suite", "start_ms", "failure_code",
    "package", "message", "trace", "stdout", "stderr",
}
_LOCAL_EVIDENCE_FIELDS = {
    "schema_version", "story_id", "result", "acceptance_criteria", "checks",
}
_LOCAL_AC_FIELDS = {"id", "status", "evidence", "source_refs"}
_LOCAL_CHECK_FIELDS = {"id", "type", "status", "summary", "source_refs"}
_LOCAL_STATUS = {
    "pass": "passed", "passed": "passed", "fail": "failed", "failed": "failed",
    "error": "broken", "broken": "broken", "skip": "skipped", "skipped": "skipped",
    "deferred": "skipped", "blocked": "skipped", "unknown": "unknown",
}


def _fail(code: str) -> None:
    raise IntegrationPortContractError(code)


def _canonical_bytes(value: Any, code: str = "PORT_REQUEST_INVALID") -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(code)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _reject_sensitive_keys(value: Mapping[Any, Any]) -> None:
    for key in value:
        if type(key) is not str:
            _fail("PORT_REQUEST_INVALID")
        if _normalized_key(key) in _SENSITIVE_KEYS:
            _fail("PORT_PAYLOAD_UNSAFE")


def _identifier(value: Any, code: str = "PORT_REQUEST_INVALID") -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _safe_text(value: Any, maximum: int, code: str = "PORT_REQUEST_INVALID") -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        _fail(code)
    if (
        value.startswith(("/", "\\\\", "~/"))
        or _WINDOWS_ABSOLUTE.search(value)
        or _FILE_URI.search(value)
        or _CREDENTIAL_TEXT.search(value)
        or "\x00" in value
    ):
        _fail("PORT_PAYLOAD_UNSAFE")
    return value


def _reference(value: Any, code: str = "PORT_REQUEST_INVALID") -> str:
    if type(value) is not str or not 1 <= len(value) <= 512:
        _fail(code)
    if (
        value.startswith(("/", "\\\\", "~/"))
        or _WINDOWS_ABSOLUTE.search(value)
        or _FILE_URI.search(value)
        or "\\" in value
        or _SHELL_META.search(value)
    ):
        _fail("PORT_PAYLOAD_UNSAFE")
    if _REFERENCE.fullmatch(value) is None:
        _fail(code)
    return value


def _output_reference(value: Any, data_dir: str, report_id: str) -> str:
    reference = _reference(value)
    data = _reference(data_dir)
    expected = f"{data}/production/qa/reports/{report_id}"
    if reference != expected:
        _fail("PORT_REQUEST_INVALID")
    return reference


def _unique_references(value: Any, *, allow_empty: bool = False) -> list[str]:
    if type(value) is not list or (not value and not allow_empty):
        _fail("PORT_REQUEST_INVALID")
    rendered = [_reference(item) for item in value]
    if len(set(rendered)) != len(rendered):
        _fail("PORT_REQUEST_INVALID")
    return rendered


def validate_reporting_result(value: Any) -> dict[str, Any]:
    """Validate one bounded, vendor-neutral automated test result."""

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(value)
    fields = set(value)
    if not _RESULT_REQUIRED <= fields or fields - _RESULT_REQUIRED - _RESULT_OPTIONAL:
        _fail("PORT_REQUEST_INVALID")
    result = copy.deepcopy(value)
    _identifier(result["id"])
    _safe_text(result["name"], 256)
    if result["status"] not in STATUS_ORDER:
        _fail("PORT_REQUEST_INVALID")
    if type(result["duration_ms"]) is not int or result["duration_ms"] < 0:
        _fail("PORT_REQUEST_INVALID")
    result["source_ref"] = _reference(result["source_ref"])
    if "suite" in result:
        _safe_text(result["suite"], 256)
    if "start_ms" in result and (
        type(result["start_ms"]) is not int or result["start_ms"] < 0
    ):
        _fail("PORT_REQUEST_INVALID")
    if "failure_code" in result:
        _identifier(result["failure_code"])
    return result


def _validate_evidence_item(value: Any, *, check: bool) -> dict[str, Any]:
    fields = _CHECK_FIELDS if check else _AC_FIELDS
    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(value)
    if set(value) != fields:
        _fail("PORT_REQUEST_INVALID")
    result = copy.deepcopy(value)
    _identifier(result["id"])
    if result["status"] not in {"pass", "fail", "deferred"}:
        _fail("PORT_REQUEST_INVALID")
    if check:
        _identifier(result["type"])
    result["source_refs"] = _unique_references(result["source_refs"])
    return result


def validate_reporting_evidence(value: Any) -> dict[str, Any]:
    """Validate a bounded neutral Evidence 1.0 projection."""

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(value)
    if set(value) != _EVIDENCE_FIELDS:
        _fail("PORT_REQUEST_INVALID")
    result = copy.deepcopy(value)
    _identifier(result["story_id"])
    if result["result"] not in {"pass", "fail", "blocked"}:
        _fail("PORT_REQUEST_INVALID")
    result["source_ref"] = _reference(result["source_ref"])
    for key, check in (("acceptance_criteria", False), ("checks", True)):
        items = result[key]
        if type(items) is not list or not 1 <= len(items) <= MAX_EVIDENCE_ITEMS:
            _fail("PORT_REQUEST_INVALID")
        rendered = [_validate_evidence_item(item, check=check) for item in items]
        if len({item["id"] for item in rendered}) != len(rendered):
            _fail("PORT_REQUEST_INVALID")
        result[key] = rendered
    statuses = [
        item["status"]
        for key in ("acceptance_criteria", "checks")
        for item in result[key]
    ]
    consistent = (
        (result["result"] == "pass" and all(item == "pass" for item in statuses))
        or (result["result"] == "fail" and "fail" in statuses)
        or (
            result["result"] == "blocked"
            and "fail" not in statuses
            and "deferred" in statuses
        )
    )
    if not consistent:
        _fail("PORT_REQUEST_INVALID")
    return result


def validate_reporting_payload(value: Any, *, data_dir: str) -> dict[str, Any]:
    """Validate Reporting Request Data 1.0 before any adapter side effect."""

    if type(value) is not dict:
        _fail("PORT_REQUEST_INVALID")
    _reject_sensitive_keys(value)
    if set(value) != _PAYLOAD_FIELDS:
        _fail("PORT_REQUEST_INVALID")
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    report_id = _identifier(value["report_id"])
    results = value["results"]
    if type(results) is not list or not 1 <= len(results) <= MAX_RESULTS:
        _fail("PORT_REQUEST_INVALID")
    rendered_results = [validate_reporting_result(item) for item in results]
    if len({item["id"] for item in rendered_results}) != len(rendered_results):
        _fail("PORT_REQUEST_INVALID")
    rendered = {
        "contract_version": CONTRACT_VERSION,
        "report_id": report_id,
        "results": rendered_results,
        "evidence": validate_reporting_evidence(value["evidence"]),
        "output_ref": _output_reference(value["output_ref"], data_dir, report_id),
    }
    if len(_canonical_bytes(rendered)) > MAX_PAYLOAD_BYTES:
        _fail("PORT_REQUEST_INVALID")
    return rendered


def reporting_references(payload: Mapping[str, Any]) -> list[str]:
    """Return the exact stable de-duplicated references bound to a payload."""

    values: list[str] = [item["source_ref"] for item in payload["results"]]
    evidence = payload["evidence"]
    values.append(evidence["source_ref"])
    for key in ("acceptance_criteria", "checks"):
        for item in evidence[key]:
            values.extend(item["source_refs"])
    values.append(payload["output_ref"])
    return list(dict.fromkeys(values))


def build_reporting_request(
    results: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any], *,
    data_dir: str, report_id: str, request_id: str, project_id: str,
) -> dict[str, Any]:
    """Build and validate one Integration Port Request 1.0 for reporting."""

    payload = validate_reporting_payload({
        "contract_version": CONTRACT_VERSION,
        "report_id": report_id,
        "results": list(results),
        "evidence": dict(evidence),
        "output_ref": f"{data_dir}/production/qa/reports/{report_id}",
    }, data_dir=data_dir)
    request = {
        "contract_version": CONTRACT_VERSION,
        "request_id": _identifier(request_id),
        "project_id": _identifier(project_id),
        "port": REPORTING_PORT,
        "operation": REPORTING_OPERATION,
        "capability": REPORTING_CAPABILITY,
        "payload": payload,
        "references": reporting_references(payload),
    }
    return validate_reporting_request(request, data_dir=data_dir)


def validate_reporting_request(value: Any, *, data_dir: str) -> dict[str, Any]:
    """Validate capability binding, identity, payload, and exact references."""

    request = validate_port_request(value)
    if (
        request["port"] != REPORTING_PORT
        or request["operation"] != REPORTING_OPERATION
        or request["capability"] != REPORTING_CAPABILITY
    ):
        _fail("PORT_REQUEST_INVALID")
    payload = validate_reporting_payload(request["payload"], data_dir=data_dir)
    if request["references"] != reporting_references(payload):
        _fail("PORT_REQUEST_INVALID")
    request["payload"] = payload
    return request


def project_normalized_results(document: Any, *, source_ref: str) -> list[dict[str, Any]]:
    """Project a trusted normalized-test document, dropping legacy free text."""

    reference = _reference(source_ref)
    if type(document) is not dict or set(document) != {"schema_version", "tests"}:
        _fail("PORT_REQUEST_INVALID")
    if document["schema_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    tests = document["tests"]
    if type(tests) is not list or not 1 <= len(tests) <= MAX_RESULTS:
        _fail("PORT_REQUEST_INVALID")
    projected: list[dict[str, Any]] = []
    for item in tests:
        if type(item) is not dict or set(item) - _LOCAL_TEST_FIELDS:
            _fail("PORT_REQUEST_INVALID")
        status = item.get("status")
        if type(status) is not str or status.casefold() not in _LOCAL_STATUS:
            _fail("PORT_REQUEST_INVALID")
        value: dict[str, Any] = {
            "id": item.get("id"), "name": item.get("name"),
            "status": _LOCAL_STATUS[status.casefold()],
            "duration_ms": item.get("duration_ms", 0), "source_ref": reference,
        }
        for key in ("suite", "start_ms", "failure_code"):
            if key in item:
                value[key] = item[key]
        projected.append(validate_reporting_result(value))
    if len({item["id"] for item in projected}) != len(projected):
        _fail("PORT_REQUEST_INVALID")
    return projected


def project_evidence(document: Any, *, source_ref: str) -> dict[str, Any]:
    """Project trusted Evidence 1.0 without free-text evidence or summaries."""

    reference = _reference(source_ref)
    if type(document) is not dict or set(document) != _LOCAL_EVIDENCE_FIELDS:
        _fail("PORT_REQUEST_INVALID")
    if document["schema_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")

    def items(key: str, fields: set[str], *, check: bool) -> list[dict[str, Any]]:
        source = document[key]
        if type(source) is not list:
            _fail("PORT_REQUEST_INVALID")
        rendered = []
        for item in source:
            if type(item) is not dict or set(item) - fields:
                _fail("PORT_REQUEST_INVALID")
            value = {
                "id": item.get("id"), "status": item.get("status"),
                "source_refs": item.get("source_refs", [reference]),
            }
            if check:
                value["type"] = item.get("type")
            rendered.append(value)
        return rendered

    return validate_reporting_evidence({
        "story_id": document.get("story_id"),
        "result": document.get("result"),
        "acceptance_criteria": items("acceptance_criteria", _LOCAL_AC_FIELDS, check=False),
        "checks": items("checks", _LOCAL_CHECK_FIELDS, check=True),
        "source_ref": reference,
    })


def stable_report_fingerprint(request: Mapping[str, Any]) -> str:
    """Return the deterministic content identity for one validated request."""

    payload = request["payload"]
    identity = {"project_id": request["project_id"], "payload": payload}
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def validate_reporting_response_data(value: Any, *, request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Reporting Response Data 1.0 against its request identity."""

    if type(value) is not dict:
        _fail("PORT_PROTOCOL_INVALID")
    _reject_sensitive_keys(value)
    if set(value) != _RESPONSE_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    payload = request["payload"]
    if value["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    if value["outcome"] not in {"generated", "failed"}:
        _fail("PORT_PROTOCOL_INVALID")
    if value["report_id"] != payload["report_id"] or value["output_ref"] != payload["output_ref"]:
        _fail("PORT_PROTOCOL_INVALID")
    artifacts = value["artifact_refs"]
    if type(artifacts) is not list or len(artifacts) > MAX_ARTIFACTS:
        _fail("PORT_PROTOCOL_INVALID")
    if artifacts != sorted(set(artifacts)):
        _fail("PORT_PROTOCOL_INVALID")
    prefix = f"{payload['output_ref']}/"
    if any(not _reference(item, "PORT_PROTOCOL_INVALID").startswith(prefix) for item in artifacts):
        _fail("PORT_PROTOCOL_INVALID")
    total = value["total_results"]
    if type(total) is not int or total != len(payload["results"]) + 1:
        _fail("PORT_PROTOCOL_INVALID")
    counts = value["status_counts"]
    if type(counts) is not dict or set(counts) != set(STATUS_ORDER):
        _fail("PORT_PROTOCOL_INVALID")
    if any(type(item) is not int or item < 0 for item in counts.values()) or sum(counts.values()) != total:
        _fail("PORT_PROTOCOL_INVALID")
    expected_counts = {status: 0 for status in STATUS_ORDER}
    for item in payload["results"]:
        expected_counts[item["status"]] += 1
    evidence_status = {
        "pass": "passed", "fail": "failed", "blocked": "skipped",
    }[payload["evidence"]["result"]]
    expected_counts[evidence_status] += 1
    if counts != expected_counts:
        _fail("PORT_PROTOCOL_INVALID")
    if type(value["reused"]) is not bool:
        _fail("PORT_PROTOCOL_INVALID")
    failures = value["failures"]
    if type(failures) is not list or len(failures) > MAX_FAILURES:
        _fail("PORT_PROTOCOL_INVALID")
    rendered: list[dict[str, Any]] = []
    identities: set[tuple[str, str, bool]] = set()
    for failure in failures:
        if type(failure) is not dict or set(failure) != _FAILURE_FIELDS:
            _fail("PORT_PROTOCOL_INVALID")
        code = _identifier(failure["code"], "PORT_PROTOCOL_INVALID")
        message = _safe_text(failure["message"], 512, "PORT_PROTOCOL_INVALID")
        retryable = failure["retryable"]
        if type(retryable) is not bool or retryable is not (
            code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}
        ):
            _fail("PORT_PROTOCOL_INVALID")
        identity = (code, message, retryable)
        if identity not in identities:
            identities.add(identity)
            rendered.append({"code": code, "message": message, "retryable": retryable})
    if value["outcome"] == "generated" and rendered:
        _fail("PORT_PROTOCOL_INVALID")
    if value["outcome"] == "failed" and (not rendered or value["reused"]):
        _fail("PORT_PROTOCOL_INVALID")
    result = copy.deepcopy(value)
    result["failures"] = rendered
    if len(_canonical_bytes(result, "PORT_PROTOCOL_INVALID")) > MAX_PAYLOAD_BYTES:
        _fail("PORT_PROTOCOL_INVALID")
    return result


def _port_failure(request: Any, code: str, *, called: bool) -> dict[str, Any]:
    source = request if type(request) is dict else {}
    rejected = code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID", "PORT_PROTOCOL_INVALID",
        "PORT_PAYLOAD_UNSAFE",
    }
    retryable = called and code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": source.get("request_id") if type(source.get("request_id")) is str and _IDENTIFIER.fullmatch(source["request_id"]) else "invalid",
        "project_id": source.get("project_id") if type(source.get("project_id")) is str and _IDENTIFIER.fullmatch(source["project_id"]) else "invalid",
        "port": REPORTING_PORT, "operation": REPORTING_OPERATION,
        "capability": REPORTING_CAPABILITY, "ok": False,
        "status": "rejected" if rejected else "degraded",
        "action": "reject" if rejected else "degraded", "called": called,
        "data": {},
        "error": {"code": code, "message": "Reporting operation did not complete", "retryable": retryable, "details": {}},
    }


def invoke_reporting(
    request: Any, capability_document: Any,
    adapter_call: Callable[[dict[str, Any], float], Any] | None, *,
    data_dir: str, dry_run: bool, timeout_seconds: float = 120.0,
    dry_run_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preflight and validate concrete adapter data without inventing artifacts."""

    if (
        type(dry_run) is not bool
        or (dry_run_data is not None and not isinstance(dry_run_data, Mapping))
        or type(timeout_seconds) not in {int, float}
    ):
        return _port_failure(request, "PORT_REQUEST_INVALID", called=False)
    if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= 300:
        return _port_failure(request, "PORT_REQUEST_INVALID", called=False)
    try:
        checked = validate_reporting_request(request, data_dir=data_dir)
    except IntegrationPortContractError as exc:
        return _port_failure(request, exc.code, called=False)
    response = invoke_port(
        checked, capability_document, adapter_call,
        write=not dry_run, timeout_seconds=float(timeout_seconds),
    )
    if not response.get("ok"):
        return response
    if dry_run:
        if dry_run_data is None:
            return _port_failure(checked, "PORT_REQUEST_INVALID", called=False)
        try:
            preview = validate_reporting_response_data(dry_run_data, request=checked)
        except IntegrationPortContractError as exc:
            code = exc.code if exc.code == "PORT_PAYLOAD_UNSAFE" else "PORT_PROTOCOL_INVALID"
            return _port_failure(checked, code, called=False)
        response["data"] = preview
        return response
    try:
        response["data"] = validate_reporting_response_data(response.get("data"), request=checked)
    except IntegrationPortContractError as exc:
        code = exc.code if exc.code in {"PORT_VERSION_UNSUPPORTED", "PORT_PAYLOAD_UNSAFE"} else "PORT_PROTOCOL_INVALID"
        return _port_failure(checked, code, called=True)
    return response
