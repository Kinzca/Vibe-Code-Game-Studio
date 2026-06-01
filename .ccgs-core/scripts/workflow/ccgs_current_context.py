#!/usr/bin/env python3
"""Generate a short current-context memo from the CCGS index and sprint files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from ccgs_context_index import build_index, find_root


DEFAULT_OUTPUT_TEMPLATE = "{data_dir}/production/context/current-context.md"


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def newest(entries: list[dict[str, Any]], doc_type: str, limit: int = 6) -> list[dict[str, Any]]:
    filtered = [entry for entry in entries if entry.get("type") == doc_type]
    return sorted(filtered, key=lambda entry: int(entry.get("mtime", 0)), reverse=True)[:limit]


def status_counts(entries: list[dict[str, Any]], doc_type: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("type") != doc_type:
            continue
        status = str(entry.get("status") or "unknown").strip()
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def extract_sprint_status(root: Path, data_dir: str) -> tuple[list[str], list[str]]:
    path = root / data_dir / "production/sprint-status.yaml"
    text = read_text(path)
    if not text:
        return [], []

    header: list[str] = []
    for key in ["current_sprint", "active_sprint", "sprint", "goal", "phase", "status"]:
        match = re.search(rf"^{key}:\s*(.+)$", text, flags=re.M)
        if match:
            header.append(f"- {key}: {match.group(1).strip()}")

    story_lines: list[str] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        id_match = re.match(r"\s*-\s+id:\s*['\"]?([^'\"]+)", line)
        if id_match:
            if current:
                story_lines.append(format_story(current))
            current = {"id": id_match.group(1)}
            continue
        if not current:
            continue
        for key in ["name", "status", "owner", "blocker"]:
            match = re.match(rf"\s+{key}:\s*(.*)$", line)
            if match:
                current[key] = match.group(1).strip().strip("\"'")
    if current:
        story_lines.append(format_story(current))

    return header, story_lines[:12]


def format_story(story: dict[str, str]) -> str:
    bits = [story.get("id", "?")]
    if story.get("status"):
        bits.append(story["status"])
    if story.get("name"):
        bits.append(story["name"])
    if story.get("blocker"):
        bits.append(f"blocker: {story['blocker']}")
    return "- " + " | ".join(bits)


def render_context(root: Path, index: dict[str, Any]) -> str:
    data_dir = str(index["data_dir"])
    files = list(index["files"])
    sprint_header, sprint_stories = extract_sprint_status(root, data_dir)
    accepted_adrs = [entry for entry in files if entry.get("type") == "adr" and str(entry.get("status")).lower().startswith("accepted")]
    proposed_adrs = [entry for entry in files if entry.get("type") == "adr" and str(entry.get("status")).lower().startswith("proposed")]

    lines = [
        "# CCGS Current Context",
        "",
        "This memo is a low-cost startup file. Read it before broad GDD/ADR/QA scans.",
        "",
        "## Project Index",
        "",
        f"- Data dir: `{data_dir}`",
        f"- Indexed files: {len(files)}",
        f"- Counts: `{index['counts']}`",
        f"- Accepted ADRs: {len(accepted_adrs)}",
        f"- Proposed ADRs: {len(proposed_adrs)}",
        "",
        "## Sprint Snapshot",
        "",
    ]

    lines.extend(sprint_header or ["- No `production/sprint-status.yaml` snapshot found."])
    lines.append("")
    lines.append("## Active Stories")
    lines.append("")
    lines.extend(sprint_stories or ["- No active story rows found."])

    lines.extend(["", "## Recent ADRs", ""])
    for entry in newest(files, "adr"):
        lines.append(f"- `{entry.get('id') or ''}` {entry.get('status') or 'unknown'} - {entry.get('title') or entry['path']} ({entry['path']})")

    lines.extend(["", "## Recent Stories", ""])
    for entry in newest(files, "story"):
        ident = entry.get("id") or ""
        lines.append(f"- `{ident}` {entry.get('status') or 'unknown'} - {entry.get('title') or entry['path']} ({entry['path']})")

    lines.extend(
        [
            "",
            "## Read Policy",
            "",
            "1. Prefer this file first.",
            "2. Use `production/context/ccgs-index.json` to choose exact source files.",
            "3. Generate a story pack with `ccgs_story_context.py` before running readiness/dev/done on a story.",
            "4. Open full GDD/ADR/QA documents only when the index or story pack points to them.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate compact CCGS current-context memo.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--output", default="", help="Output path relative to repo when --write is used.")
    parser.add_argument("--write", action="store_true", help="Write memo to production/context/current-context.md.")
    args = parser.parse_args()

    root = find_root(Path(args.repo))
    index = build_index(root)
    output = args.output or DEFAULT_OUTPUT_TEMPLATE.format(data_dir=index["data_dir"])
    memo = render_context(root, index)

    if args.write:
        out_path = root / output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(memo, encoding="utf-8")
        print(out_path.relative_to(root).as_posix())
    else:
        print(memo, end="")


if __name__ == "__main__":
    main()
