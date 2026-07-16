"""Regression tests for the explicit-source Qdrant adapter."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QDRANT = ROOT / "integrations/qdrant"
sys.path.insert(0, str(QDRANT))

from ccgs_qdrant_adapter import (
    MAX_REMOTE_RESPONSE_BYTES, QdrantAdapterError, QdrantHttpError,
    QdrantHttpStore, QdrantProtocolError, build_index_plan, plan_report,
    query_index, source_documents, sync_index, validate_qdrant_url,
)


def records() -> list[dict[str, Any]]:
    text = "# Heading\n\nneutral semantic content"
    return [{"source_id": "guide", "path": "knowledge/guide.md",
             "media_type": "text/markdown", "text": text,
             "source_hash": hashlib.sha256(text.encode()).hexdigest()}]


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[.1, .2, .3] for _ in texts]


class FakeStore:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}; self.filters = []
        self.exists = False; self.vector_size = 0; self.upserts = []; self.deletes = []; self.fail = False
        self.query_results: list[dict[str, Any]] = []
    def collection_info(self, collection: str) -> dict[str, Any] | None:
        return {"result": {}} if self.exists else None
    def ensure_collection(self, collection: str, vector_size: int, distance: str = "Cosine") -> bool:
        created = not self.exists; self.exists = True; self.vector_size = vector_size; return created
    def list_project_points(self, collection: str, project_id: str) -> dict[str, dict[str, Any]]:
        return {key: value["payload"] for key, value in self.points.items()
                if value["payload"].get("project_id") == project_id}
    def upsert_points(self, collection: str, points: Sequence[dict[str, Any]]) -> None:
        if self.fail: raise QdrantAdapterError("synthetic upsert failure")
        self.upserts.append([item["id"] for item in points])
        self.points.update({item["id"]: item for item in points})
    def delete_points(self, collection: str, point_ids: Sequence[str]) -> None:
        self.deletes.append(list(point_ids))
        for point_id in point_ids: self.points.pop(point_id, None)
    def query_points(self, collection: str, project_id: str, source_ids: Sequence[str],
                     vector: Sequence[float], limit: int) -> list[dict[str, Any]]:
        self.filters.append((project_id, tuple(source_ids))); return self.query_results


class RecordingStore(QdrantHttpStore):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__("http://127.0.0.1:6333"); self.response = response; self.requests = []
    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, path, payload)); return self.response


class BinaryResponse:
    def __init__(self, body: bytes) -> None:
        self.stream = io.BytesIO(body)
    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)
    def __enter__(self) -> "BinaryResponse":
        return self
    def __exit__(self, *_args: Any) -> None:
        return None


class RepeatingScrollStore(QdrantHttpStore):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:6333")
    def collection_info(self, collection: str) -> dict[str, Any] | None:
        return {"result": {}}
    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": {"points": [], "next_page_offset": "same"}}


class PointOverflowScrollStore(RepeatingScrollStore):
    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": {"points": [{"id": "one", "payload": {}},
                                         {"id": "two", "payload": {}}]}}


class QdrantExplicitSourceTests(unittest.TestCase):
    def test_plan_contains_source_identity_and_no_filesystem_discovery(self) -> None:
        plan = build_index_plan(records(), "project-alpha")
        self.assertTrue(plan.chunks)
        self.assertEqual("guide", plan.chunks[0].payload["source_id"])
        self.assertEqual("text/markdown", plan.chunks[0].payload["media_type"])
        self.assertNotIn("project_root", json.dumps(plan_report(plan, "neutral", "dry-run")))

    def test_duplicate_source_id_or_path_is_rejected(self) -> None:
        duplicate_id = records() + [{**records()[0], "path": "knowledge/other.md"}]
        duplicate_path = records() + [{**records()[0], "source_id": "other"}]
        for value in (duplicate_id, duplicate_path):
            with self.assertRaises(QdrantAdapterError): source_documents(value)

    def test_sync_uses_injected_sources_and_store(self) -> None:
        plan = build_index_plan(records(), "project-alpha")
        store = FakeStore(); result = sync_index(plan, "neutral", store, FakeEmbedder())
        self.assertEqual(len(plan.chunks), result["upserted"])
        self.assertEqual(len(plan.chunks), len(store.points))

    def test_identical_second_sync_performs_zero_work(self) -> None:
        plan = build_index_plan(records(), "project-alpha")
        store = FakeStore(); sync_index(plan, "neutral", store, FakeEmbedder())
        upserts = len(store.upserts)
        result = sync_index(plan, "neutral", store, FakeEmbedder())
        self.assertEqual(0, result["upserted"])
        self.assertEqual(upserts, len(store.upserts))

    def test_changed_source_updates_only_its_points(self) -> None:
        original_records = records() + [{**records()[0], "source_id": "facts",
                                         "path": "knowledge/facts.txt"}]
        first = build_index_plan(original_records, "project-alpha")
        store = FakeStore(); sync_index(first, "neutral", store, FakeEmbedder())
        changed_records = [dict(item) for item in original_records]
        changed_records[0]["text"] += " changed"
        changed_records[0]["source_hash"] = hashlib.sha256(changed_records[0]["text"].encode()).hexdigest()
        changed = build_index_plan(changed_records, "project-alpha")
        sync_index(changed, "neutral", store, FakeEmbedder())
        paths = {store.points[item]["payload"]["source_path"] for item in store.upserts[-1]}
        self.assertEqual({"knowledge/guide.md"}, paths)

    def test_removed_source_prunes_only_current_project(self) -> None:
        two = records() + [{**records()[0], "source_id": "facts", "path": "knowledge/facts.txt"}]
        first = build_index_plan(two, "project-alpha")
        store = FakeStore(); sync_index(first, "neutral", store, FakeEmbedder())
        foreign = "foreign"; store.points[foreign] = {"id": foreign, "payload": {"project_id": "project-beta"}}
        result = sync_index(build_index_plan(records(), "project-alpha"), "neutral", store, FakeEmbedder())
        self.assertGreater(result["deleted"], 0); self.assertIn(foreign, store.points)

    def test_upsert_failure_never_prunes_stale_points(self) -> None:
        two = records() + [{**records()[0], "source_id": "facts", "path": "knowledge/facts.txt"}]
        store = FakeStore(); sync_index(build_index_plan(two, "project-alpha"), "neutral", store, FakeEmbedder())
        before = set(store.points); changed = records(); changed[0]["text"] += " changed"
        changed[0]["source_hash"] = hashlib.sha256(changed[0]["text"].encode()).hexdigest()
        store.fail = True
        with self.assertRaises(QdrantAdapterError):
            sync_index(build_index_plan(changed, "project-alpha"), "neutral", store, FakeEmbedder())
        self.assertEqual(before, set(store.points)); self.assertEqual([], store.deletes)

    def test_model_change_reembeds_without_changing_ids(self) -> None:
        first = build_index_plan(records(), "project-alpha", "model-a")
        second = build_index_plan(records(), "project-alpha", "model-b")
        self.assertEqual([item.point_id for item in first.chunks], [item.point_id for item in second.chunks])
        store = FakeStore(); sync_index(first, "neutral", store, FakeEmbedder())
        result = sync_index(second, "neutral", store, FakeEmbedder())
        self.assertEqual(len(second.chunks), result["embedded"])

    def test_chunk_boundaries_are_bounded_and_stable(self) -> None:
        long = records(); long[0]["text"] = "neutral content " * 200
        long[0]["source_hash"] = hashlib.sha256(long[0]["text"].encode()).hexdigest()
        first = build_index_plan(long, "project-alpha", max_chars=400, overlap=60)
        second = build_index_plan(long, "project-alpha", max_chars=400, overlap=60)
        self.assertGreater(len(first.chunks), 1)
        self.assertTrue(all(len(item.payload["text"]) <= 400 for item in first.chunks))
        self.assertEqual(first, second)

    def test_empty_source_is_skipped_without_invalid_point(self) -> None:
        empty = records()
        empty[0]["text"] = " \n\t "
        empty[0]["source_hash"] = hashlib.sha256(empty[0]["text"].encode()).hexdigest()
        first = build_index_plan(empty, "project-alpha")
        second = build_index_plan(empty, "project-alpha")
        self.assertEqual((), first.chunks)
        self.assertEqual((), first.sources)
        self.assertEqual(("knowledge/guide.md",), first.skipped_empty)
        self.assertEqual(first, second)
        report = plan_report(first, "neutral", "dry-run")
        self.assertEqual(["knowledge/guide.md"], report["skipped_empty"])

    def test_index_output_respects_public_text_and_heading_bounds(self) -> None:
        source = records()
        source[0]["text"] = "# " + ("H" * 600) + "\n\n" + ("word " * 800)
        source[0]["source_hash"] = hashlib.sha256(source[0]["text"].encode()).hexdigest()
        plan = build_index_plan(source, "project-alpha", max_chars=2400, overlap=240)
        self.assertTrue(all(len(item.payload["text"]) <= 2400 for item in plan.chunks))
        self.assertTrue(all(len(item.payload["heading"]) <= 512 for item in plan.chunks))
        with self.assertRaises(QdrantAdapterError):
            build_index_plan(source, "project-alpha", max_chars=2401)

    def test_query_filter_contains_project_and_source_ids(self) -> None:
        store = FakeStore()
        data = query_index("project-alpha", ["guide"], ["knowledge/guide.md"],
                           "neutral", "query", 5, -1, store, FakeEmbedder())
        self.assertEqual({"contract_version": "1.0", "results": []}, data)
        self.assertEqual([("project-alpha", ("guide",))], store.filters)

    def test_query_rejects_crossed_source_id_path_pairs(self) -> None:
        source = records() + [{**records()[0], "source_id": "facts",
                               "path": "knowledge/facts.txt"}]
        plan = build_index_plan(source, "project-alpha")
        guide = next(item for item in plan.chunks if item.payload["source_id"] == "guide")
        raw = {"id": guide.point_id, "score": .8,
               "payload": dict(guide.payload)}
        raw["payload"]["source_path"] = "knowledge/facts.txt"
        store = FakeStore(); store.query_results = [raw]
        with self.assertRaises(QdrantProtocolError):
            query_index(
                "project-alpha", ["guide", "facts"],
                ["knowledge/guide.md", "knowledge/facts.txt"],
                "neutral", "query", 5, -1, store, FakeEmbedder(),
            )

    def test_logical_identifiers_allow_contract_colons(self) -> None:
        colon = records(); colon[0]["source_id"] = "domain:guide"
        plan = build_index_plan(colon, "project:alpha")
        self.assertEqual("domain:guide", plan.chunks[0].payload["source_id"])

    def test_http_query_uses_double_filter(self) -> None:
        store = RecordingStore({"result": {"points": []}})
        store.query_points("neutral", "project-alpha", ["guide", "facts"], [.1], 5)
        self.assertEqual({"must": [
            {"key": "project_id", "match": {"value": "project-alpha"}},
            {"key": "source_id", "match": {"any": ["guide", "facts"]}},
        ]}, store.requests[0][2]["filter"])

    def test_http_query_rejects_malformed_remote_envelopes(self) -> None:
        for response in ({"result": []}, {"result": {}}, {}):
            store = RecordingStore(response)
            with self.assertRaises(QdrantProtocolError):
                store.query_points("neutral", "project-alpha", ["guide"], [.1], 5)

    def test_http_response_and_scroll_are_bounded(self) -> None:
        store = QdrantHttpStore("http://127.0.0.1:6333")
        oversized = BinaryResponse(b"x" * (MAX_REMOTE_RESPONSE_BYTES + 1))
        with patch("ccgs_qdrant_adapter.urlopen", return_value=oversized):
            with self.assertRaises(QdrantProtocolError):
                store._request("GET", "/collections/neutral")
        with self.assertRaises(QdrantProtocolError):
            RepeatingScrollStore().list_project_points("neutral", "project-alpha")
        with patch("ccgs_qdrant_adapter.MAX_SCROLL_PAGES", 1):
            with self.assertRaises(QdrantProtocolError):
                RepeatingScrollStore().list_project_points("neutral", "project-alpha")
        with patch("ccgs_qdrant_adapter.MAX_SCROLL_POINTS", 1):
            with self.assertRaises(QdrantProtocolError):
                PointOverflowScrollStore().list_project_points("neutral", "project-alpha")

    def test_invalid_http_json_is_a_protocol_error(self) -> None:
        store = QdrantHttpStore("http://127.0.0.1:6333")
        with patch("ccgs_qdrant_adapter.urlopen", return_value=BinaryResponse(b"not-json")):
            with self.assertRaises(QdrantProtocolError):
                store._request("GET", "/collections/neutral")

    def test_url_safety_remains_fail_closed(self) -> None:
        self.assertEqual("http://127.0.0.1:6333", validate_qdrant_url("http://127.0.0.1:6333"))
        with self.assertRaises(QdrantAdapterError):
            validate_qdrant_url("https://user:secret@example.invalid")


if __name__ == "__main__": unittest.main()
