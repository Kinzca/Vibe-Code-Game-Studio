#!/usr/bin/env python3
"""Cross-platform CCGS command entrypoint with repository boundary checks."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


VERSION = "0.1.0"
DEFAULT_DATA_DIR = "ccgs-data"
MINIMUM_PYTHON = (3, 10)
ENTRY_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules"}
PROTECTED_PREFIXES = (("client", "assets"), ("server",))


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

    if project == framework:
        return "standalone"
    try:
        framework.relative_to(project)
        return "embedded"
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

    parts = tuple(part.casefold() for part in relative.parts)
    if not parts:
        raise PolicyError("project root itself is not a valid write target")
    for protected in PROTECTED_PREFIXES:
        if parts[: len(protected)] == protected:
            raise PolicyError(f"target is protected: {relative.as_posix()}")

    first = relative.parts[0]
    if first.casefold() == data_dir.casefold() or first == ".agents":
        return candidate
    if relative.as_posix() in ENTRY_FILES:
        return candidate
    raise PolicyError(
        "target is outside CCGS-owned project paths "
        f"({data_dir}, .agents, or generated entry files)"
    )


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

    if mode == "embedded":
        isolated = framework_git is not None and project_git is not None and framework_git != project_git
        checks.append(
            Check(
                "boundary.git",
                "pass" if isolated else "error",
                "embedded framework has an independent Git boundary"
                if isolated
                else "embedded framework resolves to the consumer Git repository",
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


def build_parser() -> argparse.ArgumentParser:
    """Create the stable batch 0/1 CLI surface."""

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and return a process exit code."""

    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
