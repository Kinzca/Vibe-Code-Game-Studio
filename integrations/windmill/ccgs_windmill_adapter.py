#!/usr/bin/env python3
"""Windmill adapter that delegates all workflow decisions to ccgs.cmd."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA_VERSION = "1.0"
ADAPTER_NAME = "windmill"
ALLOWED_COMMANDS = {
    "doctor",
    "evidence-validate",
    "closeout",
    "qdrant-query",
    "workflow-observe",
    "langfuse-export",
}
FORBIDDEN_CMD_CHARS = frozenset("&|<>^%!\"\r\n\0")
DRIVE_RE = re.compile(r"^[A-Za-z]:")


class WindmillAdapterError(ValueError):
    """Raised when adapter configuration would violate the CLI boundary."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry settings for transport and protocol failures."""

    max_attempts: int = 3
    delay_seconds: float = 1.0
    timeout_seconds: float = 120.0

    def validate(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise WindmillAdapterError("max_attempts must be between 1 and 5")
        if not 0 <= self.delay_seconds <= 60:
            raise WindmillAdapterError("retry_delay_seconds must be between 0 and 60")
        if not 1 <= self.timeout_seconds <= 3600:
            raise WindmillAdapterError("timeout_seconds must be between 1 and 3600")


def _validate_shell_value(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WindmillAdapterError(f"{label} must be a non-empty string")
    if any(character in value for character in FORBIDDEN_CMD_CHARS):
        raise WindmillAdapterError(f"{label} contains a forbidden cmd.exe character")
    return value


def validate_relative_path(value: str, label: str) -> str:
    """Reject absolute, traversing, and cmd-sensitive project paths."""

    value = _validate_shell_value(value, label).replace("\\", "/")
    if value.startswith("/") or DRIVE_RE.match(value):
        raise WindmillAdapterError(f"{label} must be project-relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WindmillAdapterError(f"{label} contains an invalid path segment")
    return value


def _validate_root(value: str, label: str) -> Path:
    _validate_shell_value(value, label)
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise WindmillAdapterError(f"{label} directory not found")
    return path


def _failure(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


def _deduplicate_failures(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("code", "")), str(item.get("message", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "code": key[0],
                "message": key[1],
                "retryable": bool(item.get("retryable", False)),
            }
        )
    return result


class CcgsCmdRunner:
    """Invoke a fixed ccgs.cmd command set without shell interpolation."""

    def __init__(
        self,
        framework_root: str,
        project_root: str,
        *,
        retry_policy: RetryPolicy | None = None,
        executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        platform: str | None = None,
        comspec: str | None = None,
    ) -> None:
        self.framework_root = _validate_root(framework_root, "framework_root")
        self.project_root = _validate_root(project_root, "project_root")
        self.entrypoint = self.framework_root / "ccgs.cmd"
        if not self.entrypoint.is_file():
            raise WindmillAdapterError("framework_root does not contain ccgs.cmd")
        self.policy = retry_policy or RetryPolicy()
        self.policy.validate()
        self.executor = executor
        self.sleeper = sleeper
        self.platform = platform or os.name
        self.comspec = comspec or os.environ.get("COMSPEC", "cmd.exe")
        _validate_shell_value(str(self.entrypoint), "ccgs.cmd path")
        _validate_shell_value(str(self.project_root), "project_root")

    def _command_line(self, command: str, arguments: Sequence[str]) -> str:
        if command not in ALLOWED_COMMANDS:
            raise WindmillAdapterError(f"unsupported CCGS command: {command}")
        if self.platform != "nt":
            raise WindmillAdapterError(
                "ccgs.cmd requires a Windmill Windows worker"
            )
        safe_arguments = [_validate_shell_value(str(item), "CLI argument") for item in arguments]
        ccgs_arguments = [
            str(self.entrypoint),
            command,
            "--project-root",
            str(self.project_root),
            *safe_arguments,
        ]
        quoted = " ".join(f'"{item}"' for item in ccgs_arguments)
        executable = subprocess.list2cmdline([self.comspec])
        return f'{executable} /d /s /c "{quoted}"'

    def invoke(self, command: str, arguments: Sequence[str]) -> dict[str, Any]:
        """Run one CLI operation and retry transport/protocol failures only."""

        process_command = self._command_line(command, arguments)
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                process = self.executor(
                    process_command,
                    cwd=self.framework_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=self.policy.timeout_seconds,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                attempts.append(
                    {
                        "attempt": attempt,
                        "outcome": "timeout",
                        "message": "ccgs.cmd exceeded timeout_seconds",
                        "retryable": True,
                    }
                )
                if attempt < self.policy.max_attempts:
                    self.sleeper(self.policy.delay_seconds)
                    continue
                return self._transport_failure(command, attempts)
            except OSError as exc:
                attempts.append(
                    {
                        "attempt": attempt,
                        "outcome": "transport-error",
                        "message": str(exc),
                        "retryable": True,
                    }
                )
                if attempt < self.policy.max_attempts:
                    self.sleeper(self.policy.delay_seconds)
                    continue
                return self._transport_failure(command, attempts)

            stdout = process.stdout.strip()
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                retryable = process.returncode not in {1, 2}
                attempts.append(
                    {
                        "attempt": attempt,
                        "outcome": "protocol-error",
                        "message": "ccgs.cmd did not return JSON",
                        "retryable": retryable,
                    }
                )
                if retryable and attempt < self.policy.max_attempts:
                    self.sleeper(self.policy.delay_seconds)
                    continue
                return {
                    "command": command,
                    "status": "error",
                    "exit_code": process.returncode,
                    "attempt_count": attempt,
                    "attempts": attempts,
                    "payload": None,
                    "stderr": process.stderr.strip()[:4000],
                    "retryable": retryable,
                }

            attempts.append(
                {
                    "attempt": attempt,
                    "outcome": (
                        "success"
                        if process.returncode == 0
                        else "business-failure"
                        if process.returncode == 1
                        else "invocation-error"
                    ),
                    "message": "",
                    "retryable": False,
                }
            )
            return {
                "command": command,
                "status": (
                    "passed"
                    if process.returncode == 0
                    else "failed"
                    if process.returncode == 1
                    else "error"
                ),
                "exit_code": process.returncode,
                "attempt_count": attempt,
                "attempts": attempts,
                "payload": payload,
                "stderr": process.stderr.strip()[:4000],
                "retryable": False,
            }
        raise AssertionError("retry loop exhausted without a report")

    @staticmethod
    def _transport_failure(
        command: str, attempts: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "command": command,
            "status": "error",
            "exit_code": None,
            "attempt_count": len(attempts),
            "attempts": list(attempts),
            "payload": None,
            "stderr": "",
            "retryable": True,
        }


def _public_invocation(invocation: dict[str, Any]) -> dict[str, Any]:
    payload = invocation.get("payload")
    if invocation["command"] == "doctor" and isinstance(payload, dict):
        payload = {
            "cli_version": payload.get("cli_version", ""),
            "repository_mode": payload.get("repository_mode", ""),
            "data_dir": payload.get("data_dir", ""),
            "read_only": payload.get("read_only", False),
            "engine_agnostic": payload.get("engine_agnostic", False),
            "summary": payload.get("summary", {}),
        }
    elif invocation["command"] == "qdrant-query" and isinstance(payload, dict):
        payload = {
            **{key: value for key, value in payload.items() if key != "results"},
            "results": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "score",
                        "source_kind",
                        "source_path",
                        "heading",
                        "chunk_index",
                    )
                }
                for item in payload.get("results", [])
                if isinstance(item, dict)
            ],
        }
    return {
        "command": invocation["command"],
        "status": invocation["status"],
        "exit_code": invocation["exit_code"],
        "attempt_count": invocation["attempt_count"],
        "attempts": invocation["attempts"],
        "payload": payload,
        "stderr": invocation["stderr"],
        "retryable": invocation["retryable"],
    }


def _invocation_failures(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    if invocation["status"] != "error":
        return []
    attempts = invocation.get("attempts", [])
    message = invocation.get("stderr", "")
    if not message and attempts:
        message = str(attempts[-1].get("message", "ccgs.cmd invocation failed"))
    return [
        _failure(
            f"adapter.{invocation['command']}",
            message or "ccgs.cmd invocation failed",
            bool(invocation.get("retryable", False)),
        )
    ]


def _evidence_failures(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    payload = invocation.get("payload")
    if not isinstance(payload, dict):
        return []
    failures = []
    for item in payload.get("errors", []):
        if isinstance(item, dict):
            failures.append(
                _failure(
                    f"evidence.schema:{item.get('path', '$')}",
                    str(item.get("message", "invalid evidence")),
                )
            )
    if invocation.get("exit_code") == 1 and not failures:
        failures.append(_failure("evidence.invalid", "Evidence validation failed"))
    return failures


def _closeout_failures(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    payload = invocation.get("payload")
    if not isinstance(payload, dict):
        return []
    failures = []
    for item in payload.get("failures", []):
        if isinstance(item, dict):
            failures.append(
                _failure(
                    str(item.get("code", "closeout.failed")),
                    str(item.get("message", "Closeout failed")),
                )
            )
    if invocation.get("exit_code") == 1 and not failures:
        failures.append(_failure("closeout.failed", "Closeout failed"))
    return failures


def _policy(
    max_attempts: int, retry_delay_seconds: float, timeout_seconds: float
) -> RetryPolicy:
    policy = RetryPolicy(max_attempts, retry_delay_seconds, timeout_seconds)
    policy.validate()
    return policy


def run_story_check(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str = "",
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    platform: str | None = None,
    comspec: str | None = None,
) -> dict[str, Any]:
    """Run doctor, Evidence validation, and read-only Closeout inspection."""

    story = validate_relative_path(story, "story")
    if evidence:
        evidence = validate_relative_path(evidence, "evidence")
    runner = CcgsCmdRunner(
        framework_root,
        project_root,
        retry_policy=_policy(max_attempts, retry_delay_seconds, timeout_seconds),
        executor=executor,
        sleeper=sleeper,
        platform=platform,
        comspec=comspec,
    )

    doctor = runner.invoke("doctor", ["--json"])
    invocations = [doctor]
    failures = _invocation_failures(doctor)
    doctor_payload = doctor.get("payload")
    if doctor["status"] == "error" or not isinstance(doctor_payload, dict):
        return _result("story-check", "error", story, evidence, invocations, failures)

    if not evidence:
        data_dir = str(doctor_payload.get("data_dir", "ccgs-data"))
        evidence = (
            Path(data_dir)
            / "production"
            / "qa"
            / "evidence"
            / f"{Path(story).stem}.json"
        ).as_posix()
        evidence = validate_relative_path(evidence, "evidence")

    evidence_check = runner.invoke(
        "evidence-validate", ["--evidence", evidence]
    )
    closeout_check = runner.invoke(
        "closeout", ["--story", story, "--evidence", evidence, "--dry-run"]
    )
    invocations.extend([evidence_check, closeout_check])
    failures.extend(_invocation_failures(evidence_check))
    failures.extend(_invocation_failures(closeout_check))
    failures.extend(_evidence_failures(evidence_check))
    failures.extend(_closeout_failures(closeout_check))

    has_error = any(item["status"] == "error" for item in invocations)
    closeout_payload = closeout_check.get("payload")
    passed = (
        evidence_check.get("exit_code") == 0
        and closeout_check.get("exit_code") == 0
        and isinstance(closeout_payload, dict)
        and closeout_payload.get("verdict") == "pass"
    )
    status = "error" if has_error else "passed" if passed else "failed"
    return _result("story-check", status, story, evidence, invocations, failures)


def run_story_closeout(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str = "",
    apply: bool = True,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    platform: str | None = None,
    comspec: str | None = None,
) -> dict[str, Any]:
    """Inspect a Story, then let ccgs.cmd advance or persist failure reasons."""

    check = run_story_check(
        framework_root,
        project_root,
        story,
        evidence,
        max_attempts,
        retry_delay_seconds,
        timeout_seconds,
        executor=executor,
        sleeper=sleeper,
        platform=platform,
        comspec=comspec,
    )
    if not apply or check["status"] == "error":
        return {
            **check,
            "operation": "story-closeout",
            "apply": apply,
            "advance": None,
        }

    story = validate_relative_path(story, "story")
    evidence = validate_relative_path(str(check["evidence"]), "evidence")
    runner = CcgsCmdRunner(
        framework_root,
        project_root,
        retry_policy=_policy(max_attempts, retry_delay_seconds, timeout_seconds),
        executor=executor,
        sleeper=sleeper,
        platform=platform,
        comspec=comspec,
    )
    advance = runner.invoke(
        "closeout", ["--story", story, "--evidence", evidence, "--write"]
    )
    failures = list(check["failures"])
    failures.extend(_invocation_failures(advance))
    failures.extend(_closeout_failures(advance))
    if advance["status"] == "error":
        status = "error"
    elif advance.get("exit_code") == 0:
        status = "passed"
    else:
        status = "failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER_NAME,
        "operation": "story-closeout",
        "status": status,
        "ok": status == "passed",
        "retryable": bool(advance.get("retryable", False)),
        "story": story,
        "evidence": evidence,
        "apply": True,
        "commands": [*check["commands"], _public_invocation(advance)],
        "failures": _deduplicate_failures(failures),
        "advance": _public_invocation(advance),
    }


def _safe_failure_code(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9._:-]+", "-", value).strip("-.")
    return (rendered or "workflow.failed")[:128]


def run_observed_story_closeout(
    framework_root: str,
    project_root: str,
    story: str,
    evidence: str,
    project_id: str,
    event_id: str,
    trace_key: str,
    session_id: str,
    environment: str = "automation",
    query: str = "",
    apply: bool = True,
    qdrant_url: str = "http://127.0.0.1:6333",
    qdrant_collection: str = "ccgs-context",
    qdrant_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    qdrant_limit: int = 8,
    langfuse_host: str = "https://cloud.langfuse.com",
    langfuse_send: bool = True,
    allow_insecure_http: bool = False,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    timeout_seconds: float = 120.0,
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    platform: str | None = None,
    comspec: str | None = None,
) -> dict[str, Any]:
    """Retrieve context, close out a Story, materialize an event, and export it."""

    story = validate_relative_path(story, "story")
    evidence = validate_relative_path(evidence, "evidence")
    for value, label in (
        (project_id, "project_id"),
        (event_id, "event_id"),
        (trace_key, "trace_key"),
        (session_id, "session_id"),
        (environment, "environment"),
        (qdrant_collection, "qdrant_collection"),
        (qdrant_embedding_model, "qdrant_embedding_model"),
        (qdrant_url, "qdrant_url"),
        (langfuse_host, "langfuse_host"),
    ):
        _validate_shell_value(value, label)
    query = query.strip() or f"Is {Path(story).stem} ready to close?"
    _validate_shell_value(query, "query")
    if not 1 <= qdrant_limit <= 20:
        raise WindmillAdapterError("qdrant_limit must be between 1 and 20")

    runner = CcgsCmdRunner(
        framework_root,
        project_root,
        retry_policy=_policy(max_attempts, retry_delay_seconds, timeout_seconds),
        executor=executor,
        sleeper=sleeper,
        platform=platform,
        comspec=comspec,
    )
    retrieval_args = [
        "--project-id",
        project_id,
        "--query",
        query,
        "--limit",
        str(qdrant_limit),
        "--collection",
        qdrant_collection,
        "--qdrant-url",
        qdrant_url,
        "--embedding-model",
        qdrant_embedding_model,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if allow_insecure_http:
        retrieval_args.append("--allow-insecure-http")
    retrieval = runner.invoke("qdrant-query", retrieval_args)
    if retrieval["status"] == "error":
        failures = _invocation_failures(retrieval)
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter": ADAPTER_NAME,
            "operation": "observed-story-closeout",
            "status": "error",
            "ok": False,
            "retryable": bool(retrieval.get("retryable", False)),
            "story": story,
            "evidence": evidence,
            "retrieval": _public_invocation(retrieval),
            "closeout": None,
            "observation": None,
            "telemetry": None,
            "failures": _deduplicate_failures(failures),
        }

    closeout = run_story_closeout(
        framework_root,
        project_root,
        story,
        evidence,
        apply,
        max_attempts,
        retry_delay_seconds,
        timeout_seconds,
        executor=executor,
        sleeper=sleeper,
        platform=platform,
        comspec=comspec,
    )
    retrieval_payload = retrieval.get("payload")
    retrieval_results = (
        retrieval_payload.get("results", [])
        if isinstance(retrieval_payload, dict)
        else []
    )
    references = list(
        dict.fromkeys(
            validate_relative_path(str(item.get("source_path", "")), "retrieval reference")
            for item in retrieval_results
            if isinstance(item, dict) and item.get("source_path")
        )
    )[:20]
    failure_codes = [
        _safe_failure_code(str(item.get("code", "workflow.failed")))
        for item in closeout.get("failures", [])
        if isinstance(item, dict)
    ]
    observe_args = [
        "--story",
        story,
        "--evidence",
        evidence,
        "--project-id",
        project_id,
        "--event-id",
        event_id,
        "--trace-key",
        trace_key,
        "--session-id",
        session_id,
        "--environment",
        environment,
        "--surface",
        "windmill",
        "--operation",
        "story-closeout",
        "--status",
        str(closeout.get("status", "error")),
        "--query",
        query,
    ]
    for reference in references:
        observe_args.extend(["--retrieval-reference", reference])
    for code in failure_codes:
        observe_args.extend(["--failure-code", code])
    observe_args.append("--write")
    observation = runner.invoke("workflow-observe", observe_args)
    if observation["status"] == "error":
        failures = [*closeout.get("failures", []), *_invocation_failures(observation)]
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter": ADAPTER_NAME,
            "operation": "observed-story-closeout",
            "status": "error",
            "ok": False,
            "retryable": bool(observation.get("retryable", False)),
            "story": story,
            "evidence": evidence,
            "retrieval": _public_invocation(retrieval),
            "closeout": closeout,
            "observation": _public_invocation(observation),
            "telemetry": None,
            "failures": _deduplicate_failures(failures),
        }

    observation_payload = observation.get("payload")
    if not isinstance(observation_payload, dict) or not observation_payload.get("event"):
        raise WindmillAdapterError("workflow-observe did not return an event path")
    telemetry_args = [
        "--event",
        validate_relative_path(str(observation_payload["event"]), "event"),
        "--host",
        langfuse_host,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if allow_insecure_http:
        telemetry_args.append("--allow-insecure-http")
    telemetry_args.append("--send" if langfuse_send else "--dry-run")
    telemetry = runner.invoke("langfuse-export", telemetry_args)
    failures = list(closeout.get("failures", []))
    failures.extend(_invocation_failures(telemetry))
    status = "error" if telemetry["status"] == "error" else str(closeout["status"])
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER_NAME,
        "operation": "observed-story-closeout",
        "status": status,
        "ok": status == "passed",
        "retryable": bool(telemetry.get("retryable", False)),
        "story": story,
        "evidence": evidence,
        "retrieval": _public_invocation(retrieval),
        "closeout": closeout,
        "observation": _public_invocation(observation),
        "telemetry": _public_invocation(telemetry),
        "failures": _deduplicate_failures(failures),
    }

def _result(
    operation: str,
    status: str,
    story: str,
    evidence: str,
    invocations: Sequence[dict[str, Any]],
    failures: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    normalized_failures = _deduplicate_failures(failures)
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER_NAME,
        "operation": operation,
        "status": status,
        "ok": status == "passed",
        "retryable": any(item.get("retryable", False) for item in normalized_failures),
        "story": story,
        "evidence": evidence,
        "commands": [_public_invocation(item) for item in invocations],
        "failures": normalized_failures,
    }


def raise_for_windmill(result: dict[str, Any]) -> dict[str, Any]:
    """Raise only adapter errors so Windmill can apply selective retries."""

    if result.get("status") != "error":
        return result
    marker = "[CCGS_RETRYABLE]" if result.get("retryable") else "[CCGS_PERMANENT]"
    compact = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    raise RuntimeError(f"{marker}{compact}")