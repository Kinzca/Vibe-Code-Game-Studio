#!/usr/bin/env python3
"""Build a compact machine-readable index for CCGS project documents.

The index is intentionally shallow: it records paths, titles, statuses,
references, and line counts so agents can choose what to read before opening
large documents.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INDEX_SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_TEMPLATE = "{data_dir}/production/context/ccgs-index.json"


SCAN_TARGETS = [
    ("architecture", "{data_dir}/project-docs/architecture", ["*.md", "*.yaml", "*.yml"]),
    ("engine-reference", "{data_dir}/project-docs/engine-reference", ["*.md", "*.yaml", "*.yml"]),
    ("guide", "{data_dir}/project-docs/guides", ["*.md", "*.json", "*.yaml", "*.yml"]),
    ("gdd", "{data_dir}/design/gdd", ["*.md"]),
    ("design", "{data_dir}/design", ["*.md"]),
    ("sprint", "{data_dir}/production/sprints", ["*.md"]),
    ("sprint-status", "{data_dir}/production", ["sprint-status.yaml", "sprint-status.yml"]),
    ("epic-story", "{data_dir}/production/epics", ["*.md", "*.yaml", "*.yml"]),
    ("qa-plan", "{data_dir}/production/qa/plans", ["*.md", "*.yaml", "*.yml"]),
    ("qa-evidence", "{data_dir}/production/qa/evidence", ["*.md", "*.yaml", "*.yml"]),
    ("qa-smoke", "{data_dir}/production/qa/smoke", ["*.md", "*.yaml", "*.yml"]),
    ("tracking", "{data_dir}/production/tracking", ["*.md", "*.yaml", "*.yml"]),
    ("context", "{data_dir}/production/context", ["*.md", "*.json", "*.yaml", "*.yml"]),
]


def find_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".ccgs-core").exists():
            return candidate
    raise SystemExit("ccgs_context_index: not inside a CCGS project root")


def parse_env(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r'^([A-Z_]+)="?([^"#]+)"?', line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def read_text(path: Path, limit: int = 8000) -> str:
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


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.+?)\s*$", line)
        if item:
            values[item.group(1)] = item.group(2).strip().strip("\"'")
    return values


def status_from_text(text: str, fm: dict[str, str]) -> str:
    if "status" in fm:
        return fm["status"]
    patterns = [
        r"Status:\s*([A-Za-z][A-Za-z0-9 _/-]+)",
        r"##\s*Status\s*\n+\s*Status:\s*([A-Za-z][A-Za-z0-9 _/-]+)",
        r"状态[:：]\s*([A-Za-z\u4e00-\u9fff][A-Za-z0-9 _/\-\u4e00-\u9fff]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip()
    return ""


def infer_doc_type(path: Path, rel: str, target_type: str, text: str) -> str:
    name = path.name.lower()
    if re.match(r"adr[-_]\d+", name) or re.match(r"adr[-_]\d+", first_heading(text).lower()):
        return "adr"
    if name.startswith("story-") or "/story-" in rel.lower():
        return "story"
    if name == "epic.md":
        return "epic"
    if "sprint-status" in name:
        return "sprint-status"
    if target_type != "design" and target_type:
        return target_type
    return "doc"


def collect_refs(text: str) -> dict[str, list[str]]:
    return {
        "adrs": sorted(set(re.findall(r"\bADR-\d{4}\b", text))),
        "trs": sorted(set(re.findall(r"\bTR-[A-Za-z0-9_.-]+\b", text))),
        "stories": sorted(set(re.findall(r"\bS\d+-\d+\b", text))),
        "gdds": sorted(set(re.findall(r"CCGS-Data/design/gdd/[A-Za-z0-9_.\-/]+\.md", text))),
    }


def adr_id_from(path: Path, title: str) -> str:
    source = f"{path.name} {title}"
    match = re.search(r"\bADR[-_](\d{4})\b", source, flags=re.I)
    return f"ADR-{match.group(1)}" if match else ""


def story_id_from(path: Path, text: str, fm: dict[str, str]) -> str:
    if "id" in fm:
        return fm["id"]
    filename_match = re.search(r"story[-_](\d+)", path.name, flags=re.I)
    if filename_match and fm.get("sprint"):
        return f"S{fm['sprint']}-{int(filename_match.group(1)):02d}"
    match = re.search(r"\bS\d+-\d+\b", f"{path.name}\n{text[:1200]}")
    return match.group(0) if match else ""


def iter_files(root: Path, data_dir: str) -> list[tuple[str, Path, str]]:
    seen: set[Path] = set()
    files: list[tuple[str, Path, str]] = []
    for target_type, folder_template, patterns in SCAN_TARGETS:
        folder = root / folder_template.format(data_dir=data_dir)
        if not folder.exists():
            continue
        for pattern in patterns:
            matches = folder.rglob(pattern) if "*" in pattern else folder.glob(pattern)
            for path in matches:
                if not path.is_file() or path in seen:
                    continue
                rel = path.relative_to(root).as_posix()
                if "/archive/" in rel or "/.git/" in rel:
                    continue
                seen.add(path)
                files.append((target_type, path, rel))
    return sorted(files, key=lambda item: item[2])


def build_index(root: Path) -> dict[str, Any]:
    env = parse_env(root / ".ccgs-core/ccgs.env")
    data_dir = env.get("DATA_DIR", "CCGS-Data")
    core_dir = env.get("CORE_DIR", ".ccgs-core")
    entries: list[dict[str, Any]] = []

    for target_type, path, rel in iter_files(root, data_dir):
        text = read_text(path)
        fm = frontmatter(text)
        title = first_heading(text) or fm.get("title", "")
        doc_type = infer_doc_type(path, rel, target_type, text)
        refs = collect_refs(text)
        entry: dict[str, Any] = {
            "path": rel,
            "type": doc_type,
            "title": title,
            "status": status_from_text(text, fm),
            "line_count": count_lines(path),
            "mtime": int(path.stat().st_mtime),
            "refs": refs,
        }
        if doc_type == "adr":
            entry["id"] = adr_id_from(path, title)
        elif doc_type == "story":
            entry["id"] = story_id_from(path, text, fm)
            if "sprint" in fm:
                entry["sprint"] = fm["sprint"]
            if "epic" in fm:
                entry["epic"] = fm["epic"]
        elif doc_type == "gdd":
            entry["id"] = path.stem
        entries.append(entry)

    counts = Counter(entry["type"] for entry in entries)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "data_dir": data_dir,
        "core_dir": core_dir,
        "counts": dict(sorted(counts.items())),
        "files": entries,
    }


def write_or_print(index: dict[str, Any], root: Path, output: str, write: bool) -> None:
    data = json.dumps(index, ensure_ascii=False, indent=2)
    if write:
        out_path = root / output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(data + "\n", encoding="utf-8")
        print(out_path.relative_to(root).as_posix())
    else:
        print(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build compact CCGS document index.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--output", default="", help="Output path relative to repo when --write is used.")
    parser.add_argument("--write", action="store_true", help="Write index to CCGS-Data/production/context/ccgs-index.json.")
    parser.add_argument("--summary", action="store_true", help="Print only counts and output path.")
    args = parser.parse_args()

    root = find_root(Path(args.repo))
    index = build_index(root)
    output = args.output or DEFAULT_OUTPUT_TEMPLATE.format(data_dir=index["data_dir"])

    if args.summary:
        print(json.dumps({"output": output, "counts": index["counts"]}, ensure_ascii=False, indent=2))
        return

    write_or_print(index, root, output, args.write)


if __name__ == "__main__":
    main()
