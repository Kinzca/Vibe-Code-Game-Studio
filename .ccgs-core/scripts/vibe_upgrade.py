#!/usr/bin/env python3
"""Deterministic, repository-safe framework upgrade planning and apply support."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ccgs_codex_bridge import (
    BRIDGE_VERSION,
    CodexBridgeError,
    build_codex_plan,
    codex_target_paths,
)
from vibe_project_manifest import (
    DEFAULT_MANIFEST_PATH,
    SUPPORTED_SCHEMA_VERSIONS,
    ManifestError,
    validate_manifest_document,
)


CONTRACT_VERSION = "1.0"
MAX_DOCUMENT_BYTES = 1_048_576
MAX_MANAGED_FILES = 1000
MAX_MIGRATIONS = 100
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class UpgradeError(ValueError):
    """Stable, bounded upgrade failure safe for machine output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def public_error(self) -> dict[str, object]:
        """Return the non-retryable public error contract."""

        return {"code": self.code, "message": self.message, "retryable": False}


@dataclass(frozen=True)
class MigrationStep:
    """One explicit, single-version manifest migration."""

    id: str
    component: str
    from_version: str
    to_version: str
    reversible: bool
    transform: Callable[[Mapping[str, Any]], Mapping[str, Any]]

    def public(self) -> dict[str, object]:
        """Return only the versioned public migration fields."""

        return {
            "id": self.id,
            "component": self.component,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "reversible": self.reversible,
        }


class MigrationRegistry:
    """Validated deterministic registry for explicit manifest migrations."""

    def __init__(self, steps: Sequence[MigrationStep] = ()) -> None:
        if len(steps) > MAX_MIGRATIONS:
            raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration registry exceeds its bounded size")
        ids: set[str] = set()
        edges: set[tuple[str, str, str]] = set()
        outgoing: dict[tuple[str, str], MigrationStep] = {}
        for step in steps:
            if not SAFE_IDENTIFIER.fullmatch(step.id) or not SAFE_IDENTIFIER.fullmatch(step.component):
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration registry contains an invalid identifier")
            if not SAFE_IDENTIFIER.fullmatch(step.from_version) or not SAFE_IDENTIFIER.fullmatch(step.to_version):
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration registry contains an invalid version")
            if step.id in ids:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration registry contains a duplicate ID")
            edge = (step.component, step.from_version, step.to_version)
            key = (step.component, step.from_version)
            if edge in edges or key in outgoing or step.from_version == step.to_version:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration registry is ambiguous or cyclic")
            ids.add(step.id)
            edges.add(edge)
            outgoing[key] = step
        self._outgoing = outgoing

    def path(self, component: str, source: str, target: str) -> tuple[MigrationStep, ...]:
        """Return the unique consecutive path, failing closed on gaps or cycles."""

        if source == target:
            return ()
        current = source
        visited: set[str] = set()
        result: list[MigrationStep] = []
        while current != target:
            if current in visited or len(result) >= MAX_MIGRATIONS:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "registered migration path contains a cycle")
            visited.add(current)
            step = self._outgoing.get((component, current))
            if step is None:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "no registered migration path reaches a supported version")
            result.append(step)
            current = step.to_version
        return tuple(result)

    @staticmethod
    def apply(document: Mapping[str, Any], path: Sequence[MigrationStep]) -> dict[str, Any]:
        """Apply pure migrations and reject mutation, loss, or non-determinism."""

        try:
            current = json.loads(_canonical_json(document))
        except Exception as exc:
            raise UpgradeError(
                "MIGRATION_PATH_UNAVAILABLE",
                "registered migration input is not canonical JSON",
            ) from exc
        for step in path:
            if current.get("schema_version") != step.from_version:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration input does not match its declared source version")
            before = _canonical_json(current)
            try:
                first_value = step.transform(json.loads(before))
                second_value = step.transform(json.loads(before))
                if not isinstance(first_value, Mapping) or not isinstance(second_value, Mapping):
                    raise TypeError("migration output is not a mapping")
                first = dict(first_value)
                second = dict(second_value)
                first_bytes = _canonical_json(first)
                second_bytes = _canonical_json(second)
            except Exception as exc:
                raise UpgradeError(
                    "MIGRATION_PATH_UNAVAILABLE",
                    "registered migration could not produce a valid deterministic output",
                ) from exc
            if first_bytes != second_bytes:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration output is not deterministic")
            if first.get("schema_version") != step.to_version:
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration output does not declare its target version")
            if not (set(current) - {"schema_version"}).issubset(first):
                raise UpgradeError("MIGRATION_PATH_UNAVAILABLE", "migration output silently discards input fields")
            current = first
        return current


DEFAULT_MIGRATIONS = MigrationRegistry()


@dataclass(frozen=True)
class PreparedUpgrade:
    """Public plan plus private desired bytes used by an authorized Apply."""

    plan: dict[str, object]
    desired: Mapping[str, bytes]
    receipt_path: str


@dataclass(frozen=True)
class FileSnapshot:
    """Rollback state for one target before Apply."""

    existed: bool
    content: bytes
    mode: int
    mtime_ns: int


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_json(value: object, label: str) -> bytes:
    try:
        encoded = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise UpgradeError("UPGRADE_CONTRACT_INVALID", f"{label} is not canonical UTF-8 JSON") from exc
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise UpgradeError("UPGRADE_DOCUMENT_TOO_LARGE", f"{label} exceeds the 1 MiB contract limit")
    return encoded


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains an invalid managed path")
    if value.startswith(("/", "\\", "~/")) or value.casefold().startswith("file:"):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains an unsafe managed path")
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt paths must be relative POSIX paths")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains a path traversal")
    return value


def _receipt_relative(data_dir: str) -> str:
    data = _safe_relative(data_dir)
    return f"{data}/production/upgrade/installation.json"


def _read_bounded(path: Path, *, code: str, label: str) -> bytes:
    if path.is_symlink():
        raise UpgradeError(code, f"{label} may not be a symbolic link")
    if not path.is_file():
        raise UpgradeError(code, f"{label} is not a regular file")
    try:
        size = path.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            raise UpgradeError("UPGRADE_DOCUMENT_TOO_LARGE", f"{label} exceeds the 1 MiB contract limit")
        return path.read_bytes()
    except UpgradeError:
        raise
    except OSError as exc:
        raise UpgradeError(code, f"{label} could not be read") from exc


def _has_symlink_component(project: Path, relative: str) -> bool:
    current = project.resolve()
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


def _load_json_bytes(content: bytes, *, code: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise UpgradeError(code, f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise UpgradeError(code, f"{label} must be a JSON object")
    return value


def _validate_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SAFE_IDENTIFIER.fullmatch(value) is None:
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", f"installation receipt has an invalid {label}")
    return value


def load_installation_receipt(project: Path, data_dir: str) -> tuple[str, dict[str, Any] | None]:
    """Load and strictly validate Installation Receipt 1.0 without writing."""

    relative = _receipt_relative(data_dir)
    path = project.resolve() / relative
    if _has_symlink_component(project, relative):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt path may not contain symbolic links")
    if not path.exists() and not path.is_symlink():
        return relative, None
    document = _load_json_bytes(
        _read_bounded(path, code="INSTALLATION_RECEIPT_INVALID", label="installation receipt"),
        code="INSTALLATION_RECEIPT_INVALID",
        label="installation receipt",
    )
    expected = {
        "contract_version", "framework_version", "bridge_version",
        "manifest_schema_version", "managed_files",
    }
    if set(document) != expected or document.get("contract_version") != CONTRACT_VERSION:
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt fields or contract version are invalid")
    for name in ("framework_version", "bridge_version", "manifest_schema_version"):
        _validate_identifier(document[name], label=name)
    managed = document["managed_files"]
    if not isinstance(managed, list) or len(managed) > MAX_MANAGED_FILES:
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt managed files are invalid")
    normalized: list[dict[str, str]] = []
    for item in managed:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains an invalid managed file")
        item_path = _safe_relative(item["path"])
        digest = item["sha256"]
        if item_path == relative or not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains an invalid managed digest")
        normalized.append({"path": item_path, "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt managed files must be uniquely sorted")
    if len({item["path"] for item in normalized}) != len(normalized):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt contains duplicate managed paths")
    document["managed_files"] = normalized
    return relative, document


def _manifest_document(project: Path) -> dict[str, Any] | None:
    path = project / DEFAULT_MANIFEST_PATH
    if not path.exists() and not path.is_symlink():
        return None
    document = _load_json_bytes(
        _read_bounded(path, code="MANIFEST_SCHEMA_INVALID", label="project workflow manifest"),
        code="MANIFEST_SCHEMA_INVALID",
        label="project workflow manifest",
    )
    version = document.get("schema_version")
    if not isinstance(version, str) or SAFE_IDENTIFIER.fullmatch(version) is None:
        raise UpgradeError("MANIFEST_SCHEMA_INVALID", "project workflow manifest schema version is invalid")
    return document


def _prepare_manifest_migration(
    framework: Path,
    project: Path,
    target_schema: str,
    supported_versions: Sequence[str],
    migrations: MigrationRegistry,
) -> tuple[str | None, tuple[MigrationStep, ...], bytes | None]:
    """Prepare and validate an explicit manifest migration without writing."""

    document = _manifest_document(project)
    if document is None:
        return None, (), None
    declared = str(document["schema_version"])
    if declared in supported_versions:
        try:
            validate_manifest_document(document, framework)
        except ManifestError as exc:
            raise UpgradeError(
                exc.code,
                "project workflow manifest does not satisfy its declared schema",
            ) from exc
        return declared, (), None
    path = migrations.path("manifest", declared, target_schema)
    migrated = MigrationRegistry.apply(document, path)
    try:
        validated = validate_manifest_document(migrated, framework)
    except ManifestError as exc:
        raise UpgradeError(
            "MIGRATION_PATH_UNAVAILABLE",
            "registered migration output does not satisfy the target manifest schema",
        ) from exc
    if validated.get("schema_version") != target_schema:
        raise UpgradeError(
            "MIGRATION_PATH_UNAVAILABLE",
            "registered migration output is not a supported target version",
        )
    return declared, path, _bounded_json(migrated, "migrated project workflow manifest") + b"\n"


def _case_conflict(project: Path, relative: str) -> bool:
    parent = project
    for part in relative.split("/"):
        if not parent.is_dir():
            return False
        try:
            matches = [item.name for item in parent.iterdir() if item.name.casefold() == part.casefold()]
        except OSError as exc:
            raise UpgradeError("UPGRADE_WRITE_POLICY_DENIED", "an upgrade target could not be safely resolved") from exc
        if len(matches) > 1 or (matches and matches[0] != part):
            return True
        parent = parent / part
    return False


def _current_hash(project: Path, relative: str) -> str | None:
    target = project / relative
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_file():
        raise UpgradeError("UPGRADE_UNMANAGED_CONFLICT", "an upgrade target is not a regular managed file")
    return _hash(_read_bounded(target, code="UPGRADE_UNMANAGED_CONFLICT", label="managed target"))


def _doctor_errors(report: Mapping[str, Any]) -> int:
    summary = report.get("summary")
    if (
        not isinstance(summary, Mapping)
        or type(summary.get("error")) is not int
        or summary["error"] < 0
    ):
        raise UpgradeError("UPGRADE_DOCTOR_FAILED", "Doctor returned an invalid result")
    return int(summary["error"])


def _checked_result(result: dict[str, object]) -> dict[str, object]:
    """Enforce the bounded canonical Upgrade Result contract."""

    _bounded_json(result, "upgrade result")
    return result


def _failure_result(
    plan_id: str,
    code: str,
    message: str,
    *,
    before: int = 0,
    after: int = 0,
    rolled_back: bool = False,
) -> dict[str, object]:
    return _checked_result({
        "contract_version": CONTRACT_VERSION,
        "plan_id": plan_id,
        "outcome": "failed",
        "written": False,
        "reused": False,
        "applied_writes": [],
        "doctor": {
            "before_errors": before,
            "after_errors": after,
            "status": "rolled-back" if rolled_back else "failed",
        },
        "failures": [{"code": code, "message": message, "retryable": False}],
    })


def prepare_upgrade(
    framework: Path,
    project: Path,
    data_dir: str,
    framework_version: str,
    *,
    bridge_version: str = BRIDGE_VERSION,
    supported_versions: Sequence[str] = SUPPORTED_SCHEMA_VERSIONS,
    migrations: MigrationRegistry = DEFAULT_MIGRATIONS,
    validate_target: Callable[[Path, Path, str], Path] | None = None,
) -> PreparedUpgrade:
    """Build a deterministic zero-write Upgrade Plan and desired byte map."""

    framework = framework.resolve()
    project = project.resolve()
    target_schema = supported_versions[-1] if supported_versions else ""
    for label, value in (("framework version", framework_version), ("bridge version", bridge_version), ("manifest schema version", target_schema)):
        if SAFE_IDENTIFIER.fullmatch(value) is None:
            raise UpgradeError("UPGRADE_VERSION_INVALID", f"target {label} is invalid")
    receipt_relative, receipt = load_installation_receipt(project, data_dir)
    declared_schema, manifest_migrations, migrated_manifest = _prepare_manifest_migration(
        framework,
        project,
        target_schema,
        supported_versions,
        migrations,
    )

    conflicts: list[dict[str, str]] = []
    desired: dict[str, bytes] = {}
    symlink_targets = [
        relative for relative in codex_target_paths()
        if _has_symlink_component(project, relative)
    ]
    if symlink_targets:
        conflicts.extend(
            {"path": relative, "code": "UPGRADE_UNMANAGED_CONFLICT"}
            for relative in symlink_targets
        )
        bridge_plan = None
    else:
        try:
            bridge_plan = build_codex_plan(framework, project, data_dir)
        except CodexBridgeError as exc:
            code = "UPGRADE_UNMANAGED_CONFLICT" if exc.code == "WRITE_PLAN_INVALID" else exc.code
            location = "AGENTS.md" if exc.location == "." else exc.location
            conflicts.append({"path": location, "code": code})
            bridge_plan = None

    managed_truth = {item["path"]: item["sha256"] for item in (receipt or {}).get("managed_files", [])}
    allowed_owned = set(codex_target_paths())
    if any(path not in allowed_owned for path in managed_truth):
        raise UpgradeError("INSTALLATION_RECEIPT_INVALID", "installation receipt declares an unsupported managed target")

    writes: list[dict[str, object]] = []
    if bridge_plan is not None:
        for item in bridge_plan.files:
            if validate_target is not None:
                try:
                    validate_target(project, Path(item.path), data_dir)
                except Exception as exc:
                    raise UpgradeError("UPGRADE_WRITE_POLICY_DENIED", "an upgrade target is outside the managed write policy") from exc
            if _case_conflict(project, item.path):
                conflicts.append({"path": item.path, "code": "UPGRADE_UNMANAGED_CONFLICT"})
                continue
            before = _current_hash(project, item.path)
            expected = managed_truth.get(item.path)
            if receipt is not None and expected is not None and before != expected:
                conflicts.append({"path": item.path, "code": "UPGRADE_MANAGED_DRIFT"})
            elif receipt is not None and expected is None and before is not None:
                conflicts.append({"path": item.path, "code": "UPGRADE_UNMANAGED_CONFLICT"})
            encoded = item.content.encode("utf-8")
            desired[item.path] = encoded
            writes.append({
                "path": item.path,
                "action": item.action,
                "before_sha256": before,
                "after_sha256": _hash(encoded),
            })

    if migrated_manifest is not None:
        if validate_target is not None:
            try:
                validate_target(project, Path(DEFAULT_MANIFEST_PATH), data_dir)
            except Exception as exc:
                raise UpgradeError(
                    "UPGRADE_WRITE_POLICY_DENIED",
                    "project workflow manifest is outside the managed migration policy",
                ) from exc
        if _case_conflict(project, DEFAULT_MANIFEST_PATH):
            conflicts.append({"path": DEFAULT_MANIFEST_PATH, "code": "UPGRADE_UNMANAGED_CONFLICT"})
        before = _current_hash(project, DEFAULT_MANIFEST_PATH)
        desired[DEFAULT_MANIFEST_PATH] = migrated_manifest
        writes.append({
            "path": DEFAULT_MANIFEST_PATH,
            "action": "update",
            "before_sha256": before,
            "after_sha256": _hash(migrated_manifest),
        })

    target_managed = [
        {"path": path, "sha256": _hash(content)}
        for path, content in sorted(desired.items())
        if path in allowed_owned
    ]
    receipt_document = {
        "contract_version": CONTRACT_VERSION,
        "framework_version": framework_version,
        "bridge_version": bridge_version,
        "manifest_schema_version": target_schema,
        "managed_files": target_managed,
    }
    receipt_content = _bounded_json(receipt_document, "installation receipt") + b"\n"
    if validate_target is not None:
        try:
            validate_target(project, Path(receipt_relative), data_dir)
        except Exception as exc:
            raise UpgradeError("UPGRADE_WRITE_POLICY_DENIED", "installation receipt is outside the managed write policy") from exc
    if _case_conflict(project, receipt_relative):
        conflicts.append({"path": receipt_relative, "code": "UPGRADE_UNMANAGED_CONFLICT"})
    receipt_before = _current_hash(project, receipt_relative)
    desired[receipt_relative] = receipt_content
    writes.append({
        "path": receipt_relative,
        "action": "create" if receipt_before is None else "unchanged" if receipt_before == _hash(receipt_content) else "update",
        "before_sha256": receipt_before,
        "after_sha256": _hash(receipt_content),
    })

    current = {
        "framework_version": receipt["framework_version"] if receipt else "untracked",
        "bridge_version": receipt["bridge_version"] if receipt else "untracked",
        "manifest_schema_version": receipt["manifest_schema_version"] if receipt else "untracked",
    }
    target = {
        "framework_version": framework_version,
        "bridge_version": bridge_version,
        "manifest_schema_version": target_schema,
    }
    migration_steps = manifest_migrations
    if not migration_steps and receipt and receipt["manifest_schema_version"] != target_schema:
        migration_steps = migrations.path("manifest", receipt["manifest_schema_version"], target_schema)
    if receipt is None:
        compatibility = "untracked"
    elif migration_steps:
        compatibility = "migration-required"
    elif current == target:
        compatibility = "compatible"
    else:
        compatibility = "compatible"
    if receipt and declared_schema and receipt["manifest_schema_version"] != declared_schema:
        conflicts.append({"path": DEFAULT_MANIFEST_PATH, "code": "UPGRADE_MANIFEST_VERSION_MISMATCH"})

    base: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "mode": "dry-run",
        "current": current,
        "target": target,
        "compatibility": compatibility,
        "migrations": [step.public() for step in sorted(migration_steps, key=lambda step: step.id)],
        "writes": sorted(writes, key=lambda item: str(item["path"])),
        "conflicts": sorted(
            {(_safe_relative(item["path"]), item["code"]) for item in conflicts},
            key=lambda item: (item[0], item[1]),
        ),
        "doctor_required": True,
    }
    base["conflicts"] = [{"path": path, "code": code} for path, code in base["conflicts"]]
    plan_id = _hash(_bounded_json(base, "upgrade plan"))
    plan = {"contract_version": CONTRACT_VERSION, "plan_id": plan_id, **{key: value for key, value in base.items() if key != "contract_version"}}
    _bounded_json(plan, "upgrade plan")
    return PreparedUpgrade(plan=plan, desired=desired, receipt_path=receipt_relative)


def build_upgrade_plan(*args: Any, **kwargs: Any) -> dict[str, object]:
    """Return only the public deterministic Upgrade Plan 1.0."""

    return prepare_upgrade(*args, **kwargs).plan


def _snapshot(path: Path) -> FileSnapshot:
    if not path.exists() and not path.is_symlink():
        return FileSnapshot(False, b"", 0, 0)
    if path.is_symlink() or not path.is_file():
        raise UpgradeError("UPGRADE_UNMANAGED_CONFLICT", "an upgrade target changed type before Apply")
    metadata = path.stat()
    return FileSnapshot(True, path.read_bytes(), stat.S_IMODE(metadata.st_mode), metadata.st_mtime_ns)


def _assert_before_state(project: Path, item: Mapping[str, object]) -> None:
    """Reject a target that changed after the authorized plan was built."""

    relative = str(item["path"])
    if _has_symlink_component(project, relative) or _case_conflict(project, relative):
        raise UpgradeError("UPGRADE_PLAN_STALE", "an upgrade target changed after planning")
    try:
        current = _current_hash(project, relative)
    except UpgradeError as exc:
        raise UpgradeError("UPGRADE_PLAN_STALE", "an upgrade target changed after planning") from exc
    if current != item["before_sha256"]:
        raise UpgradeError("UPGRADE_PLAN_STALE", "an upgrade target changed after planning")


def _read_back(path: Path) -> bytes:
    """Read one replaced target for injectable post-write verification."""

    return path.read_bytes()


def secure_atomic_replace_supported() -> bool:
    """Return whether the runtime exposes the required no-follow atomic primitives."""

    required = (os.open, os.mkdir, os.rename, os.stat, os.unlink)
    return bool(
        getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and all(function in os.supports_dir_fd for function in required)
    )


def _open_parent_directory(path: Path, *, create_missing: bool = True) -> int:
    """Open/create the parent chain without following a symbolic link."""

    if (
        not path.is_absolute()
        or not secure_atomic_replace_supported()
    ):
        raise UpgradeError(
            "UPGRADE_WRITE_POLICY_DENIED",
            "secure no-follow replacement is unavailable on this platform",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError as exc:
                if not create_missing:
                    raise UpgradeError(
                        "UPGRADE_PLAN_STALE",
                        "upgrade target parent changed before replacement",
                    ) from exc
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except UpgradeError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UpgradeError(
                "UPGRADE_PLAN_STALE",
                "upgrade target path changed before replacement",
            ) from exc
        raise


def _assert_same_parent(path: Path, directory_fd: int) -> None:
    """Verify the current no-follow path still names the opened parent inode."""

    current_fd = _open_parent_directory(path, create_missing=False)
    try:
        opened = os.fstat(directory_fd)
        current = os.fstat(current_fd)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise UpgradeError(
                "UPGRADE_PLAN_STALE",
                "upgrade target parent changed before replacement",
            )
    finally:
        os.close(current_fd)


def _assert_regular_target(directory_fd: int, name: str) -> None:
    """Reject a target entry that cannot be safely replaced as a regular file."""

    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise UpgradeError(
            "UPGRADE_PLAN_STALE",
            "upgrade target changed type before replacement",
        )


def atomic_replace_file(
    path: Path,
    content: bytes,
    mode: int,
    *,
    before_commit: Callable[[Path], None] | None = None,
) -> None:
    """Atomically replace a file through a no-follow parent directory handle."""

    directory_fd = _open_parent_directory(path)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    file_fd = -1
    try:
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        os.fchmod(file_fd, mode)
        with os.fdopen(file_fd, "wb") as handle:
            file_fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if before_commit is not None:
            before_commit(path)
        _assert_same_parent(path, directory_fd)
        _assert_regular_target(directory_fd, path.name)
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        os.fsync(directory_fd)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _restore(
    project: Path,
    snapshots: Mapping[str, FileSnapshot],
    created_dirs: Sequence[Path],
    directory_snapshots: Mapping[Path, tuple[int, int]],
) -> None:
    for relative, snapshot in reversed(list(snapshots.items())):
        if _has_symlink_component(project, relative):
            raise UpgradeError("UPGRADE_ROLLBACK_FAILED", "rollback target contains a symbolic link")
        target = project / relative
        if snapshot.existed:
            atomic_replace_file(target, snapshot.content, snapshot.mode)
            os.utime(target, ns=(snapshot.mtime_ns, snapshot.mtime_ns))
        elif target.exists() or target.is_symlink():
            if target.is_file() or target.is_symlink():
                target.unlink()
    for directory in sorted(set(created_dirs), key=lambda value: len(value.parts), reverse=True):
        if directory.is_symlink():
            raise UpgradeError("UPGRADE_ROLLBACK_FAILED", "rollback directory became a symbolic link")
        if directory.exists():
            directory.rmdir()
    for directory, (mode, mtime_ns) in sorted(
        directory_snapshots.items(), key=lambda item: len(item[0].parts), reverse=True
    ):
        if directory.is_symlink():
            raise UpgradeError("UPGRADE_ROLLBACK_FAILED", "rollback directory became a symbolic link")
        os.chmod(directory, mode)
        os.utime(directory, ns=(mtime_ns, mtime_ns))


def apply_upgrade(
    framework: Path,
    project: Path,
    data_dir: str,
    framework_version: str,
    expected_plan_id: str,
    doctor: Callable[[Path, Path], Mapping[str, Any]],
    *,
    bridge_version: str = BRIDGE_VERSION,
    supported_versions: Sequence[str] = SUPPORTED_SCHEMA_VERSIONS,
    migrations: MigrationRegistry = DEFAULT_MIGRATIONS,
    validate_target: Callable[[Path, Path, str], Path] | None = None,
    replace_file: Callable[[Path, bytes, int], None] = atomic_replace_file,
    read_back: Callable[[Path], bytes] = _read_back,
) -> dict[str, object]:
    """Re-plan, authorize, and transactionally apply one Upgrade Plan."""

    prepared = prepare_upgrade(
        framework, project, data_dir, framework_version,
        bridge_version=bridge_version,
        supported_versions=supported_versions,
        migrations=migrations,
        validate_target=validate_target,
    )
    plan = prepared.plan
    plan_id = str(plan["plan_id"])
    if not SHA256.fullmatch(expected_plan_id) or expected_plan_id != plan_id:
        return _failure_result(plan_id, "UPGRADE_PLAN_STALE", "upgrade plan authorization is stale")
    if plan["conflicts"]:
        first = plan["conflicts"][0]
        return _failure_result(plan_id, str(first["code"]), "upgrade plan contains a managed-file conflict")
    try:
        before_errors = _doctor_errors(doctor(project.resolve(), framework.resolve()))
    except Exception as exc:
        code = exc.code if isinstance(exc, UpgradeError) else "UPGRADE_DOCTOR_FAILED"
        return _failure_result(plan_id, code, "pre-upgrade Doctor failed")
    if before_errors:
        return _failure_result(plan_id, "UPGRADE_DOCTOR_FAILED", "pre-upgrade Doctor reported errors", before=before_errors)

    try:
        refreshed = prepare_upgrade(
            framework, project, data_dir, framework_version,
            bridge_version=bridge_version,
            supported_versions=supported_versions,
            migrations=migrations,
            validate_target=validate_target,
        )
    except UpgradeError as exc:
        return _failure_result(plan_id, exc.code, exc.message, before=before_errors)
    if refreshed.plan["plan_id"] != plan_id:
        return _failure_result(
            plan_id,
            "UPGRADE_PLAN_STALE",
            "upgrade target state changed during pre-upgrade Doctor",
            before=before_errors,
        )
    prepared = refreshed
    plan = prepared.plan

    changed = [item for item in plan["writes"] if item["action"] != "unchanged"]
    if not changed:
        try:
            after_errors = _doctor_errors(doctor(project.resolve(), framework.resolve()))
        except Exception:
            return _failure_result(
                plan_id,
                "UPGRADE_DOCTOR_FAILED",
                "post-upgrade Doctor failed",
                before=before_errors,
            )
        if after_errors:
            return _failure_result(plan_id, "UPGRADE_DOCTOR_FAILED", "post-upgrade Doctor reported errors", after=after_errors)
        return _checked_result({
            "contract_version": CONTRACT_VERSION,
            "plan_id": plan_id,
            "outcome": "reused",
            "written": False,
            "reused": True,
            "applied_writes": [],
            "doctor": {"before_errors": 0, "after_errors": 0, "status": "pass"},
            "failures": [],
        })

    if replace_file is atomic_replace_file and not secure_atomic_replace_supported():
        return _failure_result(
            plan_id,
            "UPGRADE_WRITE_POLICY_DENIED",
            "secure no-follow replacement is unavailable on this platform",
            before=before_errors,
        )

    project = project.resolve()
    try:
        for item in changed:
            _assert_before_state(project, item)
    except UpgradeError as exc:
        return _failure_result(plan_id, exc.code, exc.message, before=before_errors)
    snapshots: dict[str, FileSnapshot] = {}
    created_dirs: list[Path] = []
    directory_snapshots: dict[Path, tuple[int, int]] = {}
    ordered = sorted((item for item in changed if item["path"] != prepared.receipt_path), key=lambda item: item["path"])
    receipt_item = next((item for item in changed if item["path"] == prepared.receipt_path), None)
    try:
        for item in changed:
            relative = str(item["path"])
            snapshots[relative] = _snapshot(project / relative)
            parent = (project / relative).parent
            existing_parent = parent
            while True:
                if existing_parent.is_dir() and existing_parent not in directory_snapshots:
                    metadata = existing_parent.stat()
                    directory_snapshots[existing_parent] = (
                        stat.S_IMODE(metadata.st_mode), metadata.st_mtime_ns,
                    )
                if existing_parent == project:
                    break
                existing_parent = existing_parent.parent
            missing: list[Path] = []
            while parent != project and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            created_dirs.extend(reversed(missing))
        for item in ordered:
            relative = str(item["path"])
            _assert_before_state(project, item)
            snapshot = snapshots[relative]
            mode = snapshot.mode if snapshot.existed else 0o644
            replace_file(project / relative, prepared.desired[relative], mode)
            if _hash(read_back(project / relative)) != item["after_sha256"]:
                raise UpgradeError("UPGRADE_VERIFY_FAILED", "an upgraded managed file failed verification")
        try:
            after_errors = _doctor_errors(doctor(project, framework.resolve()))
        except Exception as exc:
            if isinstance(exc, UpgradeError):
                raise
            raise UpgradeError("UPGRADE_DOCTOR_FAILED", "post-upgrade Doctor failed") from exc
        if after_errors:
            raise UpgradeError("UPGRADE_DOCTOR_FAILED", "post-upgrade Doctor reported errors")
        for item in ordered:
            relative = str(item["path"])
            if _has_symlink_component(project, relative):
                raise UpgradeError("UPGRADE_VERIFY_FAILED", "an upgraded managed path changed during Doctor")
            if _hash(read_back(project / relative)) != item["after_sha256"]:
                raise UpgradeError("UPGRADE_VERIFY_FAILED", "an upgraded managed file changed during Doctor")
        if receipt_item is not None:
            relative = prepared.receipt_path
            _assert_before_state(project, receipt_item)
            snapshot = snapshots[relative]
            mode = snapshot.mode if snapshot.existed else 0o644
            replace_file(project / relative, prepared.desired[relative], mode)
            if _hash(read_back(project / relative)) != receipt_item["after_sha256"]:
                raise UpgradeError("UPGRADE_VERIFY_FAILED", "installation receipt failed verification")
    except Exception as exc:
        try:
            _restore(project, snapshots, created_dirs, directory_snapshots)
        except Exception:
            return _failure_result(plan_id, "UPGRADE_ROLLBACK_FAILED", "upgrade rollback could not restore all managed files", before=before_errors, rolled_back=True)
        code = exc.code if isinstance(exc, UpgradeError) else "UPGRADE_WRITE_FAILED"
        message = exc.message if isinstance(exc, UpgradeError) else "upgrade file operation failed"
        return _failure_result(plan_id, code, message, before=before_errors, after=locals().get("after_errors", 0), rolled_back=True)

    applied = sorted(str(item["path"]) for item in changed)
    return _checked_result({
        "contract_version": CONTRACT_VERSION,
        "plan_id": plan_id,
        "outcome": "applied",
        "written": True,
        "reused": False,
        "applied_writes": applied,
        "doctor": {"before_errors": 0, "after_errors": 0, "status": "pass"},
        "failures": [],
    })
