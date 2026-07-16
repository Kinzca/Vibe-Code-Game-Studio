"""集成端口契约的标准库集成测试。"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

try:
    from jsonschema import Draft202012Validator
except ImportError:  # Optional test dependency; framework runtime stays stdlib-only.
    Draft202012Validator = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPOSITORY_ROOT / ".ccgs-core" / "scripts" / "vibe_integration_ports.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "vibe_integration_ports_under_test", MODULE_PATH
)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("Unable to load integration port contract module")
ports = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(ports)


PORT_OPERATIONS = {
    "orchestration": "trigger",
    "retrieval": "retrieve",
    "observability": "export_trace",
    "reporting": "export_report",
}


def request(
    *,
    port: str = "orchestration",
    operation: str | None = None,
    capability_id: str = "capability.basic",
    payload: dict[str, Any] | None = None,
    references: list[str] | None = None,
    contract_version: str = "1.0",
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "request_id": "request-001",
        "project_id": "project-001",
        "port": port,
        "operation": operation if operation is not None else PORT_OPERATIONS[port],
        "capability": capability_id,
        "payload": {} if payload is None else payload,
        "references": [] if references is None else references,
    }


def capability(
    *,
    port: str = "orchestration",
    operation: str | None = None,
    capability_id: str = "capability.basic",
    contract_version: str = "1.0",
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "adapter_id": "adapter-001",
        "capabilities": [
            {
                "port": port,
                "operation": operation if operation is not None else PORT_OPERATIONS[port],
                "capability": capability_id,
                "contract_versions": [contract_version],
            }
        ],
    }


def success_response(
    source: dict[str, Any],
    *,
    data: dict[str, Any] | None = None,
    action: str = "invoke",
    called: bool = True,
) -> dict[str, Any]:
    return {
        key: source[key]
        for key in (
            "contract_version",
            "request_id",
            "project_id",
            "port",
            "operation",
            "capability",
        )
    } | {
        "ok": True,
        "status": "success",
        "action": action,
        "called": called,
        "data": {} if data is None else data,
        "error": None,
    }


def error_response(
    source: dict[str, Any],
    *,
    code: str,
    status: str,
    action: str,
    called: bool,
    retryable: bool = False,
) -> dict[str, Any]:
    value = success_response(source, action=action, called=called)
    value.update(
        {
            "ok": False,
            "status": status,
            "error": {
                "code": code,
                "message": "Integration port operation did not complete",
                "retryable": retryable,
                "details": {},
            },
        }
    )
    return value


class IntegrationPortContractTest(unittest.TestCase):
    def assert_contract_error(
        self,
        expected_code: str,
        function: Callable[..., Any],
        *args: Any,
    ) -> ports.IntegrationPortContractError:
        with self.assertRaises(ports.IntegrationPortContractError) as raised:
            function(*args)
        self.assertEqual(expected_code, raised.exception.code)
        return raised.exception

    def test_ac1_all_four_ports_validate(self) -> None:
        for port, operation in PORT_OPERATIONS.items():
            with self.subTest(port=port):
                source_request = request(port=port, operation=operation)
                source_capability = capability(port=port, operation=operation)
                self.assertEqual(
                    source_request, ports.validate_port_request(source_request)
                )
                self.assertEqual(
                    source_capability,
                    ports.validate_capability_document(source_capability),
                )
                source_response = success_response(source_request)
                self.assertEqual(
                    source_response,
                    ports.validate_port_response(source_request, source_response),
                )

                call_count = 0

                def adapter(
                    adapter_request: dict[str, Any], _: float
                ) -> dict[str, Any]:
                    nonlocal call_count
                    call_count += 1
                    return success_response(adapter_request)

                invoked = ports.invoke_port(
                    source_request,
                    source_capability,
                    adapter,
                    write=True,
                    timeout_seconds=1,
                )
                self.assertEqual("success", invoked["status"])
                self.assertEqual(source_request["request_id"], invoked["request_id"])
                self.assertEqual(source_request["project_id"], invoked["project_id"])
                self.assertEqual(1, call_count)

    def test_ac1_schemas_declare_required_properties(self) -> None:
        expected_fields = {
            "integration-port-request.schema.json": {
                "contract_version",
                "request_id",
                "project_id",
                "port",
                "operation",
                "capability",
                "payload",
                "references",
            },
            "integration-port-capabilities.schema.json": {
                "contract_version",
                "adapter_id",
                "capabilities",
            },
            "integration-port-response.schema.json": {
                "contract_version",
                "request_id",
                "project_id",
                "port",
                "operation",
                "capability",
                "ok",
                "status",
                "action",
                "called",
                "data",
                "error",
            },
        }
        for filename, fields in expected_fields.items():
            with self.subTest(schema=filename):
                schema = json.loads(
                    (REPOSITORY_ROOT / "schemas" / filename).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(fields, set(schema["required"]))
                self.assertEqual(fields, set(schema["properties"]))

        response_schema = json.loads(
            (
                REPOSITORY_ROOT
                / "schemas"
                / "integration-port-response.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("object", response_schema["properties"]["data"]["type"])
        self.assertEqual(
            {"success", "degraded", "rejected"},
            set(response_schema["properties"]["status"]["enum"]),
        )
        self.assertEqual(
            {"invoke", "validate", "degraded", "reject"},
            set(response_schema["properties"]["action"]["enum"]),
        )
        error_schema = response_schema["$defs"]["error"]
        self.assertEqual(
            {
                "PORT_VERSION_UNSUPPORTED",
                "PORT_REQUEST_INVALID",
                "PORT_CAPABILITY_UNAVAILABLE",
                "PORT_ADAPTER_UNAVAILABLE",
                "PORT_ADAPTER_TIMEOUT",
                "PORT_ADAPTER_FAILED",
                "PORT_PROTOCOL_INVALID",
                "PORT_PAYLOAD_UNSAFE",
            },
            set(error_schema["properties"]["code"]["enum"]),
        )
        retryable_rule = error_schema["allOf"][0]
        self.assertEqual(
            {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"},
            set(retryable_rule["if"]["properties"]["code"]["enum"]),
        )
        self.assertIs(
            True,
            retryable_rule["then"]["properties"]["retryable"]["const"],
        )
        self.assertIs(
            False,
            retryable_rule["else"]["properties"]["retryable"]["const"],
        )
        request_schema = json.loads(
            (
                REPOSITORY_ROOT
                / "schemas"
                / "integration-port-request.schema.json"
            ).read_text(encoding="utf-8")
        )
        runtime_rules = request_schema["properties"]["payload"][
            "x-vibe-runtime-validation"
        ]
        self.assertEqual(ports.MAX_PAYLOAD_BYTES, runtime_rules["max_canonical_utf8_bytes"])
        self.assertEqual(ports.MAX_JSON_DEPTH, runtime_rules["max_depth"])

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is an optional test dependency",
    )
    def test_ac1_draft_2020_12_schemas_validate_contract_relationships(self) -> None:
        schemas = {
            name: json.loads((REPOSITORY_ROOT / "schemas" / name).read_text(encoding="utf-8"))
            for name in (
                "integration-port-request.schema.json",
                "integration-port-response.schema.json",
                "integration-port-capabilities.schema.json",
            )
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)

        request_validator = Draft202012Validator(
            schemas["integration-port-request.schema.json"]
        )
        capability_validator = Draft202012Validator(
            schemas["integration-port-capabilities.schema.json"]
        )
        response_validator = Draft202012Validator(schemas["integration-port-response.schema.json"])
        for port, operation in PORT_OPERATIONS.items():
            source_request = request(port=port, operation=operation)
            request_validator.validate(source_request)
            capability_validator.validate(capability(port=port, operation=operation))
            response_validator.validate(success_response(source_request))

        source_request = request()
        invalid_responses = [
            success_response(source_request, action="validate", called=True),
            error_response(
                source_request,
                code="PORT_PROTOCOL_INVALID",
                status="degraded",
                action="degraded",
                called=True,
            ),
            error_response(
                source_request,
                code="PORT_REQUEST_INVALID",
                status="rejected",
                action="reject",
                called=True,
            ),
        ]
        for invalid_response in invalid_responses:
            with self.subTest(response=invalid_response):
                self.assertTrue(list(response_validator.iter_errors(invalid_response)))

    def test_ac2_rejects_unknown_versions(self) -> None:
        self.assert_contract_error(
            "PORT_VERSION_UNSUPPORTED",
            ports.validate_port_request,
            request(contract_version="2.0"),
        )
        self.assert_contract_error(
            "PORT_VERSION_UNSUPPORTED",
            ports.validate_capability_document,
            capability(contract_version="2.0"),
        )
        source_request = request()

        def unknown_response_version(
            adapter_request: dict[str, Any], _: float
        ) -> dict[str, Any]:
            response = success_response(adapter_request)
            response["contract_version"] = "2.0"
            return response

        response_report = ports.invoke_port(
            source_request,
            capability(),
            unknown_response_version,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual(
            "PORT_VERSION_UNSUPPORTED", response_report["error"]["code"]
        )
        self.assertTrue(response_report["called"])

    def test_ac2_rejects_unknown_capability_operation_extra_and_identity(self) -> None:
        unavailable = ports.invoke_port(
            request(capability_id="capability.unknown"),
            capability(),
            None,
            write=False,
            timeout_seconds=1,
        )
        self.assertEqual("degraded", unavailable["status"])
        self.assertEqual("PORT_CAPABILITY_UNAVAILABLE", unavailable["error"]["code"])

        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            ports.validate_port_request,
            request(operation="operation.unknown"),
        )
        extra = request()
        extra["extra"] = True
        self.assert_contract_error(
            "PORT_REQUEST_INVALID", ports.validate_port_request, extra
        )
        empty_identifier = request()
        empty_identifier["request_id"] = ""
        self.assert_contract_error(
            "PORT_REQUEST_INVALID", ports.validate_port_request, empty_identifier
        )
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            ports.validate_capability_document,
            capability(operation="retrieve"),
        )

        source_request = request()
        mismatched = success_response(source_request)
        mismatched["project_id"] = "project-other"
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            ports.validate_port_response,
            source_request,
            mismatched,
        )

    def test_ac2_malformed_values_return_stable_errors(self) -> None:
        cycle: dict[str, Any] = {}
        cycle["loop"] = cycle
        cyclic_report = ports.invoke_port(
            request(payload=cycle),
            capability(),
            None,
            write=False,
            timeout_seconds=1,
        )
        self.assertEqual("PORT_REQUEST_INVALID", cyclic_report["error"]["code"])
        self.assertFalse(cyclic_report["called"])

        deep_payload: dict[str, Any] = {}
        cursor = deep_payload
        for _ in range(ports.MAX_JSON_DEPTH + 1):
            cursor["next"] = {}
            cursor = cursor["next"]
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            ports.validate_port_request,
            request(payload=deep_payload),
        )
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            ports.validate_port_request,
            request(payload={"not_json": {"value"}}),
        )

        source_request = request()
        invalid_status = success_response(source_request)
        invalid_status["status"] = []
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            ports.validate_port_response,
            source_request,
            invalid_status,
        )
        invalid_code = error_response(
            source_request,
            code="PORT_PROTOCOL_INVALID",
            status="rejected",
            action="reject",
            called=True,
        )
        invalid_code["error"]["code"] = []
        self.assert_contract_error(
            "PORT_PROTOCOL_INVALID",
            ports.validate_port_response,
            source_request,
            invalid_code,
        )

        for timeout in (True, 0, -1, float("nan"), float("inf"), 10**10000, "1"):
            with self.subTest(timeout=type(timeout).__name__):
                report = ports.invoke_port(
                    request(),
                    capability(),
                    None,
                    write=False,
                    timeout_seconds=timeout,
                )
                self.assertEqual("PORT_REQUEST_INVALID", report["error"]["code"])
                self.assertFalse(report["called"])

    def test_ac2_preflight_and_adapter_response_called_semantics(self) -> None:
        call_count = 0

        def adapter(adapter_request: dict[str, Any], _: float) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return success_response(adapter_request)

        unavailable = ports.invoke_port(
            request(capability_id="capability.unknown"),
            capability(),
            adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("PORT_CAPABILITY_UNAVAILABLE", unavailable["error"]["code"])
        self.assertFalse(unavailable["called"])
        self.assertEqual(0, call_count)

        missing_adapter = ports.invoke_port(
            request(), capability(), None, write=True, timeout_seconds=1
        )
        self.assertEqual(
            "PORT_CAPABILITY_UNAVAILABLE", missing_adapter["error"]["code"]
        )
        self.assertFalse(missing_adapter["called"])

        def invalid_response_adapter(
            adapter_request: dict[str, Any], _: float
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            response = success_response(adapter_request)
            response["extra"] = True
            return response

        rejected = ports.invoke_port(
            request(),
            capability(),
            invalid_response_adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", rejected["error"]["code"])
        self.assertTrue(rejected["called"])
        self.assertEqual(1, call_count)

        def false_preflight_adapter(
            adapter_request: dict[str, Any], _: float
        ) -> dict[str, Any]:
            return error_response(
                adapter_request,
                code="PORT_REQUEST_INVALID",
                status="rejected",
                action="reject",
                called=True,
            )

        false_preflight = ports.invoke_port(
            request(),
            capability(),
            false_preflight_adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("PORT_PROTOCOL_INVALID", false_preflight["error"]["code"])
        self.assertTrue(false_preflight["called"])

    def test_ac3_validators_return_isolated_deep_copies(self) -> None:
        source_request = request(payload={"nested": {"values": [1]}})
        request_copy = ports.validate_port_request(source_request)
        source_request["payload"]["nested"]["values"].append(2)
        self.assertEqual([1], request_copy["payload"]["nested"]["values"])
        request_copy["payload"]["nested"]["values"].append(3)
        self.assertEqual([1, 2], source_request["payload"]["nested"]["values"])

        source_capability = capability()
        capability_copy = ports.validate_capability_document(source_capability)
        source_capability["capabilities"][0]["contract_versions"].append("1.0")
        self.assertEqual(
            ["1.0"], capability_copy["capabilities"][0]["contract_versions"]
        )

        response_request = request()
        source_response = success_response(
            response_request, data={"nested": {"values": [1]}}
        )
        response_copy = ports.validate_port_response(response_request, source_response)
        source_response["data"]["nested"]["values"].append(2)
        self.assertEqual([1], response_copy["data"]["nested"]["values"])

    def test_ac3_module_imports_are_standard_library_only(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        import_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                import_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                import_roots.add(node.module.split(".", 1)[0])
        self.assertEqual(
            {"__future__", "copy", "json", "math", "re", "typing", "urllib"},
            import_roots,
        )
        self.assertFalse(
            import_roots
            & {"adapter", "adapters", "integration", "integrations"}
        )

    def test_ac3_adapter_input_is_isolated_and_control_fields_are_enforced(self) -> None:
        source_request = request(payload={"nested": {"values": [1]}})

        def mutating_adapter(adapter_request: dict[str, Any], _: float) -> dict[str, Any]:
            adapter_request["payload"]["nested"]["values"].append(2)
            return success_response(adapter_request)

        result = ports.invoke_port(
            source_request,
            capability(),
            mutating_adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("success", result["status"])
        self.assertEqual([1], source_request["payload"]["nested"]["values"])

        def invalid_control_adapter(
            adapter_request: dict[str, Any], _: float
        ) -> dict[str, Any]:
            return success_response(adapter_request, action="validate", called=False)

        rejected = ports.invoke_port(
            request(),
            capability(),
            invalid_control_adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("rejected", rejected["status"])
        self.assertTrue(rejected["called"])
        self.assertEqual("PORT_PROTOCOL_INVALID", rejected["error"]["code"])

        unsafe_data_fields = (
            "state_transition",
            "policy_override",
            "evidence_override",
            "project_writes",
            "commands",
            "private_prompt",
            "source_text",
            "source_code",
        )
        for field in unsafe_data_fields:
            with self.subTest(response_data_field=field):
                def unsafe_data_adapter(
                    adapter_request: dict[str, Any],
                    _: float,
                    field_name: str = field,
                ) -> dict[str, Any]:
                    return success_response(
                        adapter_request, data={field_name: "forbidden"}
                    )

                unsafe_result = ports.invoke_port(
                    request(),
                    capability(),
                    unsafe_data_adapter,
                    write=True,
                    timeout_seconds=1,
                )
                self.assertEqual("rejected", unsafe_result["status"])
                self.assertTrue(unsafe_result["called"])
                self.assertEqual(
                    "PORT_PAYLOAD_UNSAFE", unsafe_result["error"]["code"]
                )

    def test_ac4_failures_are_closed_and_leave_tempfile_unchanged(self) -> None:
        def raise_error(error: Exception) -> Callable[[dict[str, Any], float], Any]:
            def adapter(_: dict[str, Any], __: float) -> Any:
                raise error

            return adapter

        cases: list[
            tuple[
                str,
                Callable[[dict[str, Any], float], Any] | None,
                str,
                str,
                bool,
                bool,
            ]
        ] = [
            (
                "missing_adapter",
                None,
                "degraded",
                "PORT_CAPABILITY_UNAVAILABLE",
                False,
                False,
            ),
            (
                "none_response",
                lambda _request, _timeout: None,
                "rejected",
                "PORT_PROTOCOL_INVALID",
                True,
                False,
            ),
            (
                "os_error",
                raise_error(OSError("unavailable")),
                "degraded",
                "PORT_ADAPTER_UNAVAILABLE",
                True,
                True,
            ),
            (
                "timeout",
                raise_error(TimeoutError("expired")),
                "degraded",
                "PORT_ADAPTER_TIMEOUT",
                True,
                True,
            ),
            (
                "exception",
                raise_error(RuntimeError("failed")),
                "degraded",
                "PORT_ADAPTER_FAILED",
                True,
                False,
            ),
            (
                "bad_response",
                lambda _request, _timeout: {"ok": True},
                "rejected",
                "PORT_PROTOCOL_INVALID",
                True,
                False,
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_names = ("Plan", "Result", "Evidence", "Replay", "Story")
            artifacts = {
                name: Path(temporary_directory) / f"{name.lower()}.bin"
                for name in artifact_names
            }
            for name, artifact in artifacts.items():
                artifact.write_bytes(f"unchanged-{name}".encode("utf-8"))
            snapshots = {
                name: (
                    artifact.read_bytes(),
                    artifact.stat().st_mtime_ns,
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                )
                for name, artifact in artifacts.items()
            }
            expected_entries = sorted(path.name for path in Path(temporary_directory).iterdir())
            for name, adapter, status, code, called, retryable in cases:
                with self.subTest(case=name):
                    result = ports.invoke_port(
                        request(),
                        capability(),
                        adapter,
                        write=True,
                        timeout_seconds=1,
                    )
                    self.assertEqual(status, result["status"])
                    expected_action = "reject" if status == "rejected" else "degraded"
                    self.assertEqual(expected_action, result["action"])
                    self.assertEqual(code, result["error"]["code"])
                    self.assertIs(called, result["called"])
                    self.assertIs(retryable, result["error"]["retryable"])
                    for artifact_name, artifact in artifacts.items():
                        expected_bytes, expected_mtime, expected_hash = snapshots[
                            artifact_name
                        ]
                        self.assertEqual(expected_bytes, artifact.read_bytes())
                        self.assertEqual(expected_mtime, artifact.stat().st_mtime_ns)
                        self.assertEqual(
                            expected_hash,
                            hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        )
                    self.assertEqual(
                        expected_entries,
                        sorted(path.name for path in Path(temporary_directory).iterdir()),
                    )
                    serialized = json.dumps(result, sort_keys=True)
                    self.assertNotIn("unavailable", serialized)
                    self.assertNotIn("expired", serialized)
                    self.assertNotIn("failed", serialized)

    def test_ac5_dry_run_with_no_adapter_succeeds(self) -> None:
        result = ports.invoke_port(
            request(),
            capability(),
            None,
            write=False,
            timeout_seconds=1,
        )
        self.assertTrue(result["ok"])
        self.assertEqual("success", result["status"])
        self.assertEqual("validate", result["action"])
        self.assertFalse(result["called"])
        self.assertIsNone(result["error"])

    def test_ac5_rejects_paths_credentials_and_sensitive_keys(self) -> None:
        unsafe_requests = [
            (request(payload={"value": "/absolute/private-location"}), "private-location"),
            (request(payload={"value": r"C:\private-location\item"}), "private-location"),
            (request(payload={"value": r"\\server\private-location\item"}), "private-location"),
            (
                request(
                    payload={
                        "value": "https://user:credential-marker@example.invalid/item"
                    }
                ),
                "credential-marker",
            ),
            (request(payload={"API_KEY": "sensitive-marker"}), "sensitive-marker"),
            (request(payload={"authorization": "sensitive-marker"}), "sensitive-marker"),
            (request(payload={"private_prompt": "sensitive-marker"}), "sensitive-marker"),
            (request(payload={"source_text": "sensitive-marker"}), "sensitive-marker"),
            (request(payload={"source_code": "sensitive-marker"}), "sensitive-marker"),
            (request(payload={"token": "sensitive-marker"}), "sensitive-marker"),
            (request(references=["/absolute/private-location"]), "private-location"),
            (
                request(
                    references=[
                        "https://user:credential-marker@example.invalid/reference"
                    ]
                ),
                "credential-marker",
            ),
        ]
        for unsafe_request, rejected_fragment in unsafe_requests:
            with self.subTest(value=unsafe_request):
                error = self.assert_contract_error(
                    "PORT_PAYLOAD_UNSAFE",
                    ports.validate_port_request,
                    unsafe_request,
                )
                self.assertNotIn(rejected_fragment, str(error))
                report = ports.invoke_port(
                    unsafe_request,
                    capability(),
                    None,
                    write=False,
                    timeout_seconds=1,
                )
                self.assertEqual("rejected", report["status"])
                self.assertEqual("PORT_PAYLOAD_UNSAFE", report["error"]["code"])
                self.assertNotIn(
                    rejected_fragment,
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                )

    def test_ac5_rejects_embedded_locations_from_adapter_responses(self) -> None:
        unsafe_values = (
            "failed at /absolute/private-location/item",
            r"failed at C:\private-location\item",
            r"failed at \\server\private-location\item",
            "failed via https://user:credential-marker@example.invalid/item",
        )
        for unsafe_value in unsafe_values:
            with self.subTest(value=unsafe_value):
                def unsafe_adapter(
                    adapter_request: dict[str, Any],
                    _: float,
                    value: str = unsafe_value,
                ) -> dict[str, Any]:
                    return success_response(adapter_request, data={"message": value})

                report = ports.invoke_port(
                    request(),
                    capability(),
                    unsafe_adapter,
                    write=True,
                    timeout_seconds=1,
                )
                self.assertEqual("PORT_PAYLOAD_UNSAFE", report["error"]["code"])
                self.assertTrue(report["called"])
                serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
                self.assertNotIn("private-location", serialized)
                self.assertNotIn("credential-marker", serialized)

        def unsafe_error_adapter(
            adapter_request: dict[str, Any], _: float
        ) -> dict[str, Any]:
            response = error_response(
                adapter_request,
                code="PORT_ADAPTER_FAILED",
                status="degraded",
                action="degraded",
                called=True,
            )
            response["error"]["details"] = {
                "location": "failed at /absolute/private-location/item"
            }
            return response

        unsafe_error = ports.invoke_port(
            request(),
            capability(),
            unsafe_error_adapter,
            write=True,
            timeout_seconds=1,
        )
        self.assertEqual("PORT_PAYLOAD_UNSAFE", unsafe_error["error"]["code"])
        self.assertNotIn("private-location", json.dumps(unsafe_error, sort_keys=True))

    def test_ac5_dry_run_and_write_share_preflight_without_dry_run_calls(self) -> None:
        call_count = 0

        def adapter(adapter_request: dict[str, Any], _: float) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return success_response(adapter_request)

        with tempfile.TemporaryDirectory() as temporary_directory:
            initial_entries = list(Path(temporary_directory).iterdir())
            dry_run = ports.invoke_port(
                request(), capability(), adapter, write=False, timeout_seconds=1
            )
            self.assertEqual("success", dry_run["status"])
            self.assertFalse(dry_run["called"])
            self.assertEqual(0, call_count)
            self.assertEqual(initial_entries, list(Path(temporary_directory).iterdir()))

            write_result = ports.invoke_port(
                request(), capability(), adapter, write=True, timeout_seconds=1
            )
            self.assertEqual("success", write_result["status"])
            self.assertTrue(write_result["called"])
            self.assertEqual(1, call_count)

            for write in (False, True):
                unavailable = ports.invoke_port(
                    request(capability_id="capability.unknown"),
                    capability(),
                    adapter,
                    write=write,
                    timeout_seconds=1,
                )
                self.assertEqual(
                    "PORT_CAPABILITY_UNAVAILABLE", unavailable["error"]["code"]
                )
                self.assertFalse(unavailable["called"])
            self.assertEqual(1, call_count)

            unsafe = request(payload={"token": "sensitive-marker"})
            for write in (False, True):
                rejected = ports.invoke_port(
                    unsafe,
                    capability(),
                    adapter,
                    write=write,
                    timeout_seconds=1,
                )
                self.assertEqual("PORT_PAYLOAD_UNSAFE", rejected["error"]["code"])
                self.assertFalse(rejected["called"])
            self.assertEqual(1, call_count)

    def test_ac5_genericity_scan_rejects_project_and_engine_special_cases(self) -> None:
        scanned_files = [
            MODULE_PATH,
            REPOSITORY_ROOT / "schemas" / "integration-port-request.schema.json",
            REPOSITORY_ROOT / "schemas" / "integration-port-response.schema.json",
            REPOSITORY_ROOT / "schemas" / "integration-port-capabilities.schema.json",
        ]
        forbidden_tokens = (
            "Marble" + "Game",
            "Co" + "cos",
            "Uni" + "ty",
            "Go" + "dot",
            "Un" + "real",
        )
        for scanned_file in scanned_files:
            content = scanned_file.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                with self.subTest(file=scanned_file.name, token=token):
                    self.assertNotIn(token, content)

    def test_boundaries_allow_one_mib_payload_and_one_hundred_references(self) -> None:
        empty_payload_size = len(
            json.dumps(
                {"blob": ""},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        exact_payload = {"blob": "x" * (ports.MAX_PAYLOAD_BYTES - empty_payload_size)}
        references = [f"refs/item-{index:03d}" for index in range(ports.MAX_REFERENCES)]
        boundary_request = request(payload=exact_payload, references=references)
        self.assertEqual(
            boundary_request, ports.validate_port_request(boundary_request)
        )

        oversized_payload_request = copy.deepcopy(boundary_request)
        oversized_payload_request["payload"]["blob"] += "x"
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            ports.validate_port_request,
            oversized_payload_request,
        )

        too_many_references_request = request(
            references=references + ["refs/item-over-limit"]
        )
        self.assert_contract_error(
            "PORT_REQUEST_INVALID",
            ports.validate_port_request,
            too_many_references_request,
        )


if __name__ == "__main__":
    unittest.main()
