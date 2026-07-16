"""Qdrant implementation of Retrieval Port 1.0.

The factory is dependency-injected and receives no project path, manifest,
state machine, Evidence writer, or Context Pack service.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from ccgs_qdrant_adapter import (
    Embedder,
    QdrantAdapterError,
    QdrantProtocolError,
    QdrantStore,
    QdrantTransportError,
    QdrantUnsafeError,
    query_index,
)

CAPABILITY_DOCUMENT = {
    "contract_version": "1.0",
    "adapter_id": "qdrant-retrieval-1",
    "capabilities": [{
        "port": "retrieval",
        "operation": "retrieve",
        "capability": "semantic_search",
        "contract_versions": ["1.0"],
    }],
}


def qdrant_capability_document() -> dict[str, Any]:
    """Return an isolated semantic-search Capability Document 1.0."""

    return copy.deepcopy(CAPABILITY_DOCUMENT)


def build_qdrant_retrieval_adapter(
    collection: str, store: QdrantStore, embedder: Embedder,
) -> Callable[[dict[str, Any], float], dict[str, Any]]:
    """Build a callable adapter using only injected remote dependencies."""

    def adapter(request: dict[str, Any], _timeout_seconds: float) -> dict[str, Any]:
        payload = request["payload"]
        try:
            data = query_index(
                request["project_id"], payload["source_ids"], request["references"],
                collection, payload["query"],
                payload["limit"], payload["min_score"], store, embedder,
            )
        except QdrantUnsafeError:
            data = {"unsafe": "/rejected/remote/value"}
        except QdrantProtocolError:
            data = {"contract_version": "1.0", "results": [{"invalid": True}]}
        except QdrantTransportError as exc:
            raise OSError("Qdrant transport unavailable") from exc
        except QdrantAdapterError:
            raise
        return _success(request, data)

    return adapter


def _success(request: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        **{key: request[key] for key in ("request_id", "project_id", "port", "operation", "capability")},
        "ok": True, "status": "success", "action": "invoke", "called": True,
        "data": data, "error": None,
    }
