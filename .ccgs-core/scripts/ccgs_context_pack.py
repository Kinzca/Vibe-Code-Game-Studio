"""Build deterministic, bounded Context Packs from CCGS project data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "1.0"
DEFAULT_MAX_FILES = 8
DEFAULT_MAX_CHARS_PER_FILE = 6000
DEFAULT_MAX_TOTAL_CHARS = 24000
DEFAULT_OUTPUT_TEMPLATE = "{data_dir}/production/context/packs/{story_stem}-context-pack.md"
SUPPORTED_SOURCE_SUFFIXES = {".md", ".json", ".yaml", ".yml"}


class ContextPackError(ValueError):
    """Raised when a Context Pack request is invalid or unsafe."""


@dataclass(frozen=True)
class PackSource:
    """One bounded source embedded in a Context Pack."""

    role: str
    path: str
    content: str
    original_chars: int
    included_chars: int
    truncated: bool


@dataclass(frozen=True)
class ContextPack:
    """Rendered Context Pack and its deterministic selection metadata."""

    markdown: str
    story_path: str
    output_path: str
    sources: tuple[PackSource, ...]
    missing_references: tuple[str, ...]
    omitted_paths: tuple[str, ...]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContextPackError(f"unable to read UTF-8 source: {path}") from exc


def _frontmatter(text: str) -> dict[str, str | list[str]]:
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ContextPackError("story must start with YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ContextPackError("story frontmatter is not closed") from exc

    values: dict[str, str | list[str]] = {}
    current_key = ""
    for line in lines[1:end]:
        item = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if item:
            current_key = item.group(1).casefold().replace("-", "_")
            raw = item.group(2).strip()
            if not raw:
                values[current_key] = []
            elif raw.startswith("[") and raw.endswith("]"):
                values[current_key] = [
                    value.strip().strip("\"'")
                    for value in raw[1:-1].split(",")
                    if value.strip()
                ]
            else:
                values[current_key] = raw.strip("\"'")
            continue
        child = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if child and current_key and isinstance(values.get(current_key), list):
            value = child.group(1).strip().strip("\"'")
            cast = values[current_key]
            assert isinstance(cast, list)
            cast.append(value)
    return values


def _list_values(frontmatter: dict[str, str | list[str]], keys: Iterable[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = frontmatter.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif isinstance(value, str) and value:
            values.append(value)
    return values


def _unique(values: Iterable[str]) -> list[str]:
    """Preserve reference order while removing exact duplicates."""

    return list(dict.fromkeys(value for value in values if value))


def _body_references(text: str, data_dir: str) -> dict[str, list[str]]:
    """Find legacy path and ADR-ID references outside structured frontmatter."""

    normalized = text.replace("\\", "/")
    prefix = rf"(?:{re.escape(data_dir)}/)?"
    path_tail = r"[A-Za-z0-9_.\-/]+\.(?:md|json|ya?ml)"
    return {
        "gdd": _unique(
            re.findall(
                prefix + r"design/gdd/" + path_tail,
                normalized,
                flags=re.I,
            )
        ),
        "adr": _unique(
            re.findall(
                prefix + r"project-docs/architecture/" + path_tail,
                normalized,
                flags=re.I,
            )
            + re.findall(r"\bADR-\d{4}\b", text, flags=re.I)
        ),
        "evidence": _unique(
            re.findall(
                prefix + r"production/qa/evidence/" + path_tail,
                normalized,
                flags=re.I,
            )
        ),
    }


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return ""


def resolve_story_path(project: Path, story: str, data_dir: str) -> Path:
    """Resolve a Story while restricting reads to the CCGS epic tree."""

    project = project.resolve()
    candidate = Path(story)
    if not candidate.is_absolute():
        candidate = project / candidate
    candidate = candidate.resolve(strict=False)
    epic_root = (project / data_dir / "production" / "epics").resolve()
    try:
        candidate.relative_to(epic_root)
    except ValueError as exc:
        raise ContextPackError("story must be inside the configured CCGS production/epics directory") from exc
    if candidate.suffix.casefold() != ".md" or not candidate.is_file():
        raise ContextPackError(f"story markdown file not found: {story}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path remains below the allowed data root."""

    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_reference(
    project: Path,
    data_root: Path,
    data_dir: str,
    reference: str,
    role: str,
) -> Path | None:
    cleaned = reference.split("#", 1)[0].strip().replace("\\", "/")
    if not cleaned:
        return None

    if role == "adr" and re.fullmatch(r"ADR-\d{4}", cleaned, flags=re.I):
        architecture = data_root / "project-docs" / "architecture"
        needle = cleaned.casefold()
        if architecture.is_dir():
            for candidate in sorted(path for path in architecture.rglob("*") if path.is_file()):
                if candidate.suffix.casefold() not in SUPPORTED_SOURCE_SUFFIXES:
                    continue
                if not _is_within(candidate, data_root):
                    continue
                if needle in candidate.name.casefold() or needle in _read_text(candidate)[:2000].casefold():
                    return candidate.resolve()
        return None

    reference_path = Path(cleaned)
    if reference_path.is_absolute():
        return None
    parts = reference_path.parts
    if parts and parts[0].casefold() == data_dir.casefold():
        candidate = project / reference_path
    else:
        candidate = data_root / reference_path
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(data_root)
    except ValueError:
        return None
    if candidate.is_file() and candidate.suffix.casefold() in SUPPORTED_SOURCE_SUFFIXES:
        return candidate
    return None


def _discover_evidence(data_root: Path, story_id: str, story_stem: str) -> list[Path]:
    evidence_root = data_root / "production" / "qa" / "evidence"
    if not evidence_root.is_dir():
        return []
    needles = {value.casefold() for value in (story_id, story_stem) if value}
    matches: list[Path] = []
    for candidate in sorted(path for path in evidence_root.rglob("*") if path.is_file()):
        if candidate.suffix.casefold() not in SUPPORTED_SOURCE_SUFFIXES:
            continue
        if not _is_within(candidate, data_root):
            continue
        name = candidate.stem.casefold()
        text = _read_text(candidate)[:20000].casefold()
        if any(needle in name or needle in text for needle in needles):
            matches.append(candidate.resolve())
    return matches


def _validate_limits(max_files: int, max_chars_per_file: int, max_total_chars: int) -> None:
    if not 1 <= max_files <= 20:
        raise ContextPackError("max-files must be between 1 and 20")
    if not 200 <= max_chars_per_file <= 30000:
        raise ContextPackError("max-chars-per-file must be between 200 and 30000")
    if not 500 <= max_total_chars <= 100000:
        raise ContextPackError("max-total-chars must be between 500 and 100000")


def _indented(content: str) -> list[str]:
    if not content:
        return ["    (empty file)"]
    return [f"    {line}" if line else "" for line in content.splitlines()]


def build_context_pack(
    project: Path,
    story: str,
    data_dir: str,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> ContextPack:
    """Select and render one bounded, deterministic Story Context Pack."""

    _validate_limits(max_files, max_chars_per_file, max_total_chars)
    project = project.resolve()
    data_root = (project / data_dir).resolve()
    story_path = resolve_story_path(project, story, data_dir)
    story_text = _read_text(story_path)
    frontmatter = _frontmatter(story_text)
    story_id = str(frontmatter.get("id") or story_path.stem)
    title = str(
        frontmatter.get("title")
        or frontmatter.get("name")
        or _first_heading(story_text)
        or story_path.stem
    )
    status = str(frontmatter.get("status") or "unknown")

    selected: list[tuple[str, Path]] = [("story", story_path)]
    missing: list[str] = []
    seen = {story_path.resolve()}

    body_references = _body_references(story_text, data_dir)
    explicit_groups = [
        (
            "gdd",
            _unique(
                _list_values(frontmatter, ("gdd_refs", "gdd", "gdds"))
                + body_references["gdd"]
            ),
        ),
        (
            "adr",
            _unique(
                _list_values(frontmatter, ("adr_refs", "adr", "adrs"))
                + body_references["adr"]
            ),
        ),
        (
            "evidence",
            _unique(
                _list_values(
                    frontmatter,
                    ("evidence_refs", "qa_refs", "test_evidence"),
                )
                + body_references["evidence"]
            ),
        ),
    ]
    for role, references in explicit_groups:
        for reference in references:
            resolved = _resolve_reference(project, data_root, data_dir, reference, role)
            if resolved is None:
                missing.append(f"{role}:{reference}")
            elif resolved not in seen:
                seen.add(resolved)
                selected.append((role, resolved))

    for evidence in _discover_evidence(data_root, story_id, story_path.stem):
        if evidence not in seen:
            seen.add(evidence)
            selected.append(("evidence", evidence))

    session_state = (data_root / "production" / "session-state" / "active.md").resolve()
    if (
        session_state.is_file()
        and _is_within(session_state, data_root)
        and session_state not in seen
    ):
        selected.append(("session", session_state))

    omitted_paths = [
        path.relative_to(project).as_posix()
        for _, path in selected[max_files:]
    ]
    selected = selected[:max_files]

    sources: list[PackSource] = []
    remaining = max_total_chars
    for role, path in selected:
        if remaining <= 0:
            omitted_paths.append(path.relative_to(project).as_posix())
            continue
        content = _read_text(path)
        included = min(len(content), max_chars_per_file, remaining)
        excerpt = content[:included]
        source = PackSource(
            role=role,
            path=path.relative_to(project).as_posix(),
            content=excerpt,
            original_chars=len(content),
            included_chars=included,
            truncated=included < len(content),
        )
        sources.append(source)
        remaining -= included

    story_rel = story_path.relative_to(project).as_posix()
    output_path = DEFAULT_OUTPUT_TEMPLATE.format(
        data_dir=data_dir,
        story_stem=story_path.stem,
    )
    lines = [
        "# CCGS Context Pack",
        "",
        "## Summary",
        "",
        f"- Schema: {SCHEMA_VERSION}",
        f"- Story: {story_rel}",
        f"- Story ID: {story_id}",
        f"- Title: {title}",
        f"- Status: {status}",
        f"- Sources: {len(sources)}",
        f"- Character budget: {max_total_chars}",
        "",
        "## Source Manifest",
        "",
        "| Role | Path | Included | Original | Truncated |",
        "|:---|:---|---:|---:|:---:|",
    ]
    for source in sources:
        lines.append(
            f"| {source.role} | {source.path} | {source.included_chars} | "
            f"{source.original_chars} | {'yes' if source.truncated else 'no'} |"
        )

    lines.extend(["", "## Missing References", ""])
    lines.extend([f"- {item}" for item in missing] or ["- None."])
    lines.extend(["", "## Omitted By Limits", ""])
    lines.extend([f"- {item}" for item in omitted_paths] or ["- None."])
    lines.extend(["", "## Sources", ""])

    for source in sources:
        lines.extend([f"### {source.role}: {source.path}", ""])
        lines.extend(_indented(source.content))
        if source.truncated:
            lines.extend(["", "    [truncated by Context Pack limits]"])
        lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"
    return ContextPack(
        markdown=markdown,
        story_path=story_rel,
        output_path=output_path,
        sources=tuple(sources),
        missing_references=tuple(missing),
        omitted_paths=tuple(omitted_paths),
    )
