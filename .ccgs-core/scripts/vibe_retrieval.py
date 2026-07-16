#!/usr/bin/env python3
"""Project-scoped semantic retrieval contracts and source resolution.

Only :func:`resolve_allowed_sources` receives a machine path.  Adapters receive
isolated logical records and Integration Port envelopes, never ``project_root``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from vibe_integration_ports import (
    CONTRACT_VERSION,
    IntegrationPortContractError,
    invoke_port,
    validate_port_request,
    validate_port_response,
)

MAX_SOURCE_BYTES = 4_000_000
MAX_QUERY_CHARS = 4000
MAX_RESULTS = 50
MAX_RESULT_TEXT = 2400
MAX_HEADING = 512
ALLOWED_MEDIA_TYPES = {"text/markdown", "application/json", "text/plain"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PATH = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)
_RESULT_FIELDS = {
    "result_id", "score", "source_id", "source_path", "heading",
    "chunk_index", "text",
}


def _fail(code: str) -> None:
    raise IntegrationPortContractError(code)


def _source_declaration(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    retrieval = manifest.get("retrieval")
    if not isinstance(retrieval, Mapping):
        _fail("PORT_REQUEST_INVALID")
    return retrieval


def _validate_source(source: Any) -> tuple[str, str, str]:
    if type(source) is not dict or set(source) != {"source_id", "path", "media_type"}:
        _fail("PORT_REQUEST_INVALID")
    source_id, path, media_type = source["source_id"], source["path"], source["media_type"]
    if type(source_id) is not str or _IDENTIFIER.fullmatch(source_id) is None:
        _fail("PORT_REQUEST_INVALID")
    if type(path) is not str:
        _fail("PORT_REQUEST_INVALID")
    if (
        path.startswith(("/", "\\", "~/"))
        or path.casefold().startswith("file:")
        or re.match(r"^[A-Za-z]:", path)
    ):
        _fail("PORT_PAYLOAD_UNSAFE")
    if "\\" in path or "*" in path or "?" in path or _PATH.fullmatch(path) is None:
        _fail("PORT_REQUEST_INVALID")
    if any(part in {"", ".", ".."} for part in PurePosixPath(path).parts):
        _fail("PORT_PAYLOAD_UNSAFE")
    if type(media_type) is not str or media_type not in ALLOWED_MEDIA_TYPES:
        _fail("PORT_REQUEST_INVALID")
    return source_id, path, media_type


def validate_retrieval_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Retrieval Source Declaration 1.0 and return an isolated copy."""

    retrieval = _source_declaration(manifest)
    if set(retrieval) != {"contract_version", "sources"}:
        _fail("PORT_REQUEST_INVALID")
    if retrieval["contract_version"] != CONTRACT_VERSION:
        _fail("PORT_VERSION_UNSUPPORTED")
    sources = retrieval["sources"]
    if type(sources) is not list or not 1 <= len(sources) <= 100:
        _fail("PORT_REQUEST_INVALID")
    identities = [_validate_source(item) for item in sources]
    if len({item[0] for item in identities}) != len(identities):
        _fail("PORT_REQUEST_INVALID")
    if len({item[1] for item in identities}) != len(identities):
        _fail("PORT_REQUEST_INVALID")
    return copy.deepcopy(dict(retrieval))


def _canonical_text(raw: bytes, media_type: str) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("PORT_REQUEST_INVALID")
    if media_type == "application/json":
        try:
            document = json.loads(text, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):
            _fail("PORT_REQUEST_INVALID")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def resolve_allowed_sources(
    project_root: Path, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Resolve only explicitly allowed files beneath ``project_root``.

    Directories, missing files, symlink escapes, duplicate real paths, files
    over four million bytes, invalid UTF-8, and malformed JSON fail closed.
    Returned records contain project-relative references only.
    """

    retrieval = validate_retrieval_config(manifest)
    project = Path(project_root).resolve()
    if not project.is_dir():
        _fail("PORT_REQUEST_INVALID")
    resolved_paths: set[Path] = set()
    records: list[dict[str, Any]] = []
    for source in retrieval["sources"]:
        source_id, relative, media_type = _validate_source(source)
        candidate = project.joinpath(*PurePosixPath(relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(project)
        except ValueError:
            _fail("PORT_PAYLOAD_UNSAFE")
        except OSError:
            _fail("PORT_REQUEST_INVALID")
        if resolved in resolved_paths:
            _fail("PORT_PAYLOAD_UNSAFE")
        if not resolved.is_file():
            _fail("PORT_REQUEST_INVALID")
        try:
            if resolved.stat().st_size > MAX_SOURCE_BYTES:
                _fail("PORT_REQUEST_INVALID")
            raw = resolved.read_bytes()
        except OSError:
            _fail("PORT_REQUEST_INVALID")
        text = _canonical_text(raw, media_type)
        resolved_paths.add(resolved)
        records.append({
            "source_id": source_id,
            "path": relative,
            "media_type": media_type,
            "text": text,
            "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return tuple(records)


def build_retrieval_request(
    manifest: Mapping[str, Any], *, request_id: str, project_id: str,
    query: str, source_ids: Sequence[str], limit: int = 10,
    min_score: float = -1.0,
) -> dict[str, Any]:
    """Build and validate one project-scoped Retrieval Port Request 1.0."""

    retrieval = validate_retrieval_config(manifest)
    declared = {item["source_id"]: item["path"] for item in retrieval["sources"]}
    if type(source_ids) not in {list, tuple}:
        _fail("PORT_REQUEST_INVALID")
    ids = list(source_ids)
    if type(query) is not str:
        _fail("PORT_REQUEST_INVALID")
    query = query.strip()
    if not 1 <= len(query) <= MAX_QUERY_CHARS:
        _fail("PORT_REQUEST_INVALID")
    if not ids or len(ids) > 100 or any(type(item) is not str for item in ids):
        _fail("PORT_REQUEST_INVALID")
    if len(set(ids)) != len(ids) or any(item not in declared for item in ids):
        _fail("PORT_REQUEST_INVALID")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        _fail("PORT_REQUEST_INVALID")
    if type(min_score) not in {int, float} or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        _fail("PORT_REQUEST_INVALID")
    request = {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "project_id": project_id,
        "port": "retrieval",
        "operation": "retrieve",
        "capability": "semantic_search",
        "payload": {"query": query, "source_ids": ids, "limit": limit, "min_score": float(min_score)},
        "references": sorted(declared[item] for item in ids),
    }
    return validate_retrieval_request(request, manifest)


def validate_retrieval_request(
    request: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a Retrieval Port request against its current Manifest."""

    request_copy = validate_port_request(request)
    if (
        request_copy["port"] != "retrieval"
        or request_copy["operation"] != "retrieve"
        or request_copy["capability"] != "semantic_search"
    ):
        _fail("PORT_REQUEST_INVALID")
    payload = request_copy["payload"]
    if set(payload) != {"query", "source_ids", "limit", "min_score"}:
        _fail("PORT_REQUEST_INVALID")
    expected = build_retrieval_request_data(payload, manifest)
    if payload != expected["payload"] or request_copy["references"] != expected["references"]:
        _fail("PORT_REQUEST_INVALID")
    return request_copy


def build_retrieval_request_data(
    payload: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalize Retrieval payload and references without rebuilding identity."""

    retrieval = validate_retrieval_config(manifest)
    declared = {item["source_id"]: item["path"] for item in retrieval["sources"]}
    if type(payload) is not dict or set(payload) != {"query", "source_ids", "limit", "min_score"}:
        _fail("PORT_REQUEST_INVALID")
    query, ids = payload["query"], payload["source_ids"]
    limit, min_score = payload["limit"], payload["min_score"]
    if type(query) is not str or query != query.strip() or not 1 <= len(query) <= MAX_QUERY_CHARS:
        _fail("PORT_REQUEST_INVALID")
    if type(ids) is not list or not ids or len(ids) > 100:
        _fail("PORT_REQUEST_INVALID")
    if any(type(item) is not str for item in ids) or len(set(ids)) != len(ids):
        _fail("PORT_REQUEST_INVALID")
    if any(item not in declared for item in ids):
        _fail("PORT_REQUEST_INVALID")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        _fail("PORT_REQUEST_INVALID")
    if type(min_score) not in {int, float} or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        _fail("PORT_REQUEST_INVALID")
    return {
        "payload": {"query": query, "source_ids": ids, "limit": limit, "min_score": float(min_score)},
        "references": sorted(declared[item] for item in ids),
    }


def _result_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (-item["score"], item["source_id"], item["source_path"], item["chunk_index"], item["result_id"])


def validate_retrieval_data(
    request: Mapping[str, Any], manifest: Mapping[str, Any], data: Any
) -> dict[str, Any]:
    """Validate canonical Retrieval Response Data 1.0 against its request."""

    request_copy = validate_retrieval_request(request, manifest)
    if type(data) is not dict or set(data) != {"contract_version", "results"}:
        _fail("PORT_PROTOCOL_INVALID")
    if data["contract_version"] != CONTRACT_VERSION or type(data["results"]) is not list:
        _fail("PORT_PROTOCOL_INVALID")
    normalized = copy.deepcopy(data)
    results = normalized["results"]
    payload = request_copy["payload"]
    if len(results) > payload["limit"]:
        _fail("PORT_PROTOCOL_INVALID")
    declared = {item["source_id"]: item["path"] for item in validate_retrieval_config(manifest)["sources"]}
    allowed = set(payload["source_ids"])
    for item in results:
        _validate_result(item, declared, allowed, payload["min_score"])
    results.sort(key=_result_sort_key)
    identity = {key: request_copy[key] for key in (
        "contract_version", "request_id", "project_id", "port", "operation", "capability"
    )}
    probe = {**identity, "ok": True, "status": "success", "action": "invoke",
             "called": True, "data": normalized, "error": None}
    validate_port_response(request_copy, probe)
    return normalized


def _validate_result(
    item: Any, declared: Mapping[str, str], allowed: set[str], min_score: float
) -> None:
    if type(item) is not dict or set(item) != _RESULT_FIELDS:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["result_id"]) is not str or _IDENTIFIER.fullmatch(item["result_id"]) is None:
        _fail("PORT_PROTOCOL_INVALID")
    score = item["score"]
    if type(score) not in {int, float} or not math.isfinite(score) or not min_score <= score <= 1:
        _fail("PORT_PROTOCOL_INVALID")
    source_id, source_path = item["source_id"], item["source_path"]
    if type(source_id) is not str or type(source_path) is not str:
        _fail("PORT_PROTOCOL_INVALID")
    if source_id not in allowed or declared.get(source_id) != source_path:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["heading"]) is not str or len(item["heading"]) > MAX_HEADING:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["text"]) is not str or len(item["text"]) > MAX_RESULT_TEXT:
        _fail("PORT_PROTOCOL_INVALID")
    if type(item["chunk_index"]) is not int or item["chunk_index"] < 0:
        _fail("PORT_PROTOCOL_INVALID")


def invoke_retrieval(
    request: Mapping[str, Any], manifest: Mapping[str, Any], capability_document: Any,
    adapter: Callable[[dict[str, Any], float], dict[str, Any]] | None,
    *, dry_run: bool = False, timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Invoke the public Port and independently validate successful result data."""

    try:
        request_copy = validate_retrieval_request(request, manifest)
    except IntegrationPortContractError as exc:
        try:
            generic_request = validate_port_request(request)
        except IntegrationPortContractError:
            return invoke_port(request, capability_document, adapter, write=not dry_run,
                               timeout_seconds=timeout_seconds)
        return _not_called_rejection(generic_request, exc.code)
    response = invoke_port(request_copy, capability_document, adapter, write=not dry_run,
                           timeout_seconds=timeout_seconds)
    if response["ok"] and response["called"]:
        try:
            response["data"] = validate_retrieval_data(request_copy, manifest, response["data"])
        except IntegrationPortContractError as exc:
            response = _called_rejection(request_copy, exc.code)
    return response


def _not_called_rejection(request: Mapping[str, Any], code: str) -> dict[str, Any]:
    safe_code = code if code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID", "PORT_PAYLOAD_UNSAFE"
    } else "PORT_REQUEST_INVALID"
    return {
        "contract_version": CONTRACT_VERSION,
        **{key: request[key] for key in ("request_id", "project_id", "port", "operation", "capability")},
        "ok": False, "status": "rejected", "action": "reject", "called": False,
        "data": {},
        "error": {"code": safe_code, "message": "Integration port operation did not complete",
                  "retryable": False, "details": {}},
    }


def _called_rejection(request: Mapping[str, Any], code: str) -> dict[str, Any]:
    safe_code = "PORT_PAYLOAD_UNSAFE" if code == "PORT_PAYLOAD_UNSAFE" else "PORT_PROTOCOL_INVALID"
    return {
        "contract_version": CONTRACT_VERSION,
        **{key: request[key] for key in ("request_id", "project_id", "port", "operation", "capability")},
        "ok": False, "status": "rejected", "action": "reject", "called": True,
        "data": {},
        "error": {"code": safe_code, "message": "Integration port operation did not complete",
                  "retryable": False, "details": {}},
    }
