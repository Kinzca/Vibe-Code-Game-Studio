"""Native integration evidence for STORY-UWA-015 cross-platform contracts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vibe_upgrade import (
    apply_upgrade,
    atomic_replace_file,
    build_upgrade_plan,
    secure_atomic_replace_supported,
)
from vibe_workflow_execute import execute_step


CLI = SCRIPTS / "ccgs_cli.py"
NATIVE_REQUIRED = os.environ.get("CCGS_CROSS_PLATFORM_NATIVE_REQUIRED") == "1"
REQUIRED_ENTRIES_ENV = "CCGS_CROSS_PLATFORM_REQUIRED_ENTRIES"
EXECUTION_POLICY = {
    "contract_version": "1.0",
    "timeout_seconds": 0.5,
    "max_log_bytes": 1024,
    "termination_grace_seconds": 0.2,
}


def current_os_label() -> str:
    """Return the stable OS family recorded in native evidence."""

    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def native_entry_labels() -> tuple[str, ...]:
    """Return every public entry that must execute on this native runner."""

    return ("ccgs.cmd", "ccgs.ps1") if os.name == "nt" else ("./ccgs.sh",)


def powershell_executable() -> str:
    """Resolve a native PowerShell host without treating absence as a skip."""

    candidate = shutil.which("pwsh") or shutil.which("powershell")
    if candidate is None:
        raise AssertionError("ccgs.ps1 requires a native PowerShell host")
    return candidate


def entry_command(label: str, root: Path = ROOT) -> list[str]:
    """Build an argv array for one exact public entry label."""

    if label == "./ccgs.sh":
        return [str(root / "ccgs.sh")]
    if label == "ccgs.cmd":
        return [str(root / "ccgs.cmd")]
    if label == "ccgs.ps1":
        return [
            powershell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "ccgs.ps1"),
        ]
    raise AssertionError(f"unsupported public entry label: {label}")


def run_process(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    force_python: bool = True,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    """Run a public argv array with strict UTF-8 capture."""

    merged = os.environ.copy()
    if force_python:
        merged["CCGS_PYTHON"] = sys.executable
    else:
        merged.pop("CCGS_PYTHON", None)
    if environment is not None:
        merged.update(environment)
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )


def run_baseline(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return run_process([sys.executable, str(CLI), *arguments])


def run_entry(
    label: str,
    arguments: Sequence[str],
    root: Path = ROOT,
    *,
    force_python: bool = True,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_process(
        [*entry_command(label, root), *arguments],
        environment=environment,
        force_python=force_python,
        cwd=root,
    )


def write_manifest(
    project: Path,
    relative: str = "vibe-workflow.json",
    *,
    argv: Sequence[str] | None = None,
) -> None:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "steps": [
                    {
                        "id": "neutral-step",
                        "argv": list(argv or ("tool", "--", "参数 with spaces")),
                        "working_directory": ".",
                        "artifacts": ["out/result.json"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def schema_document(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def assert_schema(instance: Any, schema_name: str) -> None:
    """Validate the Draft 2020-12 subset used by public CCGS schemas."""

    root_schema = schema_document(schema_name)

    def resolve(reference: str, current_root: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if reference.startswith("#"):
            target_root = current_root
            fragment = reference[1:]
        else:
            file_name, _, fragment = reference.partition("#")
            target_root = schema_document(file_name)
        target: Any = target_root
        if fragment:
            for part in fragment.removeprefix("/").split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        return target, target_root

    def validate(value: Any, schema: Mapping[str, Any], current_root: dict[str, Any], path: str) -> None:
        if "$ref" in schema:
            target, target_root = resolve(str(schema["$ref"]), current_root)
            validate(value, target, target_root, path)
            return
        if "oneOf" in schema:
            matches = 0
            for option in schema["oneOf"]:
                try:
                    validate(value, option, current_root, path)
                except AssertionError:
                    continue
                matches += 1
            assert matches == 1, f"{path}: expected exactly one schema match"
            return
        if "const" in schema:
            assert value == schema["const"], f"{path}: const mismatch"
        if "enum" in schema:
            assert value in schema["enum"], f"{path}: enum mismatch"
        expected = schema.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in {int, float},
            "boolean": type(value) is bool,
            "null": value is None,
        }
        if expected is not None:
            assert type_matches.get(str(expected), False), f"{path}: expected {expected}"
        if isinstance(value, str):
            assert len(value) >= int(schema.get("minLength", 0)), f"{path}: too short"
            if "maxLength" in schema:
                assert len(value) <= int(schema["maxLength"]), f"{path}: too long"
            if "pattern" in schema:
                assert re.fullmatch(str(schema["pattern"]), value), f"{path}: pattern mismatch"
        if type(value) in {int, float} and "minimum" in schema:
            assert value >= schema["minimum"], f"{path}: below minimum"
        if isinstance(value, list):
            assert len(value) >= int(schema.get("minItems", 0)), f"{path}: too few items"
            if "maxItems" in schema:
                assert len(value) <= int(schema["maxItems"]), f"{path}: too many items"
            if schema.get("uniqueItems"):
                markers = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
                assert len(markers) == len(set(markers)), f"{path}: duplicate items"
            if isinstance(schema.get("items"), Mapping):
                for index, item in enumerate(value):
                    validate(item, schema["items"], current_root, f"{path}[{index}]")
        if isinstance(value, dict):
            required = set(schema.get("required", ()))
            assert required <= set(value), f"{path}: missing {sorted(required - set(value))}"
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            if additional is False:
                assert set(value) <= set(properties), f"{path}: unknown {sorted(set(value) - set(properties))}"
            for key, item in value.items():
                if key in properties:
                    validate(item, properties[key], current_root, f"{path}.{key}")
                elif isinstance(additional, Mapping):
                    validate(item, additional, current_root, f"{path}.{key}")

    validate(instance, root_schema, root_schema, "$")


def normalized_execution(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("duration_ms", None)
    return result


def record_native_evidence(entry: str, scenario: str, result: str = "pass") -> None:
    print(
        "NATIVE_EVIDENCE "
        f"os={current_os_label()} python={platform.python_version()} "
        f"entry={entry} scenario={scenario} result={result}"
    )


def project_tree(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    """Capture content, mode, mtime, and directory membership."""

    rows: list[tuple[str, str, int, int]] = []
    for path in (root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            digest = "symlink:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "directory"
        rows.append((relative, digest, metadata.st_mode & 0o777, metadata.st_mtime_ns))
    return tuple(rows)


def authorized_report(argv: Sequence[str]) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "ok": True,
        "plan_id": "a" * 64,
        "steps": [
            {
                "id": "native-step",
                "argv": list(argv),
                "working_directory": ".",
                "environment": {},
                "artifacts": [],
            }
        ],
    }


def process_exists(pid: int) -> bool:
    """Check one PID using the current platform's native process API."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


class CrossPlatformContractTest(unittest.TestCase):
    """All common assertions run natively; no required entry is simulated."""

    def test_ac1_launchers_share_one_core_and_preserve_arguments(self) -> None:
        launcher_text = {
            name: (ROOT / name).read_text(encoding="utf-8")
            for name in ("ccgs.sh", "ccgs.cmd", "ccgs.ps1")
        }
        for name, text in launcher_text.items():
            with self.subTest(launcher=name):
                self.assertIn(".ccgs-core", text)
                self.assertIn("ccgs_cli.py", text)
                self.assertNotIn("vibe_workflow_execute", text)
                self.assertNotIn("vibe_upgrade", text)
                self.assertNotIn("Evidence", text)
        self.assertIn("DisableDelayedExpansion", launcher_text["ccgs.cmd"])
        capability_text = (ROOT / "ccgs.workflow.yaml").read_text(encoding="utf-8")
        for required in (
            "workflow-plan:",
            "workflow-execute:",
            "schemas/doctor-result.schema.json",
            "schemas/manifest-load-result.schema.json",
            "schemas/workflow-plan.schema.json",
            "schemas/workflow-execution-result.schema.json",
        ):
            self.assertIn(required, capability_text)

        with tempfile.TemporaryDirectory(prefix="vibe 参数 space ") as temp_dir:
            project = Path(temp_dir)
            relative = "配置 ! 空格.json"
            write_manifest(project, relative)
            arguments = [
                "manifest-load",
                "--project-root",
                str(project),
                f"--manifest-path={relative}",
            ]
            baseline = run_baseline(arguments)
            self.assertEqual(0, baseline.returncode, baseline.stderr)
            for label in native_entry_labels():
                with self.subTest(entry=label):
                    completed = run_entry(label, arguments)
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual(json.loads(baseline.stdout), json.loads(completed.stdout))

        with tempfile.TemporaryDirectory(prefix="框架 root with spaces ") as temp_dir:
            isolated = Path(temp_dir)
            probe = isolated / ".ccgs-core" / "scripts" / "ccgs_cli.py"
            probe.parent.mkdir(parents=True)
            probe.write_text(
                "import json,sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
                encoding="utf-8",
            )
            exact_arguments = ["", "--", "尾部 参数", "!literal!", "%PATH_LITERAL%"]
            for label in native_entry_labels():
                source_name = label.removeprefix("./")
                shutil.copy2(ROOT / source_name, isolated / source_name)
                if source_name == "ccgs.sh":
                    (isolated / source_name).chmod(0o755)
                completed = run_entry(label, exact_arguments, isolated)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(exact_arguments, json.loads(completed.stdout))
                record_native_evidence(label, "argument-passthrough")

    def test_ac2_success_json_and_exit_code_match_public_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibe success 空格 ") as temp_dir:
            project = Path(temp_dir)
            (project / "ccgs-data").mkdir()
            manifest_name = "配置 plan.json"
            write_manifest(
                project,
                manifest_name,
                argv=(sys.executable, "-c", "print('neutral-success')"),
            )
            cases = (
                (
                    "doctor",
                    ["doctor", "--project-root", str(project), "--json"],
                    "doctor-result.schema.json",
                    False,
                ),
                (
                    "manifest",
                    ["manifest-load", "--project-root", str(project), "--manifest-path", manifest_name],
                    "manifest-load-result.schema.json",
                    False,
                ),
                (
                    "plan",
                    ["workflow-plan", "--project-root", str(project), "--manifest-path", manifest_name],
                    "workflow-plan.schema.json",
                    False,
                ),
                (
                    "execute",
                    [
                        "workflow-execute", "--project-root", str(project),
                        "--manifest-path", manifest_name, "--step-id", "neutral-step",
                    ],
                    "workflow-execution-result.schema.json",
                    True,
                ),
                (
                    "upgrade-preview",
                    ["upgrade", "--project-root", str(project), "--dry-run", "--json"],
                    "upgrade-plan.schema.json",
                    False,
                ),
            )
            for case_name, arguments, schema_name, normalize_duration in cases:
                baseline = run_baseline(arguments)
                self.assertEqual(0, baseline.returncode, baseline.stderr)
                expected = json.loads(baseline.stdout)
                assert_schema(expected, schema_name)
                if case_name == "upgrade-preview":
                    for write in expected["writes"]:
                        self.assertNotIn("\\", write["path"])
                        self.assertFalse(Path(write["path"]).is_absolute())
                for label in native_entry_labels():
                    with self.subTest(entry=label, case=case_name):
                        completed = run_entry(label, arguments)
                        self.assertEqual(0, completed.returncode, completed.stderr)
                        actual = json.loads(completed.stdout)
                        assert_schema(actual, schema_name)
                        if normalize_duration:
                            self.assertEqual(normalized_execution(expected), normalized_execution(actual))
                        else:
                            self.assertEqual(expected, actual)
            for label in native_entry_labels():
                record_native_evidence(label, "success-contracts")

    def test_ac3_failures_have_stable_json_retry_and_launcher_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibe-failure-") as temp_dir:
            missing = Path(temp_dir) / "missing"
            arguments = ["doctor", "--project-root", str(missing), "--json"]
            baseline = run_baseline(arguments)
            expected = json.loads(baseline.stdout)
            self.assertEqual(1, baseline.returncode)
            self.assertEqual(
                "1.0", expected.get("contract_version", expected.get("schema_version"))
            )
            self.assertEqual("PROJECT_ROOT_NOT_FOUND", expected["error"]["code"])
            self.assertFalse(expected["error"]["retryable"])
            self.assertNotIn(temp_dir, baseline.stdout + baseline.stderr)
            for label in native_entry_labels():
                completed = run_entry(label, arguments)
                self.assertEqual(1, completed.returncode)
                actual = json.loads(completed.stdout)
                self.assertEqual(
                    "1.0", actual.get("contract_version", actual.get("schema_version"))
                )
                self.assertEqual(expected, actual)
                self.assertNotIn(temp_dir, completed.stdout + completed.stderr)

            usage = ["upgrade", "--project-root", str(missing)]
            usage_baseline = run_baseline(usage)
            self.assertEqual(2, usage_baseline.returncode)
            usage_payload = json.loads(usage_baseline.stdout)
            self.assertEqual(
                "1.0",
                usage_payload.get("contract_version", usage_payload.get("schema_version")),
            )
            self.assertEqual("CLI_USAGE_ERROR", usage_payload["error"]["code"])
            self.assertFalse(usage_payload["error"]["retryable"])
            for label in native_entry_labels():
                completed = run_entry(label, usage)
                self.assertEqual(2, completed.returncode)
                actual = json.loads(completed.stdout)
                self.assertEqual(
                    "1.0", actual.get("contract_version", actual.get("schema_version"))
                )
                self.assertEqual(usage_payload, actual)

            scenarios: list[tuple[str, list[str], str]] = []

            invalid = Path(temp_dir) / "invalid-manifest"
            invalid.mkdir()
            (invalid / "vibe-workflow.json").write_text(
                '{"schema_version":"9.0","steps":[]}', encoding="utf-8"
            )
            scenarios.append((
                "invalid-manifest",
                ["workflow-plan", "--project-root", str(invalid)],
                "MANIFEST_SCHEMA_UNSUPPORTED",
            ))

            rejected = Path(temp_dir) / "policy-rejected"
            rejected.mkdir()
            (rejected / "vibe-workflow.json").write_text(
                json.dumps({
                    "schema_version": "1.0",
                    "steps": [{"id": "neutral-step", "argv": ["tool"], "working_directory": "../outside"}],
                }),
                encoding="utf-8",
            )
            scenarios.append((
                "policy-rejected",
                ["workflow-execute", "--project-root", str(rejected), "--step-id", "neutral-step"],
                "PREFLIGHT_PATH_INVALID",
            ))

            business = Path(temp_dir) / "business-failure"
            business.mkdir()
            write_manifest(business, argv=(sys.executable, "-c", "raise SystemExit(7)"))
            scenarios.append((
                "business-failure",
                ["workflow-execute", "--project-root", str(business), "--step-id", "neutral-step"],
                "EXECUTION_COMMAND_FAILED",
            ))

            timeout = Path(temp_dir) / "timeout"
            timeout.mkdir()
            write_manifest(timeout, argv=(sys.executable, "-c", "import time;time.sleep(10)"))
            scenarios.append((
                "timeout",
                [
                    "workflow-execute", "--project-root", str(timeout), "--step-id", "neutral-step",
                    "--timeout-seconds", "0.1", "--termination-grace-seconds", "0.05",
                ],
                "EXECUTION_TIMED_OUT",
            ))

            cancelled = Path(temp_dir) / "cancelled"
            cancelled.mkdir()
            (cancelled / "cancel.marker").write_text("cancel\n", encoding="utf-8")
            write_manifest(cancelled, argv=(sys.executable, "-c", "import time;time.sleep(10)"))
            scenarios.append((
                "cancelled",
                [
                    "workflow-execute", "--project-root", str(cancelled), "--step-id", "neutral-step",
                    "--cancel-file", "cancel.marker",
                ],
                "EXECUTION_CANCELLED",
            ))

            for scenario, arguments, expected_code in scenarios:
                baseline_failure = run_baseline(arguments)
                self.assertEqual(1, baseline_failure.returncode, baseline_failure.stderr)
                expected_failure = json.loads(baseline_failure.stdout)
                self.assertEqual(
                    "1.0",
                    expected_failure.get("contract_version", expected_failure.get("schema_version")),
                )
                error = expected_failure["error"]
                self.assertEqual(expected_code, error["code"])
                retryable = expected_failure.get("retryable", error.get("retryable"))
                self.assertIs(retryable, False)
                self.assertNotIn(temp_dir, baseline_failure.stdout + baseline_failure.stderr)
                for label in native_entry_labels():
                    completed = run_entry(label, arguments)
                    self.assertEqual(1, completed.returncode, completed.stderr)
                    actual = json.loads(completed.stdout)
                    self.assertEqual(
                        "1.0",
                        actual.get("contract_version", actual.get("schema_version")),
                    )
                    if "duration_ms" in expected_failure:
                        self.assertEqual(
                            normalized_execution(expected_failure),
                            normalized_execution(actual),
                        )
                    else:
                        self.assertEqual(expected_failure, actual)
                    self.assertNotIn(temp_dir, completed.stdout + completed.stderr)

            for label in native_entry_labels():
                with self.subTest(entry=label, failure="cli-missing"):
                    isolated = Path(temp_dir) / (label.replace("/", "_") + "-missing")
                    isolated.mkdir()
                    source_name = label.removeprefix("./")
                    shutil.copy2(ROOT / source_name, isolated / source_name)
                    if source_name == "ccgs.sh":
                        (isolated / source_name).chmod(0o755)
                    missing_cli = run_process(entry_command(label, isolated))
                    self.assertEqual(2, missing_cli.returncode)
                    self.assertEqual("", missing_cli.stdout)
                    self.assertEqual("VIBE_LAUNCHER_ERROR CLI_NOT_FOUND\n", missing_cli.stderr)
                    self.assertNotIn(str(isolated), missing_cli.stderr)

                with self.subTest(entry=label, failure="python-missing"):
                    isolated = Path(temp_dir) / (label.replace("/", "_") + "-python")
                    cli = isolated / ".ccgs-core/scripts/ccgs_cli.py"
                    cli.parent.mkdir(parents=True)
                    cli.write_text("raise SystemExit(0)\n", encoding="utf-8")
                    source_name = label.removeprefix("./")
                    shutil.copy2(ROOT / source_name, isolated / source_name)
                    if source_name == "ccgs.sh":
                        (isolated / source_name).chmod(0o755)
                    missing_python = run_process(
                        entry_command(label, isolated),
                        environment={"CCGS_PYTHON": str(isolated / "not-python")},
                    )
                    self.assertEqual(2, missing_python.returncode)
                    self.assertEqual("", missing_python.stdout)
                    self.assertEqual("VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND\n", missing_python.stderr)
                    self.assertNotIn(str(isolated), missing_python.stderr)

            for label in native_entry_labels():
                record_native_evidence(label, "failure-contracts")

    def test_ac4_unicode_paths_empty_values_and_non_utf8_output_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibe 空格 unicode ") as temp_dir:
            project = Path(temp_dir)
            write_manifest(project)
            write_manifest(project, "--")
            for arguments in (
                ["manifest-load", "--project-root", str(project), "--manifest-path="],
                ["manifest-load", "--project-root", str(project), "--manifest-path=--"],
            ):
                baseline = run_baseline(arguments)
                self.assertEqual(0, baseline.returncode, baseline.stderr)
                expected = json.loads(baseline.stdout)
                for label in native_entry_labels():
                    completed = run_entry(label, arguments)
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual(expected, json.loads(completed.stdout))

            result = execute_step(
                authorized_report([sys.executable, "-c", "import os; os.write(1, b'\\xffok')"]),
                "native-step",
                project,
                EXECUTION_POLICY,
            )
            self.assertTrue(result["ok"])
            self.assertEqual("\ufffdok", result["stdout"]["text"])
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.assertIn("\ufffdok".encode("utf-8"), encoded)

            write_manifest(
                project,
                "non-utf8.json",
                argv=(sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes([255])+b'ok')"),
            )
            arguments = [
                "workflow-execute", "--project-root", str(project),
                "--manifest-path", "non-utf8.json", "--step-id", "neutral-step",
                "--max-log-bytes", "2",
            ]
            baseline = run_baseline(arguments)
            self.assertEqual(0, baseline.returncode, baseline.stderr)
            expected = json.loads(baseline.stdout)
            self.assertEqual("\ufffdo", expected["stdout"]["text"])
            self.assertEqual(3, expected["stdout"]["byte_count"])
            self.assertTrue(expected["stdout"]["truncated"])
            for label in native_entry_labels():
                completed = run_entry(label, arguments)
                self.assertEqual(0, completed.returncode, completed.stderr)
                actual = json.loads(completed.stdout)
                self.assertEqual(normalized_execution(expected), normalized_execution(actual))
                record_native_evidence(label, "path-encoding")

    def test_ac5_dry_run_atomic_capability_and_process_tree_are_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibe-safe-") as temp_dir:
            project = Path(temp_dir)
            (project / "ccgs-data").mkdir()
            before = project_tree(project)
            plan = build_upgrade_plan(ROOT, project, "ccgs-data", "0.8.1")
            self.assertEqual(before, project_tree(project))

            result = apply_upgrade(
                ROOT,
                project,
                "ccgs-data",
                "0.8.1",
                str(plan["plan_id"]),
                lambda _project, _framework: {"summary": {"error": 0}},
            )
            if secure_atomic_replace_supported():
                self.assertEqual("applied", result["outcome"])
                self.assertTrue(result["written"])
                assert_schema(result, "upgrade-result.schema.json")
                after_hashes = {
                    item["path"]: hashlib.sha256((project / item["path"]).read_bytes()).hexdigest()
                    for item in plan["writes"]
                    if item["action"] != "unchanged"
                }
                self.assertEqual(
                    {
                        item["path"]: item["after_sha256"]
                        for item in plan["writes"]
                        if item["action"] != "unchanged"
                    },
                    after_hashes,
                )
                replay_plan = build_upgrade_plan(ROOT, project, "ccgs-data", "0.8.1")
                before_replay = project_tree(project)
                replay = apply_upgrade(
                    ROOT,
                    project,
                    "ccgs-data",
                    "0.8.1",
                    str(replay_plan["plan_id"]),
                    lambda _project, _framework: {"summary": {"error": 0}},
                )
                self.assertEqual("reused", replay["outcome"])
                self.assertTrue(replay["reused"])
                self.assertFalse(replay["written"])
                self.assertEqual(before_replay, project_tree(project))
                self.assertEqual(after_hashes, {
                    path: hashlib.sha256((project / path).read_bytes()).hexdigest()
                    for path in after_hashes
                })
            else:
                self.assertEqual("failed", result["outcome"])
                self.assertEqual("UPGRADE_WRITE_POLICY_DENIED", result["failures"][0]["code"])
                self.assertFalse(result["failures"][0]["retryable"])
                self.assertEqual(before, project_tree(project))

        with tempfile.TemporaryDirectory(prefix="vibe-rollback-") as temp_dir:
            project = Path(temp_dir)
            (project / "ccgs-data").mkdir()
            before = project_tree(project)
            plan = build_upgrade_plan(ROOT, project, "ccgs-data", "0.8.1")
            calls = 0

            def fail_mid_write(path: Path, content: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected write failure")
                atomic_replace_file(path, content, mode)

            if secure_atomic_replace_supported():
                rolled_back = apply_upgrade(
                    ROOT, project, "ccgs-data", "0.8.1", str(plan["plan_id"]),
                    lambda _project, _framework: {"summary": {"error": 0}},
                    replace_file=fail_mid_write,
                )
                self.assertEqual("UPGRADE_WRITE_FAILED", rolled_back["failures"][0]["code"])
                self.assertEqual("rolled-back", rolled_back["doctor"]["status"])
            self.assertEqual(before, project_tree(project))
            self.assertEqual([], list(project.rglob("*.tmp")))

        with tempfile.TemporaryDirectory(prefix="vibe-rollback-classification-") as temp_dir:
            project = Path(temp_dir)
            (project / "ccgs-data").mkdir()
            plan = build_upgrade_plan(ROOT, project, "ccgs-data", "0.8.1")
            calls = 0

            def break_rollback(path: Path, content: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    agents = project / ".agents"
                    if agents.exists():
                        agents.rename(project / ".agents-detached")
                    agents.write_text("rollback path changed\n", encoding="utf-8")
                    raise OSError("injected rollback boundary change")
                atomic_replace_file(path, content, mode)

            if secure_atomic_replace_supported():
                rollback_failed = apply_upgrade(
                    ROOT, project, "ccgs-data", "0.8.1", str(plan["plan_id"]),
                    lambda _project, _framework: {"summary": {"error": 0}},
                    replace_file=break_rollback,
                )
                self.assertEqual("UPGRADE_ROLLBACK_FAILED", rollback_failed["failures"][0]["code"])
                self.assertFalse(rollback_failed["failures"][0]["retryable"])

        with tempfile.TemporaryDirectory(prefix="vibe-process-") as temp_dir:
            project = Path(temp_dir)
            pid_file = project / "child.pid"
            script = (
                "import pathlib,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii');"
                "time.sleep(30)"
            )
            policy = dict(EXECUTION_POLICY, timeout_seconds=0.8)
            timed_out = execute_step(
                authorized_report([sys.executable, "-c", script, str(pid_file)]),
                "native-step",
                project,
                policy,
            )
            self.assertEqual("EXECUTION_TIMED_OUT", timed_out["error"]["code"])
            self.assertFalse(timed_out["retryable"])
            self.assertTrue(pid_file.is_file())
            child_pid = int(pid_file.read_text(encoding="ascii"))
            for _ in range(50):
                if not process_exists(child_pid):
                    break
                time.sleep(0.02)
            self.assertFalse(process_exists(child_pid), "controlled timeout left a child process alive")

            cancel_pid_file = project / "cancel-child.pid"

            def cancel_when_child_recorded() -> bool:
                try:
                    return int(cancel_pid_file.read_text(encoding="ascii")) > 0
                except (OSError, UnicodeError, ValueError):
                    return False

            cancelled_result = execute_step(
                authorized_report(
                    [sys.executable, "-c", script, str(cancel_pid_file)]
                ),
                "native-step",
                project,
                EXECUTION_POLICY,
                cancellation=cancel_when_child_recorded,
            )
            self.assertEqual("EXECUTION_CANCELLED", cancelled_result["error"]["code"])
            self.assertFalse(cancelled_result["retryable"])
            self.assertTrue(cancel_pid_file.is_file())
            cancel_child_pid = int(cancel_pid_file.read_text(encoding="ascii"))
            for _ in range(50):
                if not process_exists(cancel_child_pid):
                    break
                time.sleep(0.02)
            self.assertFalse(
                process_exists(cancel_child_pid),
                "controlled cancellation left a child process alive",
            )
        for label in native_entry_labels():
            record_native_evidence(label, "process-file-safety")

    def test_ac6_native_matrix_requires_every_declared_entry_without_skips(self) -> None:
        expected = native_entry_labels()
        if NATIVE_REQUIRED:
            self.assertEqual(
                current_os_label(),
                os.environ.get("CCGS_CROSS_PLATFORM_EXPECTED_OS"),
            )
            declared = tuple(
                item.strip()
                for item in os.environ.get(REQUIRED_ENTRIES_ENV, "").split(",")
                if item.strip()
            )
            self.assertEqual(expected, declared)
        self.assertGreaterEqual(sys.version_info, (3, 10))
        executed: list[str] = []
        for label in expected:
            discovery_path = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))
            completed = run_entry(
                label,
                ["--version"],
                force_python=False,
                environment={"PATH": discovery_path},
            )
            outcome = "pass" if completed.returncode == 0 else "fail"
            print(
                "NATIVE_EVIDENCE "
                f"os={current_os_label()} "
                f"python={platform.python_version()} "
                f"entry={label} scenario=launcher-smoke result={outcome}"
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertRegex(completed.stdout.strip(), r"^ccgs [0-9]+\.[0-9]+\.[0-9]+$")
            executed.append(label)
        self.assertEqual(list(expected), executed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
