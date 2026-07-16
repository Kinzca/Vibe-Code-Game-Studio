"""Acceptance evidence for STORY-UWA-010 project-scoped retrieval."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core/scripts"
QDRANT = ROOT / "integrations/qdrant"
sys.path[:0] = [str(SCRIPTS), str(QDRANT), str(ROOT / "tests")]

import ccgs_cli
from ccgs_qdrant_adapter import QdrantAdapterError, QdrantHttpStore, build_index_plan
from ccgs_qdrant_port import build_qdrant_retrieval_adapter, qdrant_capability_document
from ccgs_context_pack import build_context_pack
from fixture_workspace import materialized_fixture
from vibe_integration_ports import IntegrationPortContractError
from vibe_project_manifest import load_manifest
from vibe_retrieval import (
    build_retrieval_request, invoke_retrieval, resolve_allowed_sources,
    validate_retrieval_config, validate_retrieval_data, validate_retrieval_request,
)


def manifest() -> dict[str, Any]:
    return {"schema_version": "1.0", "steps": [], "retrieval": {
        "contract_version": "1.0", "sources": [
            {"source_id": "guide", "path": "knowledge/guide.md", "media_type": "text/markdown"},
            {"source_id": "facts", "path": "knowledge/facts.json", "media_type": "application/json"},
        ]}}


def point(project_id: str = "project-alpha", source_id: str = "guide",
          source_path: str = "knowledge/guide.md", text: str = "bounded text",
          score: float = .8) -> dict[str, Any]:
    payload = {"schema_version": "1.0", "project_id": project_id,
               "source_id": source_id, "source_path": source_path,
               "media_type": "text/markdown", "source_hash": "a" * 64,
               "chunk_index": 0, "heading": "Heading", "text": text,
               "content_hash": "b" * 64, "embedding_model": "neutral-model",
               "record_hash": "c" * 64}
    return {"id": "result-1", "score": score, "payload": payload}


class FakeEmbedder:
    def __init__(self) -> None: self.calls = 0
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[.1, .2] for _ in texts]


class FakeStore:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = results or []
        self.filters: list[tuple[str, tuple[str, ...]]] = []
    def query_points(self, collection: str, project_id: str, source_ids: Sequence[str],
                     vector: Sequence[float], limit: int) -> list[dict[str, Any]]:
        self.filters.append((project_id, tuple(source_ids)))
        return copy.deepcopy(self.results)


class ProjectScopedRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "knowledge").mkdir()
        (self.project / "knowledge/guide.md").write_text("# Guide\n\nneutral content", encoding="utf-8")
        (self.project / "knowledge/facts.json").write_text('{"value":1}', encoding="utf-8")
        (self.project / "vibe-workflow.json").write_text(json.dumps(manifest()), encoding="utf-8")
        self.manifest = load_manifest(self.project, ROOT)

    def tearDown(self) -> None: self.temp.cleanup()

    def request(self, **changes: Any) -> dict[str, Any]:
        values = {"request_id": "request-1", "project_id": "project-alpha",
                  "query": "bounded query", "source_ids": ["guide"],
                  "limit": 10, "min_score": -1.0}
        values.update(changes)
        return build_retrieval_request(self.manifest, **values)

    def test_ac1_manifest_declaration_is_optional_but_remote_fails_closed(self) -> None:
        plain = {"schema_version": "1.0", "steps": []}
        (self.project / "vibe-workflow.json").write_text(json.dumps(plain), encoding="utf-8")
        loaded = load_manifest(self.project, ROOT)
        self.assertNotIn("retrieval", loaded)
        with self.assertRaises(IntegrationPortContractError) as caught:
            build_retrieval_request(loaded, request_id="r", project_id="p", query="q",
                                    source_ids=["guide"])
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)

    def test_ac1_contract_rejects_duplicates_unknowns_and_implicit_discovery(self) -> None:
        invalid = manifest()
        invalid["retrieval"]["sources"].append(copy.deepcopy(invalid["retrieval"]["sources"][0]))
        with self.assertRaises(IntegrationPortContractError): validate_retrieval_config(invalid)
        adapter_source = (QDRANT / "ccgs_qdrant_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("rglob(", adapter_source)
        self.assertNotIn("discover_sources", adapter_source)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema dependency")
    def test_public_schemas_validate_real_draft_2020_12_samples(self) -> None:
        from jsonschema import Draft202012Validator

        schemas = {
            name: json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            for name in (
                "project-workflow-manifest.schema.json",
                "retrieval-request-data.schema.json",
                "retrieval-response-data.schema.json",
                "semantic-index-point.schema.json",
            )
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
        request = self.request()["payload"]
        response = {"contract_version": "1.0", "results": []}
        indexed = point()["payload"]
        self.assertFalse(list(Draft202012Validator(schemas["retrieval-request-data.schema.json"]).iter_errors(request)))
        self.assertFalse(list(Draft202012Validator(schemas["retrieval-response-data.schema.json"]).iter_errors(response)))
        self.assertFalse(list(Draft202012Validator(schemas["semantic-index-point.schema.json"]).iter_errors(indexed)))
        invalid_request = {**request, "limit": 51, "extra": True}
        invalid_response = {**response, "extra": True}
        invalid_point = {**indexed, "source_id": ""}
        self.assertTrue(list(Draft202012Validator(schemas["retrieval-request-data.schema.json"]).iter_errors(invalid_request)))
        self.assertTrue(list(Draft202012Validator(schemas["retrieval-response-data.schema.json"]).iter_errors(invalid_response)))
        self.assertTrue(list(Draft202012Validator(schemas["semantic-index-point.schema.json"]).iter_errors(invalid_point)))

    def test_ac2_resolver_reads_only_explicit_files_and_plan_has_declared_identity(self) -> None:
        (self.project / "knowledge/private.md").write_text("not allowed", encoding="utf-8")
        records = resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual({"guide", "facts"}, {item["source_id"] for item in records})
        self.assertNotIn("private", json.dumps(records))
        plan = build_index_plan(records, "project-alpha")
        self.assertTrue(plan.chunks)
        self.assertEqual({"guide", "facts"}, {item.payload["source_id"] for item in plan.chunks})
        self.assertTrue(all("media_type" in item.payload for item in plan.chunks))

    def test_ac2_resolver_rejects_symlink_escape(self) -> None:
        outside = self.project.parent / "outside-story010.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.project / "knowledge/guide.md"
        link.unlink()
        try: link.symlink_to(outside)
        except OSError as exc: self.skipTest(str(exc))
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_PAYLOAD_UNSAFE", caught.exception.code)

    def test_ac2_resolver_rejects_invalid_json_oversize_missing_directory_and_utf8(self) -> None:
        guide = self.project / "knowledge/guide.md"
        facts = self.project / "knowledge/facts.json"
        cases = []
        facts.write_text("{broken", encoding="utf-8")
        cases.append(("invalid-json", facts))
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)
        facts.write_text('{"value":1}', encoding="utf-8")

        guide.write_bytes(b"x" * 4_000_001)
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)
        guide.write_text("neutral", encoding="utf-8")

        guide.unlink()
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)
        guide.mkdir()
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)
        guide.rmdir()
        guide.write_bytes(b"\xff")
        with self.assertRaises(IntegrationPortContractError) as caught:
            resolve_allowed_sources(self.project, self.manifest)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)

    def test_ac3_request_is_normalized_and_dry_run_calls_nothing(self) -> None:
        request = self.request(query="  bounded query  ")
        self.assertEqual("bounded query", request["payload"]["query"])
        self.assertEqual(["knowledge/guide.md"], request["references"])
        calls: list[dict[str, Any]] = []
        response = invoke_retrieval(request, self.manifest, qdrant_capability_document(),
                                    lambda value, timeout: calls.append(value), dry_run=True)
        self.assertTrue(response["ok"])
        self.assertFalse(response["called"])
        self.assertEqual([], calls)
        with self.assertRaises(IntegrationPortContractError) as caught:
            self.request(source_ids=None)
        self.assertEqual("PORT_REQUEST_INVALID", caught.exception.code)

    def test_ac3_invoke_revalidates_manifest_before_adapter_call(self) -> None:
        calls: list[dict[str, Any]] = []
        unknown = self.request()
        unknown["payload"]["source_ids"] = ["not-declared"]
        unknown["references"] = ["knowledge/not-declared.md"]
        mismatched = self.request()
        mismatched["references"] = ["knowledge/facts.json"]
        for request in (unknown, mismatched):
            response = invoke_retrieval(
                request, self.manifest, qdrant_capability_document(),
                lambda value, _timeout: calls.append(value),
            )
            self.assertEqual("PORT_REQUEST_INVALID", response["error"]["code"])
            self.assertFalse(response["called"])
            with self.assertRaises(IntegrationPortContractError):
                validate_retrieval_request(request, self.manifest)
        self.assertEqual([], calls)

    def test_ac3_write_calls_once_with_an_isolated_request(self) -> None:
        request = self.request()
        original = copy.deepcopy(request)
        calls: list[dict[str, Any]] = []

        def adapter(value: dict[str, Any], _timeout: float) -> dict[str, Any]:
            calls.append(value)
            value["payload"]["query"] = "mutated"
            return {"contract_version": "1.0", **{key: original[key] for key in (
                "request_id", "project_id", "port", "operation", "capability")},
                "ok": True, "status": "success", "action": "invoke", "called": True,
                "data": {"contract_version": "1.0", "results": []}, "error": None}

        response = invoke_retrieval(
            request, self.manifest, qdrant_capability_document(), adapter,
        )
        self.assertTrue(response["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(original, request)

    def test_ac4_remote_filter_and_double_isolation(self) -> None:
        store = FakeStore([point()])
        response = invoke_retrieval(self.request(), self.manifest, qdrant_capability_document(),
                                    build_qdrant_retrieval_adapter("neutral", store, FakeEmbedder()))
        self.assertTrue(response["ok"])
        self.assertTrue(response["called"])
        self.assertEqual([("project-alpha", ("guide",))], store.filters)

        for bad in (point(project_id="project-beta"), point(source_id="facts")):
            response = invoke_retrieval(self.request(), self.manifest, qdrant_capability_document(),
                                        build_qdrant_retrieval_adapter("neutral", FakeStore([bad]), FakeEmbedder()))
            self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])
            self.assertTrue(response["called"])
        crossed = point(source_id="guide", source_path="knowledge/facts.json")
        response = invoke_retrieval(
            self.request(source_ids=["guide", "facts"]), self.manifest,
            qdrant_capability_document(),
            build_qdrant_retrieval_adapter(
                "neutral", FakeStore([crossed]), FakeEmbedder()
            ),
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])
        bad_type = point(); bad_type["payload"]["source_id"] = ["guide"]
        bad_media = point(); bad_media["payload"]["media_type"] = ["text/markdown"]
        bad_id = point(); bad_id["id"] = "not allowed/id"
        for malformed in (bad_type, bad_media, bad_id):
            response = invoke_retrieval(
                self.request(), self.manifest, qdrant_capability_document(),
                build_qdrant_retrieval_adapter("neutral", FakeStore([malformed]), FakeEmbedder()),
            )
            self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])
        malformed_data = {"contract_version": "1.0", "results": [{
            "result_id": "result-1", "score": .8, "source_id": ["guide"],
            "source_path": "knowledge/guide.md", "heading": "H",
            "chunk_index": 0, "text": "safe",
        }]}
        with self.assertRaises(IntegrationPortContractError) as caught:
            validate_retrieval_data(self.request(), self.manifest, malformed_data)
        self.assertEqual("PORT_PROTOCOL_INVALID", caught.exception.code)

    def test_ac4_absolute_path_credentials_and_sensitive_payload_are_unsafe(self) -> None:
        bad_points = [point(source_path="/private/secret.md"),
                      point(text="https://user:password@example.invalid/data")]
        secret = point(); secret["payload"]["secret"] = "hidden"; bad_points.append(secret)
        model_secret = point()
        model_secret["payload"]["embedding_model"] = "https://user:password@example.invalid/model"
        bad_points.append(model_secret)
        for bad in bad_points:
            response = invoke_retrieval(self.request(), self.manifest, qdrant_capability_document(),
                                        build_qdrant_retrieval_adapter("neutral", FakeStore([bad]), FakeEmbedder()))
            self.assertEqual("PORT_PAYLOAD_UNSAFE", response["error"]["code"])
            self.assertTrue(response["called"])
            self.assertNotIn("password", json.dumps(response))

    def test_ac4_rejects_raw_points_beyond_limit_before_projection(self) -> None:
        request = self.request(limit=1)
        bad = point(project_id="project-beta")
        bad["id"] = "result-2"
        response = invoke_retrieval(
            request, self.manifest, qdrant_capability_document(),
            build_qdrant_retrieval_adapter(
                "neutral", FakeStore([point(), bad]), FakeEmbedder()
            ),
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])
        self.assertTrue(response["called"])

        extra = point()
        extra["id"] = "result-2"
        response = invoke_retrieval(
            request, self.manifest, qdrant_capability_document(),
            build_qdrant_retrieval_adapter(
                "neutral", FakeStore([point(), extra]), FakeEmbedder()
            ),
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])

    def test_ac4_malformed_qdrant_envelope_maps_to_protocol_invalid(self) -> None:
        class MalformedStore(QdrantHttpStore):
            def __init__(self) -> None:
                super().__init__("http://127.0.0.1:6333")
            def _request(self, _method: str, _path: str,
                         _payload: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"result": []}

        response = invoke_retrieval(
            self.request(), self.manifest, qdrant_capability_document(),
            build_qdrant_retrieval_adapter("neutral", MalformedStore(), FakeEmbedder()),
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", response["error"]["code"])
        self.assertTrue(response["called"])

    def test_ac5_results_are_bounded_filtered_and_stably_sorted(self) -> None:
        request = self.request(source_ids=["guide", "facts"], limit=3, min_score=.5)
        data = {"contract_version": "1.0", "results": [
            {"result_id": "b", "score": .8, "source_id": "guide", "source_path": "knowledge/guide.md",
             "heading": "B", "chunk_index": 1, "text": "second"},
            {"result_id": "a", "score": .9, "source_id": "facts", "source_path": "knowledge/facts.json",
             "heading": "A", "chunk_index": 0, "text": "first"}]}
        validated = validate_retrieval_data(request, self.manifest, data)
        self.assertEqual(["a", "b"], [item["result_id"] for item in validated["results"]])
        for bad_score in (math.nan, math.inf, .49, 1.1):
            broken = copy.deepcopy(data); broken["results"][0]["score"] = bad_score
            with self.assertRaises(IntegrationPortContractError):
                validate_retrieval_data(request, self.manifest, broken)

    def test_ac6_failures_do_not_write_project_and_no_local_fake_results(self) -> None:
        protected = (
            "ccgs-data/production/plans/plan.json",
            "ccgs-data/production/results/result.json",
            "ccgs-data/production/qa/evidence/evidence.json",
            "ccgs-data/production/qa/replay/replay.json",
            "ccgs-data/production/qa/closeout/closeout.json",
            "ccgs-data/production/context/packs/context.md",
        )
        for relative in protected:
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"protected:{relative}\n", encoding="utf-8")
        before = {p.relative_to(self.project).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.project.rglob("*") if p.is_file()}
        response = invoke_retrieval(self.request(), self.manifest, qdrant_capability_document(), None)
        after = {p.relative_to(self.project).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.project.rglob("*") if p.is_file()}
        self.assertEqual("PORT_CAPABILITY_UNAVAILABLE", response["error"]["code"])
        self.assertEqual(before, after)
        self.assertEqual({}, response["data"])
        def unavailable(_request: dict[str, Any], _timeout: float) -> None: raise OSError()
        def timed_out(_request: dict[str, Any], _timeout: float) -> None: raise TimeoutError()
        def failed(_request: dict[str, Any], _timeout: float) -> None: raise ValueError()
        for adapter, code, retryable in (
            (unavailable, "PORT_ADAPTER_UNAVAILABLE", True),
            (timed_out, "PORT_ADAPTER_TIMEOUT", True),
            (failed, "PORT_ADAPTER_FAILED", False),
        ):
            result = invoke_retrieval(self.request(), self.manifest,
                                      qdrant_capability_document(), adapter)
            self.assertEqual(code, result["error"]["code"])
            self.assertEqual(retryable, result["error"]["retryable"])
            self.assertTrue(result["called"])

    def test_ac6_context_pack_preview_remains_byte_identical_after_remote_failure(self) -> None:
        story = "ccgs-data/production/epics/sample/story-001.md"
        with materialized_fixture("mature-project") as project:
            before = build_context_pack(project, story, "ccgs-data").markdown.encode("utf-8")
            result = invoke_retrieval(self.request(), self.manifest,
                                      qdrant_capability_document(),
                                      lambda _request, _timeout: (_ for _ in ()).throw(OSError()))
            after = build_context_pack(project, story, "ccgs-data").markdown.encode("utf-8")
            self.assertEqual("PORT_ADAPTER_UNAVAILABLE", result["error"]["code"])
            self.assertEqual(before, after)
            self.assertNotIn("results", result["data"])

    def test_cli_dry_runs_are_offline_read_only_and_missing_declaration_fails_json(self) -> None:
        before = {p.relative_to(self.project).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.project.rglob("*") if p.is_file()}
        base = [sys.executable, str(SCRIPTS / "ccgs_cli.py")]
        index = subprocess.run(base + ["qdrant-index", "--project-root", str(self.project),
                               "--project-id", "project:alpha", "--dry-run"],
                               cwd=ROOT, capture_output=True, text=True, check=False)
        query = subprocess.run(base + ["qdrant-query", "--project-root", str(self.project),
                               "--project-id", "project:alpha", "--query", "neutral",
                               "--source-id", "guide", "--dry-run"],
                               cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(0, index.returncode, index.stderr)
        self.assertEqual(0, query.returncode, query.stderr)
        self.assertFalse(json.loads(query.stdout)["called"])
        after = {p.relative_to(self.project).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.project.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

        (self.project / "vibe-workflow.json").write_text(
            json.dumps({"schema_version": "1.0", "steps": []}), encoding="utf-8")
        for command in (
            ["qdrant-index", "--project-root", str(self.project), "--project-id", "project:alpha", "--dry-run"],
            ["qdrant-query", "--project-root", str(self.project), "--project-id", "project:alpha",
             "--query", "neutral", "--source-id", "guide", "--dry-run"],
        ):
            process = subprocess.run(base + command, cwd=ROOT, capture_output=True,
                                     text=True, check=False)
            self.assertNotEqual(0, process.returncode)
            report = json.loads(process.stdout)
            self.assertEqual("PORT_REQUEST_INVALID", report["error"]["code"])
            self.assertFalse(report["called"])

        unsafe = manifest()
        unsafe["retrieval"]["sources"][0]["path"] = "file:///private/source.md"
        (self.project / "vibe-workflow.json").write_text(json.dumps(unsafe), encoding="utf-8")
        for command in (
            ["qdrant-index", "--project-root", str(self.project), "--project-id", "project:alpha", "--dry-run"],
            ["qdrant-query", "--project-root", str(self.project), "--project-id", "project:alpha",
             "--query", "neutral", "--source-id", "guide", "--dry-run"],
        ):
            process = subprocess.run(base + command, cwd=ROOT, capture_output=True,
                                     text=True, check=False)
            report = json.loads(process.stdout)
            self.assertEqual("PORT_PAYLOAD_UNSAFE", report["error"]["code"])
            self.assertFalse(report["called"])
            self.assertNotIn("private/source", process.stdout)

        unsafe["retrieval"]["sources"][0]["path"] = "FILE:///private/source.md"
        (self.project / "vibe-workflow.json").write_text(json.dumps(unsafe), encoding="utf-8")
        process = subprocess.run(
            base + ["qdrant-query", "--project-root", str(self.project),
                    "--project-id", "project:alpha", "--query", "neutral",
                    "--source-id", "guide", "--dry-run"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        report = json.loads(process.stdout)
        self.assertEqual("PORT_PAYLOAD_UNSAFE", report["error"]["code"])
        self.assertFalse(report["called"])

    def test_cli_write_dependency_failure_returns_port_envelope(self) -> None:
        args = ccgs_cli.build_parser().parse_args([
            "qdrant-query", "--project-root", str(self.project),
            "--project-id", "project-alpha", "--query", "neutral",
            "--source-id", "guide", "--write",
        ])
        output = io.StringIO()
        with patch.object(
            ccgs_cli, "FastEmbedder", side_effect=QdrantAdapterError("synthetic model failure")
        ), redirect_stdout(output):
            result = ccgs_cli.command_qdrant_query(args)
        report = json.loads(output.getvalue())
        self.assertEqual(2, result)
        self.assertEqual("PORT_ADAPTER_FAILED", report["error"]["code"])
        self.assertTrue(report["called"])
        self.assertNotIn("synthetic", output.getvalue())

    def test_cli_index_write_failure_returns_redacted_machine_envelope(self) -> None:
        cases = (
            (QdrantAdapterError("synthetic secret failure"), "PORT_ADAPTER_FAILED", 2, False),
            (TimeoutError("synthetic secret timeout"), "PORT_ADAPTER_TIMEOUT", 3, True),
            (OSError("synthetic secret transport"), "PORT_ADAPTER_UNAVAILABLE", 3, True),
        )
        for failure, code, expected_exit, retryable in cases:
            args = ccgs_cli.build_parser().parse_args([
                "qdrant-index", "--project-root", str(self.project),
                "--project-id", "project-alpha", "--write",
            ])
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(
                ccgs_cli, "FastEmbedder", side_effect=failure
            ), redirect_stdout(output), redirect_stderr(errors):
                result = ccgs_cli.command_qdrant_index(args)
            report = json.loads(output.getvalue())
            self.assertEqual(expected_exit, result)
            self.assertEqual(code, report["error"]["code"])
            self.assertEqual(retryable, report["error"]["retryable"])
            self.assertTrue(report["called"])
            self.assertEqual("", errors.getvalue())
            self.assertNotIn("synthetic", output.getvalue())


if __name__ == "__main__": unittest.main()
