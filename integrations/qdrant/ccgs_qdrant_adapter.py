"""Engine-neutral incremental semantic indexing for CCGS documents."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

SCHEMA_VERSION = "1.0"
ADAPTER_VERSION = "1.0"
DEFAULT_COLLECTION = "ccgs-context"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_MAX_CHARS = 2400
DEFAULT_OVERLAP = 240
DEFAULT_BATCH_SIZE = 64
MAX_SOURCE_BYTES = 4_000_000
MAX_QUERY_CHARS = 4000
MAX_MANIFEST_SOURCES = 100
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
POINT_NAMESPACE = uuid.UUID("02cd4162-71aa-4e7e-bf75-44e95f142b17")


class QdrantAdapterError(ValueError):
    """Raised when source data or Qdrant behavior violates the adapter contract."""


class QdrantHttpError(QdrantAdapterError):
    """An actionable HTTP failure returned by Qdrant."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Qdrant HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class SourceDocument:
    kind: str
    relative_path: str
    text: str
    source_hash: str


@dataclass(frozen=True)
class IndexChunk:
    point_id: str
    embedding_text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SourceSummary:
    kind: str
    path: str
    source_hash: str
    chunks: int


@dataclass(frozen=True)
class IndexPlan:
    project_id: str
    embedding_model: str
    chunks: tuple[IndexChunk, ...]
    sources: tuple[SourceSummary, ...]
    skipped_empty: tuple[str, ...]
    manifest_sha256: str


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one finite, fixed-size vector for every input string."""


class QdrantStore(Protocol):
    def collection_info(self, collection: str) -> dict[str, Any] | None: ...

    def ensure_collection(
        self, collection: str, vector_size: int, distance: str = "Cosine"
    ) -> bool: ...

    def list_project_points(
        self, collection: str, project_id: str
    ) -> dict[str, dict[str, Any]]: ...

    def upsert_points(
        self, collection: str, points: Sequence[dict[str, Any]]
    ) -> None: ...

    def delete_points(self, collection: str, point_ids: Sequence[str]) -> None: ...

    def query_points(
        self,
        collection: str,
        project_id: str,
        vector: Sequence[float],
        limit: int,
    ) -> list[dict[str, Any]]: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise QdrantAdapterError(
            f"{label} must be 1-128 characters using letters, digits, dot, underscore, or hyphen"
        )
    return value


def validate_qdrant_url(value: str, allow_insecure_http: bool = False) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise QdrantAdapterError("Qdrant URL must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QdrantAdapterError(
            "Qdrant URL must not contain credentials, query parameters, or fragments"
        )
    if parsed.path not in {"", "/"}:
        raise QdrantAdapterError("Qdrant URL must not contain an API path")
    loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise QdrantAdapterError(
            "remote Qdrant HTTP requires --allow-insecure-http or an https URL"
        )
    return value.rstrip("/")


def validate_api_key_env(name: str) -> str:
    if not ENV_NAME_RE.fullmatch(name):
        raise QdrantAdapterError("API key environment name must use uppercase shell syntax")
    return name


def _canonical_source_text(path: Path) -> str:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise QdrantAdapterError(
            f"semantic source exceeds the {MAX_SOURCE_BYTES} byte limit: {path.name}"
        )
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QdrantAdapterError(f"semantic source is not valid UTF-8: {path.name}") from exc
    if path.suffix.casefold() == ".json":
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QdrantAdapterError(f"invalid Evidence JSON: {path.name}: {exc}") from exc
        raw = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True)
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def _safe_source(
    project: Path,
    data_root: Path,
    root: Path,
    path: Path,
    kind: str,
) -> SourceDocument | None:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve())
        relative = resolved.relative_to(project).as_posix()
        resolved.relative_to(data_root.resolve())
    except ValueError as exc:
        raise QdrantAdapterError(f"{kind} source escaped the CCGS data root") from exc
    text = _canonical_source_text(resolved)
    if not text:
        return None
    return SourceDocument(kind, relative, text, _sha256_text(text))


def discover_sources(
    project: Path, data_dir: str
) -> tuple[tuple[SourceDocument, ...], tuple[str, ...]]:
    """Discover only the five approved CCGS semantic source families."""

    project = project.resolve()
    data_root = (project / data_dir).resolve()
    rules = (
        ("gdd", data_root / "design" / "gdd", (".md",), False),
        ("story", data_root / "production" / "epics", (".md",), False),
        (
            "adr",
            data_root / "project-docs",
            (".md",),
            True,
        ),
        (
            "evidence",
            data_root / "production" / "qa" / "evidence",
            (".md", ".json"),
            False,
        ),
        (
            "context-pack",
            data_root / "production" / "context",
            (".md",),
            False,
        ),
    )
    documents: list[SourceDocument] = []
    skipped: list[str] = []
    for kind, root, extensions, adr_only in rules:
        if not root.is_dir():
            continue
        candidates = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix.casefold() in extensions
                and (not adr_only or path.name.casefold().startswith("adr-"))
            ),
            key=lambda path: path.as_posix().casefold(),
        )
        for path in candidates:
            document = _safe_source(project, data_root, root, path, kind)
            if document is None:
                skipped.append(path.resolve().relative_to(project).as_posix())
            else:
                documents.append(document)
    return tuple(documents), tuple(sorted(skipped))


def _markdown_sections(text: str, default_heading: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = default_heading
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            sections.append((heading, body))

    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            lines = []
        else:
            lines.append(line)
    flush()
    return sections or [(default_heading, text.strip())]


def _split_section(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        end = hard_end
        if hard_end < len(text):
            floor = start + int(max_chars * 0.6)
            candidates = [
                text.rfind("\n\n", floor, hard_end),
                text.rfind("\n", floor, hard_end),
                text.rfind(" ", floor, hard_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


def build_index_plan(
    project: Path,
    data_dir: str,
    project_id: str,
    embedding_model: str = DEFAULT_MODEL,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> IndexPlan:
    """Build deterministic chunk IDs and payloads without network access."""

    project_id = validate_identifier(project_id, "project_id")
    if not embedding_model.strip() or len(embedding_model) > 256:
        raise QdrantAdapterError("embedding model must be 1-256 characters")
    if max_chars < 400 or max_chars > 20_000:
        raise QdrantAdapterError("max_chars must be between 400 and 20000")
    if overlap < 0 or overlap >= max_chars // 2:
        raise QdrantAdapterError("overlap must be non-negative and less than half max_chars")

    documents, skipped = discover_sources(project, data_dir)
    chunks: list[IndexChunk] = []
    sources: list[SourceSummary] = []
    for document in documents:
        sections = (
            _markdown_sections(document.text, Path(document.relative_path).stem)
            if Path(document.relative_path).suffix.casefold() == ".md"
            else [("Evidence JSON", document.text)]
        )
        source_count = 0
        for heading, section in sections:
            for text in _split_section(section, max_chars, overlap):
                index = source_count
                point_id = str(
                    uuid.uuid5(
                        POINT_NAMESPACE,
                        f"{project_id}:{document.relative_path}:{index}",
                    )
                )
                embedding_text = (
                    f"Type: {document.kind}\n"
                    f"Source: {document.relative_path}\n"
                    f"Section: {heading}\n\n{text}"
                )
                content_hash = _sha256_text(
                    f"{embedding_model}\0{embedding_text}"
                )
                payload: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "project_id": project_id,
                    "source_kind": document.kind,
                    "source_path": document.relative_path,
                    "source_hash": document.source_hash,
                    "chunk_index": index,
                    "heading": heading,
                    "text": text,
                    "content_hash": content_hash,
                    "embedding_model": embedding_model,
                }
                payload["record_hash"] = _sha256_bytes(_json_bytes(payload))
                chunks.append(IndexChunk(point_id, embedding_text, payload))
                source_count += 1
        sources.append(
            SourceSummary(
                document.kind,
                document.relative_path,
                document.source_hash,
                source_count,
            )
        )
    chunks.sort(key=lambda item: (item.payload["source_path"], item.payload["chunk_index"]))
    sources.sort(key=lambda item: item.path)
    manifest = "\n".join(
        f"{chunk.point_id}:{chunk.payload['record_hash']}" for chunk in chunks
    )
    return IndexPlan(
        project_id,
        embedding_model,
        tuple(chunks),
        tuple(sources),
        skipped,
        _sha256_text(manifest),
    )


def plan_report(
    plan: IndexPlan,
    collection: str,
    mode: str,
    sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    for source in plan.sources:
        entry = by_kind.setdefault(source.kind, {"sources": 0, "chunks": 0})
        entry["sources"] += 1
        entry["chunks"] += source.chunks
    visible_sources = plan.sources[:MAX_MANIFEST_SOURCES]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "adapter": "qdrant",
        "adapter_version": ADAPTER_VERSION,
        "mode": mode,
        "project_id": plan.project_id,
        "collection": collection,
        "embedding_model": plan.embedding_model,
        "source_count": len(plan.sources),
        "chunk_count": len(plan.chunks),
        "by_kind": dict(sorted(by_kind.items())),
        "manifest_sha256": plan.manifest_sha256,
        "sources": [
            {
                "kind": item.kind,
                "path": item.path,
                "source_hash": item.source_hash,
                "chunks": item.chunks,
            }
            for item in visible_sources
        ],
        "sources_truncated": max(0, len(plan.sources) - len(visible_sources)),
        "skipped_empty": list(plan.skipped_empty),
    }
    if sync is not None:
        report["sync"] = sync
    return report


def _validate_vectors(vectors: Sequence[Sequence[float]], expected: int) -> list[list[float]]:
    if len(vectors) != expected:
        raise QdrantAdapterError(
            f"embedding provider returned {len(vectors)} vectors for {expected} inputs"
        )
    normalized: list[list[float]] = []
    size = 0
    for index, vector in enumerate(vectors):
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise QdrantAdapterError(f"embedding vector {index} is empty or non-finite")
        if not size:
            size = len(values)
        elif len(values) != size:
            raise QdrantAdapterError("embedding provider returned inconsistent vector sizes")
        normalized.append(values)
    return normalized


class FastEmbedder:
    """Lazy optional FastEmbed provider so dry-run has no ML dependency."""

    def __init__(self, model_name: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise QdrantAdapterError(
                "FastEmbed is required for Qdrant writes and queries; install it with "
                "'python -m pip install fastembed'"
            ) from exc
        try:
            self._model = TextEmbedding(model_name=model_name)
        except Exception as exc:  # provider errors vary by FastEmbed release
            raise QdrantAdapterError(f"failed to load embedding model {model_name!r}: {exc}") from exc

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            raw_vectors = list(self._model.embed(list(texts)))
        except Exception as exc:  # provider errors vary by runtime backend
            raise QdrantAdapterError(f"embedding generation failed: {exc}") from exc
        vectors = [
            vector.tolist() if hasattr(vector, "tolist") else list(vector)
            for vector in raw_vectors
        ]
        return _validate_vectors(vectors, len(texts))


class QdrantHttpStore:
    """Minimal Qdrant REST client with no qdrant-client dependency."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        allow_insecure_http: bool = False,
    ) -> None:
        self.base_url = validate_qdrant_url(base_url, allow_insecure_http)
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise QdrantAdapterError("Qdrant timeout must be between 0 and 300 seconds")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = _json_bytes(payload) if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["api-key"] = self.api_key
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:1000]
            raise QdrantHttpError(exc.code, message or exc.reason) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise QdrantAdapterError(f"Qdrant request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QdrantAdapterError("Qdrant returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise QdrantAdapterError("Qdrant response must be a JSON object")
        return document

    @staticmethod
    def _collection_path(collection: str) -> str:
        validate_identifier(collection, "collection")
        return f"/collections/{quote(collection, safe='')}"

    def collection_info(self, collection: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", self._collection_path(collection))
        except QdrantHttpError as exc:
            if exc.status == 404:
                return None
            raise

    def ensure_collection(
        self, collection: str, vector_size: int, distance: str = "Cosine"
    ) -> bool:
        if vector_size <= 0:
            raise QdrantAdapterError("Qdrant vector size must be positive")
        info = self.collection_info(collection)
        if info is None:
            self._request(
                "PUT",
                self._collection_path(collection),
                {"vectors": {"size": vector_size, "distance": distance}},
            )
            return True
        try:
            vectors = info["result"]["config"]["params"]["vectors"]
            configured_size = int(vectors["size"])
            configured_distance = str(vectors["distance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QdrantAdapterError(
                "Qdrant collection must use one unnamed dense vector configuration"
            ) from exc
        if configured_size != vector_size:
            raise QdrantAdapterError(
                f"Qdrant collection vector size is {configured_size}, expected {vector_size}"
            )
        if configured_distance.casefold() != distance.casefold():
            raise QdrantAdapterError(
                f"Qdrant collection distance is {configured_distance}, expected {distance}"
            )
        return False

    @staticmethod
    def _project_filter(project_id: str) -> dict[str, Any]:
        return {
            "must": [
                {"key": "project_id", "match": {"value": project_id}}
            ]
        }

    def list_project_points(
        self, collection: str, project_id: str
    ) -> dict[str, dict[str, Any]]:
        if self.collection_info(collection) is None:
            return {}
        points: dict[str, dict[str, Any]] = {}
        offset: Any = None
        path = f"{self._collection_path(collection)}/points/scroll"
        while True:
            payload: dict[str, Any] = {
                "filter": self._project_filter(project_id),
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                payload["offset"] = offset
            response = self._request("POST", path, payload)
            try:
                result = response["result"]
                page = result["points"]
                next_offset = result.get("next_page_offset")
            except (KeyError, TypeError) as exc:
                raise QdrantAdapterError("invalid Qdrant scroll response") from exc
            if not isinstance(page, list):
                raise QdrantAdapterError("Qdrant scroll points must be an array")
            for point in page:
                point_id = str(point.get("id", ""))
                payload_value = point.get("payload", {})
                if point_id and isinstance(payload_value, dict):
                    points[point_id] = payload_value
            if next_offset is None:
                break
            offset = next_offset
        return points

    def upsert_points(
        self, collection: str, points: Sequence[dict[str, Any]]
    ) -> None:
        if not points:
            return
        self._request(
            "PUT",
            f"{self._collection_path(collection)}/points?wait=true",
            {"points": list(points)},
        )

    def delete_points(self, collection: str, point_ids: Sequence[str]) -> None:
        if not point_ids:
            return
        self._request(
            "POST",
            f"{self._collection_path(collection)}/points/delete?wait=true",
            {"points": list(point_ids)},
        )

    def query_points(
        self,
        collection: str,
        project_id: str,
        vector: Sequence[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        path = f"{self._collection_path(collection)}/points/query"
        payload = {
            "query": list(vector),
            "filter": self._project_filter(project_id),
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        try:
            response = self._request("POST", path, payload)
            points = response.get("result", {}).get("points", [])
        except QdrantHttpError as exc:
            if exc.status != 404:
                raise
            legacy = dict(payload)
            legacy["vector"] = legacy.pop("query")
            response = self._request(
                "POST",
                f"{self._collection_path(collection)}/points/search",
                legacy,
            )
            points = response.get("result", [])
        if not isinstance(points, list):
            raise QdrantAdapterError("invalid Qdrant query response")
        return points


def _embedding_batches(
    chunks: Sequence[IndexChunk], batch_size: int
) -> list[Sequence[IndexChunk]]:
    return [chunks[index : index + batch_size] for index in range(0, len(chunks), batch_size)]


def sync_index(
    plan: IndexPlan,
    collection: str,
    store: QdrantStore,
    embedder: Embedder,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Upsert changed points first, then prune stale project-scoped points."""

    collection = validate_identifier(collection, "collection")
    if batch_size < 1 or batch_size > 512:
        raise QdrantAdapterError("batch_size must be between 1 and 512")
    collection_existed = store.collection_info(collection) is not None
    existing = (
        store.list_project_points(collection, plan.project_id)
        if collection_existed
        else {}
    )
    desired = {chunk.point_id: chunk for chunk in plan.chunks}
    changed = [
        chunk
        for chunk in plan.chunks
        if existing.get(chunk.point_id, {}).get("record_hash")
        != chunk.payload["record_hash"]
    ]
    stale = sorted(set(existing) - set(desired))
    created = False
    collection_ready = collection_existed
    upserted = 0
    vector_size = 0
    for batch in _embedding_batches(changed, batch_size):
        vectors = _validate_vectors(
            embedder.embed([chunk.embedding_text for chunk in batch]),
            len(batch),
        )
        vector_size = len(vectors[0])
        if not collection_ready:
            created = store.ensure_collection(collection, vector_size)
            collection_ready = True
        elif upserted == 0:
            store.ensure_collection(collection, vector_size)
        points = [
            {
                "id": chunk.point_id,
                "vector": vector,
                "payload": chunk.payload,
            }
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        store.upsert_points(collection, points)
        upserted += len(points)
    if stale:
        store.delete_points(collection, stale)
    return {
        "collection_created": created,
        "existing": len(existing),
        "desired": len(desired),
        "embedded": len(changed),
        "upserted": upserted,
        "unchanged": len(desired) - len(changed),
        "deleted": len(stale),
        "vector_size": vector_size,
    }


def query_index(
    project_id: str,
    collection: str,
    query: str,
    limit: int,
    store: QdrantStore,
    embedder: Embedder,
) -> dict[str, Any]:
    project_id = validate_identifier(project_id, "project_id")
    collection = validate_identifier(collection, "collection")
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise QdrantAdapterError(
            f"query must be 1-{MAX_QUERY_CHARS} characters"
        )
    if limit < 1 or limit > 50:
        raise QdrantAdapterError("limit must be between 1 and 50")
    vector = _validate_vectors(embedder.embed([query]), 1)[0]
    points = store.query_points(collection, project_id, vector, limit)
    results = []
    for point in points[:limit]:
        payload = point.get("payload", {})
        if not isinstance(payload, dict):
            continue
        results.append(
            {
                "id": str(point.get("id", "")),
                "score": float(point.get("score", 0.0)),
                "source_kind": str(payload.get("source_kind", "")),
                "source_path": str(payload.get("source_path", "")),
                "heading": str(payload.get("heading", "")),
                "chunk_index": int(payload.get("chunk_index", 0)),
                "text": str(payload.get("text", "")),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": "qdrant",
        "mode": "query",
        "project_id": project_id,
        "collection": collection,
        "query": query,
        "limit": limit,
        "result_count": len(results),
        "results": results,
    }


def api_key_from_environment(name: str) -> str:
    return os.environ.get(validate_api_key_env(name), "")