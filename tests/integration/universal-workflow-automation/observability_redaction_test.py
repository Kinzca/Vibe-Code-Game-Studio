"""Integration evidence for STORY-UWA-012 observability redaction."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
LANGFUSE = ROOT / "integrations" / "langfuse"
TESTS = ROOT / "tests"
for location in (SCRIPTS, LANGFUSE, TESTS):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from fixture_workspace import materialized_fixture  # noqa: E402
import vibe_observability as observability  # noqa: E402

from ccgs_langfuse_port import (  # noqa: E402
    build_langfuse_observability_adapter,
    build_langfuse_payload,
    langfuse_capability_document,
)
from vibe_observability import (  # noqa: E402
    IntegrationPortContractError,
    build_observability_request,
    invoke_observability,
    project_workflow_event,
    stable_observability_identity,
    validate_neutral_event,
    validate_observability_request,
)


DATA_DIR = "ccgs-data"
EVENT_REF = "ccgs-data/production/observability/events/event-001.json"


def local_event() -> dict:
    return {
        "schema_version": "1.0",
        "event_id": "event-001",
        "trace_key": "story-012",
        "timestamp": "2026-07-14T10:00:00Z",
        "end_timestamp": "2026-07-14T10:00:02Z",
        "project_id": "fixture-project",
        "operation": "story-closeout",
        "status": "pass",
        "environment": "fixture",
        "surface": "windmill",
        "session_id": "session-001",
        "story_id": "STORY-UWA-012",
        "workflow_version": "1.0.0",
        "tags": ["closeout", "observed"],
        "input": {
            "summary": "private prompt-like summary that must stay local",
            "query": "private user query",
            "references": ["ccgs-data/production/evidence/story-012.json"],
            "context_manifest": "a" * 64,
        },
        "output": {
            "summary": "private completion-like summary",
            "decision": "pass",
            "failure_reasons": [],
        },
        "metadata": {"source_text": "must never leave the local event"},
        "scores": [
            {
                "name": "evidence_coverage",
                "value": 1.0,
                "data_type": "NUMERIC",
                "comment": "private score comment",
                "metadata": {"private": "local"},
            },
            {
                "name": "closeout_pass",
                "value": True,
                "data_type": "BOOLEAN",
                "comment": "private comment",
            },
        ],
    }


def request() -> dict:
    return build_observability_request(
        local_event(), data_dir=DATA_DIR, event_ref=EVENT_REF,
        request_id="request-001", project_id="fixture-project",
    )


def project_snapshot(project: Path) -> list[tuple[str, bytes, int]]:
    return [
        (path.relative_to(project).as_posix(), path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(item for item in project.rglob("*") if item.is_file())
    ]


class ObservabilityRedactionTest(unittest.TestCase):
    def test_versioned_request_dry_run_validates_without_adapter_call(self) -> None:
        calls: list[dict] = []
        result = invoke_observability(
            request(), langfuse_capability_document(),
            lambda value, timeout: calls.append(value),
            data_dir=DATA_DIR, dry_run=True, timeout_seconds=5,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["called"])
        self.assertEqual(calls, [])
        self.assertEqual(result["data"]["event_ref"], EVENT_REF)
        self.assertFalse(result["data"]["exported"])

    def test_invalid_reference_and_extra_payload_reject_before_adapter(self) -> None:
        invalid_values = []
        for event_ref in (
            "/tmp/event.json", "../event.json", "C:\\temp\\event.json",
            "\\\\server\\share\\event.json", "file:/tmp/event.json",
            "ccgs-data/production/observability/events/a;rm.json",
        ):
            value = request()
            value["payload"]["event_ref"] = event_ref
            value["references"] = [event_ref]
            invalid_values.append(value)
        extra = request()
        extra["payload"]["host"] = "https://example.invalid"
        invalid_values.append(extra)
        mismatch = request()
        mismatch["references"] = ["ccgs-data/production/observability/events/other.json"]
        invalid_values.append(mismatch)
        unsupported = request()
        unsupported["contract_version"] = "2.0"
        invalid_values.append(unsupported)
        operation = request()
        operation["operation"] = "send"
        invalid_values.append(operation)
        capability = request()
        capability["capability"] = "model_generation"
        invalid_values.append(capability)

        for value in invalid_values:
            with self.subTest(value=value["payload"].get("event_ref")):
                calls: list[dict] = []
                result = invoke_observability(
                    value, langfuse_capability_document(),
                    lambda item, timeout: calls.append(item),
                    data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
                )
                self.assertFalse(result["ok"])
                self.assertFalse(result["called"])
                self.assertEqual(calls, [])
                self.assertIn(
                    result["error"]["code"],
                    {"PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID", "PORT_PAYLOAD_UNSAFE"},
                )

    def test_projection_removes_all_local_free_text_and_vendor_private_fields(self) -> None:
        neutral = project_workflow_event(local_event(), "fixture-project")
        encoded = json.dumps(neutral, sort_keys=True)
        for forbidden in (
            "input", "output", "metadata", "summary", "query", "comment",
            "prompt", "completion", "source_text", "private",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(neutral["metrics"][0]), {"name", "value", "data_type"})
        self.assertEqual(neutral["references"], ["ccgs-data/production/evidence/story-012.json"])

    def test_all_four_explicit_metric_types_are_preserved(self) -> None:
        neutral = request()["payload"]["event"]
        neutral["metrics"].extend([
            {"name": "quality_band", "value": "high", "data_type": "CATEGORICAL"},
            {"name": "review_note", "value": "bounded", "data_type": "TEXT"},
        ])
        checked = validate_neutral_event(neutral)
        self.assertEqual(
            [item["data_type"] for item in checked["metrics"]],
            ["NUMERIC", "BOOLEAN", "CATEGORICAL", "TEXT"],
        )

    def test_neutral_event_rejects_unknown_duplicate_unbounded_and_sensitive_values(self) -> None:
        baseline = request()["payload"]["event"]
        cases = []
        unknown = copy.deepcopy(baseline)
        unknown["unexpected"] = "hidden"
        cases.append((unknown, "PORT_REQUEST_INVALID"))
        duplicate = copy.deepcopy(baseline)
        duplicate["tags"] = ["same", "same"]
        cases.append((duplicate, "PORT_REQUEST_INVALID"))
        reversed_time = copy.deepcopy(baseline)
        reversed_time["end_timestamp"] = "2026-07-14T09:00:00Z"
        cases.append((reversed_time, "PORT_REQUEST_INVALID"))
        non_finite = copy.deepcopy(baseline)
        non_finite["metrics"][0]["value"] = float("nan")
        cases.append((non_finite, "PORT_REQUEST_INVALID"))
        sensitive_metric = copy.deepcopy(baseline)
        sensitive_metric["metrics"].append({
            "name": "detail", "value": "token=do-not-export", "data_type": "TEXT",
        })
        cases.append((sensitive_metric, "PORT_PAYLOAD_UNSAFE"))
        sensitive_key = copy.deepcopy(baseline)
        sensitive_key["prompt"] = "do-not-export"
        cases.append((sensitive_key, "PORT_PAYLOAD_UNSAFE"))
        nested_sensitive_key = copy.deepcopy(baseline)
        nested_sensitive_key["metrics"][0]["source_text"] = "do-not-export"
        cases.append((nested_sensitive_key, "PORT_PAYLOAD_UNSAFE"))
        absolute_reference = copy.deepcopy(baseline)
        absolute_reference["references"] = ["/Users/example/private.txt"]
        cases.append((absolute_reference, "PORT_PAYLOAD_UNSAFE"))
        oversized = copy.deepcopy(baseline)
        oversized["metrics"].append({
            "name": "detail", "value": "x" * 501, "data_type": "TEXT",
        })
        cases.append((oversized, "PORT_REQUEST_INVALID"))
        too_many_metrics = copy.deepcopy(baseline)
        too_many_metrics["metrics"] = [
            {"name": f"metric-{index}", "value": index, "data_type": "NUMERIC"}
            for index in range(21)
        ]
        cases.append((too_many_metrics, "PORT_REQUEST_INVALID"))
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(IntegrationPortContractError) as raised:
                    validate_neutral_event(value)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("do-not-export", str(raised.exception))

    def test_stable_identity_and_vendor_payload_do_not_fabricate_model_data(self) -> None:
        event = request()["payload"]["event"]
        first = stable_observability_identity(event)
        second = stable_observability_identity(copy.deepcopy(event))
        payload = build_langfuse_payload(event, EVENT_REF)
        self.assertEqual(first, second)
        self.assertEqual(payload["trace"]["trace_id"], first[0])
        self.assertEqual(payload["trace"]["span_id"], first[1])
        encoded = json.dumps(payload, sort_keys=True).casefold()
        for forbidden in ("prompt", "completion", "generation", "token", "model", "cost", "input", "output"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(len({metric["id"] for metric in payload["metrics"]}), 2)

    def test_successful_send_exports_trace_before_metrics_and_returns_strict_data(self) -> None:
        order: list[str] = []

        def trace_exporter(payload, timeout):
            order.append("trace")
            return True

        def metric_sender(payloads, timeout):
            order.append("metrics")
            return len(payloads)

        result = invoke_observability(
            request(), langfuse_capability_document(),
            build_langfuse_observability_adapter(trace_exporter, metric_sender),
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["called"])
        self.assertTrue(result["data"]["exported"])
        self.assertEqual(result["data"]["metric_count"], 2)
        self.assertEqual(result["data"]["failures"], [])
        self.assertEqual(order, ["trace", "metrics"])

    def test_partial_metric_failure_is_non_retryable_and_never_claims_export(self) -> None:
        result = invoke_observability(
            request(), langfuse_capability_document(),
            build_langfuse_observability_adapter(lambda payload, timeout: True, None),
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["exported"])
        self.assertEqual(result["data"]["outcome"], "failed")
        self.assertFalse(result["data"]["failures"][0]["retryable"])

    def test_failures_deduplicate_by_complete_identity_without_losing_messages(self) -> None:
        def adapter(value, timeout):
            event = value["payload"]["event"]
            trace_id, span_id = stable_observability_identity(event)
            first = {
                "code": "OBSERVABILITY_METRIC_FAILED",
                "message": "First bounded failure",
                "retryable": False,
            }
            return {
                "contract_version": "1.0",
                "request_id": value["request_id"],
                "project_id": value["project_id"],
                "port": "observability",
                "operation": "export_trace",
                "capability": "workflow_trace",
                "ok": True,
                "status": "success",
                "action": "invoke",
                "called": True,
                "data": {
                    "contract_version": "1.0",
                    "outcome": "failed",
                    "event_ref": value["payload"]["event_ref"],
                    "event_id": event["event_id"],
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "exported": False,
                    "metric_count": len(event["metrics"]),
                    "failures": [
                        first,
                        copy.deepcopy(first),
                        {**first, "message": "Second bounded failure"},
                    ],
                },
                "error": None,
            }

        result = invoke_observability(
            request(), langfuse_capability_document(), adapter,
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["failures"]), 2)
        self.assertEqual(
            [item["message"] for item in result["data"]["failures"]],
            ["First bounded failure", "Second bounded failure"],
        )

    def test_only_transport_and_timeout_failures_are_retryable(self) -> None:
        for error, code in (
            (TimeoutError(), "PORT_ADAPTER_TIMEOUT"),
            (OSError(), "PORT_ADAPTER_UNAVAILABLE"),
            (RuntimeError(), "PORT_ADAPTER_FAILED"),
        ):
            def exporter(payload, timeout, failure=error):
                raise failure

            result = invoke_observability(
                request(), langfuse_capability_document(),
                build_langfuse_observability_adapter(exporter),
                data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
            )
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["error"]["retryable"], code != "PORT_ADAPTER_FAILED")
            self.assertTrue(result["called"])

        for timeout in (0, 301, float("inf")):
            result = invoke_observability(
                request(), langfuse_capability_document(), lambda value, limit: {},
                data_dir=DATA_DIR, dry_run=False, timeout_seconds=timeout,
            )
            self.assertEqual(result["error"]["code"], "PORT_REQUEST_INVALID")
            self.assertFalse(result["called"])

    def test_unhashable_status_and_outcome_fail_with_stable_port_errors(self) -> None:
        invalid_request = request()
        invalid_request["payload"]["event"]["status"] = []
        result = invoke_observability(
            invalid_request, langfuse_capability_document(), lambda value, limit: {},
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertEqual(result["error"]["code"], "PORT_REQUEST_INVALID")
        self.assertFalse(result["called"])

        def invalid_adapter(value, timeout):
            event = value["payload"]["event"]
            trace_id, span_id = stable_observability_identity(event)
            return {
                "contract_version": "1.0", "request_id": value["request_id"],
                "project_id": value["project_id"], "port": "observability",
                "operation": "export_trace", "capability": "workflow_trace",
                "ok": True, "status": "success", "action": "invoke", "called": True,
                "data": {
                    "contract_version": "1.0", "outcome": [],
                    "event_ref": value["payload"]["event_ref"],
                    "event_id": event["event_id"], "trace_id": trace_id,
                    "span_id": span_id, "exported": False,
                    "metric_count": len(event["metrics"]), "failures": [],
                },
                "error": None,
            }

        result = invoke_observability(
            request(), langfuse_capability_document(), invalid_adapter,
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertEqual(result["error"]["code"], "PORT_PROTOCOL_INVALID")
        self.assertTrue(result["called"])

    def test_event_and_port_byte_guards_enforce_their_exact_configured_limits(self) -> None:
        neutral = request()["payload"]["event"]
        event_size = len(json.dumps(
            neutral, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        self.assertEqual(observability.MAX_EVENT_BYTES, 1_000_000)
        with patch.object(observability, "MAX_EVENT_BYTES", event_size):
            validate_neutral_event(neutral)
        with patch.object(observability, "MAX_EVENT_BYTES", event_size - 1):
            with self.assertRaises(IntegrationPortContractError):
                validate_neutral_event(neutral)

        value = request()
        payload_size = len(json.dumps(
            value["payload"], ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        self.assertEqual(observability.MAX_PORT_BYTES, 1_048_576)
        with patch.object(observability, "MAX_PORT_BYTES", payload_size):
            validate_observability_request(value, data_dir=DATA_DIR)
        with patch.object(observability, "MAX_PORT_BYTES", payload_size - 1):
            with self.assertRaises(IntegrationPortContractError):
                validate_observability_request(value, data_dir=DATA_DIR)

    def test_cli_dry_run_is_offline_credential_free_and_read_only(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = project_snapshot(project)
            environment = os.environ.copy()
            environment.pop("LANGFUSE_PUBLIC_KEY", None)
            environment.pop("LANGFUSE_SECRET_KEY", None)
            process = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "ccgs_cli.py"),
                    "langfuse-export",
                    "--project-root", str(project),
                    "--event", "ccgs-data/production/observability/events/story-001-closeout.json",
                    "--host", "https://user:secret@example.invalid",
                    "--public-key-env", "MISSING_PUBLIC_KEY",
                    "--secret-key-env", "MISSING_SECRET_KEY",
                    "--dry-run",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            after = project_snapshot(project)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["ok"])
        self.assertFalse(result["called"])
        self.assertFalse(result["data"]["exported"])
        self.assertEqual(before, after)
        serialized = process.stdout.casefold()
        self.assertNotIn("user:secret", serialized)
        self.assertNotIn(str(project).casefold(), serialized)

    def test_cli_configuration_failure_does_not_modify_core_artifacts_or_echo_secret(self) -> None:
        with materialized_fixture("mature-project") as project:
            before = project_snapshot(project)
            environment = os.environ.copy()
            environment.pop("LANGFUSE_PUBLIC_KEY", None)
            environment.pop("LANGFUSE_SECRET_KEY", None)
            process = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "ccgs_cli.py"),
                    "langfuse-export",
                    "--project-root", str(project),
                    "--event", "ccgs-data/production/observability/events/story-001-closeout.json",
                    "--host", "https://user:do-not-echo@example.invalid",
                    "--send",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            after = project_snapshot(project)
        self.assertEqual(process.returncode, 2)
        result = json.loads(process.stdout)
        self.assertFalse(result["ok"])
        self.assertFalse(result["called"])
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(before, after)
        self.assertNotIn("do-not-echo", process.stdout)
        self.assertNotIn("do-not-echo", process.stderr)

    def test_windmill_observation_stage_delegates_to_public_cli_port(self) -> None:
        adapter = (ROOT / "integrations/windmill/ccgs_windmill_adapter.py").read_text()
        cli = (SCRIPTS / "ccgs_cli.py").read_text()
        flow = (ROOT / "integrations/windmill/f/ccgs/story_observed_closeout__flow/flow.yaml").read_text()
        self.assertIn('OBSERVABILITY_EXPORT_COMMAND = "langfuse-export"', adapter)
        self.assertIn("runner.invoke(OBSERVABILITY_EXPORT_COMMAND", adapter)
        self.assertIn("invoke_observability(", cli)
        self.assertIn("neutral Observability Port", flow)
        for forbidden in (
            "apply_closeout", "apply_advance", "state_transition",
            "evidence_override", "project_writes",
        ):
            self.assertNotIn(forbidden, flow)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema dependency")
    def test_runtime_samples_match_draft_2020_12_schemas(self) -> None:
        from jsonschema import Draft202012Validator

        request_schema = json.loads((ROOT / "schemas/observability-request-data.schema.json").read_text())
        response_schema = json.loads((ROOT / "schemas/observability-response-data.schema.json").read_text())
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(response_schema)
        value = request()
        result = invoke_observability(
            value, langfuse_capability_document(),
            build_langfuse_observability_adapter(
                lambda payload, timeout: True,
                lambda payloads, timeout: len(payloads),
            ),
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=5,
        )
        self.assertFalse(list(Draft202012Validator(request_schema).iter_errors(value["payload"])))
        self.assertFalse(list(Draft202012Validator(response_schema).iter_errors(result["data"])))

        invalid_payloads = []
        invalid_status = copy.deepcopy(value["payload"])
        invalid_status["event"]["status"] = []
        invalid_payloads.append(invalid_status)
        extra = copy.deepcopy(value["payload"])
        extra["host"] = "https://example.invalid"
        invalid_payloads.append(extra)
        for payload in invalid_payloads:
            self.assertTrue(list(Draft202012Validator(request_schema).iter_errors(payload)))

        invalid_response = copy.deepcopy(result["data"])
        invalid_response["outcome"] = []
        self.assertTrue(list(Draft202012Validator(response_schema).iter_errors(invalid_response)))


if __name__ == "__main__":
    unittest.main()
