#!/usr/bin/env python3
"""Generate a compact context pack for one CCGS story file."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from ccgs_context_index import build_index, collect_refs, find_root, frontmatter


DEFAULT_OUTPUT_TEMPLATE = "{data_dir}/production/context/story/{story_stem}-context.md"


def read_text(path: Path, limit: int = 30000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def first_heading(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def acceptance_criteria(text: str, limit: int = 16) -> list[str]:
    rows: list[str] = []
    in_ac = False
    for line in text.splitlines():
        if re.match(r"^##+\s+Acceptance Criteria", line, flags=re.I):
            in_ac = True
            continue
        if in_ac and re.match(r"^##+\s+", line):
            break
        if in_ac and re.match(r"^\s*-\s+\[[ xX]\]", line):
            rows.append(line.strip())
    return rows[:limit]


def section_excerpt(text: str, heading: str, max_lines: int = 10) -> list[str]:
    lines = text.splitlines()
    start = -1
    for idx, line in enumerate(lines):
        if re.match(rf"^##+\s+{re.escape(heading)}\s*$", line, flags=re.I):
            start = idx + 1
            break
    if start == -1:
        return []
    out: list[str] = []
    for line in lines[start:]:
        if re.match(r"^##+\s+", line):
            break
        if line.strip():
            out.append(line.rstrip())
        if len(out) >= max_lines:
            break
    return out


def find_related(index: dict[str, Any], refs: dict[str, list[str]], story_text: str, max_related: int) -> list[dict[str, Any]]:
    related: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    files = list(index["files"])

    def add(entry: dict[str, Any], priority: int) -> None:
        path = str(entry["path"])
        if path not in seen:
            seen.add(path)
            related.append((priority, entry))

    for entry in files:
        path = str(entry["path"])
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in refs["adrs"]:
            add(entry, 10)
            continue
        if path in refs["gdds"]:
            add(entry, 20)
            continue
        if entry_id and entry_id in refs["stories"]:
            add(entry, 30)
            continue
        if path in story_text:
            add(entry, 40)

    for entry in files:
        if len(related) >= max_related:
            break
        if entry.get("type") in {"sprint-status", "sprint"}:
            add(entry, 80)

    ordered = sorted(related, key=lambda item: (item[0], str(item[1]["path"])))
    return [entry for _, entry in ordered[:max_related]]


def render_pack(root: Path, story_rel: str, index: dict[str, Any], max_related: int) -> str:
    story_path = root / story_rel
    text = read_text(story_path)
    fm = frontmatter(text)
    refs = collect_refs(text)
    related = find_related(index, refs, text, max_related)

    lines = [
        f"# Story Context Pack: {first_heading(text) or story_path.name}",
        "",
        "This pack is a low-cost entry point. It lists the minimum files and decisions to inspect before opening broad project documents.",
        "",
        "## Story",
        "",
        f"- Path: `{story_rel}`",
        f"- Status: `{fm.get('status', 'unknown')}`",
        f"- Epic: `{fm.get('epic', 'unknown')}`",
        f"- Sprint: `{fm.get('sprint', 'unknown')}`",
        f"- Owner: `{fm.get('owner', 'unknown')}`",
        f"- Type: `{fm.get('type', 'unknown')}`",
        "",
        "## References Detected",
        "",
        f"- ADRs: {', '.join(refs['adrs']) or '(none)'}",
        f"- TRs: {', '.join(refs['trs']) or '(none)'}",
        f"- GDD paths: {', '.join(f'`{item}`' for item in refs['gdds']) or '(none)'}",
        "",
        "## Acceptance Criteria",
        "",
    ]
    lines.extend(acceptance_criteria(text) or ["- No checkbox acceptance criteria found."])

    notes = section_excerpt(text, "Implementation Notes", 12)
    if notes:
        lines.extend(["", "## Implementation Notes Excerpt", ""])
        lines.extend(f"- {line}" for line in notes)

    lines.extend(["", "## Minimal Related Reads", ""])
    for entry in related:
        ident = f" {entry.get('id')}" if entry.get("id") else ""
        title = entry.get("title") or entry["path"]
        lines.append(f"- `{entry['path']}`{ident} - {entry.get('type')} / {entry.get('status') or 'unknown'} / {title}")

    lines.extend(["", "## Recommended Commands", ""])
    lines.append(f"sed -n '1,220p' {story_rel}")
    for entry in related:
        path = entry["path"]
        if path.endswith(".json"):
            lines.append(f"python3 -m json.tool {path}")
        else:
            lines.append(f"sed -n '1,180p' {path}")

    lines.extend(["", "## Guardrails", ""])
    lines.append("- Read this pack and the listed files before broad repository searches.")
    lines.append("- Do not open full historical sprint archives unless this pack or the user request points to them.")
    lines.append("- If references are missing, update the story or TR/ADR index before implementation.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate compact CCGS story context pack.")
    parser.add_argument("story", help="Story file path, relative to repo or absolute.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--output", default="", help="Output path relative to repo when --write is used.")
    parser.add_argument("--write", action="store_true", help="Write pack to production/context/story/.")
    parser.add_argument("--max-related", type=int, default=12, help="Maximum related files.")
    args = parser.parse_args()

    root = find_root(Path(args.repo))
    story_path = Path(args.story)
    if not story_path.is_absolute():
        story_path = root / story_path
    if not story_path.exists():
        raise SystemExit(f"story not found: {args.story}")
    story_rel = story_path.relative_to(root).as_posix()
    index = build_index(root)
    output = args.output or DEFAULT_OUTPUT_TEMPLATE.format(data_dir=index["data_dir"], story_stem=story_path.stem)
    pack = render_pack(root, story_rel, index, args.max_related)

    if args.write:
        out_path = root / output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(pack, encoding="utf-8")
        print(out_path.relative_to(root).as_posix())
    else:
        print(pack, end="")


if __name__ == "__main__":
    main()
