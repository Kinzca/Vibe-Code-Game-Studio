"""Plan deterministic, non-destructive Codex Bridge project files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = "1.0"
BRIDGE_VERSION = "1.0"
AGENTS_BEGIN = "<!-- CCGS CODEX BRIDGE:BEGIN -->"
AGENTS_END = "<!-- CCGS CODEX BRIDGE:END -->"
SKILL_MARKER = "<!-- CCGS CODEX BRIDGE:MANAGED -->"
AGENTS_TARGET = "AGENTS.md"
SKILL_TARGETS = {
    ".agents/skills/ccgs-context/SKILL.md": "skills/ccgs-context/SKILL.md.tmpl",
    ".agents/skills/ccgs-workflow/SKILL.md": "skills/ccgs-workflow/SKILL.md.tmpl",
}


class CodexBridgeError(ValueError):
    """Stable validation failure for one project-relative managed target."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "WRITE_PLAN_INVALID",
        location: str = ".",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location


@dataclass(frozen=True)
class PlannedFile:
    """One deterministic bootstrap file operation."""

    path: str
    action: str
    sha256: str
    content: str


@dataclass(frozen=True)
class BootstrapPlan:
    """Complete Codex Bridge write plan."""

    files: tuple[PlannedFile, ...]

    def manifest(self, mode: str) -> dict[str, object]:
        """Return a stable, machine-readable write manifest."""

        summary = {
            action: sum(1 for item in self.files if item.action == action)
            for action in ("create", "update", "unchanged")
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "bridge": "codex",
            "bridge_version": BRIDGE_VERSION,
            "mode": mode,
            "files": [
                {
                    "path": item.path,
                    "action": item.action,
                    "sha256": item.sha256,
                }
                for item in self.files
            ],
            "summary": summary,
            "would_write": any(item.action != "unchanged" for item in self.files),
        }


def codex_target_paths() -> tuple[str, ...]:
    """Return every consumer path managed by this bridge."""

    return (AGENTS_TARGET, *SKILL_TARGETS)


def _read_existing(path: Path, location: str) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise CodexBridgeError(
            "managed target may not be a symbolic link",
            location=location,
        )
    if not path.is_file():
        raise CodexBridgeError(
            "managed target is not a regular file",
            location=location,
        )
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CodexBridgeError(
            "managed target is not valid UTF-8",
            location=location,
        ) from exc


def _load_template(framework: Path, relative: str) -> str:
    path = framework / "templates" / "codex" / relative
    if not path.is_file():
        raise CodexBridgeError(
            "Codex template is missing",
            code="FRAMEWORK_TEMPLATE_INVALID",
            location=f"templates/codex/{relative}",
        )
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise CodexBridgeError(
            "Codex template is not valid UTF-8",
            code="FRAMEWORK_TEMPLATE_INVALID",
            location=f"templates/codex/{relative}",
        ) from exc


def _render(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"{{([A-Z0-9_]+)}}", rendered)))
    if unresolved:
        raise CodexBridgeError(
            "unresolved Codex template values: " + ", ".join(unresolved)
        )
    return rendered.rstrip() + "\n"


def _managed_agents(existing: str | None, rendered_body: str) -> str:
    source = existing or ""
    newline = "\r\n" if "\r\n" in source else "\n"
    body = newline.join(rendered_body.rstrip().splitlines())
    block = newline.join((AGENTS_BEGIN, body, AGENTS_END))

    begin_count = source.count(AGENTS_BEGIN)
    end_count = source.count(AGENTS_END)
    if begin_count != end_count or begin_count > 1:
        raise CodexBridgeError("AGENTS.md contains malformed CCGS managed markers")

    if begin_count == 1:
        start = source.index(AGENTS_BEGIN)
        end = source.index(AGENTS_END, start) + len(AGENTS_END)
        return source[:start] + block + source[end:]

    if not source.strip():
        return "# AGENTS.md" + newline + newline + block + newline
    return source.rstrip("\r\n") + newline + newline + block + newline


def _is_managed_skill(content: str) -> bool:
    """Require the management marker immediately after valid frontmatter."""

    lines = content.lstrip("﻿").splitlines()
    if not lines or lines[0] != "---" or content.count(SKILL_MARKER) != 1:
        return False
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError:
        return False
    return SKILL_MARKER in lines[frontmatter_end + 1:frontmatter_end + 4]


def _managed_skill(existing: str | None, rendered: str, relative: str) -> str:
    lines = rendered.rstrip().splitlines()
    if not lines or lines[0] != "---":
        raise CodexBridgeError(
            "Codex Skill template lacks frontmatter",
            code="FRAMEWORK_TEMPLATE_INVALID",
            location=relative,
        )
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError as exc:
        raise CodexBridgeError(
            "Codex Skill template has unclosed frontmatter",
            code="FRAMEWORK_TEMPLATE_INVALID",
            location=relative,
        ) from exc

    lines[frontmatter_end + 1:frontmatter_end + 1] = ["", SKILL_MARKER]
    newline = chr(10)
    desired = newline.join(lines).rstrip() + newline
    if existing is None:
        return desired
    if not _is_managed_skill(existing):
        raise CodexBridgeError(
            "refusing to replace unmanaged Codex Skill",
            location=relative,
        )
    return desired

def _planned_file(relative: str, existing: str | None, desired: str) -> PlannedFile:
    if existing is None:
        action = "create"
    elif existing == desired:
        action = "unchanged"
    else:
        action = "update"
    return PlannedFile(
        path=relative,
        action=action,
        sha256=hashlib.sha256(desired.encode("utf-8")).hexdigest(),
        content=desired,
    )


def build_codex_plan(framework: Path, project: Path, data_dir: str) -> BootstrapPlan:
    """Render all Codex Bridge files without changing the consumer project."""

    framework = framework.resolve()
    project = project.resolve()
    values = {
        "DATA_DIR": data_dir,
        "BRIDGE_VERSION": BRIDGE_VERSION,
    }

    agents_existing = _read_existing(project / AGENTS_TARGET, AGENTS_TARGET)
    agents_body = _render(_load_template(framework, "AGENTS.md.tmpl"), values)
    files = [
        _planned_file(
            AGENTS_TARGET,
            agents_existing,
            _managed_agents(agents_existing, agents_body),
        )
    ]

    for relative, template_relative in SKILL_TARGETS.items():
        existing = _read_existing(project / relative, relative)
        rendered = _render(_load_template(framework, template_relative), values)
        desired = _managed_skill(existing, rendered, relative)
        files.append(_planned_file(relative, existing, desired))

    return BootstrapPlan(files=tuple(files))


def apply_codex_plan(
    project: Path,
    plan: BootstrapPlan,
    writer: Callable[[Path, str], None],
) -> None:
    """Apply changed files through the caller's atomic writer."""

    project = project.resolve()
    for item in plan.files:
        if item.action != "unchanged":
            writer(project / item.path, item.content)


def verify_codex_plan(project: Path, plan: BootstrapPlan) -> None:
    """Verify every managed file matches the planned content hash."""

    project = project.resolve()
    for item in plan.files:
        target = project / item.path
        existing = _read_existing(target, item.path)
        if existing is None:
            raise CodexBridgeError(f"managed file was not created: {item.path}")
        digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
        if digest != item.sha256:
            raise CodexBridgeError(f"managed file hash mismatch: {item.path}")


def render_plan(plan: BootstrapPlan, mode: str) -> str:
    """Render a concise human-readable bootstrap manifest."""

    manifest = plan.manifest(mode)
    lines = [f"CCGS Codex Bridge ({mode})", ""]
    for item in plan.files:
        lines.append(f"{item.action.upper():9} {item.path}")
    summary = manifest["summary"]
    lines.extend(
        [
            "",
            (
                "Summary: "
                f"{summary['create']} create, "
                f"{summary['update']} update, "
                f"{summary['unchanged']} unchanged"
            ),
        ]
    )
    return "\n".join(lines) + "\n"
