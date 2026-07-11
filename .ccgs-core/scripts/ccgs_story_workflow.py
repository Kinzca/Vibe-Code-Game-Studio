#!/usr/bin/env python3
"""Engine-agnostic Story state, evidence, and closeout contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "1.0"
STATES = ("draft", "ready", "in-progress", "review", "blocked", "done")
TRANSITIONS = {
    "draft": {"ready", "blocked"},
    "ready": {"in-progress", "blocked"},
    "in-progress": {"review", "blocked"},
    "review": {"in-progress", "blocked", "done"},
    "blocked": {"ready"},
    "done": set(),
}
ALIASES = {
    "todo": "draft", "not-started": "draft", "notstarted": "draft",
    "inprogress": "in-progress", "complete": "done", "completed": "done",
}
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
STATUS_RE = re.compile(r"^(status\s*:\s*)([^#\r\n]+)(.*)$", re.IGNORECASE)
AC_RE = re.compile(r"^\s*-\s+(?:\[[ xX]\]\s*)?(?:(AC[-_ ]?\d+)\s*[:.-]\s*)?(.+?)\s*$")
BEGIN = "<!-- CCGS CLOSEOUT:BEGIN -->"
END = "<!-- CCGS CLOSEOUT:END -->"


class StoryWorkflowError(ValueError):
    """Raised for malformed or unsafe workflow inputs."""


@dataclass(frozen=True)
class Criterion:
    id: str
    text: str


@dataclass(frozen=True)
class Story:
    relative_path: str
    text: str
    story_id: str
    title: str
    status: str
    criteria: tuple[Criterion, ...]


def normalize_state(value: str) -> str:
    state = re.sub(r"[\s_]+", "-", value.strip().casefold())
    state = ALIASES.get(state, state)
    if state not in STATES:
        raise StoryWorkflowError(
            f"unknown Story state {value!r}; expected one of: {', '.join(STATES)}"
        )
    return state


def can_transition(current: str, target: str) -> bool:
    current, target = normalize_state(current), normalize_state(target)
    return current == target or target in TRANSITIONS[current]


def _scoped_file(
    project: Path, raw_path: str, scope: Path, label: str
) -> tuple[Path, str]:
    project, scope = project.resolve(), scope.resolve()
    candidate = Path(raw_path)
    candidate = (candidate if candidate.is_absolute() else project / candidate).resolve(
        strict=False
    )
    try:
        candidate.relative_to(scope)
        relative = candidate.relative_to(project).as_posix()
    except ValueError as exc:
        raise StoryWorkflowError(
            f"{label} must stay under {scope.relative_to(project).as_posix()}"
        ) from exc
    if not candidate.is_file():
        raise StoryWorkflowError(f"{label} file not found: {relative}")
    return candidate, relative


def resolve_story(project: Path, raw_path: str, data_dir: str) -> tuple[Path, str]:
    return _scoped_file(
        project, raw_path, project / data_dir / "production" / "epics", "Story"
    )


def resolve_evidence(project: Path, raw_path: str, data_dir: str) -> tuple[Path, str]:
    path, relative = _scoped_file(
        project,
        raw_path,
        project / data_dir / "production" / "qa" / "evidence",
        "evidence",
    )
    if path.suffix.casefold() != ".json":
        raise StoryWorkflowError("machine-readable evidence must use the .json extension")
    return path, relative


def default_evidence_path(data_dir: str, story_relative: str) -> str:
    return (
        Path(data_dir)
        / "production"
        / "qa"
        / "evidence"
        / f"{Path(story_relative).stem}.json"
    ).as_posix()


def parse_story(relative_path: str, text: str) -> Story:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise StoryWorkflowError("Story requires YAML frontmatter")
    end = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
    )
    if end is None:
        raise StoryWorkflowError("Story frontmatter is not closed")
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        match = FIELD_RE.match(line)
        if match:
            fields[match.group(1).casefold()] = match.group(2).strip().strip("\"'")
    story_id = fields.get("id", "")
    title = fields.get("title", fields.get("name", ""))
    status = fields.get("status", "")
    for key, value in (("id", story_id), ("title", title), ("status", status)):
        if not value:
            raise StoryWorkflowError(f"Story frontmatter requires {key}")

    criteria: list[Criterion] = []
    active = False
    for line in lines[end + 1 :]:
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            active = heading.group(1).strip().casefold() in {
                "acceptance criteria",
                "验收标准",
            }
            continue
        if active and (match := AC_RE.match(line)):
            identifier = (match.group(1) or f"AC-{len(criteria) + 1}").upper()
            identifier = re.sub(r"AC[-_ ]*", "AC-", identifier)
            criteria.append(Criterion(identifier, match.group(2).strip()))
    if not criteria:
        raise StoryWorkflowError("Story requires at least one Acceptance Criterion")
    if len({item.id for item in criteria}) != len(criteria):
        raise StoryWorkflowError("Story Acceptance Criterion IDs must be unique")
    return Story(
        relative_path, text, story_id, title, normalize_state(status), tuple(criteria)
    )


def load_story(project: Path, raw_path: str, data_dir: str) -> tuple[Path, Story]:
    path, relative = resolve_story(project, raw_path, data_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise StoryWorkflowError(f"Story is not valid UTF-8: {relative}") from exc
    return path, parse_story(relative, text)


def set_story_status(text: str, target: str) -> str:
    target = normalize_state(target)
    lines = text.splitlines(keepends=True)
    end = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
    )
    if end is None:
        raise StoryWorkflowError("Story frontmatter is not closed")
    for index in range(1, end):
        raw = lines[index].rstrip("\r\n")
        ending = lines[index][len(raw) :]
        if match := STATUS_RE.match(raw):
            lines[index] = f"{match.group(1)}{target}{match.group(3)}{ending}"
            return "".join(lines)
    raise StoryWorkflowError("Story frontmatter requires status")


def replace_closeout_block(text: str, block: str) -> str:
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise StoryWorkflowError("Story contains malformed CCGS closeout markers")
    rendered = f"{BEGIN}\n{block.rstrip()}\n{END}"
    if BEGIN in text:
        return re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END),
            lambda _: rendered,
            text,
            flags=re.DOTALL,
        )
    separator = "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    return f"{text}{separator}{rendered}\n"


def validate_evidence(document: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []

    def fail(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    if not isinstance(document, dict):
        fail("$", "must be an object")
        return errors
    allowed_root = {
        "schema_version", "story_id", "result", "acceptance_criteria", "checks"
    }
    for field in sorted(set(document) - allowed_root):
        fail(f"$.{field}", "is not allowed")
    for field in ("schema_version", "story_id", "result", "acceptance_criteria", "checks"):
        if field not in document:
            fail(f"$.{field}", "is required")
    if document.get("schema_version") != "1.0":
        fail("$.schema_version", "must equal '1.0'")
    if not isinstance(document.get("story_id"), str) or not document.get("story_id", "").strip():
        fail("$.story_id", "must be a non-empty string")
    if document.get("result") not in {"pass", "fail", "blocked"}:
        fail("$.result", "must be pass, fail, or blocked")

    criteria = document.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        fail("$.acceptance_criteria", "must be a non-empty array")
    else:
        seen: set[str] = set()
        for index, item in enumerate(criteria):
            base = f"$.acceptance_criteria[{index}]"
            if not isinstance(item, dict):
                fail(base, "must be an object")
                continue
            for field in sorted(set(item) - {"id", "status", "evidence"}):
                fail(f"{base}.{field}", "is not allowed")
            identifier = item.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"AC-\d+", identifier):
                fail(f"{base}.id", "must match AC-<number>")
            elif identifier in seen:
                fail(f"{base}.id", "must be unique")
            else:
                seen.add(identifier)
            if item.get("status") not in {"pass", "fail", "deferred"}:
                fail(f"{base}.status", "must be pass, fail, or deferred")
            if not isinstance(item.get("evidence"), str) or not item.get("evidence", "").strip():
                fail(f"{base}.evidence", "must be a non-empty string")

    checks = document.get("checks")
    if not isinstance(checks, list) or not checks:
        fail("$.checks", "must be a non-empty array")
    else:
        seen_checks: set[str] = set()
        for index, item in enumerate(checks):
            base = f"$.checks[{index}]"
            if not isinstance(item, dict):
                fail(base, "must be an object")
                continue
            for field in sorted(set(item) - {"id", "type", "status", "summary"}):
                fail(f"{base}.{field}", "is not allowed")
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                fail(f"{base}.id", "must be a non-empty string")
            elif identifier in seen_checks:
                fail(f"{base}.id", "must be unique")
            else:
                seen_checks.add(identifier)
            if item.get("type") not in {
                "automated-test",
                "manual-test",
                "review",
                "build",
                "analysis",
            }:
                fail(f"{base}.type", "uses an unsupported check type")
            if item.get("status") not in {"pass", "fail", "deferred"}:
                fail(f"{base}.status", "must be pass, fail, or deferred")
            if not isinstance(item.get("summary"), str) or not item.get("summary", "").strip():
                fail(f"{base}.summary", "must be a non-empty string")
    return errors


def load_evidence(
    project: Path, raw_path: str, data_dir: str
) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
    path, relative = resolve_evidence(project, raw_path, data_dir)
    try:
        document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoryWorkflowError(f"invalid evidence JSON: {relative}: {exc}") from exc
    return relative, document, validate_evidence(document)


def evidence_report(
    relative: str, document: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "evidence-validate",
        "valid": not errors,
        "evidence": relative,
        "story_id": document.get("story_id", ""),
        "result": document.get("result", ""),
        "errors": errors,
    }


def advance_report(story: Story, target: str, reason: str) -> dict[str, Any]:
    target = normalize_state(target)
    allowed = can_transition(story.status, target)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "story-advance",
        "allowed": allowed,
        "changed": allowed and story.status != target,
        "story": story.relative_path,
        "story_id": story.story_id,
        "from": story.status,
        "to": target,
        "reason": reason.strip(),
        "failure": "" if allowed else f"transition {story.status} -> {target} is not allowed",
    }


def apply_advance(
    path: Path,
    story: Story,
    report: dict[str, Any],
    atomic_write: Callable[[Path, str], None],
) -> bool:
    if not report["allowed"] or not report["changed"]:
        return False
    atomic_write(path, set_story_status(story.text, str(report["to"])))
    return True


def _check(key: str, passed: bool, message: str) -> dict[str, str]:
    return {"key": key, "status": "pass" if passed else "fail", "message": message}


def closeout_report(
    story: Story,
    evidence_relative: str,
    evidence: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    checks = [
        _check("story.state", story.status in {"review", "done"}, f"state is {story.status}"),
        _check("story.acceptance", bool(story.criteria), f"{len(story.criteria)} criteria declared"),
        _check(
            "evidence.schema",
            not errors,
            (
                "evidence matches schema"
                if not errors
                else "; ".join(
                    f"{item['path']}: {item['message']}" for item in errors
                )
            ),
        ),
        _check(
            "evidence.story",
            evidence.get("story_id") == story.story_id,
            f"evidence story_id is {evidence.get('story_id', '')!r}",
        ),
        _check(
            "evidence.result",
            evidence.get("result") == "pass",
            f"result is {evidence.get('result', '')!r}",
        ),
    ]
    evidence_criteria = {
        item.get("id"): item
        for item in evidence.get("acceptance_criteria", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing = [item.id for item in story.criteria if item.id not in evidence_criteria]
    nonpassing = [
        item.id
        for item in story.criteria
        if item.id in evidence_criteria and evidence_criteria[item.id].get("status") != "pass"
    ]
    message = "all criteria have passing evidence"
    if missing:
        message = f"missing evidence for {', '.join(missing)}"
    elif nonpassing:
        message = f"non-passing evidence for {', '.join(nonpassing)}"
    checks.append(_check("evidence.acceptance", not missing and not nonpassing, message))

    evidence_checks = evidence.get("checks", [])
    failing_checks = [
        item.get("id", "unknown")
        for item in evidence_checks
        if isinstance(item, dict) and item.get("status") != "pass"
    ]
    checks_ok = bool(evidence_checks) and not failing_checks
    checks.append(
        _check(
            "evidence.checks",
            checks_ok,
            "all checks passed"
            if checks_ok
            else "non-passing checks: " + (", ".join(failing_checks) or "none supplied"),
        )
    )
    failures = [
        {"code": item["key"], "message": item["message"]}
        for item in checks
        if item["status"] == "fail"
    ]
    verdict = "pass" if not failures else "fail"
    target = "done" if verdict == "pass" else story.status
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "closeout",
        "verdict": verdict,
        "story": story.relative_path,
        "story_id": story.story_id,
        "evidence": evidence_relative,
        "current_state": story.status,
        "target_state": target,
        "would_write": (verdict == "pass" and story.status != "done") or bool(failures),
        "checks": checks,
        "failures": failures,
    }


def render_closeout_block(report: dict[str, Any]) -> str:
    passed = sum(item["status"] == "pass" for item in report["checks"])
    failed = len(report["checks"]) - passed
    tick = chr(96)
    lines = [
        "## CCGS Closeout",
        "",
        f"- Verdict: {str(report['verdict']).upper()}",
        f"- Evidence: {tick}{report['evidence']}{tick}",
        f"- State: {tick}{report['current_state']}{tick} -> {tick}{report['target_state']}{tick}",
        f"- Checks: {passed} passed, {failed} failed",
    ]
    if report["failures"]:
        lines.extend(["", "### Failure Reasons", ""])
        lines.extend(
            f"- {tick}{failure['code']}{tick}: {failure['message']}"
            for failure in report["failures"]
        )
    return "\n".join(lines)


def apply_closeout(
    path: Path,
    story: Story,
    report: dict[str, Any],
    atomic_write: Callable[[Path, str], None],
) -> bool:
    if report["verdict"] == "pass" and story.status == "done" and BEGIN in story.text:
        return False
    content = story.text
    if report["verdict"] == "pass":
        content = set_story_status(content, "done")
    content = replace_closeout_block(content, render_closeout_block(report))
    if content == story.text:
        return False
    atomic_write(path, content)
    return True