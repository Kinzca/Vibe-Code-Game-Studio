#!/usr/bin/env python3
"""Windmill implementation of Orchestration Port 1.0.

Machine roots and runner configuration are injected by the worker.  They are
never copied into the public request or response.  All Story, Evidence, policy,
and Closeout decisions remain owned by the fixed core CLI operations delegated
to :mod:`ccgs_windmill_adapter`.
"""

from __future__ import annotations

import copy
import hashlib
import re
import subprocess
import time
from typing import Any, Callable

from ccgs_windmill_adapter import run_story_check, run_story_closeout
from vibe_orchestration import validate_orchestration_request


CAPABILITY_DOCUMENT = {
    "contract_version": "1.0",
    "adapter_id": "windmill-orchestration-1",
    "capabilities": [
        {
            "port": "orchestration",
            "operation": "trigger",
            "capability": "story_check",
            "contract_versions": ["1.0"],
        },
        {
            "port": "orchestration",
            "operation": "trigger",
            "capability": "story_closeout",
            "contract_versions": ["1.0"],
        },
    ],
}

_CHECK_NAMES = {
    "doctor": "doctor",
    "evidence-validate": "evidence_validate",
    "closeout": "closeout_dry_run",
}
_SAFE_CODE = re.compile(r"[^A-Za-z0-9._:-]+")
_UNSAFE_MESSAGE = re.compile(
    r"(?i)(?:^|\s)(?:/\S+|[A-Za-z]:[\\/]\S*|\\\\\S+|~/\S+|file:)|"
    r"(?:secret|token|password|credential|authorization|api[_-]?key)\s*[:=]"
)
_SHELL_MESSAGE_CHARS = frozenset("&|<>^%!\"'`$;{}[]\r\n\0")


def windmill_capability_document() -> dict[str, Any]:
    """Return an isolated Capability Document 1.0."""

    return copy.deepcopy(CAPABILITY_DOCUMENT)


def stable_request_id(
    project_id: str, action: str, story: str, evidence: str = "",
) -> str:
    """Return a deterministic identifier reused by Windmill retries."""

    material = "\0".join((project_id, action, story, evidence)).encode("utf-8")
    return f"wm-{hashlib.sha256(material).hexdigest()[:24]}"


def _transport_failure(result: dict[str, Any]) -> str | None:
    """Return ``timeout``/``unavailable`` for exhausted runner transport."""

    commands = result.get("commands")
    if not isinstance(commands, list):
        return None
    for command in commands:
        if not isinstance(command, dict) or not command.get("retryable"):
            continue
        attempts = command.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return "unavailable"
        final = attempts[-1]
        if isinstance(final, dict) and final.get("outcome") == "timeout":
            return "timeout"
        return "unavailable"
    return None


def _has_protocol_failure(result: dict[str, Any]) -> bool:
    commands = result.get("commands")
    if not isinstance(commands, list):
        return False
    return any(
        isinstance(attempt, dict) and attempt.get("outcome") == "protocol-error"
        for command in commands if isinstance(command, dict)
        for attempt in command.get("attempts", []) if isinstance(command.get("attempts"), list)
    )


def _check_summary(command: dict[str, Any], *, is_write: bool) -> dict[str, Any]:
    """Project one CLI invocation onto non-sensitive public scalar fields."""

    payload = command.get("payload")
    summary: dict[str, Any] = {}
    if command.get("command") == "doctor" and isinstance(payload, dict):
        counts = payload.get("summary")
        if isinstance(counts, dict):
            for key in ("pass", "warn", "error", "info"):
                if type(counts.get(key)) is int:
                    summary[key] = counts[key]
        for key in ("read_only", "engine_agnostic"):
            if type(payload.get(key)) is bool:
                summary[key] = payload[key]
    elif command.get("command") == "evidence-validate" and isinstance(payload, dict):
        if type(payload.get("valid")) is bool:
            summary["valid"] = payload["valid"]
        errors = payload.get("errors")
        if isinstance(errors, list):
            summary["error_count"] = len(errors)
    elif command.get("command") == "closeout" and isinstance(payload, dict):
        verdict = payload.get("verdict")
        if verdict in {"pass", "fail"}:
            summary["verdict"] = verdict
        failures = payload.get("failures")
        if isinstance(failures, list):
            summary["failure_count"] = len(failures)
        if is_write and type(payload.get("written")) is bool:
            summary["written"] = payload["written"]
    return summary


def _checks(result: dict[str, Any]) -> list[dict[str, Any]]:
    commands = result.get("commands")
    if not isinstance(commands, list):
        return []
    projected: list[dict[str, Any]] = []
    closeout_seen = 0
    for command in commands:
        if not isinstance(command, dict):
            continue
        raw_name = command.get("command")
        name = _CHECK_NAMES.get(raw_name)
        is_write = raw_name == "closeout" and closeout_seen > 0
        if raw_name == "closeout":
            closeout_seen += 1
            if is_write:
                name = "closeout_write"
        if name is None:
            continue
        status = command.get("status")
        if status not in {"passed", "failed", "error"}:
            status = "error"
        attempt_count = command.get("attempt_count")
        if type(attempt_count) is not int or not 1 <= attempt_count <= 5:
            attempt_count = 1
        projected.append({
            "name": name,
            "status": status,
            "attempt_count": attempt_count,
            "summary": _check_summary(command, is_write=is_write),
        })
    return projected


def _failures(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("failures")
    safe: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool]] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = _SAFE_CODE.sub("-", str(item.get("code", "core.failed"))).strip("-.")
            code = (code or "core.failed")[:128]
            message = str(item.get("message", "")).strip()
            if (
                not message
                or len(message) > 512
                or _UNSAFE_MESSAGE.search(message)
                or any(character in message for character in _SHELL_MESSAGE_CHARS)
            ):
                message = "Core operation reported a failure"
            identity = (code, message, False)
            if identity in seen:
                continue
            seen.add(identity)
            safe.append({
                "code": code,
                "message": message,
                "retryable": False,
            })
    if result.get("status") != "passed" and not safe:
        safe.append({
            "code": "orchestration.failed",
            "message": "Core operation did not pass",
            "retryable": False,
        })
    return safe


def _response_data(
    request: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    payload = request["payload"]
    checks = _checks(result)
    if not checks:
        checks = [{
            "name": "doctor",
            "status": "error",
            "attempt_count": 1,
            "summary": {},
        }]
    outcome = result.get("status")
    if outcome not in {"passed", "failed", "error"}:
        outcome = "error"
    advance = result.get("advance")
    advance_payload = advance.get("payload") if isinstance(advance, dict) else None
    closeout_applied = bool(
        payload["action"] == "story_closeout"
        and isinstance(advance_payload, dict)
        and advance_payload.get("written") is True
    )
    return {
        "contract_version": "1.0",
        "action": payload["action"],
        "outcome": outcome,
        "story": result.get("story", payload["story"]),
        "evidence": result.get("evidence", payload.get("evidence", "")),
        "checks": checks,
        "closeout_applied": closeout_applied,
        "failures": _failures(result),
    }


def _success(request: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        **{key: request[key] for key in (
            "request_id", "project_id", "port", "operation", "capability",
        )},
        "ok": True,
        "status": "success",
        "action": "invoke",
        "called": True,
        "data": data,
        "error": None,
    }


def build_windmill_orchestration_adapter(
    framework_root: str,
    project_root: str,
    *,
    data_dir: str,
    max_attempts: int = 1,
    retry_delay_seconds: float = 0.0,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    platform: str | None = None,
    comspec: str | None = None,
) -> Callable[[dict[str, Any], float], dict[str, Any]]:
    """Build an injected adapter that invokes only the fixed core CLI sequence."""

    def adapter(request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        request = validate_orchestration_request(request, data_dir=data_dir)
        payload = request["payload"]
        common = {
            "max_attempts": max_attempts,
            "retry_delay_seconds": retry_delay_seconds,
            "timeout_seconds": timeout_seconds,
            "executor": executor,
            "sleeper": sleeper,
            "platform": platform,
            "comspec": comspec,
        }
        if payload["action"] == "story_check":
            result = run_story_check(
                framework_root,
                project_root,
                payload["story"],
                payload.get("evidence", ""),
                **common,
            )
        else:
            result = run_story_closeout(
                framework_root,
                project_root,
                payload["story"],
                payload.get("evidence", ""),
                True,
                **common,
            )
        transport = _transport_failure(result)
        if transport == "timeout":
            raise TimeoutError("Windmill worker timed out")
        if transport == "unavailable":
            raise OSError("Windmill worker unavailable")
        if _has_protocol_failure(result):
            # Returning a non-envelope lets Integration Port 1.0 classify the
            # completed adapter call as PORT_PROTOCOL_INVALID (called=true,
            # retryable=false) without exposing raw worker output.
            return {}
        return _success(request, _response_data(request, result))

    return adapter


def raise_port_error_for_windmill(response: dict[str, Any]) -> dict[str, Any]:
    """Expose only a stable retry marker; never serialize rejected payloads."""

    if response.get("ok") is True:
        return response
    error = response.get("error")
    retryable = isinstance(error, dict) and error.get("retryable") is True
    code = error.get("code", "PORT_PROTOCOL_INVALID") if isinstance(error, dict) else "PORT_PROTOCOL_INVALID"
    marker = "[CCGS_RETRYABLE]" if retryable else "[CCGS_PERMANENT]"
    raise RuntimeError(f"{marker}{code}")
