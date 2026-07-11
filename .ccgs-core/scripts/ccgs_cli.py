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

from ccgs_context_pack import (
    DEFAULT_MAX_CHARS_PER_FILE,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_CHARS,
    ContextPackError,
    build_context_pack,
)


VERSION = "0.2.0"
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and return a process exit code."""

    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
