#!/usr/bin/env python3
"""Archive old CCGS session-state sections and keep active.md small."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re


SECTION_RE = re.compile(r"(?m)^## ")


def find_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() and (candidate / ".ccgs-core").exists():
            return candidate
    raise SystemExit("archive-session-state: not inside a CCGS project root")


def parse_env(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r'^([A-Z_]+)="?([^"#]+)"?', line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def split_sections(text: str) -> tuple[str, list[str]]:
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return text.strip(), []
    preamble = text[: matches[0].start()].strip()
    sections: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end].strip()
        if section:
            sections.append(section)
    return preamble, sections


def archive_header(source: Path, kept: int, archived: int) -> str:
    stamp = datetime.now().isoformat(timespec="seconds")
    return (
        f"\n\n---\n\n"
        f"## Session State Archive Run - {stamp}\n"
        f"- Source: `{source.as_posix()}`\n"
        f"- Kept in active.md: {kept} sections\n"
        f"- Archived sections: {archived}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive old CCGS session-state sections.")
    parser.add_argument("--repo", default=".", help="Project repository path.")
    parser.add_argument("--active", default=None, help="Path to active session state relative to repo.")
    parser.add_argument("--archive-dir", default=None, help="Archive directory relative to repo.")
    parser.add_argument("--keep", type=int, default=10, help="Number of newest top-level sections to keep.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned changes without writing files.")
    parser.add_argument("--brief", action="store_true", help="Print a one-line summary.")
    args = parser.parse_args()

    if args.keep < 1:
        raise SystemExit("archive-session-state: --keep must be >= 1")

    root = find_root(Path(args.repo))
    env = parse_env(root / ".ccgs-core/ccgs.env")
    data_dir = env.get("DATA_DIR", "ccgs-data")
    active_rel = args.active or f"{data_dir}/production/session-state/active.md"
    archive_rel = args.archive_dir or f"{data_dir}/production/session-state/archive"

    active_path = root / active_rel
    archive_dir = root / archive_rel
    if not active_path.exists():
        raise SystemExit(f"archive-session-state: active file not found: {active_path}")

    original = active_path.read_text(encoding="utf-8")
    preamble, sections = split_sections(original)
    keep_sections = sections[: args.keep]
    archive_sections = sections[args.keep :]

    if not archive_sections:
        message = f"active.md already compact: sections={len(sections)} keep={args.keep}"
        print(message if args.brief else f"[archive-session-state] {message}")
        return

    archive_path = archive_dir / f"{datetime.now().strftime('%Y-%m')}.md"

    if not args.dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_payload = archive_header(active_path.relative_to(root), len(keep_sections), len(archive_sections))
        archive_payload += "\n\n".join(archive_sections).rstrip() + "\n"
        with archive_path.open("a", encoding="utf-8") as handle:
            handle.write(archive_payload)

        note = (
            "<!-- SESSION-STATE: compacted by .ccgs-core/scripts/workflow/archive-session-state.py; "
            f"older entries archived in {archive_path.relative_to(root).as_posix()} -->"
        )
        active_parts = [part for part in [note, preamble, "\n\n".join(keep_sections).strip()] if part]
        active_path.write_text("\n\n".join(active_parts).rstrip() + "\n", encoding="utf-8")

    summary = (
        f"kept={len(keep_sections)} archived={len(archive_sections)} "
        f"active={active_path.relative_to(root).as_posix()} archive={archive_path.relative_to(root).as_posix()}"
    )
    if args.brief:
        print(summary)
    else:
        print(f"[archive-session-state] {summary}")


if __name__ == "__main__":
    main()
