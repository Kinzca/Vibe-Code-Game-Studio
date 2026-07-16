"""Qdrant storage primitives for explicitly declared semantic sources."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
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
MAX_REMOTE_RESPONSE_BYTES = 1_048_576
MAX_SCROLL_PAGES = 1000
MAX_SCROLL_POINTS = 100_000
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
POINT_NAMESPACE = uuid.UUID("02cd4162-71aa-4e7e-bf75-44e95f142b17")


class QdrantAdapterError(ValueError):
    """Raised when source data or Qdrant behavior violates the adapter contract."""


class QdrantTransportError(QdrantAdapterError):
    """Raised for transient Qdrant transport failures that may be retried."""


class QdrantProtocolError(QdrantAdapterError):
    """Raised when a remote point violates the public retrieval boundary."""


class QdrantUnsafeError(QdrantProtocolError):
    """Raised when a remote point contains a forbidden location or secret."""


class QdrantHttpError(QdrantAdapterError):
    """An actionable HTTP failure returned by Qdrant."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Qdrant HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    relative_path: str
    media_type: str
    text: str
    source_hash: str


@dataclass(frozen=True)
class IndexChunk:
    point_id: str
    embedding_text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SourceSummary:
    source_id: str
    path: str
    media_type: str
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
    """Storage boundary required by indexing and retrieval operations."""

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
        source_ids: Sequence[str],
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
    """Validate one stable logical identifier used by the adapter contract."""

    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise QdrantAdapterError(
            f"{label} must be 1-128 characters using letters, digits, dot, underscore, or hyphen"
        )
    return value


def validate_collection_identifier(value: str) -> str:
    """Validate the narrower identifier accepted for a Qdrant collection."""

    if not isinstance(value, str) or COLLECTION_RE.fullmatch(value) is None:
        raise QdrantAdapterError("collection must use letters, digits, dot, underscore, or hyphen")
    return value


def validate_qdrant_url(value: str, allow_insecure_http: bool = False) -> str:
    """Validate a credential-free Qdrant base URL."""

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
    """Validate an environment variable name used to obtain an API key."""

    if not ENV_NAME_RE.fullmatch(name):
        raise QdrantAdapterError("API key environment name must use uppercase shell syntax")
    return name


def source_documents(records: Sequence[dict[str, Any]]) -> tuple[SourceDocument, ...]:
    """Validate core-resolved logical records without accepting filesystem roots."""

    documents: list[SourceDocument] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for record in records:
        if type(record) is not dict or set(record) != {
            "source_id", "path", "media_type", "text", "source_hash"
        }:
            raise QdrantAdapterError("resolved source record has an invalid shape")
        source_id = validate_identifier(record["source_id"], "source_id")
        path, media_type, text, source_hash = (
            record["path"], record["media_type"], record["text"], record["source_hash"]
        )
        if not all(type(value) is str for value in (path, media_type, text, source_hash)):
            raise QdrantAdapterError("resolved source record has invalid field types")
        if media_type not in {"text/markdown", "application/json", "text/plain"}:
            raise QdrantAdapterError("resolved source record has an invalid media type")
        if not path or path.startswith(("/", "\\", "~/")) or "\\" in path or ".." in path.split("/"):
            raise QdrantAdapterError("resolved source path must be project-relative")
        if source_hash != _sha256_text(text):
            raise QdrantAdapterError("resolved source hash does not match content")
        if source_id in seen_ids or path in seen_paths:
            raise QdrantAdapterError("resolved sources must be unique")
        seen_ids.add(source_id)
        seen_paths.add(path)
        documents.append(SourceDocument(source_id, path, media_type, text, source_hash))
    return tuple(documents)


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
    resolved_sources: Sequence[dict[str, Any]],
    project_id: str,
    embedding_model: str = DEFAULT_MODEL,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> IndexPlan:
    """Build deterministic chunk IDs and payloads without network access."""

    project_id = validate_identifier(project_id, "project_id")
    if not embedding_model.strip() or len(embedding_model) > 256:
        raise QdrantAdapterError("embedding model must be 1-256 characters")
    if max_chars < 400 or max_chars > 2400:
        raise QdrantAdapterError("max_chars must be between 400 and 2400")
    if overlap < 0 or overlap >= max_chars // 2:
        raise QdrantAdapterError("overlap must be non-negative and less than half max_chars")

    documents = source_documents(resolved_sources)
    skipped: list[str] = []
    chunks: list[IndexChunk] = []
    sources: list[SourceSummary] = []
    for document in documents:
        if not document.text.strip():
            skipped.append(document.relative_path)
            continue
        sections = (
            _markdown_sections(document.text, document.source_id)
            if document.media_type == "text/markdown"
            else [(document.source_id, document.text)]
        )
        source_count = 0
        for heading, section in sections:
            heading = heading[:512]
            for text in _split_section(section, max_chars, overlap):
                index = source_count
                point_id = str(
                    uuid.uuid5(
                        POINT_NAMESPACE,
                        f"{project_id}:{document.relative_path}:{index}",
                    )
                )
                embedding_text = (
                    f"Media-Type: {document.media_type}\n"
                    f"Source: {document.relative_path}\n"
                    f"Section: {heading}\n\n{text}"
                )
                content_hash = _sha256_text(
                    f"{embedding_model}\0{embedding_text}"
                )
                payload: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "project_id": project_id,
                    "source_id": document.source_id,
                    "source_path": document.relative_path,
                    "media_type": document.media_type,
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
                document.source_id,
                document.relative_path,
                document.media_type,
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
        tuple(sorted(skipped)),
        _sha256_text(manifest),
    )


def plan_report(
    plan: IndexPlan,
    collection: str,
    mode: str,
    sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_media_type: dict[str, dict[str, int]] = {}
    for source in plan.sources:
        entry = by_media_type.setdefault(source.media_type, {"sources": 0, "chunks": 0})
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
        "by_media_type": dict(sorted(by_media_type.items())),
        "manifest_sha256": plan.manifest_sha256,
        "sources": [
            {
                "source_id": item.source_id,
                "path": item.path,
                "media_type": item.media_type,
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
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 300
        ):
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
                raw = response.read(MAX_REMOTE_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            message = exc.read(1001).decode("utf-8", errors="replace")[:1000]
            if exc.code in {408, 425, 429} or exc.code >= 500:
                raise QdrantTransportError(
                    f"Qdrant HTTP {exc.code}: {message or exc.reason}"
                ) from exc
            raise QdrantHttpError(exc.code, message or exc.reason) from exc
        except TimeoutError:
            raise
        except (URLError, OSError) as exc:
            raise QdrantTransportError(f"Qdrant request failed: {exc}") from exc
        if len(raw) > MAX_REMOTE_RESPONSE_BYTES:
            raise QdrantProtocolError("Qdrant response exceeds the bounded response limit")
        if not raw:
            return {}
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QdrantProtocolError("Qdrant returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise QdrantProtocolError("Qdrant response must be a JSON object")
        return document

    @staticmethod
    def _collection_path(collection: str) -> str:
        validate_collection_identifier(collection)
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
        """Load a bounded set of points for one logical project."""

        if self.collection_info(collection) is None:
            return {}
        points: dict[str, dict[str, Any]] = {}
        offset: Any = None
        seen_offsets: set[str] = set()
        page_count = 0
        path = f"{self._collection_path(collection)}/points/scroll"
        while True:
            page_count += 1
            if page_count > MAX_SCROLL_PAGES:
                raise QdrantProtocolError("Qdrant scroll exceeds the bounded page limit")
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
                raise QdrantProtocolError("invalid Qdrant scroll response") from exc
            if not isinstance(page, list):
                raise QdrantProtocolError("Qdrant scroll points must be an array")
            if len(points) + len(page) > MAX_SCROLL_POINTS:
                raise QdrantProtocolError("Qdrant scroll exceeds the bounded point limit")
            for point in page:
                if type(point) is not dict:
                    raise QdrantProtocolError("Qdrant scroll point has an invalid shape")
                point_id = str(point.get("id", ""))
                payload_value = point.get("payload", {})
                if point_id and isinstance(payload_value, dict):
                    points[point_id] = payload_value
            if next_offset is None:
                break
            try:
                offset_key = json.dumps(next_offset, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise QdrantProtocolError("Qdrant scroll offset has an invalid shape") from exc
            if offset_key in seen_offsets:
                raise QdrantProtocolError("Qdrant scroll repeated a page offset")
            seen_offsets.add(offset_key)
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
        source_ids: Sequence[str],
        vector: Sequence[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Query points with exact project and allowed-source filters."""

        path = f"{self._collection_path(collection)}/points/query"
        payload = {
            "query": list(vector),
            "filter": {
                "must": [
                    {"key": "project_id", "match": {"value": project_id}},
                    {"key": "source_id", "match": {"any": list(source_ids)}},
                ]
            },
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        try:
            response = self._request("POST", path, payload)
            result = response.get("result")
            if type(result) is not dict or "points" not in result:
                raise QdrantProtocolError("invalid Qdrant query response")
            points = result["points"]
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
            if "result" not in response:
                raise QdrantProtocolError("invalid legacy Qdrant query response")
            points = response["result"]
        if not isinstance(points, list):
            raise QdrantProtocolError("invalid Qdrant query response")
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

    collection = validate_collection_identifier(collection)
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
    source_ids: Sequence[str],
    allowed_paths: Sequence[str],
    collection: str,
    query: str,
    limit: int,
    min_score: float,
    store: QdrantStore,
    embedder: Embedder,
) -> dict[str, Any]:
    """Query exact project/source filters and project validated public results."""

    project_id = validate_identifier(project_id, "project_id")
    collection = validate_collection_identifier(collection)
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise QdrantAdapterError(
            f"query must be 1-{MAX_QUERY_CHARS} characters"
        )
    if limit < 1 or limit > 50:
        raise QdrantAdapterError("limit must be between 1 and 50")
    if not source_ids or len(source_ids) > 100 or len(source_ids) != len(allowed_paths):
        raise QdrantAdapterError("source_ids must contain 1-100 declared sources")
    if (
        any(type(item) is not str for item in source_ids)
        or any(type(item) is not str for item in allowed_paths)
        or len(set(source_ids)) != len(source_ids)
        or len(set(allowed_paths)) != len(allowed_paths)
    ):
        raise QdrantAdapterError("source IDs and paths must form unique declared pairs")
    if type(min_score) not in {int, float} or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        raise QdrantAdapterError("min_score must be finite and between -1 and 1")
    for source_id in source_ids:
        validate_identifier(source_id, "source_id")
    allowed_sources = dict(zip(source_ids, allowed_paths, strict=True))
    vector = _validate_vectors(embedder.embed([query]), 1)[0]
    points = store.query_points(collection, project_id, tuple(source_ids), vector, limit)
    if type(points) is not list or len(points) > limit:
        raise QdrantProtocolError("Qdrant returned more points than the requested limit")
    results = []
    for point in points:
        results.append(_project_point(point, project_id, allowed_sources, min_score))
    results = [item for item in results if item is not None]
    results.sort(key=lambda item: (
        -item["score"], item["source_id"], item["source_path"],
        item["chunk_index"], item["result_id"],
    ))
    return {
        "contract_version": SCHEMA_VERSION,
        "results": results[:limit],
    }


def _project_point(
    point: Any, project_id: str, allowed_sources: dict[str, str], min_score: float
) -> dict[str, Any] | None:
    if type(point) is not dict or type(point.get("payload")) is not dict:
        raise QdrantProtocolError("Qdrant point has an invalid shape")
    payload = point["payload"]
    required = {
        "schema_version", "project_id", "source_id", "source_path", "media_type",
        "source_hash", "chunk_index", "heading", "text", "content_hash",
        "embedding_model", "record_hash",
    }
    sensitive_keys = {
        "secret", "token", "password", "credential", "authorization", "api_key",
        "private_prompt", "source_text", "source_code", "state_transition",
        "policy_override", "evidence_override", "project_writes", "commands",
    }
    if any(str(key).casefold() in sensitive_keys for key in payload):
        raise QdrantUnsafeError("Qdrant point contains sensitive data")
    if set(payload) != required or payload["schema_version"] != SCHEMA_VERSION:
        raise QdrantProtocolError("Qdrant point payload violates the index contract")
    source_id, source_path = payload["source_id"], payload["source_path"]
    if type(source_id) is not str or type(source_path) is not str:
        raise QdrantProtocolError("Qdrant point source fields have invalid types")
    if type(payload["project_id"]) is not str:
        raise QdrantProtocolError("Qdrant point project identity has an invalid type")
    try:
        validate_identifier(payload["project_id"], "project_id")
        validate_identifier(source_id, "source_id")
    except QdrantAdapterError as exc:
        raise QdrantProtocolError("Qdrant point identity violates the index contract") from exc
    if type(payload["media_type"]) is not str or payload["media_type"] not in {
        "text/markdown", "application/json", "text/plain"
    }:
        raise QdrantProtocolError("Qdrant point media type violates the index contract")
    if any(type(payload[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None
           for key in ("source_hash", "content_hash", "record_hash")):
        raise QdrantProtocolError("Qdrant point hash violates the index contract")
    model = payload["embedding_model"]
    if type(model) is not str or not 1 <= len(model) <= 256:
        raise QdrantProtocolError("Qdrant point model identity violates the index contract")
    if any(_unsafe_remote_text(payload.get(key)) for key in (
        "source_path", "heading", "text", "embedding_model"
    )):
        raise QdrantUnsafeError("Qdrant point contains an unsafe value")
    if payload["project_id"] != project_id:
        raise QdrantProtocolError("Qdrant point project identity mismatch")
    if allowed_sources.get(source_id) != source_path:
        raise QdrantProtocolError("Qdrant point source identity mismatch")
    score = point.get("score")
    if type(score) not in {int, float} or not math.isfinite(score) or not -1 <= score <= 1:
        raise QdrantProtocolError("Qdrant point score violates the retrieval contract")
    if score < min_score:
        return None
    result_id, heading, text, chunk_index = (
        point.get("id"), payload["heading"], payload["text"], payload["chunk_index"]
    )
    if type(result_id) is not str:
        raise QdrantProtocolError("Qdrant point result identity has an invalid type")
    try:
        validate_identifier(result_id, "result_id")
    except QdrantAdapterError as exc:
        raise QdrantProtocolError("Qdrant point result identity violates the contract") from exc
    if type(heading) is not str or len(heading) > 512:
        raise QdrantProtocolError("Qdrant point heading violates the retrieval contract")
    if type(text) is not str or len(text) > 2400:
        raise QdrantProtocolError("Qdrant point text violates the retrieval contract")
    if type(chunk_index) is not int or chunk_index < 0:
        raise QdrantProtocolError("Qdrant point chunk index violates the retrieval contract")
    return {
        "result_id": result_id, "score": score, "source_id": source_id,
        "source_path": source_path, "heading": heading,
        "chunk_index": chunk_index, "text": text,
    }


def _unsafe_remote_text(value: Any) -> bool:
    if type(value) is not str:
        return False
    return bool(
        value.startswith(("/", "\\\\", "~/"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or re.search(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:[^\s/@]+@", value)
        or re.search(r"(?<![A-Za-z0-9:/])/(?!/)[^\s/]+(?:/[^\s/]+)*", value)
    )


def api_key_from_environment(name: str) -> str:
    """Read an API key from an explicitly named environment variable."""

    return os.environ.get(validate_api_key_env(name), "")
