#!/usr/bin/env python3
"""Convert CCGS automated test results and Closeout Evidence to Allure results."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ccgs_story_workflow import (
    Story,
    StoryWorkflowError,
    load_evidence,
    load_story,
)

SCHEMA_VERSION = "1.0"
ADAPTER_VERSION = "1.0"
MAX_INPUT_BYTES = 10_000_000
MAX_ATTACHMENT_CHARS = 1_000_000
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLURE_STATUS_ORDER = ("passed", "failed", "broken", "skipped", "unknown")
NORMALIZED_STATUS = {
    "pass": "passed",
    "passed": "passed",
    "fail": "failed",
    "failed": "failed",
    "error": "broken",
    "broken": "broken",
    "skip": "skipped",
    "skipped": "skipped",
    "deferred": "skipped",
    "blocked": "skipped",
    "unknown": "unknown",
}
NAMESPACE = uuid.UUID("c47fc951-1b3c-45db-aec1-94be0cf6b2a8")


class AllureAdapterError(ValueError):
    """Raised when an Allure export input or target violates the contract."""


@dataclass(frozen=True)
class TestRecord:
    """One framework-neutral automated test result."""

    identity: str
    name: str
    suite: str
    package: str
    status: str
    duration_ms: int
    start_ms: int
    message: str
    trace: str
    stdout: str
    stderr: str
    source: str
    framework: str


@dataclass(frozen=True)
class AllureBundle:
    """An immutable set of files for one Allure launch."""

    files: dict[str, bytes]
    summary: dict[str, Any]


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise AllureAdapterError(
            "run_id must be 1-64 characters using letters, digits, dot, underscore, or hyphen"
        )
    return run_id


def _resolve_qa_file(
    project: Path,
    raw_path: str,
    data_dir: str,
    subdirectory: str,
    extensions: set[str],
    label: str,
) -> tuple[Path, str]:
    project = project.resolve()
    candidate = Path(raw_path)
    candidate = candidate if candidate.is_absolute() else project / candidate
    candidate = candidate.resolve(strict=False)
    root = (project / data_dir / "production" / "qa" / subdirectory).resolve()
    try:
        candidate.relative_to(root)
        relative = candidate.relative_to(project).as_posix()
    except ValueError as exc:
        raise AllureAdapterError(
            f"{label} must stay under {(root.relative_to(project)).as_posix()}"
        ) from exc
    if not candidate.is_file():
        raise AllureAdapterError(f"{label} file not found: {relative}")
    if candidate.suffix.casefold() not in extensions:
        expected = ", ".join(sorted(extensions))
        raise AllureAdapterError(f"{label} must use one of: {expected}")
    if candidate.stat().st_size > MAX_INPUT_BYTES:
        raise AllureAdapterError(f"{label} exceeds the {MAX_INPUT_BYTES} byte limit")
    return candidate, relative


def resolve_output(
    project: Path, data_dir: str, run_id: str
) -> tuple[Path, str]:
    run_id = validate_run_id(run_id)
    project = project.resolve()
    target = (
        project
        / data_dir
        / "production"
        / "qa"
        / "allure-results"
        / run_id
    ).resolve(strict=False)
    root = (
        project / data_dir / "production" / "qa" / "allure-results"
    ).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AllureAdapterError("Allure output escaped the qa/allure-results root") from exc
    return target, target.relative_to(project).as_posix()


def _parse_timestamp(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1000)


def _duration_ms(value: Any) -> int:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, int(round(duration * 1000)))


def _limited(value: str | None) -> str:
    return (value or "")[:MAX_ATTACHMENT_CHARS]


def _junit_records(path: Path, relative: str) -> list[TestRecord]:
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError as exc:
        raise AllureAdapterError(f"invalid JUnit XML: {relative}: {exc}") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise AllureAdapterError(
            f"unsupported XML root {root.tag!r}; expected testsuite or testsuites"
        )
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    records: list[TestRecord] = []
    for suite in suites:
        suite_name = suite.attrib.get("name", "JUnit")
        suite_start = _parse_timestamp(suite.attrib.get("timestamp", ""))
        offset = 0
        for index, case in enumerate(suite.findall("./testcase")):
            name = case.attrib.get("name", f"test-{index + 1}")
            package = case.attrib.get("classname", suite_name)
            identity = f"{package}.{name}"
            duration = _duration_ms(case.attrib.get("time", "0"))
            failure = case.find("./failure")
            error = case.find("./error")
            skipped = case.find("./skipped")
            if failure is not None:
                status = "failed"
                detail = failure
            elif error is not None:
                status = "broken"
                detail = error
            elif skipped is not None:
                status = "skipped"
                detail = skipped
            else:
                status = "passed"
                detail = None
            message = detail.attrib.get("message", "") if detail is not None else ""
            trace = _limited(detail.text if detail is not None else "")
            stdout = _limited(
                (case.findtext("./system-out") or suite.findtext("./system-out"))
            )
            stderr = _limited(
                (case.findtext("./system-err") or suite.findtext("./system-err"))
            )
            start = suite_start + offset if suite_start else 0
            records.append(
                TestRecord(
                    identity=identity,
                    name=name,
                    suite=suite_name,
                    package=package,
                    status=status,
                    duration_ms=duration,
                    start_ms=start,
                    message=message,
                    trace=trace,
                    stdout=stdout,
                    stderr=stderr,
                    source=relative,
                    framework="junit",
                )
            )
            offset += duration
    if not records:
        raise AllureAdapterError(f"JUnit XML contains no test cases: {relative}")
    return records


def _validate_normalized(document: Any, relative: str) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        raise AllureAdapterError(f"normalized test result must be an object: {relative}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise AllureAdapterError(
            f"normalized test result schema_version must be {SCHEMA_VERSION}: {relative}"
        )
    tests = document.get("tests")
    if not isinstance(tests, list) or not tests:
        raise AllureAdapterError(
            f"normalized test result requires a non-empty tests array: {relative}"
        )
    allowed = {
        "id",
        "name",
        "suite",
        "package",
        "status",
        "duration_ms",
        "start_ms",
        "message",
        "trace",
        "stdout",
        "stderr",
    }
    for index, item in enumerate(tests):
        if not isinstance(item, dict):
            raise AllureAdapterError(f"tests[{index}] must be an object: {relative}")
        extras = sorted(set(item) - allowed)
        if extras:
            raise AllureAdapterError(
                f"tests[{index}] contains unsupported fields: {', '.join(extras)}"
            )
        for required in ("id", "name", "status"):
            if not isinstance(item.get(required), str) or not item[required].strip():
                raise AllureAdapterError(
                    f"tests[{index}].{required} must be a non-empty string"
                )
        status = item["status"].strip().casefold()
        if status not in NORMALIZED_STATUS:
            raise AllureAdapterError(
                f"tests[{index}].status is unsupported: {item['status']!r}"
            )
        for field in ("duration_ms", "start_ms"):
            value = item.get(field, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AllureAdapterError(
                    f"tests[{index}].{field} must be a non-negative integer"
                )
        for field in ("suite", "package", "message", "trace", "stdout", "stderr"):
            value = item.get(field, "")
            if not isinstance(value, str):
                raise AllureAdapterError(
                    f"tests[{index}].{field} must be a string"
                )
    return tests


def _normalized_records(path: Path, relative: str) -> list[TestRecord]:
    try:
        document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AllureAdapterError(
            f"invalid normalized test JSON: {relative}: {exc}"
        ) from exc
    tests = _validate_normalized(document, relative)
    return [
        TestRecord(
            identity=item["id"],
            name=item["name"],
            suite=item.get("suite", "CCGS Automated Tests"),
            package=item.get("package", "ccgs.automated"),
            status=NORMALIZED_STATUS[item["status"].strip().casefold()],
            duration_ms=item.get("duration_ms", 0),
            start_ms=item.get("start_ms", 0),
            message=item.get("message", ""),
            trace=_limited(item.get("trace", "")),
            stdout=_limited(item.get("stdout", "")),
            stderr=_limited(item.get("stderr", "")),
            source=relative,
            framework="ccgs-normalized",
        )
        for item in tests
    ]


def load_test_records(
    project: Path,
    data_dir: str,
    result_paths: Sequence[str],
) -> list[TestRecord]:
    if not result_paths:
        raise AllureAdapterError("at least one test_result is required")
    records: list[TestRecord] = []
    for raw_path in result_paths:
        path, relative = _resolve_qa_file(
            project,
            raw_path,
            data_dir,
            "test-results",
            {".json", ".xml"},
            "test result",
        )
        if path.suffix.casefold() == ".xml":
            records.extend(_junit_records(path, relative))
        else:
            records.extend(_normalized_records(path, relative))
    return records


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_uuid(run_id: str, identity: str, occurrence: int) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{run_id}:{identity}:{occurrence}"))


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _attachment(
    files: dict[str, bytes],
    result_uuid: str,
    key: str,
    name: str,
    content: str | bytes,
    extension: str,
    media_type: str,
) -> dict[str, str]:
    source_uuid = uuid.uuid5(NAMESPACE, f"{result_uuid}:attachment:{key}")
    source = f"{source_uuid}-attachment.{extension}"
    files[source] = content if isinstance(content, bytes) else content.encode("utf-8")
    return {"name": name, "source": source, "type": media_type}


def _labels(
    story: Story,
    suite: str,
    package: str,
    engine: str,
    environment: str,
    framework: str,
) -> list[dict[str, str]]:
    values = [
        ("epic", Path(story.relative_path).parent.name),
        ("feature", story.title),
        ("story", story.story_id),
        ("parentSuite", "CCGS"),
        ("suite", suite),
        ("package", package),
        ("framework", framework),
        ("language", "python"),
        ("engine", engine or "agnostic"),
        ("environment", environment or "unspecified"),
    ]
    return [{"name": name, "value": value} for name, value in values]


def _automated_result(
    record: TestRecord,
    story: Story,
    run_id: str,
    occurrence: int,
    engine: str,
    environment: str,
    files: dict[str, bytes],
) -> tuple[str, dict[str, Any]]:
    result_uuid = _run_uuid(run_id, record.identity, occurrence)
    attachments = []
    if record.stdout:
        attachments.append(
            _attachment(
                files,
                result_uuid,
                "stdout",
                "stdout",
                record.stdout,
                "txt",
                "text/plain",
            )
        )
    if record.stderr:
        attachments.append(
            _attachment(
                files,
                result_uuid,
                "stderr",
                "stderr",
                record.stderr,
                "txt",
                "text/plain",
            )
        )
    full_name = f"{record.package}.{record.name}"
    result = {
        "uuid": result_uuid,
        "historyId": _stable_id(f"history:{record.identity}"),
        "testCaseId": _stable_id(f"case:{record.identity}"),
        "fullName": full_name,
        "name": record.name,
        "description": f"Imported from {record.source}.",
        "links": [],
        "labels": _labels(
            story,
            record.suite,
            record.package,
            engine,
            environment,
            record.framework,
        ),
        "parameters": [
            {"name": "run_id", "value": run_id, "excluded": True},
            {"name": "source", "value": record.source, "excluded": True},
        ],
        "attachments": attachments,
        "status": record.status,
        "stage": "finished",
        "start": record.start_ms,
        "stop": record.start_ms + record.duration_ms,
        "steps": [],
    }
    if record.message or record.trace:
        result["statusDetails"] = {
            "known": False,
            "muted": False,
            "flaky": False,
            "message": record.message,
            "trace": record.trace,
        }
    return f"{result_uuid}-result.json", result


def _step(
    name: str,
    status: str,
    message: str,
    start_ms: int,
) -> dict[str, Any]:
    result = {
        "name": name,
        "status": status,
        "stage": "finished",
        "start": start_ms,
        "stop": start_ms,
        "steps": [],
        "attachments": [],
        "parameters": [],
    }
    if message:
        result["statusDetails"] = {"message": message, "trace": ""}
    return result


def _evidence_result(
    story: Story,
    evidence_relative: str,
    evidence: dict[str, Any],
    evidence_bytes: bytes,
    run_id: str,
    engine: str,
    environment: str,
    start_ms: int,
    files: dict[str, bytes],
) -> tuple[str, dict[str, Any]]:
    identity = f"ccgs.closeout.{story.story_id}"
    result_uuid = _run_uuid(run_id, identity, 0)
    status = NORMALIZED_STATUS.get(str(evidence.get("result", "")).casefold(), "unknown")
    steps = []
    for item in evidence.get("acceptance_criteria", []):
        steps.append(
            _step(
                f"{item.get('id', 'AC')}: Acceptance Criterion",
                NORMALIZED_STATUS.get(str(item.get("status", "")).casefold(), "unknown"),
                str(item.get("evidence", "")),
                start_ms,
            )
        )
    for item in evidence.get("checks", []):
        steps.append(
            _step(
                f"{item.get('id', 'check')}: {item.get('type', 'check')}",
                NORMALIZED_STATUS.get(str(item.get("status", "")).casefold(), "unknown"),
                str(item.get("summary", "")),
                start_ms,
            )
        )
    failed_steps = [
        item["name"] for item in steps if item["status"] in {"failed", "broken"}
    ]
    message = ""
    if status != "passed":
        message = "[CCGS Evidence] " + (
            ", ".join(failed_steps) if failed_steps else f"result is {evidence.get('result')}"
        )
    attachment = _attachment(
        files,
        result_uuid,
        "evidence",
        "CCGS Closeout Evidence",
        evidence_bytes,
        "json",
        "application/json",
    )
    result = {
        "uuid": result_uuid,
        "historyId": _stable_id(f"history:{identity}"),
        "testCaseId": _stable_id(f"case:{identity}"),
        "fullName": identity,
        "name": f"{story.story_id} Closeout Evidence",
        "description": (
            "CCGS acceptance criteria and checks used by the Closeout gate."
        ),
        "links": [],
        "labels": _labels(
            story,
            "Closeout Evidence",
            "ccgs.closeout",
            engine,
            environment,
            "ccgs-evidence",
        ),
        "parameters": [
            {"name": "run_id", "value": run_id, "excluded": True},
            {"name": "evidence", "value": evidence_relative, "excluded": True},
        ],
        "attachments": [attachment],
        "status": status,
        "stage": "finished",
        "start": start_ms,
        "stop": start_ms,
        "steps": steps,
    }
    if message:
        result["statusDetails"] = {
            "known": False,
            "muted": False,
            "flaky": False,
            "message": message,
            "trace": "",
        }
    return f"{result_uuid}-result.json", result


def _properties(values: dict[str, str]) -> bytes:
    def escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("=", "\\=")
            .replace(":", "\\:")
        )

    lines = [f"{escape(key)}={escape(value)}" for key, value in sorted(values.items())]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _categories() -> list[dict[str, Any]]:
    return [
        {
            "name": "CCGS Evidence failures",
            "messageRegex": ".*\\[CCGS Evidence\\].*",
            "matchedStatuses": ["failed", "broken"],
        },
        {
            "name": "Infrastructure problems",
            "messageRegex": ".*(timeout|infrastructure|configuration|protocol).*",
            "matchedStatuses": ["broken"],
        },
    ]


def build_allure_bundle(
    project: Path,
    data_dir: str,
    story_path: str,
    evidence_path: str,
    test_result_paths: Sequence[str],
    run_id: str,
    *,
    engine: str = "",
    environment: str = "",
    build_name: str = "",
    build_url: str = "",
    report_url: str = "",
    build_order: int | None = None,
    start_ms: int = 0,
) -> AllureBundle:
    """Build one deterministic Allure launch without writing it."""

    validate_run_id(run_id)
    try:
        _, story = load_story(project, story_path, data_dir)
        evidence_relative, evidence, evidence_errors = load_evidence(
            project, evidence_path, data_dir
        )
    except StoryWorkflowError as exc:
        raise AllureAdapterError(str(exc)) from exc
    if evidence_errors:
        messages = "; ".join(
            f"{item['path']}: {item['message']}" for item in evidence_errors
        )
        raise AllureAdapterError(f"Evidence is invalid: {messages}")
    evidence_file = project.resolve() / evidence_relative
    records = load_test_records(project, data_dir, test_result_paths)
    files: dict[str, bytes] = {}
    if start_ms < 0:
        raise AllureAdapterError("--start-ms must be zero or greater")

    statuses = {status: 0 for status in ALLURE_STATUS_ORDER}
    occurrences: dict[str, int] = {}
    for record in records:
        occurrence = occurrences.get(record.identity, 0)
        occurrences[record.identity] = occurrence + 1
        path, result = _automated_result(
            record,
            story,
            run_id,
            occurrence,
            engine,
            environment,
            files,
        )
        files[path] = _json_bytes(result)
        statuses[result["status"]] += 1

    evidence_start = start_ms
    if not evidence_start:
        starts = [record.start_ms for record in records if record.start_ms]
        evidence_start = min(starts) if starts else 0
    evidence_name, evidence_result = _evidence_result(
        story,
        evidence_relative,
        evidence,
        evidence_file.read_bytes(),
        run_id,
        engine,
        environment,
        evidence_start,
        files,
    )
    files[evidence_name] = _json_bytes(evidence_result)
    statuses[evidence_result["status"]] += 1

    files["categories.json"] = _json_bytes(_categories())
    files["environment.properties"] = _properties(
        {
            "CCGS Adapter": "allure",
            "CCGS Adapter Version": ADAPTER_VERSION,
            "CCGS Run ID": run_id,
            "CCGS Story ID": story.story_id,
            "Engine": engine or "agnostic",
            "Environment": environment or "unspecified",
        }
    )
    executor: dict[str, Any] = {
        "name": "CCGS",
        "buildName": build_name or run_id,
        "reportName": f"CCGS {story.story_id} - {run_id}",
    }
    if build_url:
        executor["buildUrl"] = build_url
    if report_url:
        executor["reportUrl"] = report_url
    if build_order is not None:
        if build_order < 0:
            raise AllureAdapterError("--build-order must be non-negative")
        executor["buildOrder"] = build_order
    files["executor.json"] = _json_bytes(executor)

    return AllureBundle(
        files=dict(sorted(files.items())),
        summary={
            "schema_version": SCHEMA_VERSION,
            "adapter": "allure",
            "adapter_version": ADAPTER_VERSION,
            "run_id": run_id,
            "story_id": story.story_id,
            "automated_tests": len(records),
            "evidence_results": 1,
            "total_results": len(records) + 1,
            "statuses": statuses,
            "sources": sorted({record.source for record in records}),
            "evidence": evidence_relative,
        },
    )


def bundle_manifest(
    bundle: AllureBundle,
    output_relative: str,
    mode: str,
    written: bool,
) -> dict[str, Any]:
    files = [
        {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in bundle.files.items()
    ]
    return {
        **bundle.summary,
        "mode": mode,
        "output": output_relative,
        "written": written,
        "files": files,
    }


def _directory_matches(target: Path, files: dict[str, bytes]) -> bool:
    existing = sorted(
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    )
    if existing != sorted(files):
        return False
    return all((target / relative).read_bytes() == content for relative, content in files.items())


def write_allure_bundle(target: Path, bundle: AllureBundle) -> bool:
    """Atomically create one immutable run directory."""

    target = target.resolve(strict=False)
    if target.exists():
        if target.is_dir() and _directory_matches(target, bundle.files):
            return False
        raise AllureAdapterError(
            "Allure output already exists with different content; use a unique run_id"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    )
    try:
        for relative, content in bundle.files.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return True