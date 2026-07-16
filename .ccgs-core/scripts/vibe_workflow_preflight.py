#!/usr/bin/env python3
"""Validate a compiled workflow plan before any process can start."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, Protocol, Sequence


CONTRACT_VERSION = "1.0"

PREFLIGHT_PATH_INVALID = "PREFLIGHT_PATH_INVALID"
PREFLIGHT_ARGUMENT_INVALID = "PREFLIGHT_ARGUMENT_INVALID"
PREFLIGHT_ENVIRONMENT_INVALID = "PREFLIGHT_ENVIRONMENT_INVALID"

PATH_MESSAGE = "workflow path violates project policy"
ARGUMENT_MESSAGE = "workflow argument violates execution policy"
ENVIRONMENT_MESSAGE = "workflow environment violates execution policy"

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_INTERPOLATION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


class PathInspector(Protocol):
    """Minimal filesystem seam required by project-boundary validation."""

    def resolve(self, path: Path) -> Path:
        """Return a canonical path, resolving existing symbolic links."""

    def is_dir(self, path: Path) -> bool:
        """Return whether a canonical path names an existing directory."""


class LocalPathInspector:
    """Production path inspector backed by read-only ``pathlib`` operations."""

    def resolve(self, path: Path) -> Path:
        """Resolve a path without requiring its final component to exist."""

        return path.resolve(strict=False)

    def is_dir(self, path: Path) -> bool:
        """Check directory existence without creating or modifying anything."""

        return path.is_dir()


@dataclass(frozen=True)
class PreflightError(ValueError):
    """Stable, non-retryable workflow preflight failure."""

    code: str
    message: str
    details: Mapping[str, Any]

    def __str__(self) -> str:
        return self.message

    @property
    def retryable(self) -> bool:
        """Preflight failures are deterministic and must never be retried."""

        return False

    def report(self) -> dict[str, Any]:
        """Return the versioned, sanitized machine failure contract."""

        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": copy.deepcopy(dict(self.details)),
            },
        }


def _relative_to(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _path_error(
    step_id: str,
    field: str,
    reason: str,
    *,
    index: int | None = None,
) -> PreflightError:
    details: dict[str, Any] = {"step_id": step_id, "field": field}
    if index is not None:
        details["index"] = index
    details["reason"] = reason
    return PreflightError(PREFLIGHT_PATH_INVALID, PATH_MESSAGE, details)


def _normalize_declared_path(
    value: str,
    step_id: str,
    field: str,
    *,
    index: int | None = None,
) -> str:
    if "\x00" in value:
        raise _path_error(step_id, field, "INVALID_CHARACTER", index=index)

    windows_path = PureWindowsPath(value)
    normalized_input = value.replace("\\", "/")
    if (
        normalized_input.startswith("/")
        or normalized_input.startswith("//")
        or bool(windows_path.drive)
        or windows_path.is_absolute()
    ):
        raise _path_error(step_id, field, "ABSOLUTE", index=index)

    parts: list[str] = []
    for part in normalized_input.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise _path_error(step_id, field, "OUTSIDE_PROJECT", index=index)
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else "."


def _validate_path_boundary(
    normalized: str,
    canonical_root: Path,
    inspector: PathInspector,
    step_id: str,
    field: str,
    *,
    index: int | None = None,
    require_directory: bool = False,
) -> None:
    candidate = canonical_root
    if normalized != ".":
        candidate = canonical_root.joinpath(*normalized.split("/"))
    try:
        resolved = inspector.resolve(candidate)
    except (OSError, RuntimeError):
        raise _path_error(
            step_id,
            field,
            "RESOLUTION_FAILED",
            index=index,
        ) from None
    if _relative_to(resolved, canonical_root) is None:
        raise _path_error(step_id, field, "SYMLINK_ESCAPE", index=index)
    if require_directory:
        try:
            is_directory = inspector.is_dir(resolved)
        except (OSError, RuntimeError):
            raise _path_error(
                step_id,
                field,
                "RESOLUTION_FAILED",
                index=index,
            ) from None
        if not is_directory:
            raise _path_error(step_id, field, "NOT_DIRECTORY", index=index)


def _validate_paths(
    step: dict[str, Any],
    canonical_root: Path,
    inspector: PathInspector,
) -> None:
    step_id = step["id"]
    working_directory = _normalize_declared_path(
        step.get("working_directory", "."),
        step_id,
        "working_directory",
    )
    _validate_path_boundary(
        working_directory,
        canonical_root,
        inspector,
        step_id,
        "working_directory",
        require_directory=True,
    )
    step["working_directory"] = working_directory

    artifacts = step.get("artifacts")
    if artifacts is None:
        return
    step["artifacts"] = _normalize_artifacts(
        artifacts,
        canonical_root,
        inspector,
        step_id,
    )


def _normalize_artifacts(
    artifacts: Sequence[str],
    canonical_root: Path,
    inspector: PathInspector,
    step_id: str,
) -> list[str]:
    """Normalize declared artifacts while preserving declaration order."""

    normalized_artifacts: list[str] = []
    for index, artifact in enumerate(artifacts):
        normalized = _normalize_declared_path(
            artifact,
            step_id,
            "artifacts",
            index=index,
        )
        _validate_path_boundary(
            normalized,
            canonical_root,
            inspector,
            step_id,
            "artifacts",
            index=index,
        )
        normalized_artifacts.append(normalized)
    return normalized_artifacts


def _validate_arguments(step: Mapping[str, Any]) -> None:
    for index, argument in enumerate(step["argv"]):
        reason = None
        if "\x00" in argument:
            reason = "NUL"
        elif _RESERVED_INTERPOLATION.search(argument):
            reason = "UNDECLARED_INTERPOLATION"
        if reason is not None:
            raise PreflightError(
                PREFLIGHT_ARGUMENT_INVALID,
                ARGUMENT_MESSAGE,
                {
                    "step_id": step["id"],
                    "argument_index": index,
                    "reason": reason,
                },
            )


def _validate_environment(step: Mapping[str, Any]) -> None:
    environment = step.get("environment", {})
    for key in sorted(environment):
        value = environment[key]
        if not _ENVIRONMENT_NAME.fullmatch(key):
            reason = "INVALID_NAME"
        elif "\x00" in value:
            reason = "NUL"
        elif _RESERVED_INTERPOLATION.search(value):
            reason = "UNDECLARED_INTERPOLATION"
        else:
            continue
        raise PreflightError(
            PREFLIGHT_ENVIRONMENT_INVALID,
            ENVIRONMENT_MESSAGE,
            {"step_id": step["id"], "key": key, "reason": reason},
        )


def preflight_plan(
    plan: Mapping[str, Any],
    project_root: Path,
    *,
    inspector: PathInspector | None = None,
) -> dict[str, Any]:
    """Validate and normalize a compiled plan without process or file writes.

    ``project_root`` must already have passed the repository-boundary contract.
    The optional inspector is a test seam; production callers use the local,
    read-only implementation. The returned steps are a deep copy and never
    contain machine-specific absolute roots.
    """

    path_inspector = inspector or LocalPathInspector()
    canonical_root = path_inspector.resolve(project_root)
    steps: Sequence[dict[str, Any]] = copy.deepcopy(plan["steps"])

    for step in steps:
        _validate_paths(step, canonical_root, path_inspector)
        _validate_arguments(step)
        _validate_environment(step)

    return {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "plan_id": plan["plan_id"],
        "steps": list(steps),
    }
