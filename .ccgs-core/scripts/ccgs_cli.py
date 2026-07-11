#!/usr/bin/env python3
"""Cross-platform CCGS command entrypoint with repository boundary checks."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

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
    build_allure_bundle,
    bundle_manifest,
    resolve_output,
    write_allure_bundle,
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
    api_key_from_environment,
    build_index_plan,
    plan_report,
    query_index,
    sync_index,
    validate_identifier,
)

LANGFUSE_DIR = Path(__file__).resolve().parents[2] / "integrations" / "langfuse"
if str(LANGFUSE_DIR) not in sys.path:
    sys.path.insert(0, str(LANGFUSE_DIR))

from ccgs_langfuse_adapter import (
    DEFAULT_HOST as DEFAULT_LANGFUSE_HOST,
    LangfuseAdapterError,
    LangfuseScoreClient,
    OtelLangfuseExporter,
    build_langfuse_bundle,
    bundle_report as langfuse_bundle_report,
    credentials_from_environment as langfuse_credentials,
    load_workflow_event,
    send_bundle as send_langfuse_bundle,
    validate_host as validate_langfuse_host,
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


VERSION = "0.7.0"
DEFAULT_DATA_DIR = "ccgs-data"
MINIMUM_PYTHON = (3, 10)
ENTRY_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules"}


@dataclass(frozen=True)
class Check:
    """One stable, machine-readable doctor result."""

    key: str
    status: str
    message: str
    path: str = ""


class PolicyError(ValueError):
    """Raised when a proposed write crosses the CCGS project boundary."""


def framework_root() -> Path:
    """Return the repository that owns this CLI implementation."""

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
    """Classify standalone, embedded-submodule, or external framework use."""

    project = project.resolve()
    framework = framework.resolve()
    if project == framework:
        return "standalone"
    try:
        framework.relative_to(project)
        return "embedded-submodule"
    except ValueError:
        return "external"


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
    try:
        relative = candidate.relative_to(project)
    except ValueError as exc:
        raise PolicyError("target escapes the explicit project root") from exc

    if not relative.parts:
        raise PolicyError("project root itself is not a valid write target")

    first = relative.parts[0]
    if first.casefold() == data_dir.casefold() or first == ".agents":
        return candidate
    if relative.as_posix() in ENTRY_FILES:
        return candidate
    raise PolicyError(
        "target is outside CCGS-owned project paths "
        f"({data_dir}, .agents, or generated entry files)"
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


def utf8_check(path: Path, key: str) -> Check:
    """Verify that a framework text file decodes as strict UTF-8."""

    if not path.is_file():
        return Check(key, "error", "required file is missing", str(path))
    try:
        path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return Check(key, "error", f"invalid UTF-8: {exc}", str(path))
    return Check(key, "pass", "valid UTF-8", str(path))


def build_doctor_report(project: Path) -> dict[str, object]:
    """Inspect framework and consumer roots without writing any files."""

    framework = framework_root()
    project = project.resolve()
    data_dir = configured_data_dir(project, framework)
    mode = repository_mode(project, framework)
    framework_git = git_toplevel(framework)
    project_git = git_toplevel(project)
    checks: list[Check] = []

    checks.append(
        Check(
            "runtime.python",
            "pass" if sys.version_info >= MINIMUM_PYTHON else "error",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            sys.executable,
        )
    )
    git_executable = shutil.which("git")
    checks.append(
        Check(
            "runtime.git",
            "pass" if git_executable else "error",
            "Git executable found" if git_executable else "Git executable not found",
            git_executable or "",
        )
    )
    checks.append(
        Check(
            "framework.core",
            "pass" if (framework / ".ccgs-core").is_dir() else "error",
            "framework core directory found",
            str(framework / ".ccgs-core"),
        )
    )
    checks.append(
        Check(
            "framework.git",
            "pass" if framework_git else "error",
            "framework is owned by an independent Git repository"
            if framework_git
            else "framework Git repository not found",
            str(framework_git or ""),
        )
    )
    checks.append(
        Check(
            "project.root",
            "pass" if project.is_dir() else "error",
            "consumer project root found" if project.is_dir() else "consumer project root is missing",
            str(project),
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
                Check("project.data", "pass", f"data directory found as {data_dir}", str(project / data_dir))
            )
        elif actual_data_name:
            checks.append(
                Check(
                    "project.data",
                    "error",
                    f"data directory case mismatch: configured {data_dir}, found {actual_data_name}",
                    str(project / actual_data_name),
                )
            )
        else:
            checks.append(
                Check(
                    "project.data",
                    "warn",
                    f"data directory {data_dir} is not initialized",
                    str(project / data_dir),
                )
            )

    if mode == "embedded-submodule":
        isolated = framework_git is not None and project_git is not None and framework_git != project_git
        checks.append(
            Check(
                "boundary.git",
                "pass" if isolated else "error",
                "embedded-submodule framework has an independent Git boundary"
                if isolated
                else "embedded-submodule framework resolves to the consumer Git repository",
                str(framework_git or framework),
            )
        )
    else:
        checks.append(Check("boundary.git", "pass", f"repository mode: {mode}", str(framework)))

    checks.extend(
        [
            utf8_check(framework / ".ccgs-core" / "ccgs.env", "encoding.env"),
            utf8_check(framework / "README.md", "encoding.readme"),
            utf8_check(
                framework / ".ccgs-core" / "scripts" / "workflow" / "ccgs-context-router.py",
                "encoding.router",
            ),
        ]
    )

    for relative in ("ccgs.workflow.yaml", "ccgs.deps.lock", "ccgs.cmd", "ccgs.ps1"):
        path = framework / relative
        checks.append(
            Check(
                f"framework.{relative}",
                "pass" if path.is_file() else "error",
                "required batch 0/1 artifact found" if path.is_file() else "required batch 0/1 artifact missing",
                str(path),
            )
        )

    summary = {
        status: sum(1 for check in checks if check.status == status)
        for status in ("pass", "warn", "error", "info")
    }
    return {
        "schema_version": "1.0",
        "cli_version": VERSION,
        "framework_root": str(framework),
        "project_root": str(project),
        "framework_git_root": str(framework_git or ""),
        "project_git_root": str(project_git or ""),
        "repository_mode": mode,
        "data_dir": data_dir,
        "read_only": True,
        "write_policy": "allowlist",
        "engine_agnostic": True,
        "summary": summary,
        "checks": [asdict(check) for check in checks],
    }


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
    report = build_doctor_report(project)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_doctor(report)
    return 1 if report["summary"]["error"] else 0


def command_policy(args: argparse.Namespace) -> int:
    """Check one prospective write target without changing the project."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    try:
        target = validate_write_target(project, Path(args.target), data_dir)
        result = {
            "allowed": True,
            "project_root": str(project),
            "target": str(target),
            "reason": "allowed CCGS-owned path",
        }
        exit_code = 0
    except PolicyError as exc:
        result = {
            "allowed": False,
            "project_root": str(project),
            "target": args.target,
            "reason": str(exc),
        }
        exit_code = 1
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

    project = Path(args.project_root).resolve()
    if not project.is_dir():
        print("bootstrap: consumer project root is missing", file=sys.stderr)
        return 2

    data_dir = configured_data_dir(project, framework_root())
    try:
        for relative in codex_target_paths():
            validate_write_target(project, Path(relative), data_dir)
        plan = build_codex_plan(framework_root(), project, data_dir)
        mode = "write" if args.write else "dry-run"
        if args.write:
            apply_codex_plan(project, plan, atomic_write_text)
            verify_codex_plan(project, plan)
    except (CodexBridgeError, PolicyError, OSError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(plan.manifest(mode), ensure_ascii=False, indent=2))
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


def command_allure_export(args: argparse.Namespace) -> int:
    """Convert automated test results and Closeout Evidence into Allure files."""

    project = Path(args.project_root).resolve()
    data_dir = configured_data_dir(project, framework_root())
    try:
        story_path, story = load_story(project, args.story, data_dir)
        evidence_path = args.evidence or default_evidence_path(
            data_dir, story.relative_path
        )
        target, output_relative = resolve_output(project, data_dir, args.run_id)
        validate_write_target(project, target, data_dir)
        bundle = build_allure_bundle(
            project,
            data_dir,
            story_path.relative_to(project).as_posix(),
            evidence_path,
            args.test_result,
            args.run_id,
            engine=args.engine,
            environment=args.environment,
            build_name=args.build_name,
            build_url=args.build_url,
            report_url=args.report_url,
            build_order=args.build_order,
            start_ms=args.start_ms,
        )
        written = write_allure_bundle(target, bundle) if args.write else False
        report = bundle_manifest(
            bundle,
            output_relative,
            "write" if args.write else "dry-run",
            written,
        )
    except (AllureAdapterError, StoryWorkflowError, PolicyError, OSError) as exc:
        print(f"allure-export: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0


def _qdrant_store(args: argparse.Namespace) -> QdrantHttpStore:
    return QdrantHttpStore(
        args.qdrant_url,
        api_key=api_key_from_environment(args.api_key_env),
        timeout_seconds=args.timeout_seconds,
        allow_insecure_http=args.allow_insecure_http,
    )


def command_qdrant_index(args: argparse.Namespace) -> int:
    """Build a local plan or incrementally synchronize CCGS semantic points."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise QdrantAdapterError("explicit project root is not a directory")
        collection = validate_identifier(args.collection, "collection")
        data_dir = configured_data_dir(project, framework_root())
        plan = build_index_plan(
            project,
            data_dir,
            args.project_id,
            embedding_model=args.embedding_model,
            max_chars=args.max_chars,
            overlap=args.overlap,
        )
        sync = None
        if args.write:
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
    except (QdrantAdapterError, OSError) as exc:
        print(f"qdrant-index: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0


def command_qdrant_query(args: argparse.Namespace) -> int:
    """Run one project-scoped semantic query without changing Qdrant."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise QdrantAdapterError("explicit project root is not a directory")
        report = query_index(
            args.project_id,
            args.collection,
            args.query,
            args.limit,
            _qdrant_store(args),
            FastEmbedder(args.embedding_model),
        )
    except (QdrantAdapterError, OSError) as exc:
        print(f"qdrant-query: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0


def command_langfuse_export(args: argparse.Namespace) -> int:
    """Preview or send one privacy-bounded CCGS workflow observation."""

    project = Path(args.project_root).resolve()
    try:
        if not project.is_dir():
            raise LangfuseAdapterError("explicit project root is not a directory")
        data_dir = configured_data_dir(project, framework_root())
        _, event = load_workflow_event(project, data_dir, args.event)
        host = validate_langfuse_host(args.host, args.allow_insecure_http)
        bundle = build_langfuse_bundle(event)
        send_result = None
        if args.send:
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
            send_result = send_langfuse_bundle(bundle, exporter, scores)
        report = langfuse_bundle_report(
            bundle,
            host,
            "send" if args.send else "dry-run",
            send_result,
            allow_insecure_http=args.allow_insecure_http,
        )
    except (LangfuseAdapterError, OSError) as exc:
        print(f"langfuse-export: {exc}", file=sys.stderr)
        return 2

    _print_json(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the stable repository-safe CCGS CLI surface."""

    parser = argparse.ArgumentParser(prog="ccgs", description="CCGS repository-safe workflow CLI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subcommands = parser.add_subparsers(dest="command", required=True)

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

    allure_export = subcommands.add_parser(
        "allure-export",
        help="Export automated tests and Closeout Evidence as Allure results.",
    )
    allure_export.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
    )
    allure_export.add_argument(
        "--story", required=True, help="Story Markdown path inside production/epics."
    )
    allure_export.add_argument(
        "--evidence",
        default="",
        help="Evidence JSON path. Defaults to the Story stem under production/qa/evidence.",
    )
    allure_export.add_argument(
        "--test-result",
        action="append",
        required=True,
        help="Normalized JSON or JUnit XML under production/qa/test-results. Repeatable.",
    )
    allure_export.add_argument(
        "--run-id", required=True, help="Immutable output directory identifier."
    )
    allure_export.add_argument("--engine", default="", help="Optional engine label.")
    allure_export.add_argument(
        "--environment", default="", help="Optional environment label."
    )
    allure_export.add_argument("--build-name", default="", help="Executor build name.")
    allure_export.add_argument("--build-url", default="", help="Executor build URL.")
    allure_export.add_argument("--report-url", default="", help="Published report URL.")
    allure_export.add_argument(
        "--build-order", type=int, default=None, help="Optional non-negative build order."
    )
    allure_export.add_argument(
        "--start-ms", type=int, default=0, help="Evidence timestamp in Unix milliseconds."
    )
    allure_mode = allure_export.add_mutually_exclusive_group(required=True)
    allure_mode.add_argument(
        "--dry-run", action="store_true", help="Print the exact result manifest without writing."
    )
    allure_mode.add_argument(
        "--write", action="store_true", help="Atomically create the immutable result directory."
    )
    allure_export.set_defaults(handler=command_allure_export)

    qdrant_index = subcommands.add_parser(
        "qdrant-index",
        help="Plan or synchronize the incremental CCGS semantic index.",
    )
    qdrant_index.add_argument(
        "--project-root", required=True, help="Explicit consumer project root."
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
    qdrant_query.add_argument("--project-id", required=True, help="Project namespace filter.")
    qdrant_query.add_argument("--query", required=True, help="Semantic search text.")
    qdrant_query.add_argument(
        "--limit", type=int, default=10, help="Maximum matching chunks, from 1 to 50."
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
    qdrant_query.set_defaults(handler=command_qdrant_query)

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

    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
