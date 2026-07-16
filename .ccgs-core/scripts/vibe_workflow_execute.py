#!/usr/bin/env python3
"""Execute one preflight-authorized workflow step through a bounded contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol, Sequence


CONTRACT_VERSION = "1.0"

EXECUTION_NOT_AUTHORIZED = "EXECUTION_NOT_AUTHORIZED"
EXECUTION_BOUNDARY_INVALID = "EXECUTION_BOUNDARY_INVALID"
EXECUTION_POLICY_INVALID = "EXECUTION_POLICY_INVALID"
EXECUTION_START_FAILED = "EXECUTION_START_FAILED"
EXECUTION_COMMAND_FAILED = "EXECUTION_COMMAND_FAILED"
EXECUTION_TIMED_OUT = "EXECUTION_TIMED_OUT"
EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
EXECUTION_ARTIFACT_INVALID = "EXECUTION_ARTIFACT_INVALID"

_MESSAGES = {
    EXECUTION_NOT_AUTHORIZED: "workflow step is not authorized for execution",
    EXECUTION_BOUNDARY_INVALID: "workflow execution boundary is invalid",
    EXECUTION_POLICY_INVALID: "workflow execution policy is invalid",
    EXECUTION_START_FAILED: "workflow step could not be started",
    EXECUTION_COMMAND_FAILED: "workflow step exited with a non-zero code",
    EXECUTION_TIMED_OUT: "workflow step exceeded its timeout",
    EXECUTION_CANCELLED: "workflow step was cancelled",
    EXECUTION_ARTIFACT_INVALID: "workflow artifact violates project policy",
}

_EMPTY_STREAM = {"text": "", "byte_count": 0, "truncated": False}
_POLL_SECONDS = 0.01
_REDACTED = "<redacted>"
_SENSITIVE_ENV_MARKERS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASS",
    "SECRET",
    "TOKEN",
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s\"']+)")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s\"']+")


class Cancellation(Protocol):
    """Minimal cancellation seam accepted by :func:`execute_step`."""

    def is_set(self) -> bool:
        """Return whether cancellation has been requested."""


class _BoundedCapture:
    """Drain a binary stream while retaining only its declared prefix."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.byte_count = 0
        self._retained = bytearray()
        self._lock = threading.Lock()

    @property
    def retained_bytes(self) -> int:
        """Return the number of raw bytes retained in memory."""

        with self._lock:
            return len(self._retained)

    def feed(self, chunk: bytes) -> None:
        """Count a chunk and retain only bytes still inside the prefix limit."""

        with self._lock:
            self.byte_count += len(chunk)
            remaining = self.limit - len(self._retained)
            if remaining > 0:
                self._retained.extend(chunk[:remaining])

    def report(self) -> dict[str, Any]:
        """Return the stable stream result without exposing raw bytes."""

        with self._lock:
            retained = bytes(self._retained)
            byte_count = self.byte_count
        return {
            "text": retained.decode("utf-8", errors="replace"),
            "byte_count": byte_count,
            "truncated": byte_count > self.limit,
        }


def _copy_empty_stream() -> dict[str, Any]:
    return dict(_EMPTY_STREAM)


def _duration_ms(started: float, clock: Callable[[], float]) -> int:
    elapsed = clock() - started
    if not math.isfinite(elapsed) or elapsed <= 0:
        return 0
    return int(elapsed * 1000)


def _result(
    *,
    plan_id: str | None,
    step_id: str,
    status: str,
    exit_category: str,
    exit_code: int | None,
    duration_ms: int,
    stdout: Mapping[str, Any] | None = None,
    stderr: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
    error_code: str | None = None,
    error_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "ok": status == "passed",
        "plan_id": plan_id,
        "step_id": step_id,
        "status": status,
        "exit_category": exit_category,
        "exit_code": exit_code,
        "duration_ms": max(0, int(duration_ms)),
        "retryable": False,
        "stdout": dict(stdout) if stdout is not None else _copy_empty_stream(),
        "stderr": dict(stderr) if stderr is not None else _copy_empty_stream(),
        "artifacts": [dict(item) for item in artifacts],
    }
    if error_code is not None:
        payload["error"] = {
            "code": error_code,
            "message": _MESSAGES[error_code],
            "details": dict(error_details or {}),
        }
    return payload


def _failure(
    code: str,
    *,
    plan_id: str | None,
    step_id: str,
    started: float,
    clock: Callable[[], float],
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return _result(
        plan_id=plan_id,
        step_id=step_id,
        status="failed",
        exit_category="policy_rejected",
        exit_code=None,
        duration_ms=_duration_ms(started, clock),
        error_code=code,
        error_details=details,
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_policy(policy: Any) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    if not isinstance(policy, Mapping) or policy.get("contract_version") != CONTRACT_VERSION:
        return None, ("contract_version", "UNSUPPORTED_CONTRACT")
    timeout = policy.get("timeout_seconds")
    log_limit = policy.get("max_log_bytes")
    grace = policy.get("termination_grace_seconds")
    if not _is_number(timeout) or not 0 < timeout <= 3600:
        return None, ("timeout_seconds", "OUT_OF_RANGE")
    if isinstance(log_limit, bool) or not isinstance(log_limit, int) or not 1 <= log_limit <= 1048576:
        return None, ("max_log_bytes", "OUT_OF_RANGE")
    if not _is_number(grace) or not 0 < grace <= 10:
        return None, ("termination_grace_seconds", "OUT_OF_RANGE")
    return {
        "timeout_seconds": float(timeout),
        "max_log_bytes": log_limit,
        "termination_grace_seconds": float(grace),
    }, None


def _authorized_step(
    report: Any,
    step_id: str,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    if not isinstance(report, Mapping) or report.get("contract_version") != CONTRACT_VERSION:
        return None, None, "UNSUPPORTED_CONTRACT"
    plan_id = report.get("plan_id") if isinstance(report.get("plan_id"), str) else None
    if report.get("ok") is not True:
        return plan_id, None, "PREFLIGHT_FAILED"
    steps = report.get("steps")
    if not isinstance(steps, list) or plan_id is None:
        return plan_id, None, "PREFLIGHT_FAILED"
    for candidate in steps:
        if isinstance(candidate, Mapping) and candidate.get("id") == step_id:
            step = dict(candidate)
            argv = step.get("argv")
            environment = step.get("environment", {})
            artifacts = step.get("artifacts", [])
            working_directory = step.get("working_directory", ".")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(item, str) or not item for item in argv)
                or not isinstance(environment, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items())
                or not isinstance(artifacts, list)
                or any(not isinstance(item, str) for item in artifacts)
                or not isinstance(working_directory, str)
            ):
                return plan_id, None, "PREFLIGHT_FAILED"
            return plan_id, step, None
    return plan_id, None, "STEP_NOT_FOUND"


def _declared_relative_path(value: str) -> tuple[Path | None, str | None]:
    windows = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or bool(windows.drive) or windows.is_absolute():
        return None, "OUTSIDE_PROJECT"
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            return None, "OUTSIDE_PROJECT"
        parts.append(part)
    return Path(*parts) if parts else Path("."), None


def _resolve_project_root(project_root: Path) -> Path | None:
    try:
        resolved = Path(project_root).resolve(strict=True)
        return resolved if resolved.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _resolve_inside(
    root: Path,
    declaration: str,
    *,
    require_directory: bool,
) -> tuple[Path | None, str | None]:
    relative, invalid_reason = _declared_relative_path(declaration)
    if invalid_reason is not None or relative is None:
        return None, invalid_reason
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, "RESOLUTION_FAILED"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "SYMLINK_ESCAPE"
    if require_directory and not resolved.is_dir():
        return None, "RESOLUTION_FAILED"
    return resolved, None


def _read_stream(stream: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            capture.feed(chunk)
    finally:
        try:
            stream.close()
        except (AttributeError, OSError):
            pass


def _redaction_values(
    environment: Mapping[str, str],
    root: Path,
    working_directory: Path,
    argv: Sequence[str],
) -> tuple[str, ...]:
    """Collect known host-sensitive values without exposing them in results."""

    values = {str(root), str(working_directory)}
    for key, value in environment.items():
        if value and any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS):
            values.add(value)
    for argument in argv:
        windows = PureWindowsPath(argument)
        if argument.startswith("/") or windows.is_absolute() or bool(windows.drive):
            values.add(argument)
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _redact_text(text: str, sensitive_values: Sequence[str]) -> str:
    """Remove known secrets and machine-absolute path tokens from captured text."""

    redacted = text
    for value in sensitive_values:
        redacted = redacted.replace(value, _REDACTED)
        stripped = redacted.rstrip()
        suffix = redacted[len(stripped) :]
        for length in range(min(len(value) - 1, len(stripped)), 7, -1):
            if stripped.endswith(value[:length]):
                stripped = f"{stripped[:-length]}{_REDACTED}"
                redacted = f"{stripped}{suffix}"
                break
    redacted = _WINDOWS_ABSOLUTE_PATH.sub(_REDACTED, redacted)
    return _POSIX_ABSOLUTE_PATH.sub(_REDACTED, redacted)


def _redact_stream(stream: Mapping[str, Any], sensitive_values: Sequence[str]) -> dict[str, Any]:
    """Preserve raw byte accounting while sanitizing the retained text prefix."""

    return {
        "text": _redact_text(str(stream.get("text", "")), sensitive_values),
        "byte_count": int(stream.get("byte_count", 0)),
        "truncated": bool(stream.get("truncated", False)),
    }


def _snapshot_posix_descendants(root_pid: int) -> set[int]:
    """Return the currently observable POSIX descendants of ``root_pid``."""

    if sys.platform == "darwin":
        return _snapshot_darwin_descendants(root_pid)
    if sys.platform.startswith("linux"):
        return _snapshot_linux_descendants(root_pid)
    return _snapshot_ps_descendants(root_pid)


def _snapshot_windows_descendants(root_pid: int) -> set[int]:
    """Return descendants observable through the native Tool Help snapshot API."""

    if os.name != "nt":
        return set()
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot in {0, invalid_handle}:
        return set()
    children: dict[int, set[int]] = {}
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            children.setdefault(int(entry.th32ParentProcessID), set()).add(
                int(entry.th32ProcessID)
            )
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return _descendants_from_children(root_pid, children)


def _snapshot_darwin_descendants(root_pid: int) -> set[int]:
    """Enumerate descendants through macOS libproc without spawning a command."""

    import ctypes

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        list_children = libproc.proc_listchildpids
        list_children.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        list_children.restype = ctypes.c_int
    except (AttributeError, OSError):
        return set()
    descendants: set[int] = set()
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        capacity = 256
        while True:
            buffer = (ctypes.c_int * capacity)()
            count = list_children(parent_pid, buffer, ctypes.sizeof(buffer))
            if count < capacity:
                break
            capacity *= 2
        for child_pid in buffer[: max(0, count)]:
            if child_pid > 0 and child_pid not in descendants:
                descendants.add(child_pid)
                pending.append(child_pid)
    return descendants


def _snapshot_linux_descendants(root_pid: int) -> set[int]:
    """Enumerate descendants through Linux procfs."""

    children: dict[int, set[int]] = {}
    try:
        process_entries = tuple(Path("/proc").iterdir())
    except OSError:
        return set()
    for entry in process_entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
            parent_pid = int(parent_line.split()[1])
        except (OSError, StopIteration, ValueError, IndexError):
            continue
        children.setdefault(parent_pid, set()).add(int(entry.name))
    return _descendants_from_children(root_pid, children)


def _snapshot_ps_descendants(root_pid: int) -> set[int]:
    """Fallback descendant enumeration for other POSIX platforms."""

    try:
        completed = subprocess.run(
            ["ps", "-e", "-o", "pid=,ppid="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    children: dict[int, set[int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent_pid = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent_pid, set()).add(pid)
    return _descendants_from_children(root_pid, children)


def _descendants_from_children(root_pid: int, children: Mapping[int, set[int]]) -> set[int]:
    """Expand a parent-to-children map into a descendant set."""

    descendants: set[int] = set()
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, ()))
    return descendants


def _signal_posix_pids(pids: Sequence[int], force: bool) -> None:
    """Signal explicitly tracked descendants that left the root process group."""

    requested_signal = signal.SIGKILL if force else signal.SIGTERM
    for pid in pids:
        try:
            os.kill(pid, requested_signal)
        except (OSError, ProcessLookupError):
            pass


def _cancelled(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    try:
        if hasattr(cancellation, "is_set"):
            return bool(cancellation.is_set())
        if callable(cancellation):
            return bool(cancellation())
    except Exception:
        return False
    return False


def _signal_process_tree(process: Any, force: bool) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)
            return
        except (OSError, ProcessLookupError):
            pass
    elif os.name == "nt":
        if not force and hasattr(signal, "CTRL_BREAK_EVENT"):
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except (OSError, ProcessLookupError):
                pass
        if force:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
                return
            except OSError:
                pass
    try:
        process.kill() if force else process.terminate()
    except (OSError, ProcessLookupError):
        pass


def _force_terminate_windows_pids(pids: Sequence[int]) -> None:
    """Terminate Windows PIDs captured while they were still in the controlled tree."""

    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    for pid in pids:
        handle = kernel32.OpenProcess(0x0001, False, int(pid))
        if not handle:
            continue
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def _terminate_process_tree(process: Any, grace_seconds: float) -> None:
    if os.name == "posix":
        # ``start_new_session=True`` makes the child PID the process-group ID.
        # Keeping that stable ID also covers the race where the direct child
        # exits after ``poll()`` but a descendant still owns the group.
        process_group = process.pid
        descendants = _snapshot_posix_descendants(process_group)
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            _signal_posix_pids(tuple(descendants), force=False)
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                process.poll()
                observed = _snapshot_posix_descendants(process_group)
                new_descendants = observed - descendants
                descendants.update(observed)
                _signal_posix_pids(tuple(new_descendants), force=False)
                try:
                    os.killpg(process_group, 0)
                except (OSError, ProcessLookupError):
                    if not any(_pid_exists(pid) for pid in descendants):
                        break
                time.sleep(_POLL_SECONDS)
            else:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                _signal_posix_pids(tuple(descendants), force=True)
            try:
                process.wait(timeout=grace_seconds)
            except (subprocess.TimeoutExpired, TimeoutError):
                try:
                    process.kill()
                    process.wait()
                except (OSError, ProcessLookupError):
                    pass
            return

    if os.name == "nt":
        descendants = _snapshot_windows_descendants(process.pid)
        _signal_process_tree(process, force=False)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            process.poll()
            descendants.update(_snapshot_windows_descendants(process.pid))
            time.sleep(_POLL_SECONDS)
        _signal_process_tree(process, force=True)
        _force_terminate_windows_pids(tuple(descendants))
        try:
            process.wait(timeout=grace_seconds)
        except (subprocess.TimeoutExpired, TimeoutError):
            try:
                process.kill()
                process.wait()
            except (OSError, ProcessLookupError):
                pass
        return

    _signal_process_tree(process, force=False)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        time.sleep(_POLL_SECONDS)
    _signal_process_tree(process, force=True)
    try:
        process.wait(timeout=grace_seconds)
    except (subprocess.TimeoutExpired, TimeoutError):
        try:
            process.kill()
            process.wait()
        except (OSError, ProcessLookupError):
            pass


def _pid_exists(pid: int) -> bool:
    """Return whether a POSIX PID is still observable."""

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _join_output_threads(
    process: Any,
    threads: Sequence[threading.Thread],
    grace_seconds: float,
) -> None:
    """Bound stream draining and reclaim descendants that keep pipes open."""

    deadline = time.monotonic() + grace_seconds
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if not any(thread.is_alive() for thread in threads):
        return
    _terminate_process_tree(process, grace_seconds)
    deadline = time.monotonic() + grace_seconds
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _artifact_id(plan_id: str, step_id: str, path: str) -> str:
    canonical = json.dumps(
        [plan_id, step_id, path],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _collect_artifacts(
    root: Path,
    plan_id: str,
    step_id: str,
    declarations: Sequence[str],
) -> tuple[list[dict[str, Any]] | None, tuple[str, str] | None]:
    artifacts: list[dict[str, Any]] = []
    for declaration in declarations:
        resolved, reason = _resolve_inside(root, declaration, require_directory=False)
        if reason is not None or resolved is None:
            return None, (declaration, reason or "RESOLUTION_FAILED")
        artifacts.append(
            {
                "artifact_id": _artifact_id(plan_id, step_id, declaration),
                "path": declaration,
                "present": resolved.exists(),
            }
        )
    return artifacts, None


def execute_step(
    preflight_report: Mapping[str, Any],
    step_id: str,
    project_root: Path,
    policy: Mapping[str, Any],
    cancellation: Cancellation | Callable[[], bool] | None = None,
    *,
    process_factory: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Execute one authorized step and return Result Contract 1.0.

    The function never evaluates a shell command. Tests may inject the process,
    clock, cancellation, and sleep seams without changing production semantics.
    """

    started = clock()
    requested_step_id = step_id if isinstance(step_id, str) else ""
    plan_id, step, authorization_error = _authorized_step(preflight_report, requested_step_id)
    if authorization_error is not None or step is None:
        return _failure(
            EXECUTION_NOT_AUTHORIZED,
            plan_id=plan_id,
            step_id=requested_step_id,
            started=started,
            clock=clock,
            details={"reason": authorization_error or "PREFLIGHT_FAILED"},
        )

    validated_policy, policy_error = _validate_policy(policy)
    if policy_error is not None or validated_policy is None:
        field, reason = policy_error or ("contract_version", "UNSUPPORTED_CONTRACT")
        return _failure(
            EXECUTION_POLICY_INVALID,
            plan_id=plan_id,
            step_id=requested_step_id,
            started=started,
            clock=clock,
            details={"field": field, "reason": reason},
        )

    root = _resolve_project_root(Path(project_root))
    if root is None:
        return _failure(
            EXECUTION_BOUNDARY_INVALID,
            plan_id=plan_id,
            step_id=requested_step_id,
            started=started,
            clock=clock,
            details={"field": "working_directory", "reason": "RESOLUTION_FAILED"},
        )
    working_directory, boundary_error = _resolve_inside(
        root,
        step.get("working_directory", "."),
        require_directory=True,
    )
    if boundary_error is not None or working_directory is None:
        return _failure(
            EXECUTION_BOUNDARY_INVALID,
            plan_id=plan_id,
            step_id=requested_step_id,
            started=started,
            clock=clock,
            details={
                "field": "working_directory",
                "reason": boundary_error or "RESOLUTION_FAILED",
            },
        )

    process_environment = os.environ.copy()
    process_environment.update(dict(step.get("environment", {})))
    sensitive_values = _redaction_values(
        process_environment,
        root,
        working_directory,
        step["argv"],
    )
    process_options: dict[str, Any] = {
        "cwd": str(working_directory),
        "env": process_environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "posix":
        process_options["start_new_session"] = True
    elif os.name == "nt":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        process = process_factory(list(step["argv"]), **process_options)
    except (OSError, ValueError):
        return _result(
            plan_id=plan_id,
            step_id=requested_step_id,
            status="failed",
            exit_category="start_failed",
            exit_code=None,
            duration_ms=_duration_ms(started, clock),
            error_code=EXECUTION_START_FAILED,
            error_details={"reason": "OS_ERROR"},
        )

    stdout_capture = _BoundedCapture(validated_policy["max_log_bytes"])
    stderr_capture = _BoundedCapture(validated_policy["max_log_bytes"])
    stdout_thread = threading.Thread(
        target=_read_stream,
        args=(process.stdout, stdout_capture),
        name=f"workflow-{requested_step_id}-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(process.stderr, stderr_capture),
        name=f"workflow-{requested_step_id}-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    terminal = "natural"
    return_code: int | None = None
    while True:
        return_code = process.poll()
        if return_code is not None:
            break
        if _cancelled(cancellation):
            terminal = "cancelled"
            _terminate_process_tree(process, validated_policy["termination_grace_seconds"])
            break
        if clock() - started >= validated_policy["timeout_seconds"]:
            terminal = "timed_out"
            _terminate_process_tree(process, validated_policy["termination_grace_seconds"])
            break
        sleeper(_POLL_SECONDS)

    _join_output_threads(
        process,
        (stdout_thread, stderr_thread),
        validated_policy["termination_grace_seconds"],
    )
    stdout_result = _redact_stream(stdout_capture.report(), sensitive_values)
    stderr_result = _redact_stream(stderr_capture.report(), sensitive_values)

    artifacts, artifact_error = _collect_artifacts(
        root,
        plan_id,
        requested_step_id,
        step.get("artifacts", []),
    )
    if artifact_error is not None:
        path, reason = artifact_error
        return _result(
            plan_id=plan_id,
            step_id=requested_step_id,
            status="failed",
            exit_category="policy_rejected",
            exit_code=None,
            duration_ms=_duration_ms(started, clock),
            stdout=stdout_result,
            stderr=stderr_result,
            error_code=EXECUTION_ARTIFACT_INVALID,
            error_details={"path": path, "reason": reason},
        )

    if terminal == "cancelled":
        return _result(
            plan_id=plan_id,
            step_id=requested_step_id,
            status="cancelled",
            exit_category="cancelled",
            exit_code=None,
            duration_ms=_duration_ms(started, clock),
            stdout=stdout_result,
            stderr=stderr_result,
            artifacts=artifacts or [],
            error_code=EXECUTION_CANCELLED,
        )
    if terminal == "timed_out":
        return _result(
            plan_id=plan_id,
            step_id=requested_step_id,
            status="failed",
            exit_category="timed_out",
            exit_code=None,
            duration_ms=_duration_ms(started, clock),
            stdout=stdout_result,
            stderr=stderr_result,
            artifacts=artifacts or [],
            error_code=EXECUTION_TIMED_OUT,
            error_details={"timeout_seconds": validated_policy["timeout_seconds"]},
        )
    if return_code != 0:
        return _result(
            plan_id=plan_id,
            step_id=requested_step_id,
            status="failed",
            exit_category="command_failed",
            exit_code=return_code,
            duration_ms=_duration_ms(started, clock),
            stdout=stdout_result,
            stderr=stderr_result,
            artifacts=artifacts or [],
            error_code=EXECUTION_COMMAND_FAILED,
        )
    return _result(
        plan_id=plan_id,
        step_id=requested_step_id,
        status="passed",
        exit_category="success",
        exit_code=0,
        duration_ms=_duration_ms(started, clock),
        stdout=stdout_result,
        stderr=stderr_result,
        artifacts=artifacts or [],
    )


__all__ = [
    "CONTRACT_VERSION",
    "EXECUTION_ARTIFACT_INVALID",
    "EXECUTION_BOUNDARY_INVALID",
    "EXECUTION_CANCELLED",
    "EXECUTION_COMMAND_FAILED",
    "EXECUTION_NOT_AUTHORIZED",
    "EXECUTION_POLICY_INVALID",
    "EXECUTION_START_FAILED",
    "EXECUTION_TIMED_OUT",
    "execute_step",
]
