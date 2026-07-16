"""Versioned, engine-neutral framework and consumer repository boundaries."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


CONTRACT_VERSION = "1.0"
FRAMEWORK_MARKERS = (
    Path(".ccgs-core/scripts/ccgs_cli.py"),
    Path("ccgs.workflow.yaml"),
)


@dataclass(frozen=True)
class RootIdentity:
    """One public root identity without a machine-specific absolute path."""

    name: str
    location: str


@dataclass(frozen=True)
class RepositoryBoundary:
    """Validated private roots plus their stable public identities."""

    project_root: Path
    framework_root: Path
    repository_mode: str
    project: RootIdentity
    framework: RootIdentity
    contract_version: str = CONTRACT_VERSION

    def public_result(self) -> dict[str, object]:
        """Return the versioned machine contract with sanitized locations."""

        return _public_root_result(
            self.repository_mode,
            self.framework.location,
            contract_version=self.contract_version,
        )

    def public_path(self, path: Path, *, fallback: str = "<system>") -> str:
        """Render a path relative to one validated root, or a safe placeholder."""

        candidate = path.resolve(strict=False)
        project_relative = _relative_to(candidate, self.project_root)
        if project_relative is not None:
            return _join_public(self.project.location, project_relative)
        framework_relative = _relative_to(candidate, self.framework_root)
        if framework_relative is not None:
            return _join_public(self.framework.location, framework_relative)
        return fallback


class RepositoryBoundaryError(ValueError):
    """Stable root-validation failure suitable for public machine output."""

    def __init__(
        self,
        code: str,
        message: str,
        location: str,
        *,
        framework_location: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location
        self.framework_location = framework_location

    def report(self, mode: str) -> dict[str, object]:
        """Return one sanitized failure report shared by preview and apply modes."""

        report = {
            "schema_version": CONTRACT_VERSION,
            "mode": mode,
            "written": False,
            "validation": {
                "valid": False,
                "error_code": self.code,
            },
            "planned_writes": [],
            "error": {
                "code": self.code,
                "message": self.message,
                "location": self.location,
                "retryable": False,
            },
        }
        report.update(_public_root_result("invalid", self.framework_location))
        return report


def _public_root_result(
    repository_mode: str,
    framework_location: str,
    *,
    contract_version: str = CONTRACT_VERSION,
) -> dict[str, object]:
    """Build the mandatory root identity fields for success and failure."""

    return {
        "root_contract_version": contract_version,
        "repository_mode": repository_mode,
        "roots": {
            "project": asdict(RootIdentity(name="project", location=".")),
            "framework": asdict(
                RootIdentity(name="framework", location=framework_location)
            ),
        },
    }


def _canonical_root(path: Path) -> Path:
    """Resolve symbolic links and normalize a root without requiring existence."""

    return path.expanduser().resolve(strict=False)


def _lexical_root(path: Path) -> Path:
    """Make a requested root absolute while preserving lexical escape intent."""

    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _relative_to(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _join_public(root_location: str, relative: Path) -> str:
    if not relative.parts:
        return root_location
    suffix = relative.as_posix()
    if root_location == ".":
        return suffix
    return f"{root_location}/{suffix}"


def _framework_location(project: Path, framework: Path) -> str:
    if framework == project:
        return "."
    relative = _relative_to(framework, project)
    return relative.as_posix() if relative is not None else "<external>"


def _root_relation(project: Path, framework: Path) -> str:
    if framework == project:
        return "equal"
    if _relative_to(framework, project) is not None:
        return "framework-inside-project"
    if _relative_to(project, framework) is not None:
        return "project-inside-framework"
    return "external"


def _has_framework_markers(framework: Path) -> bool:
    return all((framework / marker).is_file() for marker in FRAMEWORK_MARKERS)


def _git_repository_identity(root: Path) -> tuple[Path, Path] | None:
    """Ask Git for a repository root and administration directory."""

    try:
        process = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--show-toplevel",
                "--absolute-git-dir",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    if process.returncode != 0:
        return None
    lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    repository = _canonical_root(Path(lines[0]))
    admin = _canonical_root(Path(lines[1]))
    if repository != root or not admin.is_dir():
        return None
    return repository, admin


def _has_independent_git_boundary(project: Path, framework: Path) -> bool:
    framework_identity = _git_repository_identity(framework)
    project_identity = _git_repository_identity(project)
    return (
        framework_identity is not None
        and project_identity is not None
        and framework_identity[1] != project_identity[1]
    )


def _is_parent_relative_selection(path: Path) -> bool:
    """Return whether a relative project root intentionally selects a parent."""

    expanded = path.expanduser()
    return (
        not expanded.is_absolute()
        and bool(expanded.parts)
        and expanded.parts[0] == ".."
    )


def _validate_requested_relation(
    project_root: Path,
    lexical_project: Path,
    lexical_framework: Path,
    project: Path,
    framework: Path,
) -> None:
    """Reject lexical roots whose canonical targets cross their requested boundary."""

    lexical_relation = _root_relation(lexical_project, lexical_framework)
    canonical_relation = _root_relation(project, framework)
    if (
        lexical_relation == "framework-inside-project"
        and canonical_relation not in {"equal", "framework-inside-project"}
    ):
        raise RepositoryBoundaryError(
            "ROOT_BOUNDARY_INVALID",
            "framework root escapes the requested consumer project boundary after symbolic-link resolution",
            _framework_location(lexical_project, lexical_framework),
            framework_location=_framework_location(
                lexical_project,
                lexical_framework,
            ),
        )
    valid_parent_selection = (
        _is_parent_relative_selection(project_root)
        and canonical_relation == "framework-inside-project"
    )
    if (
        lexical_relation == "project-inside-framework"
        and canonical_relation not in {"equal", "project-inside-framework"}
        and not valid_parent_selection
    ):
        raise RepositoryBoundaryError(
            "ROOT_BOUNDARY_INVALID",
            "consumer project root escapes the requested framework boundary after symbolic-link resolution",
            ".",
            framework_location=_framework_location(project, framework),
        )


def resolve_repository_boundary(
    project_root: Path,
    framework_root: Path,
) -> RepositoryBoundary:
    """Resolve, validate, and classify framework and consumer repository roots."""

    lexical_project = _lexical_root(project_root)
    lexical_framework = _lexical_root(framework_root)
    project = _canonical_root(project_root)
    framework = _canonical_root(framework_root)

    if not project.is_dir():
        raise RepositoryBoundaryError(
            "PROJECT_ROOT_NOT_FOUND",
            "consumer project root is not an existing directory",
            ".",
            framework_location=_framework_location(project, framework),
        )

    _validate_requested_relation(
        project_root,
        lexical_project,
        lexical_framework,
        project,
        framework,
    )

    framework_location = _framework_location(project, framework)
    if not framework.is_dir() or not _has_framework_markers(framework):
        raise RepositoryBoundaryError(
            "FRAMEWORK_ROOT_INVALID",
            "framework root is missing required framework markers",
            framework_location,
            framework_location=framework_location,
        )

    if framework == project:
        mode = "standalone"
        framework_location = "."
    elif _relative_to(project, framework) is not None:
        raise RepositoryBoundaryError(
            "ROOT_BOUNDARY_INVALID",
            "consumer project root may not be nested inside the framework root",
            ".",
            framework_location=framework_location,
        )
    elif _relative_to(framework, project) is not None:
        if not _has_independent_git_boundary(project, framework):
            raise RepositoryBoundaryError(
                "ROOT_BOUNDARY_INVALID",
                "embedded framework root requires an independent Git boundary",
                framework_location,
                framework_location=framework_location,
            )
        mode = "embedded-submodule"
    else:
        mode = "external"
        framework_location = "<external>"

    return RepositoryBoundary(
        project_root=project,
        framework_root=framework,
        repository_mode=mode,
        project=RootIdentity(name="project", location="."),
        framework=RootIdentity(name="framework", location=framework_location),
    )
