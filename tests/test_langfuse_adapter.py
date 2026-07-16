"""Batch 5D tests for privacy-bounded Langfuse observability."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch
from urllib.error import HTTPError

from fixture_workspace import materialized_fixture, tree_digest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
LANGFUSE_ROOT = ROOT / "integrations" / "langfuse"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(LANGFUSE_ROOT))

from ccgs_langfuse_adapter import (
    LangfuseAdapterError,
    LangfuseScoreClient,
    MAX_REMOTE_RESPONSE_BYTES,
    OtelLangfuseExporter,
    build_langfuse_bundle,
    bundle_report,
    credentials_from_environment,
    load_workflow_event,
    send_bundle,
    validate_event_document,
    validate_host,
)

CLI = SCRIPTS / "ccgs_cli.py"
EVENT = "ccgs-data/production/observability/events/story-001-closeout.json"


def run_export(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "langfuse-export",
            "--project-root",
            str(project),
            "--event",
            EVENT,
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def event_document(project: Path) -> dict[str, Any]:
    return json.loads((project / EVENT).read_text(encoding="utf-8"))


class FakeTraceExporter:
    def __init__(self, order: list[str], acknowledged: bool = True, fail: bool = False) -> None:
        self.order = order
        self.acknowledged = acknowledged
        self.fail = fail
        self.bundles = []

    def export(self, bundle: Any) -> dict[str, Any]:
        self.order.append("trace")
        self.bundles.append(bundle)
        if self.fail:
            raise LangfuseAdapterError("synthetic trace failure")
        return {"trace_sent": self.acknowledged, "endpoint": "https://example/otel"}


class FakeScoreSender:
    def __init__(self, order: list[str], fail: bool = False) -> None:
        self.order = order
        self.fail = fail
        self.payloads: list[dict[str, Any]] = []

    def send_scores(self, payloads: Sequence[dict[str, Any]]) -> int:
        self.order.append("scores")
        self.payloads.extend(payloads)
        if self.fail:
            raise LangfuseAdapterError("synthetic score failure")
        return len(payloads)


class RecordingScoreClient(LangfuseScoreClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:3000", "pk-test", "sk-test")
        self.posts: list[dict[str, Any]] = []

    def _post_score(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append(payload)
        return {"id": payload["id"]}


class LangfuseEventTests(unittest.TestCase):
    def test_fixture_matches_schema_and_runtime_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/langfuse-workflow-event.schema.json").read_text(encoding="utf-8")
        )
        with materialized_fixture("mature-project") as project:
            document = event_document(project)
            event = validate_event_document(document, EVENT)
            self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0")
            self.assertEqual(set(schema["required"]) - set(document), set())
            self.assertEqual(event.operation, "story-closeout")
            self.assertEqual(event.surface, "codex-client")
            self.assertEqual(len(event.scores), 2)

    def test_dry_run_is_deterministic_offline_and_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = tree_digest(project)
            first = run_export(project, "--dry-run")
            second = run_export(project, "--dry-run")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            report = json.loads(first.stdout)
            self.assertFalse(report["called"])
            self.assertFalse(report["data"]["exported"])
            self.assertEqual(report["data"]["metric_count"], 2)
            self.assertEqual(len(report["data"]["trace_id"]), 32)
            self.assertEqual(len(report["data"]["span_id"]), 16)
            self.assertEqual(tree_digest(project), before)
            self.assertNotIn(str(project), first.stdout)
            self.assertNotIn("LANGFUSE_SECRET_KEY", first.stdout)
            for forbidden in ("input", "output", "prompt", "completion", "comment"):
                self.assertNotIn(forbidden, first.stdout.casefold())

    def test_reports_are_identical_across_engine_overlays(self) -> None:
        reports = []
        for engine in ("unity", "godot", "cocos"):
            with materialized_fixture("mature-project", engine) as project:
                process = run_export(project, "--dry-run")
                self.assertEqual(process.returncode, 0, process.stderr)
                reports.append(json.loads(process.stdout))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])

    def test_trace_and_span_ids_have_separate_stability(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = event_document(project)
            first = build_langfuse_bundle(validate_event_document(document, EVENT))
            changed = dict(document)
            changed["event_id"] = "story-001-closeout-002"
            second = build_langfuse_bundle(validate_event_document(changed, EVENT))
            self.assertEqual(first.trace_id, second.trace_id)
            self.assertNotEqual(first.span_id, second.span_id)
            self.assertEqual(first, build_langfuse_bundle(validate_event_document(document, EVENT)))

    def test_attributes_use_current_langfuse_otel_contract_without_fake_usage(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            attributes = bundle.attributes
            self.assertEqual(attributes["langfuse.trace.name"], "ccgs.story-closeout")
            self.assertEqual(attributes["langfuse.observation.type"], "span")
            self.assertEqual(attributes["langfuse.session.id"], "fixture-session-001")
            self.assertEqual(attributes["langfuse.trace.metadata.surface"], "codex-client")
            serialized = json.dumps(attributes).casefold()
            for forbidden in ("token_count", "input_tokens", "output_tokens", "total_cost", "model.name"):
                self.assertNotIn(forbidden, serialized)

    def test_boolean_and_numeric_scores_map_to_current_score_shape(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            scores = {item["name"]: item for item in bundle.score_payloads}
            self.assertEqual(scores["closeout_pass"]["value"], 1.0)
            self.assertEqual(scores["closeout_pass"]["dataType"], "BOOLEAN")
            self.assertEqual(scores["evidence_coverage"]["dataType"], "NUMERIC")
            self.assertEqual(scores["closeout_pass"]["traceId"], bundle.trace_id)
            self.assertEqual(scores["closeout_pass"]["observationId"], bundle.span_id)

    def test_path_scope_rejects_non_observability_json(self) -> None:
        with materialized_fixture("mature-project") as project:
            with self.assertRaisesRegex(LangfuseAdapterError, "must stay under"):
                load_workflow_event(
                    project,
                    "ccgs-data",
                    "ccgs-data/production/qa/evidence/story-001.json",
                )

    def test_sensitive_metadata_and_absolute_paths_fail_closed(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = event_document(project)
            secret = dict(document)
            secret["metadata"] = {"api_key": "do-not-send"}
            with self.assertRaisesRegex(LangfuseAdapterError, "sensitive"):
                validate_event_document(secret, EVENT)
            absolute = json.loads(json.dumps(document))
            absolute["input"]["summary"] = "Read D:\\private\\prompt.txt"
            with self.assertRaisesRegex(LangfuseAdapterError, "absolute Windows path"):
                validate_event_document(absolute, EVENT)
            credentials = json.loads(json.dumps(document))
            credentials["output"]["summary"] = "See https://user:pass@example.test"
            with self.assertRaisesRegex(LangfuseAdapterError, "URL credentials"):
                validate_event_document(credentials, EVENT)

    def test_raw_prompt_and_unknown_fields_are_rejected(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = event_document(project)
            document["input"]["prompt"] = "raw hidden prompt"
            with self.assertRaisesRegex(LangfuseAdapterError, "unsupported fields: prompt"):
                validate_event_document(document, EVENT)

    def test_timestamp_and_status_validation(self) -> None:
        with materialized_fixture("mature-project") as project:
            document = event_document(project)
            no_zone = dict(document)
            no_zone["timestamp"] = "2026-07-11T08:00:00"
            with self.assertRaisesRegex(LangfuseAdapterError, "timezone"):
                validate_event_document(no_zone, EVENT)
            reversed_time = dict(document)
            reversed_time["end_timestamp"] = "2026-07-11T07:00:00Z"
            with self.assertRaisesRegex(LangfuseAdapterError, "must not be before"):
                validate_event_document(reversed_time, EVENT)
            invalid_status = dict(document)
            invalid_status["status"] = "done"
            with self.assertRaisesRegex(LangfuseAdapterError, "status must be"):
                validate_event_document(invalid_status, EVENT)


class LangfuseTransportTests(unittest.TestCase):
    def test_send_orders_trace_before_scores(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            order: list[str] = []
            trace = FakeTraceExporter(order)
            scores = FakeScoreSender(order)
            report = send_bundle(bundle, trace, scores)
            self.assertEqual(order, ["trace", "scores"])
            self.assertTrue(report["trace_sent"])
            self.assertEqual(report["scores_sent"], 2)
            self.assertEqual(scores.payloads, list(bundle.score_payloads))

    def test_trace_failure_or_negative_ack_prevents_scores(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            for trace in (
                FakeTraceExporter([], fail=True),
                FakeTraceExporter([], acknowledged=False),
            ):
                scores = FakeScoreSender([])
                with self.assertRaises(LangfuseAdapterError):
                    send_bundle(bundle, trace, scores)
                self.assertEqual(scores.payloads, [])

    def test_score_failure_occurs_after_acknowledged_trace(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            order: list[str] = []
            with self.assertRaisesRegex(LangfuseAdapterError, "synthetic score failure"):
                send_bundle(
                    bundle,
                    FakeTraceExporter(order),
                    FakeScoreSender(order, fail=True),
                )
            self.assertEqual(order, ["trace", "scores"])

    def test_score_client_uses_current_public_score_payloads(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            client = RecordingScoreClient()
            count = client.send_scores(bundle.score_payloads)
            self.assertEqual(count, 2)
            self.assertEqual(client.endpoint, "http://127.0.0.1:3000/api/public/scores")
            self.assertEqual(client.posts, list(bundle.score_payloads))
            self.assertTrue(client.authorization.startswith("Basic "))

    def test_score_client_maps_neutral_metrics_without_comments(self) -> None:
        client = RecordingScoreClient()
        metrics = [{
            "id": "a" * 64,
            "trace_id": "b" * 32,
            "span_id": "c" * 16,
            "name": "closeout_pass",
            "value": True,
            "data_type": "BOOLEAN",
        }]
        self.assertEqual(client.send_neutral_metrics(metrics), 1)
        self.assertEqual(
            set(client.posts[0]),
            {"id", "traceId", "observationId", "name", "value", "dataType"},
        )
        self.assertEqual(client.posts[0]["value"], 1.0)

    def test_score_client_bounds_success_and_error_response_bodies(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.read_sizes: list[int] = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return self.body if size < 0 else self.body[:size]

        client = LangfuseScoreClient(
            "http://127.0.0.1:3000", "pk-test", "sk-test"
        )
        oversized = FakeResponse(b"x" * (MAX_REMOTE_RESPONSE_BYTES + 1))
        with patch("ccgs_langfuse_adapter.urlopen", return_value=oversized):
            with self.assertRaisesRegex(LangfuseAdapterError, "size limit"):
                client._post_score({"id": "score-1"})
        self.assertEqual(oversized.read_sizes, [MAX_REMOTE_RESPONSE_BYTES + 1])

        remote_secret = b'{"message":"secret remote body"}'
        error = HTTPError(
            client.endpoint, 401, "Unauthorized", {}, io.BytesIO(remote_secret)
        )
        with patch("ccgs_langfuse_adapter.urlopen", side_effect=error):
            with self.assertRaises(LangfuseAdapterError) as raised:
                client._post_score({"id": "score-1"})
        self.assertNotIn("secret remote body", str(raised.exception))

    def test_score_ids_are_idempotent_for_retries(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            first = build_langfuse_bundle(event)
            second = build_langfuse_bundle(event)
            self.assertEqual(
                [item["id"] for item in first.score_payloads],
                [item["id"] for item in second.score_payloads],
            )

    def test_bundle_report_never_contains_credentials(self) -> None:
        with materialized_fixture("mature-project") as project:
            _, event = load_workflow_event(project, "ccgs-data", EVENT)
            bundle = build_langfuse_bundle(event)
            report = bundle_report(bundle, "https://cloud.langfuse.com", "dry-run")
            serialized = json.dumps(report)
            self.assertNotIn("pk-test", serialized)
            self.assertNotIn("sk-test", serialized)
            self.assertEqual(report["credentials"], "environment-only")
            self.assertEqual(report["content_policy"], "bounded-summary-only")

    def test_credentials_are_environment_only(self) -> None:
        with patch.dict(
            os.environ,
            {"LF_PUBLIC": "pk-test", "LF_SECRET": "sk-test"},
            clear=False,
        ):
            self.assertEqual(
                credentials_from_environment("LF_PUBLIC", "LF_SECRET"),
                ("pk-test", "sk-test"),
            )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LangfuseAdapterError, "credentials are required"):
                credentials_from_environment("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

    def test_host_security_and_send_without_credentials_fail_closed(self) -> None:
        self.assertEqual(validate_host("https://cloud.langfuse.com"), "https://cloud.langfuse.com")
        exporter = OtelLangfuseExporter(
            "https://cloud.langfuse.com", "pk-test", "sk-test"
        )
        self.assertEqual(
            exporter.endpoint,
            "https://cloud.langfuse.com/api/public/otel/v1/traces",
        )
        self.assertEqual(exporter.headers["x-langfuse-ingestion-version"], "4")
        self.assertTrue(exporter.headers["Authorization"].startswith("Basic "))
        self.assertEqual(validate_host("http://127.0.0.1:3000"), "http://127.0.0.1:3000")
        with self.assertRaisesRegex(LangfuseAdapterError, "allow-insecure-http"):
            validate_host("http://langfuse.example")
        with self.assertRaisesRegex(LangfuseAdapterError, "credentials"):
            validate_host("https://user:secret@langfuse.example")
        with materialized_fixture("mature-project") as project:
            with patch.dict(os.environ, {}, clear=True):
                process = run_export(project, "--send")
            self.assertEqual(process.returncode, 2)
            self.assertEqual(process.stderr, "")
            report = json.loads(process.stdout)
            self.assertEqual(report["error"]["code"], "PORT_REQUEST_INVALID")
            self.assertFalse(report["called"])
            self.assertNotIn("credential", process.stdout.casefold())


if __name__ == "__main__":
    unittest.main()
