"""Batch 5C tests for incremental Qdrant semantic indexing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Sequence

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
QDRANT_ROOT = ROOT / "integrations" / "qdrant"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(QDRANT_ROOT))

from ccgs_qdrant_adapter import (
    QdrantAdapterError,
    QdrantHttpError,
    QdrantHttpStore,
    build_index_plan,
    discover_sources,
    plan_report,
    query_index,
    sync_index,
    validate_qdrant_url,
)

CLI = SCRIPTS / "ccgs_cli.py"
PROJECT_ID = "fixture-project"
COLLECTION = "ccgs-context"
STORY = Path("ccgs-data/production/epics/sample/story-001.md")
ADR = Path("ccgs-data/project-docs/architecture/ADR-0001-deterministic-loop.md")
EVIDENCE = Path("ccgs-data/production/qa/evidence/story-001.json")


def add_context_pack(project: Path) -> None:
    path = project / "ccgs-data/production/context/packs/story-001-context-pack.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# STORY-001 Context Pack\n\n"
        "## Task\n\nVerify the deterministic fixture loop.\n\n"
        "## Sources\n\n- core-loop.md\n- ADR-0001-deterministic-loop.md\n",
        encoding="utf-8",
        newline="\n",
    )


def run_index(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "qdrant-index",
            "--project-root",
            str(project),
            "--project-id",
            PROJECT_ID,
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        vectors = []
        for text in batch:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([value / 255.0 for value in digest[:6]])
        return vectors


class FakeStore:
    def __init__(self) -> None:
        self.exists = False
        self.vector_size = 0
        self.distance = "Cosine"
        self.points: dict[str, dict[str, Any]] = {}
        self.upsert_calls: list[list[str]] = []
        self.delete_calls: list[list[str]] = []
        self.fail_upsert = False
        self.query_results: list[dict[str, Any]] = []

    def collection_info(self, collection: str) -> dict[str, Any] | None:
        if not self.exists:
            return None
        return {
            "result": {
                "config": {
                    "params": {
                        "vectors": {
                            "size": self.vector_size,
                            "distance": self.distance,
                        }
                    }
                }
            }
        }

    def ensure_collection(
        self, collection: str, vector_size: int, distance: str = "Cosine"
    ) -> bool:
        if not self.exists:
            self.exists = True
            self.vector_size = vector_size
            self.distance = distance
            return True
        if self.vector_size != vector_size or self.distance.casefold() != distance.casefold():
            raise QdrantAdapterError("collection vector configuration mismatch")
        return False

    def list_project_points(
        self, collection: str, project_id: str
    ) -> dict[str, dict[str, Any]]:
        return {
            point_id: point["payload"]
            for point_id, point in self.points.items()
            if point["payload"].get("project_id") == project_id
        }

    def upsert_points(
        self, collection: str, points: Sequence[dict[str, Any]]
    ) -> None:
        if self.fail_upsert:
            raise QdrantAdapterError("synthetic upsert failure")
        ids = [str(point["id"]) for point in points]
        self.upsert_calls.append(ids)
        for point in points:
            self.points[str(point["id"])] = dict(point)

    def delete_points(self, collection: str, point_ids: Sequence[str]) -> None:
        ids = list(point_ids)
        self.delete_calls.append(ids)
        for point_id in ids:
            self.points.pop(point_id, None)

    def query_points(
        self,
        collection: str,
        project_id: str,
        vector: Sequence[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.query_results[:limit]


class RecordingHttpStore(QdrantHttpStore):
    def __init__(self, responses: Sequence[Any]) -> None:
        super().__init__("http://127.0.0.1:6333")
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.requests.append((method, path, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class QdrantHttpContractTests(unittest.TestCase):
    def test_collection_upsert_and_delete_use_stable_rest_shapes(self) -> None:
        store = RecordingHttpStore(
            [
                QdrantHttpError(404, "missing"),
                {"result": True},
                {"result": {"status": "completed"}},
                {"result": {"status": "completed"}},
            ]
        )
        self.assertTrue(store.ensure_collection(COLLECTION, 6))
        store.upsert_points(
            COLLECTION,
            [{"id": "point", "vector": [0.0] * 6, "payload": {"project_id": PROJECT_ID}}],
        )
        store.delete_points(COLLECTION, ["point"])

        self.assertEqual(store.requests[0][:2], ("GET", "/collections/ccgs-context"))
        self.assertEqual(store.requests[1][0:2], ("PUT", "/collections/ccgs-context"))
        self.assertEqual(
            store.requests[1][2], {"vectors": {"size": 6, "distance": "Cosine"}}
        )
        self.assertEqual(
            store.requests[2][1], "/collections/ccgs-context/points?wait=true"
        )
        self.assertEqual(
            store.requests[3][1], "/collections/ccgs-context/points/delete?wait=true"
        )

    def test_scroll_and_query_are_filtered_by_project_id(self) -> None:
        info = {
            "result": {
                "config": {
                    "params": {"vectors": {"size": 6, "distance": "Cosine"}}
                }
            }
        }
        store = RecordingHttpStore(
            [
                info,
                {
                    "result": {
                        "points": [{"id": "p1", "payload": {"project_id": PROJECT_ID}}],
                        "next_page_offset": "next",
                    }
                },
                {"result": {"points": [], "next_page_offset": None}},
                {"result": {"points": [{"id": "p1", "score": 0.9, "payload": {}}]}},
            ]
        )
        points = store.list_project_points(COLLECTION, PROJECT_ID)
        results = store.query_points(COLLECTION, PROJECT_ID, [0.0] * 6, 5)

        self.assertEqual(set(points), {"p1"})
        self.assertEqual(results[0]["id"], "p1")
        scroll_payload = store.requests[1][2]
        query_payload = store.requests[3][2]
        expected_filter = {
            "must": [{"key": "project_id", "match": {"value": PROJECT_ID}}]
        }
        self.assertEqual(scroll_payload["filter"], expected_filter)
        self.assertEqual(query_payload["filter"], expected_filter)
        self.assertEqual(store.requests[3][1], "/collections/ccgs-context/points/query")


class QdrantSourceTests(unittest.TestCase):
    def test_discovers_exactly_five_source_families(self) -> None:
        with materialized_fixture("mature-project") as project:
            add_context_pack(project)
            documents, skipped = discover_sources(project, "ccgs-data")
            self.assertEqual(
                {item.kind for item in documents},
                {"story", "gdd", "adr", "evidence", "context-pack"},
            )
            self.assertEqual(len(documents), 6)
            self.assertEqual(skipped, ())
            serialized = json.dumps([item.relative_path for item in documents])
            self.assertNotIn(str(project), serialized)
            self.assertNotIn("Client/Assets", serialized)

    def test_plan_is_deterministic_and_engine_agnostic(self) -> None:
        reports = []
        point_sets = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                add_context_pack(project)
                before = tree_digest(project)
                first = build_index_plan(project, "ccgs-data", PROJECT_ID)
                second = build_index_plan(project, "ccgs-data", PROJECT_ID)
                reports.append(plan_report(first, COLLECTION, "dry-run"))
                point_sets.append(
                    [(item.point_id, item.payload["record_hash"]) for item in first.chunks]
                )
                self.assertEqual(first, second)
                self.assertEqual(tree_digest(project), before)
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(point_sets[0], point_sets[1])
        self.assertEqual(point_sets[1], point_sets[2])

    def test_point_payload_matches_schema_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/semantic-index-point.schema.json").read_text(encoding="utf-8")
        )
        with materialized_fixture("mature-project") as project:
            add_context_pack(project)
            plan = build_index_plan(project, "ccgs-data", PROJECT_ID)
            required = set(schema["required"])
            allowed_kinds = set(schema["properties"]["source_kind"]["enum"])
            for chunk in plan.chunks:
                self.assertEqual(set(chunk.payload), required)
                self.assertIn(chunk.payload["source_kind"], allowed_kinds)
                self.assertRegex(chunk.payload["record_hash"], r"^[0-9a-f]{64}$")
                self.assertNotIn(str(project), json.dumps(chunk.payload))

    def test_chunk_limits_and_project_namespaces_are_stable(self) -> None:
        with materialized_fixture("mature-project") as project:
            context = project / "ccgs-data/production/context/packs/long.md"
            context.parent.mkdir(parents=True, exist_ok=True)
            context.write_text(
                "# Long\n\n" + "deterministic semantic content " * 120,
                encoding="utf-8",
                newline="\n",
            )
            first = build_index_plan(
                project, "ccgs-data", PROJECT_ID, max_chars=400, overlap=60
            )
            other = build_index_plan(
                project, "ccgs-data", "other-project", max_chars=400, overlap=60
            )
            long_chunks = [
                item for item in first.chunks if item.payload["source_path"].endswith("long.md")
            ]
            self.assertGreater(len(long_chunks), 1)
            self.assertTrue(all(len(item.payload["text"]) <= 400 for item in long_chunks))
            self.assertNotEqual(
                {item.point_id for item in first.chunks},
                {item.point_id for item in other.chunks},
            )

    def test_invalid_evidence_json_fails_closed(self) -> None:
        with materialized_fixture("mature-project") as project:
            (project / EVIDENCE).write_text("{broken\n", encoding="utf-8")
            with self.assertRaisesRegex(QdrantAdapterError, "invalid Evidence JSON"):
                build_index_plan(project, "ccgs-data", PROJECT_ID)

    def test_dry_run_cli_is_offline_and_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            add_context_pack(project)
            before = tree_digest(project)
            process = run_index(project, "--dry-run")
            report = json.loads(process.stdout)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["source_count"], 6)
            self.assertGreater(report["chunk_count"], report["source_count"])
            self.assertEqual(set(report["by_kind"]), {"story", "gdd", "adr", "evidence", "context-pack"})
            self.assertEqual(tree_digest(project), before)
            self.assertNotIn(str(project), process.stdout)


class QdrantIncrementalTests(unittest.TestCase):
    def test_initial_sync_then_identical_sync_is_zero_work(self) -> None:
        with materialized_fixture("mature-project") as project:
            plan = build_index_plan(project, "ccgs-data", PROJECT_ID)
            store = FakeStore()
            embedder = FakeEmbedder()
            first = sync_index(plan, COLLECTION, store, embedder, batch_size=3)
            first_call_count = len(embedder.calls)
            second = sync_index(plan, COLLECTION, store, embedder, batch_size=3)

            self.assertTrue(first["collection_created"])
            self.assertEqual(first["upserted"], len(plan.chunks))
            self.assertEqual(first["deleted"], 0)
            self.assertEqual(second["upserted"], 0)
            self.assertEqual(second["embedded"], 0)
            self.assertEqual(second["unchanged"], len(plan.chunks))
            self.assertEqual(len(embedder.calls), first_call_count)
            self.assertEqual(store.delete_calls, [])

    def test_changed_source_updates_only_related_points(self) -> None:
        with materialized_fixture("mature-project") as project:
            original = build_index_plan(project, "ccgs-data", PROJECT_ID)
            store = FakeStore()
            sync_index(original, COLLECTION, store, FakeEmbedder())
            story = project / STORY
            story.write_text(
                story.read_text(encoding="utf-8") + "\n- A semantic change is indexed.\n",
                encoding="utf-8",
                newline="\n",
            )
            changed = build_index_plan(project, "ccgs-data", PROJECT_ID)
            result = sync_index(changed, COLLECTION, store, FakeEmbedder())
            changed_paths = {
                store.points[point_id]["payload"]["source_path"]
                for point_id in store.upsert_calls[-1]
            }

            self.assertGreater(result["upserted"], 0)
            self.assertLess(result["upserted"], len(changed.chunks))
            self.assertEqual(changed_paths, {STORY.as_posix()})

    def test_removed_source_prunes_only_stale_project_points(self) -> None:
        with materialized_fixture("mature-project") as project:
            original = build_index_plan(project, "ccgs-data", PROJECT_ID)
            store = FakeStore()
            sync_index(original, COLLECTION, store, FakeEmbedder())
            foreign_id = "11111111-1111-4111-8111-111111111111"
            store.points[foreign_id] = {
                "id": foreign_id,
                "vector": [0.0] * 6,
                "payload": {"project_id": "another-project", "record_hash": "x"},
            }
            adr_ids = {
                item.point_id for item in original.chunks if item.payload["source_kind"] == "adr"
            }
            (project / ADR).unlink()
            changed = build_index_plan(project, "ccgs-data", PROJECT_ID)
            result = sync_index(changed, COLLECTION, store, FakeEmbedder())

            self.assertEqual(result["deleted"], len(adr_ids))
            self.assertTrue(adr_ids.isdisjoint(store.points))
            self.assertIn(foreign_id, store.points)

    def test_upsert_failure_never_prunes_old_points(self) -> None:
        with materialized_fixture("mature-project") as project:
            original = build_index_plan(project, "ccgs-data", PROJECT_ID)
            store = FakeStore()
            sync_index(original, COLLECTION, store, FakeEmbedder())
            before = set(store.points)
            (project / ADR).unlink()
            story = project / STORY
            story.write_text(
                story.read_text(encoding="utf-8") + "\nChanged before failure.\n",
                encoding="utf-8",
                newline="\n",
            )
            changed = build_index_plan(project, "ccgs-data", PROJECT_ID)
            store.fail_upsert = True
            with self.assertRaisesRegex(QdrantAdapterError, "synthetic upsert failure"):
                sync_index(changed, COLLECTION, store, FakeEmbedder())
            self.assertEqual(set(store.points), before)
            self.assertEqual(store.delete_calls, [])

    def test_model_change_reembeds_every_point_without_changing_ids(self) -> None:
        with materialized_fixture("mature-project") as project:
            first = build_index_plan(project, "ccgs-data", PROJECT_ID, "model-a")
            second = build_index_plan(project, "ccgs-data", PROJECT_ID, "model-b")
            self.assertEqual(
                [item.point_id for item in first.chunks],
                [item.point_id for item in second.chunks],
            )
            store = FakeStore()
            sync_index(first, COLLECTION, store, FakeEmbedder())
            result = sync_index(second, COLLECTION, store, FakeEmbedder())
            self.assertEqual(result["embedded"], len(second.chunks))
            self.assertEqual(result["deleted"], 0)


class QdrantQueryAndSafetyTests(unittest.TestCase):
    def test_query_is_project_scoped_and_returns_bounded_payload(self) -> None:
        store = FakeStore()
        store.query_results = [
            {
                "id": "point-1",
                "score": 0.91,
                "payload": {
                    "source_kind": "gdd",
                    "source_path": "ccgs-data/design/gdd/core-loop.md",
                    "heading": "Overview",
                    "chunk_index": 0,
                    "text": "Synthetic design data.",
                    "project_id": PROJECT_ID,
                    "secret": "not returned",
                },
                "vector": [1, 2, 3],
            }
        ]
        report = query_index(
            PROJECT_ID,
            COLLECTION,
            "deterministic loop",
            5,
            store,
            FakeEmbedder(),
        )
        self.assertEqual(report["result_count"], 1)
        self.assertEqual(report["results"][0]["source_kind"], "gdd")
        self.assertNotIn("vector", report["results"][0])
        self.assertNotIn("secret", report["results"][0])

    def test_url_and_identifier_boundaries_fail_closed(self) -> None:
        self.assertEqual(
            validate_qdrant_url("http://127.0.0.1:6333"),
            "http://127.0.0.1:6333",
        )
        self.assertEqual(
            validate_qdrant_url("https://qdrant.example"),
            "https://qdrant.example",
        )
        with self.assertRaisesRegex(QdrantAdapterError, "allow-insecure-http"):
            validate_qdrant_url("http://qdrant.example:6333")
        with self.assertRaisesRegex(QdrantAdapterError, "credentials"):
            validate_qdrant_url("https://user:secret@qdrant.example")
        with materialized_fixture("mature-project") as project:
            process = run_index(project, "--collection", "../escape", "--dry-run")
            self.assertEqual(process.returncode, 2)
            self.assertIn("collection", process.stderr)

    def test_invalid_chunk_configuration_is_rejected(self) -> None:
        with materialized_fixture("mature-project") as project:
            with self.assertRaisesRegex(QdrantAdapterError, "max_chars"):
                build_index_plan(project, "ccgs-data", PROJECT_ID, max_chars=100)
            with self.assertRaisesRegex(QdrantAdapterError, "overlap"):
                build_index_plan(
                    project, "ccgs-data", PROJECT_ID, max_chars=400, overlap=250
                )


if __name__ == "__main__":
    unittest.main()