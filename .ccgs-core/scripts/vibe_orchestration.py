#!/usr/bin/env python3
"""Versioned orchestration data contracts over Integration Port 1.0.

This module owns only public boundary validation and result projection.  Story
parsing, Evidence validation, lifecycle policy, and Closeout writes remain in
the core CLI invoked by an adapter.
"""

from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from vibe_integration_ports import (
    CONTRACT_VERSION,
    IntegrationPortContractError,
    invoke_port,
    validate_port_request,
    validate_port_response,
)


ORCHESTRATION_CAPABILITIES = frozenset({"story_check", "story_closeout"})
MAX_CHECKS = 16
MAX_FAILURES = 64
MAX_FAILURE_MESSAGE = 512
_PAYLOAD_FIELDS = {"contract_version", "action", "story", "evidence"}
_RESPONSE_FIELDS = {
    "contract_version", "action", "outcome", "story", "evidence", "checks",
    "closeout_applied", "failures",
}
_CHECK_FIELDS = {"name", "status", "attempt_count", "summary"}
_FAILURE_FIELDS = {"code", "message", "retryable"}
_SUMMARY_FIELDS = {
    "pass", "warn", "error", "info", "read_only", "engine_agnostic",
    "valid", "error_count", "verdict", "failure_count", "written",
}
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHELL_CHARS = frozenset("&|<>^%!\"'`$;(){}[]\r\n\0")


def _fail(code: str) -> None:
    raise IntegrationPortContractError(code)


def _validate_data_dir(value: Any) -> str:
    """Validate the trusted worker data-directory configuration."""

    if type(value) is not str or _SAFE_SEGMENT.fullmatch(value) is None:
        _fail("PORT_REQUEST_INVALID")
    return value


def _validate_path(value: Any, *, kind: str, data_dir: str) -> str:
    if type(value) is not str or not value or len(value) > 1024:
        _fail("PORT_REQUEST_INVALID")
    if (
        value.startswith(("/", "\\", "~/"))
        or value.casefold().startswith("file:")
        or re.match(r"^[A-Za-z]:", value)
        or "\\" in value
        or any(character in value for character in _SHELL_CHARS)
    ):
        _fail("PORT_PAYLOAD_UNSAFE")
    raw_parts = value.split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        _fail("PORT_PAYLOAD_UNSAFE")
    parts = PurePosixPath(value).parts
    if any(_SAFE_SEGMENT.fullmatch(part) is None for part in parts):
        _fail("PORT_REQUEST_INVALID")
    expected = ("production", "epics") if kind == "story" else (
        "production", "qa", "evidence",
    )
    # ``data_dir`` is trusted Worker configuration, never public Port payload.
    # Binding the first segment here keeps a sibling production tree from
    # reaching either the adapter or the core CLI.
    if (
        len(parts) <= len(expected) + 1
        or parts[0] != data_dir
        or tuple(parts[1:1 + len(expected)]) != expected
    ):
        _fail("PORT_REQUEST_INVALID")
    suffix = ".md" if kind == "story" else ".json"
    if not parts[-1].endswith(suffix):
        _fail("PORT_REQUEST_INVALID")
    return value


def orchestration_request_envelope(
    *, request_id: str, project_id: str, action: str, story: str,
    evidence: str | None = None,
) -> dict[str, Any]:
    """Create an unvalidated Integration Port envelope for boundary dispatch."""

    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "action": action,
        "story": story,
    }
    if evidence not in {None, ""}:
        payload["evidence"] = evidence
    references = [story]
    if evidence not in {None, ""}:
        references.append(str(evidence))
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "project_id": project_id,
        "port": "orchestration",
        "operation": "trigger",
        "capability": action,
        "payload": payload,
        "references": references,
    }


def build_orchestration_request(
    *, request_id: str, project_id: str, action: str, story: str,
    data_dir: str, evidence: str | None = None,
) -> dict[str, Any]:
    """Build and validate an Orchestration Request Data 1.0 envelope."""

    return validate_orchestration_request(orchestration_request_envelope(
        request_id=request_id,
        project_id=project_id,
        action=action,
        story=story,
        evidence=evidence,
    ), data_dir=data_dir)


def validate_orchestration_request(
    request: Mapping[str, Any], *, data_dir: str,
) -> dict[str, Any]:
    """Validate capability binding, exact payload fields, and safe references."""

    configured_data_dir = _validate_data_dir(data_dir)
    request_copy = validate_port_request(request)
    if (
        request_copy["port"] != "orchestration"
        or request_copy["operation"] != "trigger"
        or request_copy["capability"] not in ORCHESTRATION_CAPABILITIES
    ):
        _fail("PORT_REQUEST_INVALID")
    payload = request_copy["payload"]
    if type(payload) is not dict or not set(payload) <= _PAYLOAD_FIELDS:
        _fail("PORT_REQUEST_INVALID")
    required = {"contract_version", "action", "story"}
    if not required <= set(payload) or len(payload) not in {3, 4}:
        _fail("PORT_REQUEST_INVALID")
    if payload["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    if payload["action"] != request_copy["capability"]:
        _fail("PORT_REQUEST_INVALID")
    story = _validate_path(
        payload["story"], kind="story", data_dir=configured_data_dir,
    )
    evidence = payload.get("evidence")
    expected_references = [story]
    if "evidence" in payload:
        expected_references.append(_validate_path(
            evidence, kind="evidence", data_dir=configured_data_dir,
        ))
    if request_copy["references"] != expected_references:
        _fail("PORT_REQUEST_INVALID")
    return request_copy


def _validate_check(item: Any) -> None:
    if type(item) is not dict or set(item) != _CHECK_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    if item["name"] not in {"doctor", "evidence_validate", "closeout_dry_run", "closeout_write"}:
        _fail("PORT_PROTOCOL_INVALID")
    if item["status"] not in {"passed", "failed", "error"}:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["attempt_count"]) is not int or not 1 <= item["attempt_count"] <= 5:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["summary"]) is not dict or len(item["summary"]) > 8:
        _fail("PORT_PROTOCOL_INVALID")
    if not set(item["summary"]) <= _SUMMARY_FIELDS:
        _fail("PORT_PAYLOAD_UNSAFE")
    for key, value in item["summary"].items():
        if key in {"pass", "warn", "error", "info", "error_count", "failure_count"}:
            if type(value) is not int or value < 0:
                _fail("PORT_PROTOCOL_INVALID")
        elif key in {"read_only", "engine_agnostic", "valid", "written"}:
            if type(value) is not bool:
                _fail("PORT_PROTOCOL_INVALID")
        elif key == "verdict" and value not in {"pass", "fail"}:
            _fail("PORT_PROTOCOL_INVALID")


def _validate_failure(item: Any) -> tuple[str, str, bool]:
    if type(item) is not dict or set(item) != _FAILURE_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    code, message, retryable = item["code"], item["message"], item["retryable"]
    if type(code) is not str or _IDENTIFIER.fullmatch(code) is None:
        _fail("PORT_PROTOCOL_INVALID")
    if type(message) is not str or not 1 <= len(message) <= MAX_FAILURE_MESSAGE:
        _fail("PORT_PROTOCOL_INVALID")
    if type(retryable) is not bool:
        _fail("PORT_PROTOCOL_INVALID")
    return code, message, retryable


def validate_orchestration_data(
    request: Mapping[str, Any], data: Any, *, data_dir: str,
) -> dict[str, Any]:
    """Validate bounded Orchestration Response Data 1.0 against its request."""

    configured_data_dir = _validate_data_dir(data_dir)
    request_copy = validate_orchestration_request(
        request, data_dir=configured_data_dir,
    )
    if type(data) is not dict or set(data) != _RESPONSE_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    normalized = copy.deepcopy(data)
    payload = request_copy["payload"]
    if normalized["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    if normalized["action"] != payload["action"]:
        _fail("PORT_PROTOCOL_INVALID")
    if normalized["outcome"] not in {"passed", "failed", "error"}:
        _fail("PORT_PROTOCOL_INVALID")
    if normalized["story"] != payload["story"]:
        _fail("PORT_PROTOCOL_INVALID")
    _validate_path(
        normalized["story"], kind="story", data_dir=configured_data_dir,
    )
    _validate_path(
        normalized["evidence"], kind="evidence", data_dir=configured_data_dir,
    )
    if "evidence" in payload and normalized["evidence"] != payload["evidence"]:
        _fail("PORT_PROTOCOL_INVALID")
    checks = normalized["checks"]
    if type(checks) is not list or not 1 <= len(checks) <= MAX_CHECKS:
        _fail("PORT_PROTOCOL_INVALID")
    for item in checks:
        _validate_check(item)
    failures = normalized["failures"]
    if type(failures) is not list or len(failures) > MAX_FAILURES:
        _fail("PORT_PROTOCOL_INVALID")
    identities = [_validate_failure(item) for item in failures]
    if len(set(identities)) != len(identities):
        _fail("PORT_PROTOCOL_INVALID")
    if any(retryable for _, _, retryable in identities):
        # Adapter transport failures are represented by the outer Integration
        # Port envelope.  Completed orchestration data contains business/core
        # outcomes only and can therefore never request another retry.
        _fail("PORT_PROTOCOL_INVALID")
    if type(normalized["closeout_applied"]) is not bool:
        _fail("PORT_PROTOCOL_INVALID")
    if payload["action"] == "story_check" and normalized["closeout_applied"]:
        _fail("PORT_PROTOCOL_INVALID")
    if normalized["outcome"] == "passed" and failures:
        _fail("PORT_PROTOCOL_INVALID")
    probe = {
        **{key: request_copy[key] for key in (
            "contract_version", "request_id", "project_id", "port", "operation", "capability",
        )},
        "ok": True,
        "status": "success",
        "action": "invoke",
        "called": True,
        "data": normalized,
        "error": None,
    }
    validate_port_response(request_copy, probe)
    return normalized


def _called_rejection(request: Mapping[str, Any], code: str) -> dict[str, Any]:
    safe_code = "PORT_PAYLOAD_UNSAFE" if code == "PORT_PAYLOAD_UNSAFE" else "PORT_PROTOCOL_INVALID"
    return {
        **{key: request[key] for key in (
            "contract_version", "request_id", "project_id", "port", "operation", "capability",
        )},
        "ok": False,
        "status": "rejected",
        "action": "reject",
        "called": True,
        "data": {},
        "error": {
            "code": safe_code,
            "message": "Integration port operation did not complete",
            "retryable": False,
            "details": {},
        },
    }


def _not_called_rejection(request: Mapping[str, Any], code: str) -> dict[str, Any]:
    safe = code if code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID", "PORT_PAYLOAD_UNSAFE",
    } else "PORT_REQUEST_INVALID"
    return {
        **{key: request[key] for key in (
            "contract_version", "request_id", "project_id", "port", "operation", "capability",
        )},
        "ok": False,
        "status": "rejected",
        "action": "reject",
        "called": False,
        "data": {},
        "error": {
            "code": safe,
            "message": "Integration port operation did not complete",
            "retryable": False,
            "details": {},
        },
    }


def invoke_orchestration(
    request: Mapping[str, Any], capability_document: Any,
    adapter: Callable[[dict[str, Any], float], dict[str, Any]] | None,
    *, data_dir: str, dry_run: bool = False, timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Invoke one orchestration adapter after complete request preflight."""

    try:
        request_copy = validate_orchestration_request(request, data_dir=data_dir)
    except IntegrationPortContractError as exc:
        try:
            generic = validate_port_request(request)
        except IntegrationPortContractError:
            return invoke_port(
                request, capability_document, adapter, write=not dry_run,
                timeout_seconds=timeout_seconds,
            )
        return _not_called_rejection(generic, exc.code)
    response = invoke_port(
        request_copy, capability_document, adapter, write=not dry_run,
        timeout_seconds=timeout_seconds,
    )
    if response["ok"] and response["called"]:
        try:
            response["data"] = validate_orchestration_data(
                request_copy, response["data"], data_dir=data_dir,
            )
        except IntegrationPortContractError as exc:
            response = _called_rejection(request_copy, exc.code)
    return response
