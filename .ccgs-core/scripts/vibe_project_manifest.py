#!/usr/bin/env python3
"""Load the versioned, project-neutral workflow manifest contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CONTRACT_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = ("1.0",)
DEFAULT_MANIFEST_PATH = "vibe-workflow.json"
SCHEMA_RELATIVE_PATH = "schemas/project-workflow-manifest.schema.json"

MANIFEST_NOT_FOUND = "MANIFEST_NOT_FOUND"
MANIFEST_INVALID_JSON = "MANIFEST_INVALID_JSON"
MANIFEST_SCHEMA_INVALID = "MANIFEST_SCHEMA_INVALID"
MANIFEST_SCHEMA_UNSUPPORTED = "MANIFEST_SCHEMA_UNSUPPORTED"
MANIFEST_EMPTY_STEPS = "MANIFEST_EMPTY_STEPS"
MANIFEST_RETRIEVAL_UNSAFE = "MANIFEST_RETRIEVAL_UNSAFE"


@dataclass(frozen=True)
class ManifestError(ValueError):
    """A stable manifest failure suitable for machine-readable output."""

    code: str
    message: str
    details: Mapping[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def report(self, mode: str) -> dict[str, Any]:
        """Return this failure through the versioned machine-result contract."""

        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": False,
        }
        if self.details:
            error["details"] = dict(self.details)
        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "mode": mode,
            "error": error,
        }


def _reject_json_constant(value: str) -> None:
    """Reject NaN and infinity spellings that are outside standard JSON."""

    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_json(text: str) -> Any:
    """Parse strict JSON without Python's non-standard numeric constants."""

    return json.loads(text, parse_constant=_reject_json_constant)


def _load_schema(framework_root: Path) -> dict[str, Any]:
    path = framework_root.resolve() / SCHEMA_RELATIVE_PATH
    if not path.is_file():
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "framework manifest schema is missing",
            {"schema_path": SCHEMA_RELATIVE_PATH},
        )
    try:
        document = _parse_json(path.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "framework manifest schema cannot be read",
            {"schema_path": SCHEMA_RELATIVE_PATH},
        ) from exc
    if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "framework manifest schema must use JSON Schema Draft 2020-12",
            {"schema_path": SCHEMA_RELATIVE_PATH},
        )
    return document


def _resolve_manifest_path(project_root: Path, manifest_path: str | Path | None) -> tuple[Path, str]:
    project = project_root.resolve()
    if not project.is_dir():
        raise ManifestError(MANIFEST_NOT_FOUND, "consumer project root is missing")

    requested = Path(manifest_path or DEFAULT_MANIFEST_PATH)
    if requested.is_absolute():
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "manifest_path must be project-relative",
            {"manifest_path": "<absolute>"},
        )
    candidate = (project / requested).resolve(strict=False)
    try:
        relative = candidate.relative_to(project)
    except ValueError as exc:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "manifest_path escapes the explicit project root",
            {"manifest_path": requested.as_posix()},
        ) from exc
    if not relative.parts:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "manifest_path must identify a JSON file",
            {"manifest_path": requested.as_posix()},
        )
    return candidate, relative.as_posix()


def _invalid(path: str, message: str) -> ManifestError:
    return ManifestError(
        MANIFEST_SCHEMA_INVALID,
        "project workflow manifest does not match schema",
        {"path": path, "reason": message, "schema_path": SCHEMA_RELATIVE_PATH},
    )


def _validate_unicode_scalar(value: str, path: str) -> None:
    """Reject lone UTF-16 surrogates before producing machine-readable results."""

    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _invalid(path, "must contain only Unicode scalar values")


def _resolve_ref(root_schema: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise _invalid("$schema", f"unsupported schema reference: {reference}")
    current: Any = root_schema
    for part in reference[2:].split("/"):
        if not isinstance(current, Mapping) or part not in current:
            raise _invalid("$schema", f"unresolved schema reference: {reference}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise _invalid("$schema", f"schema reference is not an object: {reference}")
    return current


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in {int, float},
    }.get(expected, False)


def _validate_array(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
) -> None:
    """Validate array keywords used by the manifest schema."""

    if not isinstance(value, list):
        return
    if len(value) < int(schema.get("minItems", 0)):
        raise _invalid(path, f"must contain at least {schema['minItems']} item(s)")
    if "maxItems" in schema and len(value) > int(schema["maxItems"]):
        raise _invalid(path, f"must contain at most {schema['maxItems']} item(s)")
    if schema.get("uniqueItems"):
        markers = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
        if len(markers) != len(set(markers)):
            raise _invalid(path, "must not contain duplicate values")
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            _validate_schema_node(item, item_schema, root_schema, f"{path}[{index}]")


def _validate_object(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
) -> None:
    """Validate object keywords used by the manifest schema."""

    if not isinstance(value, dict):
        return
    for key in value:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
            raise _invalid(path, "object member names must contain only Unicode scalar values")
    required = schema.get("required", [])
    missing = sorted(set(required) - set(value))
    if missing:
        raise _invalid(path, f"missing required fields: {', '.join(missing)}")
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    unknown = sorted(set(value) - set(properties))
    if additional is False and unknown:
        raise _invalid(path, f"contains unknown fields: {', '.join(unknown)}")
    for key, item in value.items():
        if key in properties:
            _validate_schema_node(item, properties[key], root_schema, f"{path}.{key}")
        elif isinstance(additional, Mapping):
            _validate_schema_node(item, additional, root_schema, f"{path}.{key}")


def _validate_schema_node(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    path: str,
) -> None:
    """Validate the JSON Schema subset used by the public 1.0 contract."""

    if isinstance(value, str):
        _validate_unicode_scalar(value, path)
    if "$ref" in schema:
        _validate_schema_node(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path)
        return
    if "const" in schema and value != schema["const"]:
        raise _invalid(path, f"must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise _invalid(path, "must be one of the declared values")
    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, expected_type):
        raise _invalid(path, f"must be of type {expected_type}")

    if isinstance(value, str) and len(value) < int(schema.get("minLength", 0)):
        raise _invalid(path, "must be a non-empty string")
    if isinstance(value, str) and "maxLength" in schema and len(value) > int(schema["maxLength"]):
        raise _invalid(path, f"must contain at most {schema['maxLength']} character(s)")
    if isinstance(value, str) and "pattern" in schema:
        try:
            matched = re.fullmatch(str(schema["pattern"]), value)
        except re.error as exc:
            raise _invalid("$schema", "contains an invalid regular expression") from exc
        if matched is None:
            raise _invalid(path, "must match the declared pattern")
    _validate_array(value, schema, root_schema, path)
    _validate_object(value, schema, root_schema, path)


def _validate_document(document: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise _invalid("$", "must be an object")

    version = document.get("schema_version")
    if not isinstance(version, str):
        raise _invalid("$.schema_version", "must be a string")
    _validate_unicode_scalar(version, "$.schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(
            MANIFEST_SCHEMA_UNSUPPORTED,
            f"project workflow manifest schema version {version!r} is unsupported",
            {
                "requested_version": version,
                "supported_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
                "migration_hint": "Migrate the manifest to schema_version 1.0 before execution.",
                "migration": {"command": "upgrade", "mode": "dry-run"},
            },
        )

    _validate_schema_node(document, schema, schema, "$")
    steps = document["steps"]
    step_ids: set[str] = set()
    for index, step in enumerate(steps):
        step_id = step["id"]
        if step_id in step_ids:
            raise _invalid(f"$.steps[{index}].id", f"duplicate step id: {step_id}")
        step_ids.add(step_id)

    retrieval = document.get("retrieval")
    if retrieval is not None:
        source_ids: set[str] = set()
        source_paths: set[str] = set()
        for index, source in enumerate(retrieval["sources"]):
            source_path = source["path"]
            if (
                source_path.startswith(("/", "\\", "~/"))
                or source_path.casefold().startswith("file:")
                or re.match(r"^[A-Za-z]:", source_path)
                or any(part in {"", ".", ".."} for part in source_path.replace("\\", "/").split("/"))
            ):
                raise ManifestError(
                    MANIFEST_RETRIEVAL_UNSAFE,
                    "project workflow manifest contains an unsafe retrieval source",
                    {"path": f"$.retrieval.sources[{index}].path", "schema_path": SCHEMA_RELATIVE_PATH},
                )
            if "\\" in source_path or "*" in source_path or "?" in source_path or re.fullmatch(
                r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*", source_path
            ) is None:
                raise _invalid(f"$.retrieval.sources[{index}].path", "must be a concrete project-relative POSIX file")
            if source["source_id"] in source_ids:
                raise _invalid(f"$.retrieval.sources[{index}].source_id", "must be unique")
            if source["path"] in source_paths:
                raise _invalid(f"$.retrieval.sources[{index}].path", "must be unique")
            source_ids.add(source["source_id"])
            source_paths.add(source["path"])

    return document


def load_manifest(
    project_root: Path,
    framework_root: Path,
    manifest_path: str | Path | None = None,
    *,
    for_execution: bool = False,
) -> dict[str, Any]:
    """Load and validate one manifest without starting subprocesses or inferring commands."""

    schema = _load_schema(framework_root)
    path, relative_path = _resolve_manifest_path(project_root, manifest_path)
    if not path.is_file():
        raise ManifestError(
            MANIFEST_NOT_FOUND,
            "project workflow manifest was not found",
            {"manifest_path": relative_path},
        )
    try:
        document = _parse_json(path.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(
            MANIFEST_INVALID_JSON,
            "project workflow manifest is not valid UTF-8 JSON",
            {"manifest_path": relative_path},
        ) from exc
    except OSError as exc:
        raise ManifestError(
            MANIFEST_NOT_FOUND,
            "project workflow manifest could not be read",
            {"manifest_path": relative_path},
        ) from exc

    validated = validate_manifest_document(
        document,
        framework_root,
        for_execution=for_execution,
        schema=schema,
    )
    result = {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "mode": "execution-request" if for_execution else "diagnostic",
        "schema_version": validated["schema_version"],
        "schema_path": SCHEMA_RELATIVE_PATH,
        "manifest_path": relative_path,
        "steps": validated["steps"],
    }
    if "retrieval" in validated:
        result["retrieval"] = validated["retrieval"]
    return result


def validate_manifest_document(
    document: Mapping[str, Any],
    framework_root: Path,
    *,
    for_execution: bool = False,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an in-memory manifest through the public schema contract."""

    active_schema = dict(schema) if schema is not None else _load_schema(framework_root)
    validated = _validate_document(dict(document), active_schema)
    if for_execution and not validated["steps"]:
        raise ManifestError(
            MANIFEST_EMPTY_STEPS,
            "project workflow manifest declares no executable steps",
            {"manifest_path": DEFAULT_MANIFEST_PATH},
        )
    return validated
