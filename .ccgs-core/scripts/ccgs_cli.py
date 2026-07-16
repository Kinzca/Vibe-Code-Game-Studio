#!/usr/bin/env python3
"""Cross-platform CCGS command entrypoint with repository boundary checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ccgs_codex_bridge import (
    CodexBridgeError,
    apply_codex_plan,
    build_codex_plan,
    codex_target_paths,
    render_plan,
    verify_codex_plan,
)

from ccgs_context_pack import (
    DEFAULT_MAX_CHARS_PER_FILE,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_CHARS,
    ContextPackError,
    build_context_pack,
)

ALLURE_DIR = Path(__file__).resolve().parents[2] / "integrations" / "allure"
if str(ALLURE_DIR) not in sys.path:
    sys.path.insert(0, str(ALLURE_DIR))

from ccgs_allure_adapter import (
    AllureAdapterError,
    build_neutral_allure_bundle,
    preflight_neutral_allure_target,
    validate_neutral_allure_target_path,
    write_neutral_allure_bundle,
)
from ccgs_allure_port import (
    allure_capability_document,
    build_allure_reporting_adapter,
    build_allure_reporting_data,
)

QDRANT_DIR = Path(__file__).resolve().parents[2] / "integrations" / "qdrant"
if str(QDRANT_DIR) not in sys.path:
    sys.path.insert(0, str(QDRANT_DIR))

from ccgs_qdrant_adapter import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_COLLECTION,
    DEFAULT_MAX_CHARS,
    DEFAULT_MODEL,
    DEFAULT_OVERLAP,
    FastEmbedder,
    QdrantAdapterError,
    QdrantHttpStore,
    QdrantProtocolError,
    QdrantTransportError,
    QdrantUnsafeError,
    api_key_from_environment,
    build_index_plan,
    plan_report,
    sync_index,
    validate_collection_identifier,
    validate_identifier,
)
from ccgs_qdrant_port import (
    build_qdrant_retrieval_adapter,
    qdrant_capability_document,
)

LANGFUSE_DIR = Path(__file__).resolve().parents[2] / "integrations" / "langfuse"
if str(LANGFUSE_DIR) not in sys.path:
    sys.path.insert(0, str(LANGFUSE_DIR))

from ccgs_langfuse_adapter import (
    DEFAULT_HOST as DEFAULT_LANGFUSE_HOST,
    LangfuseAdapterError,
    LangfuseScoreClient,
    LangfuseTransportError,
    OtelLangfuseExporter,
    credentials_from_environment as langfuse_credentials,
    load_workflow_event,
    validate_host as validate_langfuse_host,
)
from ccgs_langfuse_port import (
    build_langfuse_observability_adapter,
    langfuse_capability_document,
)
from ccgs_workflow_observer import (
    WorkflowObserverError,
    build_workflow_event,
    event_relative_path,
    materialize_workflow_event,
    workflow_event_report,
)

from ccgs_story_workflow import (
    StoryWorkflowError,
    advance_report,
    apply_advance,
    apply_closeout,
    closeout_report,
    default_evidence_path,
    evidence_report,
    load_evidence,
    load_story,
)
from vibe_project_manifest import (
    DEFAULT_MANIFEST_PATH,
    MANIFEST_RETRIEVAL_UNSAFE,
    ManifestError,
    load_manifest,
)
from vibe_integration_ports import IntegrationPortContractError
from vibe_observability import build_observability_request, invoke_observability
from vibe_reporting import (
    build_reporting_request,
    invoke_reporting,
    project_evidence,
    project_normalized_results,
)
from vibe_retrieval import (
    build_retrieval_request,
    invoke_retrieval,
    resolve_allowed_sources,
)
from vibe_repository_boundary import (
    RepositoryBoundary,
    RepositoryBoundaryError,
    resolve_repository_boundary,
)
from vibe_upgrade import UpgradeError, apply_upgrade, build_upgrade_plan
from vibe_workflow_execute import execute_step
from vibe_workflow_plan import PlanCompileError, compile_plan
from vibe_workflow_preflight import PreflightError, preflight_plan


VERSION = "0.8.1"
DEFAULT_DATA_DIR = "ccgs-data"
MINIMUM_PYTHON = (3, 10)
ENTRY_FILES = {
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    DEFAULT_MANIFEST_PATH,
}


class StableArgumentParser(argparse.ArgumentParser):
    """Emit one sanitized machine contract for command-line usage failures."""

    def error(self, message: str) -> None:
        del message
        _print_json({
            "schema_version": "1.0",
            "ok": False,
            "error": {
                "code": "CLI_USAGE_ERROR",
                "message": "command-line arguments are invalid",
                "retryable": False,
                "details": {},
            },
        })
        raise SystemExit(2)


@dataclass(frozen=True)
class Check:
    """One stable, machine-readable doctor result."""

    key: str
    status: str
    message: str
    path: str = ""


class PolicyError(ValueError):
    """Raised when a proposed write crosses the CCGS project boundary."""

    def __init__(self, message: str, *, location: str = ".") -> None:
        super().__init__(message)
        self.location = location


def framework_root() -> Path:
    """Return the repository that owns this CLI implementation."""

    configured = os.environ.get("CCGS_FRAMEWORK_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[2]


def parse_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without executing shell content."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        match = re.match(r'^([A-Z_]+)="?([^"#]+)"?', line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def configured_data_dir(project: Path, framework: Path) -> str:
    """Prefer a consumer projection config, then use the framework default."""

    project_env = project / ".ccgs-core" / "ccgs.env"
    env_path = project_env if project_env.is_file() else framework / ".ccgs-core" / "ccgs.env"
    return parse_env(env_path).get("DATA_DIR", DEFAULT_DATA_DIR)


def git_toplevel(path: Path) -> Path | None:
    """Resolve the owning Git repository without changing it."""

    if not path.exists() or shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def repository_mode(project: Path, framework: Path) -> str:
    """Return the validated repository mode through the shared root contract."""

    return resolve_repository_boundary(project, framework).repository_mode


def exact_child_name(parent: Path, expected: str) -> str | None:
    """Return the on-disk spelling of a direct child, if present."""

    if not parent.is_dir():
        return None
    for child in parent.iterdir():
        if child.name.casefold() == expected.casefold():
            return child.name
    return None


def validate_write_target(project: Path, target: Path, data_dir: str) -> Path:
    """Validate a future write target against explicit project policy."""

    project = project.resolve()
    candidate = target if target.is_absolute() else project / target
    candidate = candidate.resolve(strict=False)
    requested_location = target.as_posix() if not target.is_absolute() else "<external>"
    try:
        relative = candidate.relative_to(project)
    except ValueError as exc:
        raise PolicyError(
            "target escapes the explicit project root",
            location=requested_location,
        ) from exc

    if not relative.parts:
        raise PolicyError(
            "project root itself is not a valid write target",
            location=".",
        )

    first = relative.parts[0]
    if first.casefold() == data_dir.casefold() or first == ".agents":
        return candidate
    if relative.as_posix() in ENTRY_FILES:
        return candidate
    raise PolicyError(
        "target is outside CCGS-owned project paths "
        f"({data_dir}, .agents, generated entry files, or the workflow manifest)",
        location=relative.as_posix(),
    )


def validate_context_output(project: Path, target: Path, data_dir: str) -> Path:
    """Restrict Context Pack writes to Markdown under production/context."""

    candidate = validate_write_target(project, target, data_dir)
    context_root = (project.resolve() / data_dir / "production" / "context").resolve()
    try:
        candidate.relative_to(context_root)
    except ValueError as exc:
        raise PolicyError("Context Pack output must stay under the configured production/context directory") from exc
    if candidate.suffix.casefold() != ".md":
        raise PolicyError("Context Pack output must use the .md extension")
    return candidate


def atomic_write_text(target: Path, content: str) -> None:
    """Atomically replace one UTF-8 text artifact in its destination directory."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
        temporary_path.replace(target)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def utf8_check(path: Path, key: str, public_path: str) -> Check:
    """Verify that a framework text file decodes as strict UTF-8."""

    if not path.is_file():
        return Check(key, "error", "required file is missing", public_path)
    try:
        path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return Check(key, "error", f"invalid UTF-8: {exc}", public_path)
    return Check(key, "pass", "valid UTF-8", public_path)


def build_doctor_report(
    project: Path,
    framework: Path | None = None,
) -> dict[str, object]:
    """Inspect framework and consumer roots without writing any files."""

    boundary = resolve_repository_boundary(project, framework or framework_root())
    framework = boundary.framework_root
    project = boundary.project_root
    data_dir = configured_data_dir(project, framework)
    framework_git = git_toplevel(framework)
    project_git = git_toplevel(project)
    checks: list[Check] = []

    checks.append(
        Check(
            "runtime.python",
            "pass" if sys.version_info >= MINIMUM_PYTHON else "error",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "<system>",
        )
    )
    git_executable = shutil.which("git")
    checks.append(
        Check(
            "runtime.git",
            "pass" if git_executable else "error",
            "Git executable found" if git_executable else "Git executable not found",
            "<system>" if git_executable else "",
        )
    )
    checks.append(
        Check(
            "framework.core",
            "pass" if (framework / ".ccgs-core").is_dir() else "error",
            "framework core directory found",
            boundary.public_path(framework / ".ccgs-core"),
        )
    )
    checks.append(
        Check(
            "framework.git",
            "pass" if framework_git else "error",
            "framework is owned by an independent Git repository"
            if framework_git
            else "framework Git repository not found",
            boundary.public_path(framework_git) if framework_git else "",
        )
    )
    checks.append(
        Check(
            "project.root",
            "pass" if project.is_dir() else "error",
            "consumer project root found" if project.is_dir() else "consumer project root is missing",
            boundary.project.location,
        )
    )
    checks.append(
        Check(
            "policy.write_scope",
            "pass",
            "engine-agnostic allowlist protects every non-CCGS project path",
            f"{data_dir}, .agents, generated entry files",
        )
    )

    if project.is_dir():
        actual_data_name = exact_child_name(project, data_dir)
        if actual_data_name == data_dir:
            checks.append(
                Check(
                    "project.data",
                    "pass",
                    f"data directory found as {data_dir}",
                    boundary.public_path(project / data_dir),
                )
            )
        elif actual_data_name:
            checks.append(
                Check(
                    "project.data",
                    "error",
                    f"data directory case mismatch: configured {data_dir}, found {actual_data_name}",
                    boundary.public_path(project / actual_data_name),
                )
            )
        else:
            checks.append(
                Check(
                    "project.data",
                    "warn",
                    f"data directory {data_dir} is not initialized",
                    boundary.public_path(project / data_dir),
                )
            )

    if boundary.repository_mode == "embedded-submodule":
        isolated = framework_git is not None and project_git is not None and framework_git != project_git
        checks.append(
            Check(
                "boundary.git",
                "pass" if isolated else "error",
                "embedded-submodule framework has an independent Git boundary"
                if isolated
                else "embedded-submodule framework resolves to the consumer Git repository",
                boundary.public_path(framework_git or framework),
            )
        )
    else:
        checks.append(
            Check(
                "boundary.git",
                "pass",
                f"repository mode: {boundary.repository_mode}",
                boundary.framework.location,
            )
        )

    checks.extend(
        [
            utf8_check(
                framework / ".ccgs-core" / "ccgs.env",
                "encoding.env",
                boundary.public_path(framework / ".ccgs-core" / "ccgs.env"),
            ),
            utf8_check(
                framework / "README.md",
                "encoding.readme",
                boundary.public_path(framework / "README.md"),
            ),
            utf8_check(
                framework / ".ccgs-core" / "scripts" / "workflow" / "ccgs-context-router.py",
                "encoding.router",
                boundary.public_path(
                    framework
                    / ".ccgs-core"
                    / "scripts"
                    / "workflow"
                    / "ccgs-context-router.py"
                ),
            ),
        ]
    )

    for relative in (
        "ccgs.workflow.yaml",
        "ccgs.deps.lock",
        "ccgs.cmd",
        "ccgs.ps1",
        "ccgs.sh",
    ):
        path = framework / relative
        checks.append(
            Check(
                f"framework.{relative}",
                "pass" if path.is_file() else "error",
                "required batch 0/1 artifact found" if path.is_file() else "required batch 0/1 artifact missing",
                boundary.public_path(path),
            )
        )

    summary = {
        status: sum(1 for check in checks if check.status == status)
        for status in ("pass", "warn", "error", "info")
    }
    report = {
        "schema_version": "1.0",
        "cli_version": VERSION,
        "framework_root": boundary.framework.location,
        "project_root": boundary.project.location,
        "framework_git_root": (
            boundary.public_path(framework_git) if framework_git else ""
        ),
        "project_git_root": (
            boundary.public_path(project_git) if project_git else ""
        ),
        "data_dir": data_dir,
        "read_only": True,
        "write_policy": "allowlist",
        "engine_agnostic": True,
        "summary": summary,
        "checks": [asdict(check) for check in checks],
    }
    report.update(boundary.public_result())
    return report


def print_doctor(report: dict[str, object]) -> None:
    """Render a compact human-readable diagnostic report."""

    print("CCGS Doctor")
    print(f"- CLI: {report['cli_version']}")
    print(f"- Framework: {report['framework_root']}")
    print(f"- Project: {report['project_root']}")
    print(f"- Mode: {report['repository_mode']}")
    print(f"- Data: {report['data_dir']}")
    print()
    for check in report["checks"]:
        print(f"[{check['status'].upper()}] {check['key']}: {check['message']}")
    summary = report["summary"]
    print()
    print(f"Summary: {summary['pass']} pass, {summary['warn']} warn, {summary['error']} error")


def command_doctor(args: argparse.Namespace) -> int:
    """Run the read-only repository and environment diagnosis."""

    project = Path(args.project_root) if args.project_root else Path.cwd()
    try:
        report = build_doctor_report(project)
    except RepositoryBoundaryError as exc:
        report = exc.report("diagnostic")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"Doctor: {exc.code} - {exc.message} ({exc.location})")
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_doctor(report)
    return 1 if report["summary"]["error"] else 0


def command_policy(args: argparse.Namespace) -> int:
    """Check one prospective write target without changing the project."""

    if hasattr(args, "repository_boundary"):
        boundary = args.repository_boundary
    else:
        boundary = resolve_repository_boundary(
            Path(args.project_root),
            framework_root(),
        )
    project = boundary.project_root
    data_dir = configured_data_dir(project, boundary.framework_root)
    try:
        target = validate_write_target(project, Path(args.target), data_dir)
        result = {
            "allowed": True,
            "project_root": boundary.project.location,
            "target": boundary.public_path(target),
            "reason": "allowed CCGS-owned path",
        }
        exit_code = 0
    except PolicyError as exc:
        result = {
            "allowed": False,
            "project_root": boundary.project.location,
            "target": exc.location,
            "reason": str(exc),
        }
        exit_code = 1
    result.update(boundary.public_result())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(("ALLOW" if result["allowed"] else "DENY") + f": {result['target']} - {result['reason']}")
    return exit_code


def command_context_pack(args: argparse.Namespace) -> int:
    """Preview or persist one bounded Story Context Pack."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    if args.output and not (args.write or args.dry_run):
        print("context-pack: --output requires --write or --dry-run", file=sys.stderr)
        return 2

    try:
        pack = build_context_pack(
            project,
            args.story,
            data_dir,
            max_files=args.max_files,
            max_chars_per_file=args.max_chars_per_file,
            max_total_chars=args.max_total_chars,
        )
        requested_output = Path(args.output or pack.output_path)
        target = None
        if args.write or args.dry_run:
            target = validate_context_output(project, requested_output, data_dir)
    except (ContextPackError, PolicyError) as exc:
        print(f"context-pack: {exc}", file=sys.stderr)
        return 2

    if pack.missing_references:
        if not args.write:
            print(pack.markdown, end="")
        print(
            "context-pack: explicit references are missing; refusing to write",
            file=sys.stderr,
        )
        return 1

    if args.write:
        assert target is not None
        atomic_write_text(target, pack.markdown)
        print(target.relative_to(project).as_posix())
    else:
        print(pack.markdown, end="")
    return 0


def command_bootstrap(args: argparse.Namespace) -> int:
    """Plan or apply one project-local AI bridge."""

    mode = "write" if args.write else "dry-run"
    boundary: RepositoryBoundary | None = None
    try:
        boundary = resolve_repository_boundary(
            Path(args.project_root),
            framework_root(),
        )
        project = boundary.project_root
        data_dir = configured_data_dir(project, boundary.framework_root)
        for relative in codex_target_paths():
            validate_write_target(project, Path(relative), data_dir)
        plan = build_codex_plan(boundary.framework_root, project, data_dir)
        written = False
        if args.write:
            apply_codex_plan(project, plan, atomic_write_text)
            verify_codex_plan(project, plan)
            written = any(item.action != "unchanged" for item in plan.files)
    except RepositoryBoundaryError as exc:
        report = exc.report(mode)
        if args.json:
            _print_json(report)
        print(
            f"bootstrap: {exc.code}: {exc.message} ({exc.location})",
            file=sys.stderr,
        )
        return 2
    except (CodexBridgeError, PolicyError, OSError) as exc:
        if isinstance(exc, CodexBridgeError):
            code = exc.code
            message = exc.message
            location = exc.location
        elif isinstance(exc, PolicyError):
            code = "WRITE_POLICY_DENIED"
            message = str(exc)
            location = exc.location
        else:
            code = "WRITE_IO_ERROR"
            message = "project file operation failed"
            location = "."
        report = {
            "schema_version": "1.0",
            "mode": mode,
            "written": False,
            "validation": {"valid": False, "error_code": code},
            "planned_writes": [],
            "error": {
                "code": code,
                "message": message,
                "location": location,
            },
        }
        if boundary is not None:
            report.update(boundary.public_result())
        if args.json:
            _print_json(report)
        print(f"bootstrap: {code}: {message} ({location})", file=sys.stderr)
        return 2

    report = plan.manifest(mode)
    report.update(boundary.public_result())
    report["validation"] = {"valid": True, "error_code": None}
    report["planned_writes"] = list(report["files"])
    report["written"] = written
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_plan(plan, mode), end="")
    return 0


def _print_json(payload: dict[str, object]) -> None:
    """Emit stable machine-readable workflow output."""

    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_story_advance(args: argparse.Namespace) -> int:
    """Preview or atomically apply one Story state transition."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    try:
        story_path, story = load_story(project, args.story, data_dir)
        validate_write_target(project, story_path, data_dir)
        report = advance_report(story, args.to, args.reason)
        report["mode"] = "write" if args.write else "dry-run"
        report["written"] = False
        if args.write and report["allowed"]:
            report["written"] = apply_advance(
                story_path, story, report, atomic_write_text
            )
    except (StoryWorkflowError, PolicyError, OSError) as exc:
        print(f"story-advance: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0 if report["allowed"] else 1


def command_evidence_validate(args: argparse.Namespace) -> int:
    """Validate one Evidence JSON document without writing."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    try:
        relative, document, errors = load_evidence(
            project, args.evidence, data_dir
        )
    except (StoryWorkflowError, OSError) as exc:
        print(f"evidence-validate: {exc}", file=sys.stderr)
        return 2
    report = evidence_report(relative, document, errors)
    _print_json(report)
    return 0 if report["valid"] else 1


def command_closeout(args: argparse.Namespace) -> int:
    """Evaluate evidence, advance passing Stories, and write stable failures."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    try:
        story_path, story = load_story(project, args.story, data_dir)
        validate_write_target(project, story_path, data_dir)
        evidence_path = args.evidence or default_evidence_path(
            data_dir, story.relative_path
        )
        try:
            evidence_relative, evidence, errors = load_evidence(
                project, evidence_path, data_dir
            )
        except StoryWorkflowError as exc:
            message = str(exc)
            if "must stay under" in message or "must use the .json extension" in message:
                raise
            evidence_relative = Path(evidence_path).as_posix()
            evidence = {}
            errors = [{"path": "$", "message": message}]
        report = closeout_report(
            story, evidence_relative, evidence, errors
        )
        report["mode"] = "write" if args.write else "dry-run"
        report["written"] = False
        if args.write:
            report["written"] = apply_closeout(
                story_path, story, report, atomic_write_text
            )
    except (StoryWorkflowError, PolicyError, OSError) as exc:
        print(f"closeout: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0 if report["verdict"] == "pass" else 1


def _report_source(project: Path, data_dir: str, raw: str, subdir: str) -> tuple[Path, str]:
    """Resolve one declared reporting input below the configured QA root."""

    root = (project / data_dir / "production" / "qa" / subdir).resolve()
    candidate = (project / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID") from exc
    if not candidate.is_file() or candidate.stat().st_size > 10_000_000:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID")
    return candidate, candidate.relative_to(project).as_posix()


def _junit_reporting_document(path: Path, source_ref: str) -> dict[str, Any]:
    """Project JUnit structure while intentionally discarding all free text."""

    try:
        root = ET.fromstring(path.read_bytes())
    except (ET.ParseError, OSError) as exc:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID")
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    tests: list[dict[str, Any]] = []
    for suite_index, suite in enumerate(suites):
        suite_name = suite.attrib.get("name", "JUnit")
        for case_index, case in enumerate(suite.findall("./testcase")):
            name = case.attrib.get("name", f"test-{case_index + 1}")
            identity_source = ":".join((source_ref, str(suite_index), str(case_index), name))
            failure = case.find("./failure")
            error = case.find("./error")
            skipped = case.find("./skipped")
            status = "failed" if failure is not None else "broken" if error is not None else "skipped" if skipped is not None else "passed"
            try:
                duration_seconds = float(case.attrib.get("time", "0"))
                if not math.isfinite(duration_seconds) or duration_seconds < 0:
                    raise ValueError("JUnit duration must be finite and non-negative")
                duration_ms = int(round(duration_seconds * 1000))
            except (TypeError, ValueError, OverflowError):
                raise IntegrationPortContractError("PORT_REQUEST_INVALID")
            item: dict[str, Any] = {
                "id": f"junit-{hashlib.sha256(identity_source.encode('utf-8')).hexdigest()[:32]}",
                "name": name,
                "suite": suite_name,
                "status": status,
                "duration_ms": duration_ms,
            }
            if status in {"failed", "broken"}:
                item["failure_code"] = "JUNIT_FAILURE" if status == "failed" else "JUNIT_ERROR"
            tests.append(item)
    if not tests:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID")
    return {"schema_version": "1.0", "tests": tests}


def _load_reporting_inputs(
    project: Path, data_dir: str, result_refs: Sequence[str], evidence_ref: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load only declared standard result and Evidence files in the trusted core."""

    results: list[dict[str, Any]] = []
    for raw in result_refs:
        path, relative = _report_source(project, data_dir, raw, "test-results")
        if path.suffix.casefold() == ".json":
            try:
                document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
                raise IntegrationPortContractError("PORT_REQUEST_INVALID") from exc
        elif path.suffix.casefold() == ".xml":
            document = _junit_reporting_document(path, relative)
        else:
            raise IntegrationPortContractError("PORT_REQUEST_INVALID")
        results.extend(project_normalized_results(document, source_ref=relative))
    evidence_path, evidence_relative = _report_source(
        project, data_dir, evidence_ref, "evidence"
    )
    try:
        evidence_document = json.loads(
            evidence_path.read_text(encoding="utf-8", errors="strict")
        )
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise IntegrationPortContractError("PORT_REQUEST_INVALID") from exc
    return results, project_evidence(evidence_document, source_ref=evidence_relative)


def _reporting_cli_error(project_id: str, code: str) -> dict[str, Any]:
    """Return a bounded pre-adapter Reporting Port rejection."""

    safe_project = (
        project_id
        if type(project_id) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", project_id)
        else "invalid"
    )
    request = {
        "request_id": "cli-report-request", "project_id": safe_project,
    }
    return invoke_reporting(
        request, allure_capability_document(), None,
        data_dir=DEFAULT_DATA_DIR, dry_run=True,
    ) if code == "PORT_REQUEST_INVALID" else {
        "contract_version": "1.0", "request_id": "cli-report-request",
        "project_id": safe_project, "port": "reporting", "operation": "export_report",
        "capability": "evidence_report", "ok": False, "status": "rejected",
        "action": "reject", "called": False, "data": {},
        "error": {"code": code, "message": "Reporting operation did not complete", "retryable": False, "details": {}},
    }


def command_report_export(args: argparse.Namespace) -> int:
    """Export declared tests and Evidence through the neutral Reporting Port."""

    project = Path(args.project_root).resolve()
    project_id = args.project_id
    try:
        if any((args.engine, args.environment, args.build_name, args.build_url, args.report_url)) or args.build_order is not None or args.start_ms:
            raise IntegrationPortContractError("PORT_REQUEST_INVALID")
        data_dir = configured_data_dir(project, framework_root())
        evidence_ref = args.evidence or (
            Path(data_dir) / "production" / "qa" / "evidence" / f"{Path(args.story).stem}.json"
        ).as_posix()
        results, evidence = _load_reporting_inputs(
            project, data_dir, args.test_result, evidence_ref,
        )
        request = build_reporting_request(
            results, evidence, data_dir=data_dir, report_id=args.report_id,
            request_id=args.request_id, project_id=project_id,
        )
        adapter = None
        dry_run_data = None
        if args.write:
            def writer(output_ref: str, bundle: Any) -> bool:
                requested = project / output_ref
                validate_neutral_allure_target_path(requested)
                target = validate_write_target(project, requested, data_dir)
                return write_neutral_allure_bundle(target, bundle)
            adapter = build_allure_reporting_adapter(writer)
        else:
            payload = request["payload"]
            requested = project / payload["output_ref"]
            validate_neutral_allure_target_path(requested)
            target = validate_write_target(project, requested, data_dir)
            bundle = build_neutral_allure_bundle(
                payload["report_id"], payload["results"], payload["evidence"],
            )
            try:
                reused = preflight_neutral_allure_target(target, bundle)
                failures: list[dict[str, Any]] = []
            except (AllureAdapterError, OSError):
                reused = False
                failures = [{
                    "code": "REPORT_OUTPUT_CONFLICT",
                    "message": "Report output conflicts with existing content",
                    "retryable": False,
                }]
            dry_run_data = build_allure_reporting_data(
                request, bundle, reused=reused, failures=failures,
            )
        report = invoke_reporting(
            request, allure_capability_document(), adapter,
            data_dir=data_dir, dry_run=not args.write, timeout_seconds=args.timeout_seconds,
            dry_run_data=dry_run_data,
        )
    except IntegrationPortContractError as exc:
        report = _reporting_cli_error(project_id, exc.code)
    except (AllureAdapterError, PolicyError, OSError):
        report = _reporting_cli_error(project_id, "PORT_REQUEST_INVALID")
    _print_json(report)
    if report.get("ok") and report.get("data", {}).get("outcome") == "generated":
        return 0
    error = report.get("error") if isinstance(report.get("error"), dict) else {}
    return 3 if error.get("retryable") else 2


command_allure_export = command_report_export


def _qdrant_store(args: argparse.Namespace) -> QdrantHttpStore:
    return QdrantHttpStore(
        args.qdrant_url,
        api_key=api_key_from_environment(args.api_key_env),
        timeout_seconds=args.timeout_seconds,
        allow_insecure_http=args.allow_insecure_http,
    )


def command_qdrant_index(args: argparse.Namespace) -> int:
    """Build or synchronize an index from manifest-declared source files."""

    project = Path(args.project_root).resolve()
    called = False
    try:
        if not project.is_dir():
            raise QdrantAdapterError("explicit project root is not a directory")
        collection = validate_collection_identifier(args.collection)
        manifest = load_manifest(project, framework_root(), args.manifest_path or None)
        sources = resolve_allowed_sources(project, manifest)
        plan = build_index_plan(
            sources,
            args.project_id,
            embedding_model=args.embedding_model,
            max_chars=args.max_chars,
            overlap=args.overlap,
        )
        sync = None
        if args.write:
            called = True
            sync = sync_index(
                plan,
                collection,
                _qdrant_store(args),
                FastEmbedder(args.embedding_model),
                batch_size=args.batch_size,
            )
        report = plan_report(
            plan,
            collection,
            "write" if args.write else "dry-run",
            sync,
        )
    except IntegrationPortContractError as exc:
        _print_json(_retrieval_cli_error(args.project_id, exc.code, called=False))
        return 2
    except ManifestError as exc:
        code = "PORT_PAYLOAD_UNSAFE" if exc.code == MANIFEST_RETRIEVAL_UNSAFE else "PORT_REQUEST_INVALID"
        _print_json(_retrieval_cli_error(args.project_id, code, called=False))
        return 2
    except QdrantUnsafeError:
        report = _retrieval_cli_error(args.project_id, "PORT_PAYLOAD_UNSAFE", called=called)
    except QdrantProtocolError:
        report = _retrieval_cli_error(args.project_id, "PORT_PROTOCOL_INVALID", called=called)
    except TimeoutError:
        report = _retrieval_cli_error(args.project_id, "PORT_ADAPTER_TIMEOUT", called=called)
    except QdrantTransportError:
        report = _retrieval_cli_error(args.project_id, "PORT_ADAPTER_UNAVAILABLE", called=called)
    except QdrantAdapterError:
        code = "PORT_ADAPTER_FAILED" if called else "PORT_REQUEST_INVALID"
        report = _retrieval_cli_error(args.project_id, code, called=called)
    except OSError:
        code = "PORT_ADAPTER_UNAVAILABLE" if called else "PORT_REQUEST_INVALID"
        report = _retrieval_cli_error(args.project_id, code, called=called)

    _print_json(report)
    if "error" not in report or report.get("ok"):
        return 0
    return 3 if report["error"]["retryable"] else 2


def command_qdrant_query(args: argparse.Namespace) -> int:
    """Validate or run one manifest-scoped semantic Retrieval Port request."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise QdrantAdapterError("explicit project root is not a directory")
        manifest = load_manifest(project, framework_root(), args.manifest_path or None)
        retrieval = manifest.get("retrieval")
        source_ids = args.source_id or [item["source_id"] for item in (retrieval or {}).get("sources", [])]
        request = build_retrieval_request(
            manifest, request_id=args.request_id, project_id=args.project_id,
            query=args.query, source_ids=source_ids, limit=args.limit,
            min_score=args.min_score,
        )
        adapter = None
        if args.write:
            collection = args.collection
            qdrant_url = args.qdrant_url
            embedding_model = args.embedding_model
            api_key_env = args.api_key_env
            allow_insecure_http = args.allow_insecure_http

            def adapter(value: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
                """Initialize optional remote dependencies inside the Port boundary."""

                remote = build_qdrant_retrieval_adapter(
                    collection,
                    QdrantHttpStore(
                        qdrant_url,
                        api_key=api_key_from_environment(api_key_env),
                        timeout_seconds=timeout_seconds,
                        allow_insecure_http=allow_insecure_http,
                    ),
                    FastEmbedder(embedding_model),
                )
                return remote(value, timeout_seconds)
        report = invoke_retrieval(
            request, manifest, qdrant_capability_document(), adapter,
            dry_run=not args.write, timeout_seconds=args.timeout_seconds,
        )
    except IntegrationPortContractError as exc:
        report = _retrieval_cli_error(args.project_id, exc.code, called=False)
    except ManifestError as exc:
        code = "PORT_PAYLOAD_UNSAFE" if exc.code == MANIFEST_RETRIEVAL_UNSAFE else "PORT_REQUEST_INVALID"
        report = _retrieval_cli_error(args.project_id, code, called=False)
    except (QdrantAdapterError, OSError) as exc:
        print(f"qdrant-query: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0 if report.get("ok") else 2


def _retrieval_cli_error(project_id: str, code: str, *, called: bool) -> dict[str, object]:
    """Build a bounded CLI failure without rejected values or machine paths."""

    safe_project = project_id if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", project_id) else "invalid"
    rejected = code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID",
        "PORT_PROTOCOL_INVALID", "PORT_PAYLOAD_UNSAFE",
    }
    retryable = code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}
    return {
        "contract_version": "1.0", "request_id": "cli-request", "project_id": safe_project,
        "port": "retrieval", "operation": "retrieve", "capability": "semantic_search",
        "ok": False,
        "status": "rejected" if rejected else "degraded",
        "action": "reject" if rejected else "degraded",
        "called": called,
        "data": {}, "error": {"code": code, "message": "Retrieval request did not complete",
                                  "retryable": retryable, "details": {}},
    }


def command_workflow_observe(args: argparse.Namespace) -> int:
    """Create or reuse one bounded workflow event under the CCGS data root."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise WorkflowObserverError("explicit project root is not a directory")
        data_dir = configured_data_dir(project, framework_root())
        relative = event_relative_path(data_dir, args.event_id)
        validate_write_target(project, project / relative, data_dir)
        document = build_workflow_event(
            project,
            data_dir,
            story_path=args.story,
            evidence_path=args.evidence,
            project_id=args.project_id,
            event_id=args.event_id,
            trace_key=args.trace_key,
            session_id=args.session_id or args.event_id,
            environment=args.environment,
            surface=args.surface,
            operation=args.operation,
            status=args.status,
            query=args.query,
            retrieval_references=args.retrieval_reference,
            failure_codes=args.failure_code,
            timestamp=args.timestamp,
            workflow_version=VERSION,
        )
        relative, written, document = materialize_workflow_event(
            project,
            data_dir,
            document,
            write=args.write,
            atomic_write=atomic_write_text,
        )
        report = workflow_event_report(
            relative,
            document,
            mode="write" if args.write else "dry-run",
            written=written,
        )
    except (WorkflowObserverError, LangfuseAdapterError, PolicyError, OSError) as exc:
        print(f"workflow-observe: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0

def command_langfuse_export(args: argparse.Namespace) -> int:
    """Validate or send one neutral observation through the public Port."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise LangfuseAdapterError("explicit project root is not a directory")
        data_dir = configured_data_dir(project, framework_root())
        event_path, _ = load_workflow_event(project, data_dir, args.event)
        event_document = json.loads(event_path.read_text(encoding="utf-8", errors="strict"))
        event_ref = event_path.relative_to(project).as_posix()
        request = build_observability_request(
            event_document,
            data_dir=data_dir,
            event_ref=event_ref,
            request_id=args.request_id,
            project_id=args.project_id or str(event_document.get("project_id", "")),
        )
        adapter = None
        if args.send:
            host = validate_langfuse_host(args.host, args.allow_insecure_http)
            public_key, secret_key = langfuse_credentials(
                args.public_key_env, args.secret_key_env
            )
            exporter = OtelLangfuseExporter(
                host,
                public_key,
                secret_key,
                timeout_seconds=args.timeout_seconds,
                allow_insecure_http=args.allow_insecure_http,
            )
            scores = LangfuseScoreClient(
                host,
                public_key,
                secret_key,
                timeout_seconds=args.timeout_seconds,
                allow_insecure_http=args.allow_insecure_http,
            )

            def export_trace(payload: dict[str, Any], timeout_seconds: float) -> bool:
                try:
                    return bool(exporter.export_neutral(payload).get("trace_sent"))
                except LangfuseTransportError as exc:
                    raise OSError("observability transport unavailable") from exc

            def send_metrics(payloads: Sequence[dict[str, Any]], timeout_seconds: float) -> int:
                try:
                    return scores.send_neutral_metrics(payloads)
                except LangfuseTransportError as exc:
                    raise OSError("observability transport unavailable") from exc

            adapter = build_langfuse_observability_adapter(export_trace, send_metrics)
        report = invoke_observability(
            request,
            langfuse_capability_document(),
            adapter,
            data_dir=data_dir,
            dry_run=not args.send,
            timeout_seconds=args.timeout_seconds,
        )
    except IntegrationPortContractError as exc:
        report = _observability_cli_error(args, exc.code)
    except (LangfuseAdapterError, json.JSONDecodeError, UnicodeError, OSError, ValueError):
        report = _observability_cli_error(args, "PORT_REQUEST_INVALID")

    _print_json(report)
    if report.get("ok"):
        return 0
    error = report.get("error") if isinstance(report.get("error"), dict) else {}
    return 3 if error.get("retryable") else 2


def _observability_cli_error(args: argparse.Namespace, code: str) -> dict[str, Any]:
    """Return a bounded failure without echoing rejected values or paths."""

    retryable = code in {"PORT_ADAPTER_UNAVAILABLE", "PORT_ADAPTER_TIMEOUT"}
    rejected = code in {
        "PORT_VERSION_UNSUPPORTED", "PORT_REQUEST_INVALID",
        "PORT_PROTOCOL_INVALID", "PORT_PAYLOAD_UNSAFE",
    }
    project_id = args.project_id if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.project_id or ""
    ) else "invalid"
    request_id = args.request_id if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.request_id or ""
    ) else "invalid"
    return {
        "contract_version": "1.0", "request_id": request_id,
        "project_id": project_id, "port": "observability",
        "operation": "export_trace", "capability": "workflow_trace",
        "ok": False, "status": "rejected" if rejected else "degraded",
        "action": "reject" if rejected else "degraded", "called": False,
        "data": {},
        "error": {"code": code, "message": "Observability request did not complete",
                  "retryable": retryable, "details": {}},
    }


def _command_project_manifest(args: argparse.Namespace, *, for_execution: bool) -> int:
    """Load a consumer manifest and emit its versioned machine result."""

    mode = "execution-request" if for_execution else "diagnostic"
    try:
        report = load_manifest(
            Path(args.project_root),
            framework_root(),
            args.manifest_path or None,
            for_execution=for_execution,
        )
    except ManifestError as exc:
        _print_json(exc.report(mode))
        return 1
    _print_json(report)
    return 0


def command_manifest_load(args: argparse.Namespace) -> int:
    """Diagnose a versioned consumer manifest; empty steps are allowed."""

    return _command_project_manifest(args, for_execution=False)


def command_workflow_request(args: argparse.Namespace) -> int:
    """Validate an execution request without compiling or running its steps."""

    return _command_project_manifest(args, for_execution=True)


def _load_compiled_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Load and compile one explicit project manifest without side effects."""

    manifest = load_manifest(
        Path(args.project_root),
        framework_root(),
        args.manifest_path or None,
        for_execution=True,
    )
    return compile_plan(manifest)


def command_workflow_plan(args: argparse.Namespace) -> int:
    """Compile and emit the deterministic public workflow plan."""

    try:
        report = _load_compiled_plan(args)
    except ManifestError as exc:
        report = exc.report("plan")
        _print_json(report)
        return 1
    except PlanCompileError as exc:
        report = exc.report()
        report["error"]["retryable"] = False
        _print_json(report)
        return 1
    _print_json(report)
    return 0


def _cancel_file_probe(project: Path, raw_path: str) -> Callable[[], bool] | None:
    """Build a read-only project-confined cancellation probe."""

    if not raw_path:
        return None
    requested = Path(raw_path)
    if requested.is_absolute():
        raise PreflightError(
            "PREFLIGHT_PATH_INVALID",
            "workflow path violates project policy",
            {"field": "cancel_file", "reason": "ABSOLUTE"},
        )
    candidate = (project / requested).resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise PreflightError(
            "PREFLIGHT_PATH_INVALID",
            "workflow path violates project policy",
            {"field": "cancel_file", "reason": "OUTSIDE_PROJECT"},
        ) from exc
    return candidate.is_file


def command_workflow_execute(args: argparse.Namespace) -> int:
    """Compile, preflight, and execute one explicitly selected workflow step."""

    project = Path(args.project_root).resolve()
    try:
        plan = _load_compiled_plan(args)
        preflight = preflight_plan(plan, project)
        cancellation = _cancel_file_probe(project, args.cancel_file)
    except ManifestError as exc:
        _print_json(exc.report("execute"))
        return 1
    except (PlanCompileError, PreflightError) as exc:
        report = exc.report()
        report["error"]["retryable"] = False
        _print_json(report)
        return 1

    report = execute_step(
        preflight,
        args.step_id,
        project,
        {
            "contract_version": "1.0",
            "timeout_seconds": args.timeout_seconds,
            "max_log_bytes": args.max_log_bytes,
            "termination_grace_seconds": args.termination_grace_seconds,
        },
        cancellation=cancellation,
    )
    _print_json(report)
    return 0 if report["ok"] else 1


def command_upgrade(args: argparse.Namespace) -> int:
    """Preview or explicitly apply one bounded managed-file upgrade."""

    project = Path(args.project_root).resolve()
    framework = framework_root()
    data_dir = configured_data_dir(project, framework)
    try:
        if args.apply:
            if not args.expected_plan_id:
                raise UpgradeError(
                    "UPGRADE_PLAN_STALE",
                    "--apply requires --expected-plan-id",
                )
            report = apply_upgrade(
                framework,
                project,
                data_dir,
                VERSION,
                args.expected_plan_id,
                build_doctor_report,
                validate_target=validate_write_target,
            )
            exit_code = 0 if report["outcome"] in {"applied", "reused"} else 1
        else:
            report = build_upgrade_plan(
                framework,
                project,
                data_dir,
                VERSION,
                validate_target=validate_write_target,
            )
            exit_code = 1 if report["conflicts"] else 0
    except UpgradeError as exc:
        if args.apply:
            report = {
                "contract_version": "1.0",
                "plan_id": args.expected_plan_id
                if re.fullmatch(r"[0-9a-f]{64}", args.expected_plan_id or "")
                else "0" * 64,
                "outcome": "failed",
                "written": False,
                "reused": False,
                "applied_writes": [],
                "doctor": {
                    "before_errors": 0,
                    "after_errors": 0,
                    "status": "failed",
                },
                "failures": [exc.public_error()],
            }
        else:
            report = {
                "contract_version": "1.0",
                "mode": "dry-run",
                "written": False,
                "error": exc.public_error(),
            }
        exit_code = 1
    _print_json(report)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    """Create the stable repository-safe CCGS CLI surface."""

    parser = StableArgumentParser(prog="ccgs", description="CCGS repository-safe workflow CLI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subcommands = parser.add_subparsers(
        dest="command", required=True, parser_class=StableArgumentParser
    )

    manifest_load = subcommands.add_parser(
        "manifest-load",
        help="Load and validate the consumer project's versioned workflow manifest.",
    )
    manifest_load.add_argument("--project-root", required=True, help="Explicit consumer project root.")
    manifest_load.add_argument(
        "--manifest-path",
        default="",
        help="Optional project-relative manifest path. Defaults to vibe-workflow.json.",
    )
    manifest_load.set_defaults(handler=command_manifest_load)

    workflow_request = subcommands.add_parser(
        "workflow-request",
        help="Validate a workflow execution request without starting processes.",
    )
    workflow_request.add_argument("--project-root", required=True, help="Explicit consumer project root.")
    workflow_request.add_argument(
        "--manifest-path",
        default="",
        help="Optional project-relative manifest path. Defaults to vibe-workflow.json.",
    )
    workflow_request.set_defaults(handler=command_workflow_request)

    workflow_plan = subcommands.add_parser(
        "workflow-plan",
        help="Compile the deterministic workflow plan without starting processes.",
    )
    workflow_plan.add_argument("--project-root", required=True)
    workflow_plan.add_argument("--manifest-path", default="")
    workflow_plan.set_defaults(handler=command_workflow_plan)

    workflow_execute = subcommands.add_parser(
        "workflow-execute",
        help="Execute one preflight-authorized workflow step.",
    )
    workflow_execute.add_argument("--project-root", required=True)
    workflow_execute.add_argument("--manifest-path", default="")
    workflow_execute.add_argument("--step-id", required=True)
    workflow_execute.add_argument("--timeout-seconds", type=float, default=30.0)
    workflow_execute.add_argument("--max-log-bytes", type=int, default=1_048_576)
    workflow_execute.add_argument("--termination-grace-seconds", type=float, default=1.0)
    workflow_execute.add_argument(
        "--cancel-file",
        default="",
        help="Optional project-relative marker whose presence requests cancellation.",
    )
    workflow_execute.set_defaults(handler=command_workflow_execute)

    upgrade = subcommands.add_parser(
        "upgrade",
        help="Preview or explicitly apply a versioned managed-file upgrade.",
    )
    upgrade.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    upgrade_mode = upgrade.add_mutually_exclusive_group(required=True)
    upgrade_mode.add_argument(
        "--dry-run", action="store_true", help="Return the deterministic Upgrade Plan without writing."
    )
    upgrade_mode.add_argument(
        "--apply", action="store_true", help="Apply only an explicitly authorized fresh plan."
    )
    upgrade.add_argument(
        "--expected-plan-id", default="", help="Required 64-character plan ID for --apply."
    )
    upgrade.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    upgrade.set_defaults(handler=command_upgrade)

    doctor = subcommands.add_parser("doctor", help="Inspect framework and consumer project boundaries.")
    doctor.add_argument("--project-root", default="", help="Consumer project to inspect. Defaults to current directory.")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    doctor.set_defaults(handler=command_doctor)

    policy = subcommands.add_parser("policy", help="Validate a prospective project write target.")
    policy.add_argument("--project-root", required=True, help="Explicit consumer project root.")
    policy.add_argument("--target", required=True, help="Prospective absolute or project-relative write target.")
    policy.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    policy.set_defaults(handler=command_policy)

    context_pack = subcommands.add_parser(
        "context-pack",
        help="Build a bounded Story Context Pack.",
    )
    context_pack.add_argument("--project-root", required=True, help="Explicit consumer project root.")
    context_pack.add_argument("--story", required=True, help="Story Markdown path inside production/epics.")
    mode = context_pack.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Persist the pack under production/context.")
    mode.add_argument("--dry-run", action="store_true", help="Validate the output target without writing.")
    context_pack.add_argument("--output", default="", help="Custom Markdown output under production/context.")
    context_pack.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"Maximum selected files. Default: {DEFAULT_MAX_FILES}.",
    )
    context_pack.add_argument(
        "--max-chars-per-file",
        type=int,
        default=DEFAULT_MAX_CHARS_PER_FILE,
        help=f"Maximum characters per source. Default: {DEFAULT_MAX_CHARS_PER_FILE}.",
    )
    context_pack.add_argument(
        "--max-total-chars",
        type=int,
        default=DEFAULT_MAX_TOTAL_CHARS,
        help=f"Maximum source characters in the pack. Default: {DEFAULT_MAX_TOTAL_CHARS}.",
    )
    context_pack.set_defaults(handler=command_context_pack)

    bootstrap = subcommands.add_parser(
        "bootstrap",
        help="Generate project-local AI bridge files from framework templates.",
    )
    bootstrap.add_argument("--project-root", required=True, help="Explicit consumer project root.")
    bootstrap.add_argument("--codex", action="store_true", required=True, help="Generate the Codex bridge.")
    bootstrap_mode = bootstrap.add_mutually_exclusive_group(required=True)
    bootstrap_mode.add_argument("--dry-run", action="store_true", help="Print the write manifest without changes.")
    bootstrap_mode.add_argument("--write", action="store_true", help="Atomically apply changed bridge files.")
    bootstrap.add_argument("--json", action="store_true", help="Emit a machine-readable write manifest.")
    bootstrap.set_defaults(handler=command_bootstrap)

    story_advance = subcommands.add_parser(
        "story-advance",
        help="Preview or apply one allowed Story state transition.",
    )
    story_advance.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    story_advance.add_argument(
        "--story", required=True, help="Story Markdown path inside production/epics."
    )
    story_advance.add_argument(
        "--to", required=True, help="Target Story state."
    )
    story_advance.add_argument(
        "--reason", default="", help="Stable reason recorded in the transition report."
    )
    advance_mode = story_advance.add_mutually_exclusive_group(required=True)
    advance_mode.add_argument(
        "--dry-run", action="store_true", help="Validate without changing the Story."
    )
    advance_mode.add_argument(
        "--write", action="store_true", help="Atomically update an allowed transition."
    )
    story_advance.set_defaults(handler=command_story_advance)

    evidence_validate = subcommands.add_parser(
        "evidence-validate",
        help="Validate machine-readable Story evidence.",
    )
    evidence_validate.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    evidence_validate.add_argument(
        "--evidence",
        required=True,
        help="Evidence JSON path inside production/qa/evidence.",
    )
    evidence_validate.set_defaults(handler=command_evidence_validate)

    closeout = subcommands.add_parser(
        "closeout",
        help="Check Story evidence and atomically persist the closeout result.",
    )
    closeout.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    closeout.add_argument(
        "--story", required=True, help="Story Markdown path inside production/epics."
    )
    closeout.add_argument(
        "--evidence",
        default="",
        help="Evidence JSON path. Defaults to the Story stem under production/qa/evidence.",
    )
    closeout_mode = closeout.add_mutually_exclusive_group(required=True)
    closeout_mode.add_argument(
        "--dry-run", action="store_true", help="Evaluate without changing the Story."
    )
    closeout_mode.add_argument(
        "--write",
        action="store_true",
        help="Atomically write done state or stable failure reasons.",
    )
    closeout.set_defaults(handler=command_closeout)

    def configure_report_export(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
        )
        parser.add_argument("--project-id", default="project", help="Stable project namespace.")
        parser.add_argument("--request-id", default="cli-report-request")
        parser.add_argument(
        "--story", required=True, help="Story Markdown path inside production/epics."
        )
        parser.add_argument(
        "--evidence",
        default="",
        help="Evidence JSON path. Defaults to the Story stem under production/qa/evidence.",
        )
        parser.add_argument(
        "--test-result",
        action="append",
        required=True,
        help="Normalized JSON or JUnit XML under production/qa/test-results. Repeatable.",
        )
        parser.add_argument("--report-id", "--run-id", dest="report_id", required=True)
        parser.add_argument("--timeout-seconds", type=float, default=30.0)
        parser.add_argument("--engine", default="", help="Deprecated vendor metadata; non-empty values are rejected.")
        parser.add_argument("--environment", default="", help="Deprecated vendor metadata; non-empty values are rejected.")
        parser.add_argument("--build-name", default="", help="Deprecated vendor metadata; non-empty values are rejected.")
        parser.add_argument("--build-url", default="", help="Deprecated vendor metadata; non-empty values are rejected.")
        parser.add_argument("--report-url", default="", help="Deprecated vendor metadata; non-empty values are rejected.")
        parser.add_argument("--build-order", type=int, default=None, help="Deprecated vendor metadata; values are rejected.")
        parser.add_argument("--start-ms", type=int, default=0, help="Deprecated vendor metadata; non-zero values are rejected.")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
        "--dry-run", action="store_true", help="Print the exact result manifest without writing."
        )
        mode.add_argument(
        "--write", action="store_true", help="Atomically create the immutable result directory."
        )
        parser.set_defaults(handler=command_report_export)

    report_export = subcommands.add_parser(
        "report-export", help="Export neutral test and Evidence data through the Reporting Port."
    )
    configure_report_export(report_export)
    allure_export = subcommands.add_parser(
        "allure-export", help="Compatibility alias for report-export."
    )
    configure_report_export(allure_export)

    qdrant_index = subcommands.add_parser(
        "qdrant-index",
        help="Plan or synchronize the incremental CCGS semantic index.",
    )
    qdrant_index.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    qdrant_index.add_argument(
        "--manifest-path", default="", help="Project-relative workflow manifest path."
    )
    qdrant_index.add_argument(
        "--project-id", required=True, help="Stable project namespace stored in payloads."
    )
    qdrant_index.add_argument(
        "--collection", default=DEFAULT_COLLECTION, help="Qdrant collection name."
    )
    qdrant_index.add_argument(
        "--qdrant-url", default="http://127.0.0.1:6333", help="Qdrant base URL."
    )
    qdrant_index.add_argument(
        "--embedding-model", default=DEFAULT_MODEL, help="FastEmbed model name."
    )
    qdrant_index.add_argument(
        "--api-key-env", default="QDRANT_API_KEY", help="Environment variable holding the API key."
    )
    qdrant_index.add_argument(
        "--allow-insecure-http", action="store_true", help="Allow HTTP for non-loopback Qdrant hosts."
    )
    qdrant_index.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Maximum characters per chunk."
    )
    qdrant_index.add_argument(
        "--overlap", type=int, default=DEFAULT_OVERLAP, help="Character overlap between split chunks."
    )
    qdrant_index.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Embedding and upsert batch size."
    )
    qdrant_index.add_argument(
        "--timeout-seconds", type=float, default=30.0, help="Qdrant request timeout."
    )
    qdrant_mode = qdrant_index.add_mutually_exclusive_group(required=True)
    qdrant_mode.add_argument(
        "--dry-run", action="store_true", help="Build a deterministic offline plan only."
    )
    qdrant_mode.add_argument(
        "--write", action="store_true", help="Embed and incrementally synchronize Qdrant."
    )
    qdrant_index.set_defaults(handler=command_qdrant_index)

    qdrant_query = subcommands.add_parser(
        "qdrant-query",
        help="Query the project-scoped CCGS semantic index.",
    )
    qdrant_query.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    qdrant_query.add_argument(
        "--manifest-path", default="", help="Project-relative workflow manifest path."
    )
    qdrant_query.add_argument("--project-id", required=True, help="Project namespace filter.")
    qdrant_query.add_argument("--request-id", default="cli-request", help="Stable request identity.")
    qdrant_query.add_argument("--query", required=True, help="Semantic search text.")
    qdrant_query.add_argument(
        "--source-id", action="append", default=[], help="Declared source ID; repeat to select several."
    )
    qdrant_query.add_argument(
        "--limit", type=int, default=10, help="Maximum matching chunks, from 1 to 50."
    )
    qdrant_query.add_argument(
        "--min-score", type=float, default=-1.0, help="Minimum finite score from -1 to 1."
    )
    qdrant_query.add_argument(
        "--collection", default=DEFAULT_COLLECTION, help="Qdrant collection name."
    )
    qdrant_query.add_argument(
        "--qdrant-url", default="http://127.0.0.1:6333", help="Qdrant base URL."
    )
    qdrant_query.add_argument(
        "--embedding-model", default=DEFAULT_MODEL, help="FastEmbed model name."
    )
    qdrant_query.add_argument(
        "--api-key-env", default="QDRANT_API_KEY", help="Environment variable holding the API key."
    )
    qdrant_query.add_argument(
        "--allow-insecure-http", action="store_true", help="Allow HTTP for non-loopback Qdrant hosts."
    )
    qdrant_query.add_argument(
        "--timeout-seconds", type=float, default=30.0, help="Qdrant request timeout."
    )
    qdrant_query_mode = qdrant_query.add_mutually_exclusive_group(required=True)
    qdrant_query_mode.add_argument("--dry-run", action="store_true", help="Validate without model or network access.")
    qdrant_query_mode.add_argument("--write", action="store_true", help="Invoke the configured Qdrant adapter once.")
    qdrant_query.set_defaults(handler=command_qdrant_query)

    workflow_observe = subcommands.add_parser(
        "workflow-observe",
        help="Create or reuse one bounded Langfuse workflow event.",
    )
    workflow_observe.add_argument("--project-root", required=True)
    workflow_observe.add_argument("--story", required=True)
    workflow_observe.add_argument("--evidence", required=True)
    workflow_observe.add_argument("--project-id", required=True)
    workflow_observe.add_argument("--event-id", required=True)
    workflow_observe.add_argument("--trace-key", required=True)
    workflow_observe.add_argument("--session-id", default="")
    workflow_observe.add_argument("--environment", default="automation")
    workflow_observe.add_argument("--surface", default="windmill")
    workflow_observe.add_argument("--operation", default="story-closeout")
    workflow_observe.add_argument(
        "--status",
        required=True,
        choices=("pass", "fail", "blocked", "error", "unknown", "passed", "failed"),
    )
    workflow_observe.add_argument("--query", default="")
    workflow_observe.add_argument(
        "--retrieval-reference", action="append", default=[]
    )
    workflow_observe.add_argument("--failure-code", action="append", default=[])
    workflow_observe.add_argument("--timestamp", default="")
    workflow_observe_mode = workflow_observe.add_mutually_exclusive_group(required=True)
    workflow_observe_mode.add_argument("--dry-run", action="store_true")
    workflow_observe_mode.add_argument("--write", action="store_true")
    workflow_observe.set_defaults(handler=command_workflow_observe)
    langfuse_export = subcommands.add_parser(
        "langfuse-export",
        help="Preview or send a bounded CCGS workflow observation.",
    )
    langfuse_export.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    langfuse_export.add_argument(
        "--event",
        required=True,
        help="Workflow Event JSON under production/observability/events.",
    )
    langfuse_export.add_argument(
        "--request-id",
        default="cli-observability-request",
        help="Stable Integration Port request identity.",
    )
    langfuse_export.add_argument(
        "--project-id",
        default="",
        help="Expected project namespace; defaults to the validated local event.",
    )
    langfuse_export.add_argument(
        "--host", default=DEFAULT_LANGFUSE_HOST, help="Langfuse base URL."
    )
    langfuse_export.add_argument(
        "--public-key-env",
        default="LANGFUSE_PUBLIC_KEY",
        help="Environment variable holding the Langfuse public key.",
    )
    langfuse_export.add_argument(
        "--secret-key-env",
        default="LANGFUSE_SECRET_KEY",
        help="Environment variable holding the Langfuse secret key.",
    )
    langfuse_export.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow HTTP for a non-loopback Langfuse host.",
    )
    langfuse_export.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Langfuse request timeout.",
    )
    langfuse_mode = langfuse_export.add_mutually_exclusive_group(required=True)
    langfuse_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the deterministic outbound preview only.",
    )
    langfuse_mode.add_argument(
        "--send",
        action="store_true",
        help="Send the OTLP span and explicit scores to Langfuse.",
    )
    langfuse_export.set_defaults(handler=command_langfuse_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and return a process exit code."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    if hasattr(args, "project_root"):
        requested_project = Path(args.project_root) if args.project_root else Path.cwd()
        try:
            boundary = resolve_repository_boundary(
                requested_project,
                framework_root(),
            )
        except RepositoryBoundaryError as exc:
            if getattr(args, "dry_run", False):
                mode = "dry-run"
            elif getattr(args, "write", False):
                mode = "write"
            elif getattr(args, "send", False):
                mode = "send"
            elif getattr(args, "apply", False):
                mode = "apply"
            elif args.command in {"workflow-request", "workflow-plan"}:
                mode = "execution-request"
            elif args.command == "workflow-execute":
                mode = "execute"
            else:
                mode = "diagnostic"
            machine_output = (
                getattr(args, "json", False)
                or args.command
                in {
                    "manifest-load",
                    "workflow-request",
                    "workflow-plan",
                    "workflow-execute",
                    "upgrade",
                    "story-advance",
                    "evidence-validate",
                    "closeout",
                    "allure-export",
                    "report-export",
                    "qdrant-index",
                    "qdrant-query",
                    "workflow-observe",
                    "langfuse-export",
                }
            )
            if machine_output:
                _print_json(exc.report(mode))
            print(
                f"{args.command}: {exc.code}: {exc.message} ({exc.location})",
                file=sys.stderr,
            )
            return 1
        args.project_root = str(boundary.project_root)
        args.repository_boundary = boundary
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
