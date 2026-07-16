#!/usr/bin/env python3
"""Deterministic identities and idempotent local replay records."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

# Story 007 already owns the repository's strict Plan Contract 1.0 consumer
# validation. Reusing it here keeps Replay from defining a second Plan shape.
from vibe_workflow_evidence import EvidenceBuildError, validate_plan_contract


CONTRACT_VERSION = "1.0"
REPLAY_INPUT_INVALID = "REPLAY_INPUT_INVALID"
REPLAY_IDENTITY_CONFLICT = "REPLAY_IDENTITY_CONFLICT"
REPLAY_RECORD_INVALID = "REPLAY_RECORD_INVALID"
REPLAY_RETRY_FORBIDDEN = "REPLAY_RETRY_FORBIDDEN"
REPLAY_RETRY_EXHAUSTED = "REPLAY_RETRY_EXHAUSTED"
REPLAY_WRITE_FAILED = "REPLAY_WRITE_FAILED"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_AC_ID = re.compile(r"AC-[0-9]+")
_TRANSIENT_FAILURES = {
    "transient_transport_failure",
    "declared_worker_unavailable",
}
_TERMINAL_FAILURES = {
    "business_failure",
    "configuration_error",
    "schema_error",
    "policy_rejected",
    "path_error",
}
_FAILURE_CLASSES = _TRANSIENT_FAILURES | _TERMINAL_FAILURES
_IDENTITY_FIELDS = {
    "contract_version",
    "event_id",
    "run_id",
    "input_fingerprint",
    "plan_id",
}
_RECORD_FIELDS = _IDENTITY_FIELDS | {
    "attempt",
    "status",
    "failure_class",
    "result",
}
RecordReader = Callable[
    [Path],
    tuple[Mapping[str, Any] | None, tuple[str, str] | None],
]


@dataclass(frozen=True)
class ReplayContractError(ValueError):
    """A stable Replay Contract failure."""

    code: str
    message: str
    details: Mapping[str, Any]

    def __str__(self) -> str:
        return self.message

    def report(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": copy.deepcopy(dict(self.details)),
            },
        }


def _raise_input(field: str, reason: str) -> None:
    raise ReplayContractError(
        REPLAY_INPUT_INVALID,
        "replay input is invalid",
        {"field": field, "reason": reason},
    )


def _is_json_scalar(value: Any, field: str) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, bool, int)):
        return True
    if not isinstance(value, float):
        return False
    if not math.isfinite(value):
        _raise_input(field, "NON_FINITE_NUMBER")
    return True


def _validate_json(value: Any, field: str = "value") -> None:
    if _is_json_scalar(value, field):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise_input(field, "NON_STRING_OBJECT_KEY")
            _validate_json(item, f"{field}.{key}")
        return
    _raise_input(field, "NON_JSON_VALUE")


def _canonical_bytes(value: Any, field: str) -> bytes:
    _validate_json(value, field)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        _raise_input(field, "NON_JSON_VALUE")
    raise AssertionError("unreachable")


def _digest(value: Any, field: str = "identity") -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value, field)).hexdigest()}"


def _plan_id(plan: Any) -> str:
    if not isinstance(plan, Mapping):
        _raise_input("plan", "INVALID_PLAN_CONTRACT")
    criteria: list[str] = []
    raw_steps = plan.get("steps")
    if isinstance(raw_steps, list):
        for step in raw_steps:
            if not isinstance(step, Mapping):
                continue
            mapping = step.get("acceptance_mapping", [])
            if isinstance(mapping, list):
                criteria.extend(
                    item
                    for item in mapping
                    if isinstance(item, str) and _AC_ID.fullmatch(item)
                )
    try:
        validated_id, _ = validate_plan_contract(plan, tuple(dict.fromkeys(criteria)))
    except (EvidenceBuildError, KeyError, TypeError, ValueError):
        _raise_input("plan", "INVALID_PLAN_CONTRACT")
    return validated_id


def build_replay_identity(
    event_key: str,
    input_version: str,
    input_payload: Any,
    plan: Mapping[str, Any],
) -> dict[str, str]:
    """Build Replay Identity Contract 1.0 from versioned structured input.

    Example::

        identity = build_replay_identity("event.received", "v1", {"value": 1}, plan)
        assert identity["contract_version"] == "1.0"
    """

    if not isinstance(event_key, str) or not event_key:
        _raise_input("event_key", "NON_EMPTY_STRING_REQUIRED")
    if not isinstance(input_version, str) or not input_version:
        _raise_input("input_version", "NON_EMPTY_STRING_REQUIRED")
    plan_id = _plan_id(plan)
    input_fingerprint = _digest(
        ["replay-input", CONTRACT_VERSION, input_version, input_payload],
        "input_payload",
    )
    event_id = _digest(
        ["replay-event", CONTRACT_VERSION, event_key, input_fingerprint, plan_id]
    )
    run_id = _digest(["replay-run", CONTRACT_VERSION, event_id])
    return {
        "contract_version": CONTRACT_VERSION,
        "event_id": event_id,
        "run_id": run_id,
        "input_fingerprint": input_fingerprint,
        "plan_id": plan_id,
    }


def _validate_identity(identity: Any) -> dict[str, str]:
    if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_FIELDS:
        _raise_input("identity", "INVALID_IDENTITY_STRUCTURE")
    if identity.get("contract_version") != CONTRACT_VERSION:
        _raise_input("identity.contract_version", "UNSUPPORTED_CONTRACT")
    for field in ("event_id", "run_id", "input_fingerprint", "plan_id"):
        value = identity.get(field)
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            _raise_input(f"identity.{field}", "INVALID_DIGEST")
    expected_run = _digest(["replay-run", CONTRACT_VERSION, identity["event_id"]])
    if identity["run_id"] != expected_run:
        _raise_input("identity.run_id", "IDENTITY_MISMATCH")
    return {field: str(identity[field]) for field in _IDENTITY_FIELDS}


def _error(code: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    messages = {
        REPLAY_INPUT_INVALID: "replay input is invalid",
        REPLAY_IDENTITY_CONFLICT: "replay identity conflicts with the stored record",
        REPLAY_RECORD_INVALID: "stored replay record is invalid",
        REPLAY_RETRY_FORBIDDEN: "replay retry is forbidden for this result",
        REPLAY_RETRY_EXHAUSTED: "replay retry limit is exhausted",
        REPLAY_WRITE_FAILED: "replay record could not be written atomically",
    }
    return {"code": code, "message": messages[code], "details": dict(details or {})}


def _report(
    identity: Mapping[str, str],
    *,
    ok: bool,
    action: str,
    attempt: int,
    record: Mapping[str, Any] | None = None,
    written: bool = False,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "ok": ok,
        "action": action,
        "event_id": identity["event_id"],
        "run_id": identity["run_id"],
        "input_fingerprint": identity["input_fingerprint"],
        "plan_id": identity["plan_id"],
        "attempt": attempt,
        "written": written,
    }
    if record is not None:
        report["record"] = copy.deepcopy(dict(record))
    if error is not None:
        report["error"] = copy.deepcopy(dict(error))
    return report


def _record_digest_problem(record: Mapping[str, Any]) -> tuple[str, str] | None:
    for field in ("event_id", "run_id", "input_fingerprint", "plan_id"):
        if not isinstance(record.get(field), str) or _DIGEST.fullmatch(record[field]) is None:
            return f"record.{field}", "INVALID_DIGEST"
    return None


def _record_attempt_problem(attempt: Any) -> tuple[str, str] | None:
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= 10
    ):
        return "record.attempt", "OUT_OF_RANGE"
    return None


def _record_payload_problem(record: Mapping[str, Any]) -> tuple[str, str] | None:
    if record.get("status") not in {"succeeded", "failed"}:
        return "record.status", "UNSUPPORTED_STATUS"
    failure_class = record.get("failure_class")
    if record["status"] == "succeeded" and failure_class is not None:
        return "record.failure_class", "SUCCESS_REQUIRES_NULL"
    if record["status"] == "failed" and failure_class not in _FAILURE_CLASSES:
        return "record.failure_class", "UNSUPPORTED_FAILURE_CLASS"
    if not isinstance(record.get("result"), dict):
        return "record.result", "OBJECT_REQUIRED"
    try:
        _canonical_bytes(record["result"], "record.result")
    except ReplayContractError:
        return "record.result", "NON_JSON_VALUE"
    return None


def _record_problem(record: Any) -> tuple[str, str] | None:
    if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
        return "record", "INVALID_RECORD_STRUCTURE"
    if record.get("contract_version") != CONTRACT_VERSION:
        return "record.contract_version", "UNSUPPORTED_CONTRACT"
    digest_problem = _record_digest_problem(record)
    if digest_problem is not None:
        return digest_problem
    attempt_problem = _record_attempt_problem(record.get("attempt"))
    return attempt_problem or _record_payload_problem(record)


def _record_decision(
    identity: Mapping[str, str], previous_record: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if previous_record is None:
        return None, None
    problem = _record_problem(previous_record)
    attempt = previous_record.get("attempt", 1) if isinstance(previous_record, Mapping) else 1
    if problem is not None:
        return None, _report(
            identity,
            ok=False,
            action="reject",
            attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 1,
            error=_error(REPLAY_RECORD_INVALID, {"field": problem[0], "reason": problem[1]}),
        )
    record = copy.deepcopy(dict(previous_record))
    if record["event_id"] != identity["event_id"]:
        return None, _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(REPLAY_RECORD_INVALID, {"field": "record.event_id", "reason": "TARGET_MISMATCH"}),
        )
    conflicts = [
        field for field in ("input_fingerprint", "run_id", "plan_id")
        if record[field] != identity[field]
    ]
    if conflicts:
        return None, _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(REPLAY_IDENTITY_CONFLICT, {"fields": conflicts}),
        )
    return record, None


def _validate_policy(retry_policy: Any) -> int:
    if not isinstance(retry_policy, Mapping) or set(retry_policy) != {"max_attempts"}:
        _raise_input("retry_policy", "INVALID_POLICY_STRUCTURE")
    maximum = retry_policy.get("max_attempts")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 10:
        _raise_input("retry_policy.max_attempts", "OUT_OF_RANGE")
    return maximum


def _missing_record_decision(
    identity: Mapping[str, str],
    failure_class: str | None,
) -> dict[str, Any]:
    if failure_class in _TERMINAL_FAILURES:
        return _report(
            identity, ok=False, action="reject", attempt=1,
            error=_error(REPLAY_RETRY_FORBIDDEN, {"failure_class": failure_class}),
        )
    if failure_class in _TRANSIENT_FAILURES:
        return _report(
            identity, ok=False, action="reject", attempt=1,
            error=_error(
                REPLAY_INPUT_INVALID,
                {"field": "previous_record", "reason": "FAILURE_RECORD_REQUIRED"},
            ),
        )
    return _report(identity, ok=True, action="execute", attempt=1)


def _failed_record_decision(
    identity: Mapping[str, str],
    record: Mapping[str, Any],
    failure_class: str | None,
    maximum: int,
) -> dict[str, Any]:
    effective_failure = failure_class if failure_class is not None else record["failure_class"]
    if effective_failure != record["failure_class"]:
        return _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(
                REPLAY_INPUT_INVALID,
                {"field": "failure_class", "reason": "RECORD_MISMATCH"},
            ),
        )
    if effective_failure not in _TRANSIENT_FAILURES:
        return _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(REPLAY_RETRY_FORBIDDEN, {"failure_class": effective_failure}),
        )
    next_attempt = record["attempt"] + 1
    if next_attempt > maximum:
        return _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(REPLAY_RETRY_EXHAUSTED, {"max_attempts": maximum}),
        )
    return _report(
        identity, ok=True, action="retry", attempt=next_attempt, record=record
    )


def decide_replay(
    identity: Mapping[str, Any],
    previous_record: Mapping[str, Any] | None,
    failure_class: str | None,
    retry_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable execute/reuse/retry/reject decision for one identity.

    Example::

        report = decide_replay(identity, None, None, {"max_attempts": 3})
        assert report["action"] == "execute"
    """

    checked = _validate_identity(identity)
    maximum = _validate_policy(retry_policy)
    if failure_class is not None and failure_class not in _FAILURE_CLASSES:
        _raise_input("failure_class", "UNSUPPORTED_FAILURE_CLASS")
    record, rejection = _record_decision(checked, previous_record)
    if rejection is not None:
        return rejection
    if record is None:
        return _missing_record_decision(checked, failure_class)
    if record["status"] == "succeeded":
        return _report(
            checked, ok=True, action="reuse", attempt=record["attempt"], record=record
        )
    return _failed_record_decision(checked, record, failure_class, maximum)


def _require_within(child: Path, parent: Path, field: str, reason: str) -> None:
    try:
        child.relative_to(parent)
    except ValueError:
        _raise_input(field, reason)


def _resolve_record_path(
    project_root: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    identity: Mapping[str, str],
) -> Path:
    if not isinstance(project_root, (str, os.PathLike)) or not os.fspath(project_root):
        _raise_input("project_root", "NON_EMPTY_PATH_REQUIRED")
    if not isinstance(data_dir, (str, os.PathLike)) or not os.fspath(data_dir):
        _raise_input("data_dir", "NON_EMPTY_PATH_REQUIRED")
    try:
        project = Path(project_root).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        _raise_input("project_root", "RESOLUTION_FAILED")
    if not project.is_dir():
        _raise_input("project_root", "DIRECTORY_REQUIRED")
    raw_data = Path(data_dir)
    data = (raw_data if raw_data.is_absolute() else project / raw_data).resolve(strict=False)
    _require_within(data, project, "data_dir", "OUTSIDE_PROJECT")
    target = data / "production" / "workflow" / "replays" / f"{identity['event_id'][7:]}.json"
    resolved = target.resolve(strict=False)
    _require_within(resolved, data, "data_dir", "REPLAY_PATH_ESCAPE")
    return resolved


def _read_record(path: Path) -> tuple[Mapping[str, Any] | None, tuple[str, str] | None]:
    if not path.exists():
        return None, None
    try:
        document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ("record", "UNREADABLE_RECORD")
    return document, None


def _atomic_write_json(target: Path, content: str) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(raw)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _validate_materialization_inputs(
    identity: Mapping[str, Any],
    result: Mapping[str, Any],
    failure_class: str | None,
    attempt: int,
    write: bool,
    atomic_write: Callable[[Path, str], None],
    record_reader: RecordReader,
) -> dict[str, str]:
    checked = _validate_identity(identity)
    if not isinstance(result, dict):
        _raise_input("result", "OBJECT_REQUIRED")
    _canonical_bytes(result, "result")
    if failure_class is not None and failure_class not in _FAILURE_CLASSES:
        _raise_input("failure_class", "UNSUPPORTED_FAILURE_CLASS")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        _raise_input("attempt", "POSITIVE_INTEGER_REQUIRED")
    if not isinstance(write, bool):
        _raise_input("write", "BOOLEAN_REQUIRED")
    if not callable(atomic_write):
        _raise_input("atomic_write", "CALLABLE_REQUIRED")
    if not callable(record_reader):
        _raise_input("record_reader", "CALLABLE_REQUIRED")
    return checked


def _load_record(
    target: Path,
    record_reader: RecordReader,
) -> tuple[Any, tuple[str, str] | None]:
    try:
        loaded = record_reader(target)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ("record", "UNREADABLE_RECORD")
    if not isinstance(loaded, tuple) or len(loaded) != 2:
        _raise_input("record_reader", "INVALID_READER_RESULT")
    previous, problem = loaded
    if problem is not None and problem != ("record", "UNREADABLE_RECORD"):
        _raise_input("record_reader", "INVALID_READER_RESULT")
    return previous, problem


def _candidate_record(
    identity: Mapping[str, str],
    result: Mapping[str, Any],
    failure_class: str | None,
    attempt: int,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "event_id": identity["event_id"],
        "run_id": identity["run_id"],
        "input_fingerprint": identity["input_fingerprint"],
        "plan_id": identity["plan_id"],
        "attempt": attempt,
        "status": "succeeded" if failure_class is None else "failed",
        "failure_class": failure_class,
        "result": copy.deepcopy(dict(result)),
    }


def _authorize_retry(
    identity: Mapping[str, str],
    record: Mapping[str, Any],
    candidate: Mapping[str, Any],
    retry_policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if retry_policy is None:
        return _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(
                REPLAY_INPUT_INVALID,
                {"field": "retry_policy", "reason": "REQUIRED_FOR_RETRY"},
            ),
        )
    decision = decide_replay(identity, record, record["failure_class"], retry_policy)
    if not decision["ok"]:
        return decision
    if candidate["attempt"] != decision["attempt"]:
        return _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(
                REPLAY_INPUT_INVALID,
                {"field": "attempt", "reason": "AUTHORIZED_ATTEMPT_REQUIRED"},
            ),
        )
    return None


def _materialization_action(
    identity: Mapping[str, str],
    record: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
    retry_policy: Mapping[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    if record is None:
        if candidate["attempt"] == 1:
            return "execute", None
        return None, _report(
            identity, ok=False, action="reject", attempt=candidate["attempt"],
            error=_error(
                REPLAY_INPUT_INVALID,
                {"field": "attempt", "reason": "FIRST_ATTEMPT_MUST_BE_ONE"},
            ),
        )
    if candidate == record:
        return None, _report(
            identity, ok=True, action="reuse", attempt=record["attempt"], record=record
        )
    if record["status"] == "succeeded" or record["failure_class"] in _TERMINAL_FAILURES:
        return None, _report(
            identity, ok=False, action="reject", attempt=record["attempt"], record=record,
            error=_error(REPLAY_RETRY_FORBIDDEN, {"reason": "TERMINAL_RECORD"}),
        )
    rejection = _authorize_retry(identity, record, candidate, retry_policy)
    return (None, rejection) if rejection is not None else ("retry", None)


def _write_candidate(
    target: Path,
    candidate: Mapping[str, Any],
    atomic_write: Callable[[Path, str], None],
) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            target,
            json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        )
    except (OSError, RuntimeError):
        return False
    return True


def materialize_replay_result(
    project_root: str | os.PathLike[str], data_dir: str | os.PathLike[str],
    identity: Mapping[str, Any], result: Mapping[str, Any],
    failure_class: str | None, attempt: int, write: bool,
    atomic_write: Callable[[Path, str], None] = _atomic_write_json,
    *, retry_policy: Mapping[str, Any] | None = None,
    record_reader: RecordReader = _read_record,
) -> dict[str, Any]:
    """Persist Replay Record 1.0; retries require an explicit bounded policy."""

    checked = _validate_materialization_inputs(
        identity, result, failure_class, attempt, write, atomic_write, record_reader
    )
    target = _resolve_record_path(project_root, data_dir, checked)
    previous, read_problem = _load_record(target, record_reader)
    if read_problem is not None:
        return _report(
            checked, ok=False, action="reject", attempt=attempt,
            error=_error(REPLAY_RECORD_INVALID, {"field": read_problem[0], "reason": read_problem[1]}),
        )
    record, rejection = _record_decision(checked, previous)
    if rejection is not None:
        return rejection
    candidate = _candidate_record(checked, result, failure_class, attempt)
    action, decision = _materialization_action(checked, record, candidate, retry_policy)
    if decision is not None:
        return decision
    assert action is not None
    if not write:
        return _report(
            checked, ok=True, action=action, attempt=attempt, record=record
        )
    if not _write_candidate(target, candidate, atomic_write):
        return _report(
            checked, ok=False, action="reject", attempt=attempt,
            record=record,
            error=_error(REPLAY_WRITE_FAILED),
        )
    return _report(
        checked, ok=True, action=action, attempt=attempt, record=candidate, written=True
    )


__all__ = [
    "CONTRACT_VERSION",
    "REPLAY_IDENTITY_CONFLICT",
    "REPLAY_INPUT_INVALID",
    "REPLAY_RECORD_INVALID",
    "REPLAY_RETRY_EXHAUSTED",
    "REPLAY_RETRY_FORBIDDEN",
    "REPLAY_WRITE_FAILED",
    "ReplayContractError",
    "build_replay_identity",
    "decide_replay",
    "materialize_replay_result",
]
