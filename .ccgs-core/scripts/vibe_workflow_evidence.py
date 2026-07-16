#!/usr/bin/env python3
"""Build deterministic Story Evidence from versioned workflow results."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Mapping, Sequence

from ccgs_story_workflow import validate_evidence


CONTRACT_VERSION = "1.0"
EVIDENCE_INPUT_INVALID = "EVIDENCE_INPUT_INVALID"
EVIDENCE_PLAN_INVALID = "EVIDENCE_PLAN_INVALID"
EVIDENCE_RESULT_INVALID = "EVIDENCE_RESULT_INVALID"

_AC_ID = re.compile(r"AC-[0-9]+")
_PLAN_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CHECK_TYPES = {
    "automated-test",
    "manual-test",
    "review",
    "build",
    "analysis",
}
_CHECK_STATUSES = {"pass", "fail", "deferred"}
_RESULT_STATUSES = {"passed", "failed", "cancelled"}
_EXIT_CATEGORIES = {
    "success",
    "command_failed",
    "start_failed",
    "timed_out",
    "cancelled",
    "policy_rejected",
}
_ERROR_BY_EXIT = {
    "command_failed": {"EXECUTION_COMMAND_FAILED"},
    "start_failed": {"EXECUTION_START_FAILED"},
    "timed_out": {"EXECUTION_TIMED_OUT"},
    "cancelled": {"EXECUTION_CANCELLED"},
    "policy_rejected": {
        "EXECUTION_NOT_AUTHORIZED",
        "EXECUTION_BOUNDARY_INVALID",
        "EXECUTION_POLICY_INVALID",
        "EXECUTION_ARTIFACT_INVALID",
    },
}
_ARTIFACT_COLLECTION_EXITS = {"success", "command_failed", "timed_out", "cancelled"}
_PLAN_FIELDS = {"contract_version", "ok", "plan_id", "step_order", "steps"}
_STEP_FIELDS = {
    "id",
    "argv",
    "depends_on",
    "working_directory",
    "environment",
    "artifacts",
    "acceptance_mapping",
}
_RESULT_FIELDS = {
    "contract_version",
    "ok",
    "plan_id",
    "step_id",
    "status",
    "exit_category",
    "exit_code",
    "duration_ms",
    "retryable",
    "stdout",
    "stderr",
    "artifacts",
}


@dataclass(frozen=True)
class EvidenceBuildError(ValueError):
    """A stable Evidence generation failure with a machine-readable report."""

    code: str
    message: str
    details: Mapping[str, Any]

    def __str__(self) -> str:
        return self.message

    def report(self) -> dict[str, Any]:
        """Return this failure through Evidence Build Contract 1.0."""

        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": copy.deepcopy(dict(self.details)),
            },
        }


def _fail(code: str, field: str, reason: str) -> None:
    messages = {
        EVIDENCE_INPUT_INVALID: "Evidence input is invalid",
        EVIDENCE_PLAN_INVALID: "workflow plan is invalid for Evidence generation",
        EVIDENCE_RESULT_INVALID: "workflow result is invalid for Evidence generation",
    }
    raise EvidenceBuildError(code, messages[code], {"field": field, "reason": reason})


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _unique_strings(value: Any, *, allow_empty: bool = True) -> bool:
    if not _is_sequence(value) or (not allow_empty and not value):
        return False
    if any(not isinstance(item, str) or not item for item in value):
        return False
    return len(set(value)) == len(value)


def _validate_criteria(story_id: Any, criteria: Any) -> tuple[str, ...]:
    if not isinstance(story_id, str) or not story_id.strip():
        _fail(EVIDENCE_INPUT_INVALID, "story_id", "NON_EMPTY_STRING_REQUIRED")
    if not _is_sequence(criteria) or not criteria:
        _fail(EVIDENCE_INPUT_INVALID, "criteria", "NON_EMPTY_SEQUENCE_REQUIRED")
    criterion_ids = tuple(criteria)
    if any(not isinstance(item, str) or _AC_ID.fullmatch(item) is None for item in criterion_ids):
        _fail(EVIDENCE_INPUT_INVALID, "criteria", "INVALID_CRITERION_ID")
    if len(set(criterion_ids)) != len(criterion_ids):
        _fail(EVIDENCE_INPUT_INVALID, "criteria", "DUPLICATE_CRITERION_ID")
    return criterion_ids


def _validate_checks(checks: Any) -> list[dict[str, str]]:
    if not _is_sequence(checks) or not checks:
        _fail(EVIDENCE_INPUT_INVALID, "checks", "NON_EMPTY_SEQUENCE_REQUIRED")
    normalized: list[dict[str, str]] = []
    identifiers: set[str] = set()
    for index, check in enumerate(checks):
        field = f"checks[{index}]"
        if not isinstance(check, Mapping) or set(check) != {"id", "type", "status", "summary"}:
            _fail(EVIDENCE_INPUT_INVALID, field, "INVALID_CHECK_STRUCTURE")
        identifier = check.get("id")
        summary = check.get("summary")
        if not isinstance(identifier, str) or not identifier.strip():
            _fail(EVIDENCE_INPUT_INVALID, f"{field}.id", "NON_EMPTY_STRING_REQUIRED")
        if identifier in identifiers:
            _fail(EVIDENCE_INPUT_INVALID, f"{field}.id", "DUPLICATE_CHECK_ID")
        if check.get("type") not in _CHECK_TYPES:
            _fail(EVIDENCE_INPUT_INVALID, f"{field}.type", "UNSUPPORTED_CHECK_TYPE")
        if check.get("status") not in _CHECK_STATUSES:
            _fail(EVIDENCE_INPUT_INVALID, f"{field}.status", "UNSUPPORTED_CHECK_STATUS")
        if not isinstance(summary, str) or not summary.strip():
            _fail(EVIDENCE_INPUT_INVALID, f"{field}.summary", "NON_EMPTY_STRING_REQUIRED")
        identifiers.add(identifier)
        normalized.append({key: str(check[key]) for key in ("id", "type", "status", "summary")})
    return normalized


def _validate_step(step: Any, index: int, criterion_ids: set[str]) -> dict[str, Any]:
    field = f"plan.steps[{index}]"
    if not isinstance(step, Mapping) or not {"id", "argv"}.issubset(step) or set(step) - _STEP_FIELDS:
        _fail(EVIDENCE_PLAN_INVALID, field, "INVALID_STEP_STRUCTURE")
    identifier = step.get("id")
    if not isinstance(identifier, str) or not identifier:
        _fail(EVIDENCE_PLAN_INVALID, f"{field}.id", "NON_EMPTY_STRING_REQUIRED")
    argv = step.get("argv")
    if not _is_sequence(argv) or not argv or any(
        not isinstance(item, str) or not item for item in argv
    ):
        _fail(EVIDENCE_PLAN_INVALID, f"{field}.argv", "NON_EMPTY_STRING_SEQUENCE_REQUIRED")
    for key in ("depends_on", "artifacts", "acceptance_mapping"):
        value = step.get(key, [])
        if not _unique_strings(value):
            _fail(EVIDENCE_PLAN_INVALID, f"{field}.{key}", "UNIQUE_STRING_SEQUENCE_REQUIRED")
    working_directory = step.get("working_directory", ".")
    if not isinstance(working_directory, str) or not working_directory:
        _fail(EVIDENCE_PLAN_INVALID, f"{field}.working_directory", "NON_EMPTY_STRING_REQUIRED")
    environment = step.get("environment", {})
    if not isinstance(environment, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        _fail(EVIDENCE_PLAN_INVALID, f"{field}.environment", "STRING_MAPPING_REQUIRED")
    unknown = [item for item in step.get("acceptance_mapping", []) if item not in criterion_ids]
    if unknown:
        _fail(EVIDENCE_PLAN_INVALID, f"{field}.acceptance_mapping", "UNKNOWN_CRITERION")
    return copy.deepcopy(dict(step))


def validate_plan_contract(
    plan: Any,
    criterion_ids: tuple[str, ...],
) -> tuple[str, list[dict[str, Any]]]:
    """Validate Plan Contract 1.0 for downstream workflow consumers."""

    if not isinstance(plan, Mapping) or set(plan) != _PLAN_FIELDS:
        _fail(EVIDENCE_PLAN_INVALID, "plan", "INVALID_PLAN_STRUCTURE")
    if plan.get("contract_version") != CONTRACT_VERSION:
        _fail(EVIDENCE_PLAN_INVALID, "plan.contract_version", "UNSUPPORTED_CONTRACT")
    if plan.get("ok") is not True:
        _fail(EVIDENCE_PLAN_INVALID, "plan.ok", "SUCCESS_PLAN_REQUIRED")
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        _fail(EVIDENCE_PLAN_INVALID, "plan.plan_id", "INVALID_PLAN_ID")
    raw_steps = plan.get("steps")
    if not _is_sequence(raw_steps) or not raw_steps:
        _fail(EVIDENCE_PLAN_INVALID, "plan.steps", "NON_EMPTY_SEQUENCE_REQUIRED")
    steps = [_validate_step(step, index, set(criterion_ids)) for index, step in enumerate(raw_steps)]
    step_ids = [step["id"] for step in steps]
    if len(set(step_ids)) != len(step_ids):
        _fail(EVIDENCE_PLAN_INVALID, "plan.steps", "DUPLICATE_STEP_ID")
    step_id_set = set(step_ids)
    for index, step in enumerate(steps):
        if any(item not in step_id_set for item in step.get("depends_on", [])):
            _fail(EVIDENCE_PLAN_INVALID, f"plan.steps[{index}].depends_on", "UNKNOWN_DEPENDENCY")
    step_order = plan.get("step_order")
    if not _unique_strings(step_order, allow_empty=False) or set(step_order) != step_id_set:
        _fail(EVIDENCE_PLAN_INVALID, "plan.step_order", "STEP_ORDER_MISMATCH")
    return plan_id, steps


def _validate_stream(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"text", "byte_count", "truncated"}
        and isinstance(value.get("text"), str)
        and isinstance(value.get("byte_count"), int)
        and not isinstance(value.get("byte_count"), bool)
        and value["byte_count"] >= 0
        and isinstance(value.get("truncated"), bool)
    )


def _artifact_id(plan_id: str, step_id: str, path: str) -> str:
    canonical = json.dumps(
        [plan_id, step_id, path],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_artifact_shape(artifact: Any, field: str) -> None:
    if (
        not isinstance(artifact, Mapping)
        or set(artifact) != {"artifact_id", "path", "present"}
        or not isinstance(artifact.get("artifact_id"), str)
        or _PLAN_ID.fullmatch(str(artifact.get("artifact_id", ""))) is None
        or not isinstance(artifact.get("path"), str)
        or not artifact.get("path")
        or not isinstance(artifact.get("present"), bool)
    ):
        _fail(EVIDENCE_RESULT_INVALID, field, "INVALID_ARTIFACT_RESULT")
    path = artifact["path"]
    windows_path = PureWindowsPath(path)
    if (
        "\\" in path
        or path.startswith("/")
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in path.split("/")
    ):
        _fail(EVIDENCE_RESULT_INVALID, field, "INVALID_ARTIFACT_PATH")


def _validate_artifacts(
    result: Mapping[str, Any],
    field: str,
    plan_id: str,
    step: Mapping[str, Any],
) -> None:
    artifacts = result.get("artifacts")
    if not _is_sequence(artifacts):
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.artifacts", "SEQUENCE_REQUIRED")
    for index, artifact in enumerate(artifacts):
        _validate_artifact_shape(artifact, f"{field}.artifacts[{index}]")

    declarations = step.get("artifacts", [])
    if result["exit_category"] not in _ARTIFACT_COLLECTION_EXITS:
        if artifacts:
            _fail(EVIDENCE_RESULT_INVALID, f"{field}.artifacts", "UNEXPECTED_ARTIFACT_RESULT")
        return
    if len(artifacts) != len(declarations):
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.artifacts", "ARTIFACT_COUNT_MISMATCH")
    for index, (artifact, path) in enumerate(zip(artifacts, declarations)):
        artifact_field = f"{field}.artifacts[{index}]"
        if artifact["path"] != path:
            _fail(EVIDENCE_RESULT_INVALID, artifact_field, "ARTIFACT_DECLARATION_MISMATCH")
        if artifact["artifact_id"] != _artifact_id(plan_id, step["id"], path):
            _fail(EVIDENCE_RESULT_INVALID, artifact_field, "ARTIFACT_ID_MISMATCH")


def _validate_result_error(result: Mapping[str, Any], field: str) -> None:
    status = result["status"]
    error = result.get("error")
    if status == "passed":
        if "error" in result:
            _fail(EVIDENCE_RESULT_INVALID, f"{field}.error", "NOT_ALLOWED_FOR_PASS")
        return
    if (
        not isinstance(error, Mapping)
        or set(error) != {"code", "message", "details"}
        or not isinstance(error.get("code"), str)
        or not error.get("code")
        or not isinstance(error.get("message"), str)
        or not error.get("message")
        or not isinstance(error.get("details"), Mapping)
    ):
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.error", "FAILURE_ERROR_REQUIRED")
    if error["code"] not in _ERROR_BY_EXIT[result["exit_category"]]:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.error.code", "EXIT_ERROR_MISMATCH")


def _validate_result_semantics(result: Mapping[str, Any], field: str) -> None:
    status = result["status"]
    exit_category = result["exit_category"]
    exit_code = result["exit_code"]
    if status == "passed" and (exit_category != "success" or exit_code != 0):
        _fail(EVIDENCE_RESULT_INVALID, field, "INCONSISTENT_SUCCESS_RESULT")
    if status == "cancelled" and (exit_category != "cancelled" or exit_code is not None):
        _fail(EVIDENCE_RESULT_INVALID, field, "INCONSISTENT_CANCELLED_RESULT")
    if status == "failed" and exit_category not in {
        "command_failed", "start_failed", "timed_out", "policy_rejected"
    }:
        _fail(EVIDENCE_RESULT_INVALID, field, "INCONSISTENT_FAILURE_RESULT")
    if exit_category == "command_failed" and exit_code in {None, 0}:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.exit_code", "NON_ZERO_INTEGER_REQUIRED")
    if exit_category not in {"success", "command_failed"} and exit_code is not None:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.exit_code", "NULL_REQUIRED")


def _validate_result(
    result: Any,
    index: int,
    plan_id: str,
    steps_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    field = f"results[{index}]"
    if not isinstance(result, Mapping):
        _fail(EVIDENCE_RESULT_INVALID, field, "INVALID_RESULT_STRUCTURE")
    allowed_fields = _RESULT_FIELDS | {"error"}
    if not _RESULT_FIELDS.issubset(result) or set(result) - allowed_fields:
        _fail(EVIDENCE_RESULT_INVALID, field, "INVALID_RESULT_STRUCTURE")
    if result.get("contract_version") != CONTRACT_VERSION:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.contract_version", "UNSUPPORTED_CONTRACT")
    if result.get("plan_id") != plan_id:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.plan_id", "PLAN_ID_MISMATCH")
    step_id = result.get("step_id")
    if not isinstance(step_id, str) or step_id not in steps_by_id:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.step_id", "UNKNOWN_STEP")
    status = result.get("status")
    if status not in _RESULT_STATUSES or result.get("ok") is not (status == "passed"):
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.status", "INCONSISTENT_STATUS")
    if result.get("exit_category") not in _EXIT_CATEGORIES:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.exit_category", "UNSUPPORTED_EXIT_CATEGORY")
    exit_category = result["exit_category"]
    exit_code = result.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.exit_code", "INTEGER_OR_NULL_REQUIRED")
    duration = result.get("duration_ms")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.duration_ms", "NON_NEGATIVE_INTEGER_REQUIRED")
    if result.get("retryable") is not False:
        _fail(EVIDENCE_RESULT_INVALID, f"{field}.retryable", "FALSE_REQUIRED")
    if not _validate_stream(result.get("stdout")) or not _validate_stream(result.get("stderr")):
        _fail(EVIDENCE_RESULT_INVALID, field, "INVALID_STREAM_RESULT")
    _validate_artifacts(result, field, plan_id, steps_by_id[step_id])
    _validate_result_semantics(result, field)
    _validate_result_error(result, field)
    return copy.deepcopy(dict(result))


def _references(plan_id: str, step_ids: Sequence[str]) -> str:
    return json.dumps(
        [{"plan_id": plan_id, "step_id": step_id} for step_id in step_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_evidence(
    story_id: str,
    criteria: Sequence[str],
    plan: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build Evidence 1.0 only from explicit Plan, Result, and Check inputs.

    The function performs no file, process, command-name, log-text, artifact-content,
    project, or engine inspection. Invalid input raises :class:`EvidenceBuildError`
    and therefore never returns a document usable by Closeout.
    """

    criterion_ids = _validate_criteria(story_id, criteria)
    normalized_checks = _validate_checks(checks)
    plan_id, steps = validate_plan_contract(plan, criterion_ids)
    if not _is_sequence(results):
        _fail(EVIDENCE_RESULT_INVALID, "results", "SEQUENCE_REQUIRED")
    steps_by_id = {step["id"]: step for step in steps}
    normalized_results = [
        _validate_result(result, index, plan_id, steps_by_id)
        for index, result in enumerate(results)
    ]
    result_by_step: dict[str, dict[str, Any]] = {}
    for result in normalized_results:
        if result["step_id"] in result_by_step:
            _fail(EVIDENCE_RESULT_INVALID, "results", "DUPLICATE_STEP_RESULT")
        result_by_step[result["step_id"]] = result

    mapped_steps = {
        criterion_id: [
            step["id"]
            for step in steps
            if criterion_id in step.get("acceptance_mapping", [])
        ]
        for criterion_id in criterion_ids
    }
    criterion_records: list[dict[str, str]] = []
    for criterion_id in criterion_ids:
        mapped = mapped_steps[criterion_id]
        provided = [result_by_step[step_id] for step_id in mapped if step_id in result_by_step]
        if any(item["status"] in {"failed", "cancelled"} for item in provided):
            status = "fail"
        elif mapped and len(provided) == len(mapped) and all(item["status"] == "passed" for item in provided):
            status = "pass"
        else:
            status = "deferred"
        criterion_records.append(
            {"id": criterion_id, "status": status, "evidence": _references(plan_id, mapped)}
        )

    statuses = [item["status"] for item in criterion_records] + [
        item["status"] for item in normalized_checks
    ]
    if "fail" in statuses:
        aggregate = "fail"
    elif statuses and all(item == "pass" for item in statuses):
        aggregate = "pass"
    else:
        aggregate = "blocked"
    evidence = {
        "schema_version": CONTRACT_VERSION,
        "story_id": story_id,
        "result": aggregate,
        "acceptance_criteria": criterion_records,
        "checks": normalized_checks,
    }
    schema_errors = validate_evidence(evidence)
    if schema_errors:
        _fail(EVIDENCE_INPUT_INVALID, "evidence", "SCHEMA_MISMATCH")
    return evidence


__all__ = [
    "CONTRACT_VERSION",
    "EVIDENCE_INPUT_INVALID",
    "EVIDENCE_PLAN_INVALID",
    "EVIDENCE_RESULT_INVALID",
    "EvidenceBuildError",
    "build_evidence",
    "validate_plan_contract",
]
