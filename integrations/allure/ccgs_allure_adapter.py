#!/usr/bin/env python3
"""Convert CCGS automated test results and Closeout Evidence to Allure results."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = "1.0"
ADAPTER_VERSION = "1.0"
ALLURE_STATUS_ORDER = ("passed", "failed", "broken", "skipped", "unknown")
REPORT_RESULT_FIELDS = {"id", "name", "status", "duration_ms", "source_ref"}
REPORT_RESULT_OPTIONAL_FIELDS = {"suite", "start_ms", "failure_code"}
REPORT_EVIDENCE_FIELDS = {
    "story_id", "result", "acceptance_criteria", "checks", "source_ref",
}
REPORT_AC_FIELDS = {"id", "status", "source_refs"}
REPORT_CHECK_FIELDS = {"id", "type", "status", "source_refs"}
REPORT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REPORT_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)
NORMALIZED_STATUS = {
    "pass": "passed",
    "passed": "passed",
    "fail": "failed",
    "failed": "failed",
    "error": "broken",
    "broken": "broken",
    "skip": "skipped",
    "skipped": "skipped",
    "deferred": "skipped",
    "blocked": "skipped",
    "unknown": "unknown",
}
NAMESPACE = uuid.UUID("c47fc951-1b3c-45db-aec1-94be0cf6b2a8")


class AllureAdapterError(ValueError):
    """Raised when an Allure export input or target violates the contract."""


@dataclass(frozen=True)
class AllureBundle:
    """An immutable set of files for one Allure launch."""

    files: dict[str, bytes]
    summary: dict[str, Any]


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _step(
    name: str,
    status: str,
    message: str,
    start_ms: int,
) -> dict[str, Any]:
    result = {
        "name": name,
        "status": status,
        "stage": "finished",
        "start": start_ms,
        "stop": start_ms,
        "steps": [],
        "attachments": [],
        "parameters": [],
    }
    if message:
        result["statusDetails"] = {"message": message, "trace": ""}
    return result


def _categories() -> list[dict[str, Any]]:
    return [
        {
            "name": "CCGS Evidence failures",
            "messageRegex": ".*\\[CCGS Evidence\\].*",
            "matchedStatuses": ["failed", "broken"],
        },
        {
            "name": "Infrastructure problems",
            "messageRegex": ".*(timeout|infrastructure|configuration|protocol).*",
            "matchedStatuses": ["broken"],
        },
    ]


def _report_identifier(value: Any, label: str) -> str:
    if type(value) is not str or REPORT_IDENTIFIER_RE.fullmatch(value) is None:
        raise AllureAdapterError(f"{label} must be a stable identifier")
    return value


def _report_display(value: Any, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise AllureAdapterError(f"{label} must contain 1-256 characters")
    if any(character in value for character in "\r\n\x00"):
        raise AllureAdapterError(f"{label} contains unsupported characters")
    return value


def _report_reference(value: Any, label: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 512
        or REPORT_REFERENCE_RE.fullmatch(value) is None
    ):
        raise AllureAdapterError(f"{label} must be a safe project-relative reference")
    return value


def _report_references(value: Any, label: str) -> list[str]:
    if type(value) is not list:
        raise AllureAdapterError(f"{label} must be an array")
    references = [
        _report_reference(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(references)) != len(references):
        raise AllureAdapterError(f"{label} must be unique")
    return references


def _neutral_test_result(report_id: str, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    identity = _report_identifier(item["id"], "result.id")
    result_uuid = str(uuid.uuid5(NAMESPACE, f"report:{report_id}:test:{identity}"))
    status = item["status"]
    source_ref = _report_reference(item["source_ref"], "result.source_ref")
    suite = _report_display(item.get("suite", "Automated Results"), "result.suite")
    start_ms = item.get("start_ms", 0)
    duration_ms = item["duration_ms"]
    result: dict[str, Any] = {
        "uuid": result_uuid,
        "historyId": _stable_id(f"report-history:{identity}"),
        "testCaseId": _stable_id(f"report-case:{identity}"),
        "fullName": f"report.tests.{identity}",
        "name": _report_display(item["name"], "result.name"),
        "links": [],
        "labels": [
            {"name": "parentSuite", "value": "Reporting"},
            {"name": "suite", "value": suite},
            {"name": "framework", "value": "neutral"},
        ],
        "parameters": [{"name": "source_ref", "value": source_ref, "excluded": True}],
        "attachments": [],
        "status": status,
        "stage": "finished",
        "start": start_ms,
        "stop": start_ms + duration_ms,
        "steps": [],
    }
    failure_code = item.get("failure_code")
    if failure_code:
        result["statusDetails"] = {
            "known": False,
            "muted": False,
            "flaky": False,
            "message": f"[REPORT_FAILURE] {_report_identifier(failure_code, 'result.failure_code')}",
            "trace": "",
        }
    return f"{result_uuid}-result.json", result


def _neutral_evidence_result(
    report_id: str, evidence: dict[str, Any], files: dict[str, bytes],
) -> tuple[str, dict[str, Any]]:
    story_id = _report_identifier(evidence["story_id"], "evidence.story_id")
    result_uuid = str(uuid.uuid5(NAMESPACE, f"report:{report_id}:evidence:{story_id}"))
    evidence_status = {"pass": "passed", "fail": "failed", "blocked": "skipped"}[
        evidence["result"]
    ]
    steps: list[dict[str, Any]] = []
    for collection, kind in ((evidence["acceptance_criteria"], "criterion"), (evidence["checks"], "check")):
        for item in collection:
            identity = _report_identifier(item["id"], f"evidence.{kind}.id")
            step_status = NORMALIZED_STATUS.get(str(item["status"]), "unknown")
            step_name = identity if kind == "criterion" else f"{identity}: {_report_identifier(item['type'], 'evidence.check.type')}"
            steps.append(_step(step_name, step_status, "", 0))
    attachment_source = f"{uuid.uuid5(NAMESPACE, f'report:{report_id}:evidence-attachment:{story_id}')}-attachment.json"
    files[attachment_source] = _json_bytes(evidence)
    result = {
        "uuid": result_uuid,
        "historyId": _stable_id(f"report-history:evidence:{story_id}"),
        "testCaseId": _stable_id(f"report-case:evidence:{story_id}"),
        "fullName": f"report.evidence.{story_id}",
        "name": f"{story_id} Evidence",
        "links": [],
        "labels": [
            {"name": "parentSuite", "value": "Reporting"},
            {"name": "suite", "value": "Evidence"},
            {"name": "framework", "value": "neutral-evidence"},
        ],
        "parameters": [{
            "name": "source_ref",
            "value": _report_reference(evidence["source_ref"], "evidence.source_ref"),
            "excluded": True,
        }],
        "attachments": [{
            "name": "Neutral Evidence",
            "source": attachment_source,
            "type": "application/json",
        }],
        "status": evidence_status,
        "stage": "finished",
        "start": 0,
        "stop": 0,
        "steps": steps,
    }
    return f"{result_uuid}-result.json", result


def _validate_neutral_evidence(evidence: Any) -> dict[str, Any]:
    if type(evidence) is not dict or set(evidence) != REPORT_EVIDENCE_FIELDS:
        raise AllureAdapterError("evidence must use the neutral reporting fields")
    _report_identifier(evidence["story_id"], "evidence.story_id")
    if type(evidence["result"]) is not str or evidence["result"] not in {"pass", "fail", "blocked"}:
        raise AllureAdapterError("evidence.result is invalid")
    _report_reference(evidence["source_ref"], "evidence.source_ref")
    for key, fields in (("acceptance_criteria", REPORT_AC_FIELDS), ("checks", REPORT_CHECK_FIELDS)):
        items = evidence[key]
        if type(items) is not list or len(items) > 200:
            raise AllureAdapterError(f"evidence.{key} must contain at most 200 items")
        identities: set[str] = set()
        for index, item in enumerate(items):
            if type(item) is not dict or set(item) != fields:
                raise AllureAdapterError(f"evidence.{key}[{index}] uses unsupported fields")
            identity = _report_identifier(item["id"], f"evidence.{key}[{index}].id")
            if identity in identities:
                raise AllureAdapterError(f"evidence.{key} identities must be unique")
            identities.add(identity)
            if key == "checks":
                _report_identifier(item["type"], f"evidence.{key}[{index}].type")
            if type(item["status"]) is not str or item["status"] not in {"pass", "fail", "deferred"}:
                raise AllureAdapterError(f"evidence.{key}[{index}].status is invalid")
            _report_references(item["source_refs"], f"evidence.{key}[{index}].source_refs")
    return json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))


def build_neutral_allure_bundle(
    report_id: str, results: Sequence[dict[str, Any]], evidence: dict[str, Any],
) -> AllureBundle:
    """Build a deterministic Allure bundle from public neutral reporting data only."""

    report_id = _report_identifier(report_id, "report_id")
    if type(results) is not list or not 1 <= len(results) <= 4_999:
        raise AllureAdapterError("results must contain 1-4999 neutral items")
    neutral_evidence = _validate_neutral_evidence(evidence)
    files: dict[str, bytes] = {}
    statuses = {status: 0 for status in ALLURE_STATUS_ORDER}
    identities: set[str] = set()
    for index, raw in enumerate(results):
        if type(raw) is not dict:
            raise AllureAdapterError(f"results[{index}] must be an object")
        fields = set(raw)
        if not REPORT_RESULT_FIELDS <= fields or fields - REPORT_RESULT_FIELDS - REPORT_RESULT_OPTIONAL_FIELDS:
            raise AllureAdapterError(f"results[{index}] uses unsupported fields")
        identity = _report_identifier(raw["id"], f"results[{index}].id")
        if identity in identities:
            raise AllureAdapterError("result identities must be unique")
        identities.add(identity)
        if type(raw["status"]) is not str or raw["status"] not in ALLURE_STATUS_ORDER:
            raise AllureAdapterError(f"results[{index}].status is invalid")
        for numeric in ("duration_ms", "start_ms"):
            value = raw.get(numeric, 0)
            if type(value) is not int or value < 0:
                raise AllureAdapterError(f"results[{index}].{numeric} must be non-negative")
        if "failure_code" in raw:
            _report_identifier(raw["failure_code"], f"results[{index}].failure_code")
        path, result = _neutral_test_result(report_id, raw)
        files[path] = _json_bytes(result)
        statuses[result["status"]] += 1
    evidence_path, evidence_result = _neutral_evidence_result(
        report_id, neutral_evidence, files,
    )
    files[evidence_path] = _json_bytes(evidence_result)
    statuses[evidence_result["status"]] += 1
    files["categories.json"] = _json_bytes(_categories())
    return AllureBundle(
        files=dict(sorted(files.items())),
        summary={
            "schema_version": SCHEMA_VERSION,
            "adapter": "allure",
            "adapter_version": ADAPTER_VERSION,
            "report_id": report_id,
            "total_results": len(results) + 1,
            "statuses": statuses,
        },
    )


def _directory_matches(target: Path, files: dict[str, bytes]) -> bool:
    entries = list(target.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise AllureAdapterError(
            "neutral report output contains unsupported symbolic links"
        )
    existing = sorted(
        path.relative_to(target).as_posix() for path in entries if path.is_file()
    )
    if existing != sorted(files):
        return False
    return all((target / relative).read_bytes() == content for relative, content in files.items())


def validate_neutral_allure_target_path(target: Path) -> Path:
    """Resolve a report target only after rejecting every symbolic path component."""

    candidate = target.absolute()
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise AllureAdapterError(
                "neutral report output path contains unsupported symbolic links"
            )
    return candidate.resolve(strict=False)


def preflight_neutral_allure_target(target: Path, bundle: AllureBundle) -> bool:
    """Read-only check for immutable neutral report reuse or conflict.

    ``False`` means the target does not exist and a later authorized write may
    publish it.  ``True`` means every existing byte matches and the report can
    be reused.  Any other existing target is a stable, non-destructive conflict.
    """

    target = validate_neutral_allure_target_path(target)
    if not target.exists():
        return False
    if target.is_dir() and _directory_matches(target, bundle.files):
        return True
    raise AllureAdapterError("neutral report output conflicts with existing content")


def _write_allure_bundle(target: Path, bundle: AllureBundle) -> bool:
    """Atomically create one immutable run directory."""

    target = validate_neutral_allure_target_path(target)
    if target.exists():
        if target.is_dir() and _directory_matches(target, bundle.files):
            return False
        raise AllureAdapterError(
            "Allure output already exists with different content; use a unique run_id"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    )
    try:
        for relative, content in bundle.files.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return True


def write_neutral_allure_bundle(target: Path, bundle: AllureBundle) -> bool:
    """Atomically publish or idempotently reuse one neutral Allure bundle."""

    return _write_allure_bundle(target, bundle)
