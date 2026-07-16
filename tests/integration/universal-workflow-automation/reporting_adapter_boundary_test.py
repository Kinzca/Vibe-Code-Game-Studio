"""Integration evidence for STORY-UWA-013's neutral Reporting Port boundary."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_integration_ports import MAX_PAYLOAD_BYTES, IntegrationPortContractError
from vibe_reporting import (
    REPORTING_CAPABILITY,
    REPORTING_OPERATION,
    REPORTING_PORT,
    build_reporting_request,
    invoke_reporting,
    project_evidence,
    project_normalized_results,
    stable_report_fingerprint,
    validate_reporting_payload,
    validate_reporting_request,
    validate_reporting_response_data,
)


DATA_DIR = "fixture-data"
RESULT_REF = f"{DATA_DIR}/production/qa/test-results/tests.json"
EVIDENCE_REF = f"{DATA_DIR}/production/qa/evidence/story-001.json"


def neutral_results() -> list[dict[str, object]]:
    """Return one record for each supported neutral status."""

    return [
        {
            "id": f"test-{status}",
            "name": f"Result {status}",
            "suite": "Neutral suite",
            "status": status,
            "duration_ms": index,
            "start_ms": index * 10,
            "source_ref": RESULT_REF,
            **({"failure_code": "ASSERTION_FAILED"} if status == "failed" else {}),
        }
        for index, status in enumerate(
            ("passed", "failed", "broken", "skipped", "unknown"), start=1
        )
    ]


def neutral_evidence(*, result: str = "fail") -> dict[str, object]:
    """Return internally consistent bounded Evidence."""

    status = {"pass": "pass", "fail": "fail", "blocked": "deferred"}[result]
    return {
        "story_id": "STORY-001",
        "result": result,
        "acceptance_criteria": [
            {"id": "AC-1", "status": status, "source_refs": [EVIDENCE_REF]}
        ],
        "checks": [
            {
                "id": "integration-tests",
                "type": "automated-test",
                "status": status,
                "source_refs": [RESULT_REF],
            }
        ],
        "source_ref": EVIDENCE_REF,
    }


def request_fixture() -> dict[str, object]:
    """Build a fully validated Reporting Port request."""

    return build_reporting_request(
        neutral_results(),
        neutral_evidence(),
        data_dir=DATA_DIR,
        report_id="report-001",
        request_id="request-001",
        project_id="project-001",
    )


def capability_document() -> dict[str, object]:
    """Return the static shape expected from any Reporting adapter."""

    return {
        "contract_version": "1.0",
        "adapter_id": "fixture-reporting-adapter",
        "capabilities": [
            {
                "port": REPORTING_PORT,
                "operation": REPORTING_OPERATION,
                "capability": REPORTING_CAPABILITY,
                "contract_versions": ["1.0"],
            }
        ],
    }


def success_response(request: dict[str, object]) -> dict[str, object]:
    """Return a generic Port success carrying validated reporting data."""

    return {
        "contract_version": "1.0",
        "request_id": request["request_id"],
        "project_id": request["project_id"],
        "port": REPORTING_PORT,
        "operation": REPORTING_OPERATION,
        "capability": REPORTING_CAPABILITY,
        "ok": True,
        "status": "success",
        "action": "invoke",
        "called": True,
        "data": response_data_fixture(request),
        "error": None,
    }


def response_data_fixture(request: dict[str, object]) -> dict[str, object]:
    """Return concrete adapter-planned data without relying on core guesses."""

    payload = request["payload"]
    counts = {status: 0 for status in ("passed", "failed", "broken", "skipped", "unknown")}
    for item in payload["results"]:
        counts[item["status"]] += 1
    counts[{"pass": "passed", "fail": "failed", "blocked": "skipped"}[payload["evidence"]["result"]]] += 1
    output = payload["output_ref"]
    return {
        "contract_version": "1.0", "outcome": "generated",
        "report_id": payload["report_id"], "output_ref": output,
        "artifact_refs": [f"{output}/adapter-result.json"],
        "total_results": len(payload["results"]) + 1,
        "status_counts": counts, "reused": False, "failures": [],
    }


class ReportingAdapterBoundaryTests(unittest.TestCase):
    def assert_contract_error(self, code: str, callback) -> None:
        """Assert a stable contract error without depending on rejected text."""

        with self.assertRaises(IntegrationPortContractError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("report-001", str(raised.exception))

    def test_ac1_request_binds_version_capability_output_and_references(self) -> None:
        request = request_fixture()
        payload = request["payload"]
        self.assertEqual(
            (request["port"], request["operation"], request["capability"]),
            ("reporting", "export_report", "evidence_report"),
        )
        self.assertEqual(payload["contract_version"], "1.0")
        self.assertEqual(
            payload["output_ref"],
            "fixture-data/production/qa/reports/report-001",
        )
        self.assertEqual(
            request["references"],
            [RESULT_REF, EVIDENCE_REF, payload["output_ref"]],
        )

    def test_ac1_invalid_identity_and_path_fail_before_adapter_call(self) -> None:
        for mutation, expected in (
            (("capability", "other"), "PORT_REQUEST_INVALID"),
            (("operation", "retrieve"), "PORT_REQUEST_INVALID"),
            (("contract_version", "2.0"), "PORT_VERSION_UNSUPPORTED"),
        ):
            with self.subTest(mutation=mutation):
                request = request_fixture()
                request[mutation[0]] = mutation[1]
                calls: list[object] = []
                response = invoke_reporting(
                    request, capability_document(),
                    lambda value, timeout: calls.append(value),
                    data_dir=DATA_DIR, dry_run=False,
                )
                self.assertEqual(response["error"]["code"], expected)
                self.assertFalse(response["called"])
                self.assertEqual(calls, [])

        for unsafe in (
            "/tmp/report", "C:/report", "\\\\host\\share", "file:/tmp/report",
            "fixture-data/production/qa/reports/../report-001", "report;touch",
        ):
            request = request_fixture()
            request["payload"]["output_ref"] = unsafe
            response = invoke_reporting(
                request, capability_document(), None,
                data_dir=DATA_DIR, dry_run=True,
            )
            self.assertIn(
                response["error"]["code"],
                {"PORT_REQUEST_INVALID", "PORT_PAYLOAD_UNSAFE"},
            )
            self.assertFalse(response["called"])

    def test_ac2_all_statuses_optional_fields_and_bounds_are_validated(self) -> None:
        request = request_fixture()
        statuses = {item["status"] for item in request["payload"]["results"]}
        self.assertEqual(
            statuses, {"passed", "failed", "broken", "skipped", "unknown"}
        )
        invalid = copy.deepcopy(request["payload"])
        invalid["results"][0]["duration_ms"] = -1
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(invalid, data_dir=DATA_DIR),
        )

        duplicate = copy.deepcopy(request["payload"])
        duplicate["results"][1]["id"] = duplicate["results"][0]["id"]
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(duplicate, data_dir=DATA_DIR),
        )

        extra = copy.deepcopy(request["payload"])
        extra["results"][0]["vendor"] = "specific"
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(extra, data_dir=DATA_DIR),
        )

    def test_ac2_inconsistent_evidence_fails_closed(self) -> None:
        payload = copy.deepcopy(request_fixture()["payload"])
        payload["evidence"]["result"] = "pass"
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(payload, data_dir=DATA_DIR),
        )

    def test_ac2_result_and_evidence_collection_limits_are_exact(self) -> None:
        payload = copy.deepcopy(request_fixture()["payload"])
        payload["results"] = [
            {
                "id": f"result-{index}", "name": "Result", "status": "passed",
                "duration_ms": 0, "source_ref": RESULT_REF,
            }
            for index in range(4_999)
        ]
        self.assertEqual(
            len(validate_reporting_payload(payload, data_dir=DATA_DIR)["results"]),
            4_999,
        )
        payload["results"].append({**payload["results"][-1], "id": "result-5000"})
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(payload, data_dir=DATA_DIR),
        )

        payload = copy.deepcopy(request_fixture()["payload"])
        for key, check in (("acceptance_criteria", False), ("checks", True)):
            items = []
            for index in range(200):
                item = {
                    "id": f"item-{index}", "status": "pass",
                    "source_refs": [EVIDENCE_REF],
                }
                if check:
                    item["type"] = "automated-test"
                items.append(item)
            payload["evidence"][key] = items
        payload["evidence"]["result"] = "pass"
        validated = validate_reporting_payload(payload, data_dir=DATA_DIR)
        self.assertEqual(len(validated["evidence"]["acceptance_criteria"]), 200)
        self.assertEqual(len(validated["evidence"]["checks"]), 200)
        payload["evidence"]["checks"].append({
            "id": "item-201", "type": "automated-test", "status": "pass",
            "source_refs": [EVIDENCE_REF],
        })
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(payload, data_dir=DATA_DIR),
        )

    def test_ac2_string_and_canonical_byte_limits_are_exact(self) -> None:
        payload = copy.deepcopy(request_fixture()["payload"])
        reference_512 = "a/" * 255 + "aa"
        payload["results"][0].update({
            "id": "i" * 128, "name": "n" * 256,
            "source_ref": reference_512,
        })
        validate_reporting_payload(payload, data_dir=DATA_DIR)
        for field, value in (
            ("id", "i" * 129),
            ("name", "n" * 257),
            ("source_ref", "a/" * 256 + "a"),
        ):
            invalid = copy.deepcopy(payload)
            invalid["results"][0][field] = value
            self.assert_contract_error(
                "PORT_REQUEST_INVALID",
                lambda invalid=invalid: validate_reporting_payload(
                    invalid, data_dir=DATA_DIR,
                ),
            )

        sized = copy.deepcopy(request_fixture()["payload"])
        sized["results"] = [
            {
                "id": f"size-{index}", "name": "x", "status": "passed",
                "duration_ms": 0, "source_ref": RESULT_REF,
            }
            for index in range(3_000)
        ]
        canonical = lambda value: len(json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        remaining = MAX_PAYLOAD_BYTES - canonical(sized)
        self.assertGreaterEqual(remaining, 0)
        for item in sized["results"]:
            added = min(remaining, 255)
            item["name"] += "x" * added
            remaining -= added
            if not remaining:
                break
        self.assertEqual(remaining, 0)
        self.assertEqual(canonical(sized), MAX_PAYLOAD_BYTES)
        validate_reporting_payload(sized, data_dir=DATA_DIR)
        oversized = copy.deepcopy(sized)
        expandable = next(item for item in oversized["results"] if len(item["name"]) < 256)
        expandable["name"] += "x"
        self.assertEqual(canonical(oversized), MAX_PAYLOAD_BYTES + 1)
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            lambda: validate_reporting_payload(oversized, data_dir=DATA_DIR),
        )

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema dependency")
    def test_ac2_runtime_byte_bound_and_draft_202012_shape_have_explicit_ownership(self) -> None:
        from jsonschema import Draft202012Validator

        request_schema = json.loads(
            (ROOT / "schemas/reporting-request-data.schema.json").read_text(encoding="utf-8")
        )
        response_schema = json.loads(
            (ROOT / "schemas/reporting-response-data.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(response_schema)
        request = request_fixture()
        response = response_data_fixture(request)
        self.assertFalse(list(Draft202012Validator(request_schema).iter_errors(request["payload"])))
        self.assertFalse(list(Draft202012Validator(response_schema).iter_errors(response)))
        structurally_invalid = copy.deepcopy(request["payload"])
        structurally_invalid["vendor"] = "unsupported"
        self.assertTrue(list(Draft202012Validator(request_schema).iter_errors(structurally_invalid)))
        self.assertEqual(
            request_schema["x-vibe-runtime-validation"],
            {
                "max_canonical_utf8_bytes": MAX_PAYLOAD_BYTES,
                "enforcement": "runtime",
                "$comment": (
                    "Draft 2020-12 cannot express canonical serialized byte length; "
                    "the versioned runtime validator enforces this bound."
                ),
                "unique_result_ids": True,
                "evidence_consistency": True,
                "output_ref_binding": "<data_dir>/production/qa/reports/<report_id>",
            },
        )
        self.assertEqual(
            response_schema["x-vibe-runtime-validation"]["enforcement"],
            "runtime",
        )

    def test_ac3_trusted_projection_drops_logs_source_and_credentials(self) -> None:
        projected = project_normalized_results(
            {
                "schema_version": "1.0",
                "tests": [
                    {
                        "id": "test-1", "name": "Neutral test", "status": "pass",
                        "stdout": "secret=not-forwarded", "stderr": "/private/path",
                        "trace": "source code", "message": "exception text",
                        "package": "legacy.package",
                    }
                ],
            },
            source_ref=RESULT_REF,
        )
        self.assertEqual(
            set(projected[0]), {"id", "name", "status", "duration_ms", "source_ref"}
        )
        serialized = json.dumps(projected)
        for forbidden in ("secret", "/private/path", "source code", "exception text"):
            self.assertNotIn(forbidden, serialized)

    def test_ac3_public_sensitive_fields_reject_without_echo(self) -> None:
        payload = copy.deepcopy(request_fixture()["payload"])
        payload["results"][0]["stdout"] = "SECRET_VALUE"
        try:
            validate_reporting_payload(payload, data_dir=DATA_DIR)
        except IntegrationPortContractError as exc:
            self.assertEqual(exc.code, "PORT_PAYLOAD_UNSAFE")
            self.assertNotIn("SECRET_VALUE", str(exc))
        else:
            self.fail("sensitive public field was accepted")

    def test_ac3_evidence_projection_contains_only_neutral_fields(self) -> None:
        projected = project_evidence(
            {
                "schema_version": "1.0", "story_id": "STORY-001", "result": "pass",
                "acceptance_criteria": [
                    {"id": "AC-1", "status": "pass", "evidence": "free text /private/path"}
                ],
                "checks": [
                    {"id": "tests", "type": "automated-test", "status": "pass", "summary": "SECRET"}
                ],
            },
            source_ref=EVIDENCE_REF,
        )
        self.assertEqual(set(projected), {"story_id", "result", "acceptance_criteria", "checks", "source_ref"})
        self.assertNotIn("free text", json.dumps(projected))
        self.assertNotIn("SECRET", json.dumps(projected))

    def test_ac4_dry_run_requires_concrete_plan_and_never_calls_adapter(self) -> None:
        request = request_fixture()
        calls: list[object] = []
        planned = response_data_fixture(request)
        first = invoke_reporting(
            request, capability_document(), lambda value, timeout: calls.append(value),
            data_dir=DATA_DIR, dry_run=True, dry_run_data=planned,
        )
        second = invoke_reporting(
            request, capability_document(), lambda value, timeout: calls.append(value),
            data_dir=DATA_DIR, dry_run=True, dry_run_data=planned,
        )
        self.assertEqual(first, second)
        self.assertEqual(calls, [])
        self.assertFalse(first["called"])
        self.assertEqual(first["data"]["total_results"], 6)
        self.assertEqual(sum(first["data"]["status_counts"].values()), 6)
        self.assertTrue(all(
            item.startswith(first["data"]["output_ref"] + "/")
            for item in first["data"]["artifact_refs"]
        ))
        missing_plan = invoke_reporting(
            request, capability_document(), None,
            data_dir=DATA_DIR, dry_run=True,
        )
        self.assertEqual(missing_plan["error"]["code"], "PORT_REQUEST_INVALID")
        self.assertFalse(missing_plan["called"])
        self.assertEqual(stable_report_fingerprint(request), stable_report_fingerprint(copy.deepcopy(request)))

    def test_ac4_adapter_response_identity_and_counts_are_postvalidated(self) -> None:
        request = request_fixture()
        invalid = response_data_fixture(request)
        invalid["total_results"] += 1
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            lambda: validate_reporting_response_data(invalid, request=request),
        )
        invalid = response_data_fixture(request)
        invalid["status_counts"] = {
            "passed": invalid["total_results"], "failed": 0, "broken": 0,
            "skipped": 0, "unknown": 0,
        }
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            lambda: validate_reporting_response_data(invalid, request=request),
        )
        invalid = response_data_fixture(request)
        invalid["outcome"] = "failed"
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            lambda: validate_reporting_response_data(invalid, request=request),
        )
        invalid["failures"] = [{
            "code": "REPORT_OUTPUT_CONFLICT", "message": "Report output conflict",
            "retryable": False,
        }]
        invalid["reused"] = True
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            lambda: validate_reporting_response_data(invalid, request=request),
        )
        invalid = response_data_fixture(request)
        invalid["artifact_refs"].append("outside/report.json")
        invalid["artifact_refs"].sort()
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            lambda: validate_reporting_response_data(invalid, request=request),
        )

    def test_ac5_adapter_invocation_is_injected_and_called_once(self) -> None:
        request = request_fixture()
        received: list[dict[str, object]] = []

        def adapter(value: dict[str, object], timeout: float) -> dict[str, object]:
            received.append(value)
            self.assertEqual(timeout, 10.0)
            return success_response(value)

        response = invoke_reporting(
            request, capability_document(), adapter,
            data_dir=DATA_DIR, dry_run=False, timeout_seconds=10,
        )
        self.assertTrue(response["ok"])
        self.assertTrue(response["called"])
        self.assertEqual(len(received), 1)
        self.assertIsNot(received[0], request)

    def test_ac5_only_called_transport_failures_are_retryable(self) -> None:
        request = request_fixture()
        for error, code in (
            (TimeoutError(), "PORT_ADAPTER_TIMEOUT"),
            (OSError(), "PORT_ADAPTER_UNAVAILABLE"),
        ):
            with self.subTest(code=code):
                def adapter(value, timeout, error=error):
                    raise error

                response = invoke_reporting(
                    request, capability_document(), adapter,
                    data_dir=DATA_DIR, dry_run=False,
                )
                self.assertEqual(response["error"]["code"], code)
                self.assertTrue(response["called"])
                self.assertTrue(response["error"]["retryable"])

        invalid = copy.deepcopy(request)
        invalid["payload"]["report_id"] = "different"
        response = invoke_reporting(
            invalid, capability_document(), None,
            data_dir=DATA_DIR, dry_run=False,
        )
        self.assertFalse(response["called"])
        self.assertFalse(response["error"]["retryable"])

    def test_ac5_business_and_protocol_failures_are_not_retryable(self) -> None:
        request = request_fixture()

        def business_failure(value, timeout):
            response = success_response(value)
            response["data"] = response_data_fixture(value)
            response["data"]["outcome"] = "failed"
            response["data"]["failures"] = [
                {"code": "REPORT_RENDER_FAILED", "message": "Report rendering failed", "retryable": False}
            ]
            return response

        failed = invoke_reporting(
            request, capability_document(), business_failure,
            data_dir=DATA_DIR, dry_run=False,
        )
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["data"]["outcome"], "failed")
        self.assertFalse(failed["data"]["failures"][0]["retryable"])

        malformed = invoke_reporting(
            request, capability_document(), lambda value, timeout: {"secret": "value"},
            data_dir=DATA_DIR, dry_run=False,
        )
        self.assertEqual(malformed["error"]["code"], "PORT_PROTOCOL_INVALID")
        self.assertTrue(malformed["called"])
        self.assertFalse(malformed["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
