#!/usr/bin/env python3
"""Recommend the smallest useful CCGS context set for a task.

The router prints paths, reasons, and short read commands instead of expanding
documents into the conversation. Agents can then read only the relevant files
for the current request.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_MAX_FILES = 12
SEARCH_READ_LIMIT = 6000


DEFAULT_ROUTES = [
    {
        "name": "workflow-git",
        "terms": ["branch", "分支", "线程", "merge", "合并", "worktree", "hook", "自动化"],
        "paths": [
            ".ccgs-core/docs/context-management.md",
            ".ccgs-core/docs/coordination-rules.md",
        ],
        "roles": ["producer", "devops-engineer"],
    },
    {
        "name": "qa-test",
        "terms": ["qa", "测试", "test", "evidence", "证据", "验收", "bug", "回归"],
        "paths": [
            ".ccgs-core/docs/coding-standards.md",
            "{data_dir}/production/tracking/bug-tracker.md",
        ],
        "roles": ["qa-lead"],
    },
    {
        "name": "ui-ux",
        "terms": ["ui", "ux", "界面", "交互", "菜单", "hud", "按钮", "屏幕"],
        "paths": [
            ".ccgs-core/docs/technical-preferences.md",
        ],
        "roles": ["ui-programmer", "ux-designer"],
    },
    {
        "name": "art-assets",
        "terms": ["art", "asset", "assets", "素材", "美术", "贴图", "图片", "音效"],
        "paths": [
            ".ccgs-core/docs/technical-preferences.md",
        ],
        "roles": ["art-director", "technical-artist", "sound-designer"],
    },
    {
        "name": "combat",
        "terms": ["combat", "战斗", "damage", "伤害", "enemy", "敌人", "skill", "技能"],
        "paths": [
            ".ccgs-core/docs/technical-preferences.md",
        ],
        "roles": ["gameplay-programmer", "game-designer"],
    },
    {
        "name": "economy-progression",
        "terms": ["economy", "经济", "数值", "progression", "成长", "resource", "资源", "掉落"],
        "paths": [
            ".ccgs-core/docs/technical-preferences.md",
        ],
        "roles": ["economy-designer", "gameplay-programmer"],
    },
    {
        "name": "narrative-world",
        "terms": ["narrative", "叙事", "world", "世界观", "角色", "剧情", "dialogue", "对话"],
        "paths": [
            ".ccgs-core/docs/technical-preferences.md",
        ],
        "roles": ["narrative-director", "writer", "world-builder"],
    },
    {
        "name": "release-liveops",
        "terms": ["release", "发布", "上线", "patch", "补丁", "live ops", "运营", "版本"],
        "paths": [
            ".ccgs-core/docs/context-management.md",
        ],
        "roles": ["release-manager", "live-ops-designer"],
    },
    {
        "name": "token-context",
        "terms": ["context", "上下文", "token", "额度", "消耗", "压缩", "compact"],
        "paths": [
            ".ccgs-core/docs/context-management.md",
            ".ccgs-core/scripts/workflow/archive-session-state.py",
        ],
        "roles": ["producer", "tools-programmer"],
    },
]


SLASH_SKILL_NAMES = {
    "architecture-decision",
    "architecture-review",
    "asset-audit",
    "asset-spec",
    "brainstorm",
    "bug-report",
    "code-review",
    "create-architecture",
    "create-epics",
    "create-stories",
    "dev-story",
    "gate-check",
    "project-stage-detect",
    "qa-plan",
    "release-checklist",
    "sprint-plan",
    "sprint-status",
    "story-done",
}


@dataclass
class Recommendation:
    path: str
    reason: str
    priority: int
    lines: int = 0
    exists: bool = True


def run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip()


def find_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() and (candidate / ".ccgs-core").exists():
            return candidate
    raise SystemExit("ccgs-context-router: not inside a CCGS project root")


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
    if not path.exists() or not path.is_file():
        return 0
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def format_path(template: str, data_dir: str, core_dir: str) -> str:
    return template.format(data_dir=data_dir, core_dir=core_dir).replace("\\", "/")


def add_rec(
    recs: dict[str, Recommendation],
    root: Path,
    rel: str,
    reason: str,
    priority: int,
    include_missing: bool = True,
) -> None:
    norm = rel.replace("\\", "/")
    path = root / norm
    exists = path.exists()
    if not exists and not include_missing:
        return
    rec = Recommendation(
        path=norm,
        reason=reason,
        priority=priority,
        lines=count_lines(path),
        exists=exists,
    )
    old = recs.get(norm)
    if old is None or priority < old.priority:
        recs[norm] = rec


def text_terms(query: str) -> set[str]:
    lowered = query.lower()
    words = set(re.findall(r"[a-z0-9_-]{2,}", lowered))
    words.update(part for part in re.split(r"\s+", lowered) if len(part) >= 2)
    return words


def infer_mode(query: str, changed_files: list[str]) -> str:
    lowered = query.lower()
    if any(term in lowered for term in ["hotfix", "崩溃", "紧急", "线上"]):
        return "Hotfix"
    if any(term in lowered for term in ["新系统", "架构", "proposal", "epic", "重构", "全新"]):
        return "Full"
    if any(term in lowered for term in ["小改", "文案", "typo", "micro"]):
        return "Micro"
    if 0 < len(changed_files) <= 2:
        return "Micro candidate"
    return "Lean"


def recent_status(root: Path) -> dict[str, object]:
    status_lines = run(["git", "status", "--short", "--untracked-files=all"], root).splitlines()
    return {
        "branch": run(["git", "branch", "--show-current"], root),
        "status_count": len(status_lines),
        "status_preview": status_lines[:12],
        "recent_commits": run(["git", "log", "--oneline", "-5"], root).splitlines(),
    }


def bug_count(root: Path, data_dir: str) -> int:
    bug_tracker = root / data_dir / "production/tracking/bug-tracker.md"
    if not bug_tracker.exists():
        return 0
    text = bug_tracker.read_text(encoding="utf-8", errors="ignore")
    return len(re.findall(r"[🔴🟡]|status:\s*(open|in progress|active)", text, flags=re.I))


def discover_named_paths(query: str) -> list[str]:
    pattern = r"(?:(?:\.ccgs-core|CCGS-Data|tools|src|js|tests|assets|docs)/[^\s`'\"，。；、)]+)"
    paths: list[str] = []
    for match in re.findall(pattern, query):
        paths.append(match.rstrip(".,;:）]"))
    return paths


def find_skill_paths(root: Path, query: str, core_dir: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    names = set(re.findall(r"[$/]([a-z0-9-]+)", query.lower()))
    names.update(name for name in SLASH_SKILL_NAMES if name in query.lower())
    for name in sorted(names):
        candidates = [
            f"{core_dir}/workflows/skills/{name}/SKILL.md",
            f"{core_dir}/workflows/Tier1-Directors/{name}.md",
            f"{core_dir}/workflows/Tier2-Leads/{name}.md",
            f"{core_dir}/workflows/Tier3-Specialists/{name}.md",
        ]
        for rel in candidates:
            if (root / rel).exists():
                found.append((rel, f"命中工作流或角色 `{name}`"))
                break
    return found


def active_sprint_paths(root: Path, data_dir: str) -> list[str]:
    sprint_status = root / data_dir / "production/sprint-status.yaml"
    if not sprint_status.exists():
        return []
    text = sprint_status.read_text(encoding="utf-8", errors="ignore")
    matches = re.findall(r"(?:current_sprint|active_sprint|sprint):\s*['\"]?([A-Za-z0-9_.-]+)", text)
    paths: list[str] = [f"{data_dir}/production/sprint-status.yaml"]
    for sprint in matches[:2]:
        rel = f"{data_dir}/production/sprints/{sprint}.md"
        if (root / rel).exists():
            paths.append(rel)
    return paths


def load_local_routes(root: Path, data_dir: str) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    candidates = [
        root / ".ccgs-core/context-router-rules.json",
        root / data_dir / "production/context-router-rules.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, list):
            routes.extend(item for item in loaded if isinstance(item, dict))
    return routes


def iter_search_roots(root: Path, data_dir: str) -> Iterable[Path]:
    for rel in [
        f"{data_dir}/design",
        f"{data_dir}/production/epics",
        f"{data_dir}/production/sprints",
        f"{data_dir}/production/proposals",
        f"{data_dir}/production/qa/evidence",
        f"{data_dir}/production/tracking",
        f"{data_dir}/project-docs",
    ]:
        path = root / rel
        if path.exists():
            yield path


def score_project_docs(root: Path, data_dir: str, query: str, limit: int) -> list[Recommendation]:
    terms = [term for term in text_terms(query) if len(term) >= 3]
    if not terms:
        return []

    scored: list[tuple[int, str, Path]] = []
    for search_root in iter_search_roots(root, data_dir):
        for path in search_root.rglob("*.md"):
            rel = path.relative_to(root).as_posix()
            if "/session-state/archive/" in rel:
                continue
            haystack = rel.lower()
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:SEARCH_READ_LIMIT].lower()
            except OSError:
                head = ""
            score = 0
            for term in terms:
                term_l = term.lower()
                if term_l in haystack:
                    score += 6
                if term_l in head:
                    score += 2
            if score:
                scored.append((score, rel, path))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        Recommendation(path=rel, reason=f"内容/路径关键词匹配，score={score}", priority=40, lines=count_lines(path))
        for score, rel, path in scored[:limit]
    ]


def build_recommendations(
    root: Path,
    query: str,
    max_files: int,
    include_search: bool,
) -> tuple[list[Recommendation], dict[str, object]]:
    env = parse_env(root / ".ccgs-core/ccgs.env")
    data_dir = env.get("DATA_DIR", "CCGS-Data")
    core_dir = env.get("CORE_DIR", ".ccgs-core")
    status = recent_status(root)
    recs: dict[str, Recommendation] = {}

    add_rec(recs, root, f"{core_dir}/workflows/pipeline-core.md", "CCGS 入口；确认 Phase 0/模式即可", 0)
    add_rec(recs, root, f"{core_dir}/ccgs.env", "读取 DATA_DIR/CORE_DIR，避免硬编码项目数据目录", 1)
    add_rec(recs, root, f"{data_dir}/production/session-state/active.md", "会话真源；优先读最近状态而非全部历史", 2)

    lowered = query.lower()
    changed_files = [line[3:] if len(line) > 3 else line for line in status["status_preview"]]
    mode = infer_mode(query, changed_files)

    code_terms = ["代码", "实现", "修改", "fix", "feat", "bug", "test", "测试", "重构", "开发"]
    if any(term in lowered for term in code_terms):
        add_rec(recs, root, f"{core_dir}/docs/technical-preferences.md", "代码变更前必读技术偏好", 5)
        add_rec(recs, root, f"{core_dir}/docs/coding-standards.md", "代码变更前必读编码/测试标准", 6)

    for rel in discover_named_paths(query):
        add_rec(recs, root, rel, "用户请求中显式提到的路径", 3)

    for rel in active_sprint_paths(root, data_dir):
        add_rec(recs, root, rel, "当前 Sprint 状态入口", 10)

    for rel, reason in find_skill_paths(root, query, core_dir):
        add_rec(recs, root, rel, reason, 8)

    matched_rules: list[str] = []
    for rule in [*DEFAULT_ROUTES, *load_local_routes(root, data_dir)]:
        terms = rule.get("terms", [])
        if not isinstance(terms, list):
            continue
        if not any(str(term).lower() in lowered for term in terms):
            continue

        name = str(rule.get("name", "local-route"))
        matched_rules.append(name)
        paths = rule.get("paths", [])
        if not isinstance(paths, list):
            paths = []
        for rel_template in paths:
            rel = format_path(str(rel_template), data_dir, core_dir)
            add_rec(recs, root, rel, f"任务命中 `{name}` 关键词", 12, include_missing=False)
        roles = rule.get("roles", [])
        if not isinstance(roles, list):
            roles = []
        for role in roles:
            for prefix in ("Tier1-Directors", "Tier2-Leads", "Tier3-Specialists"):
                rel = f"{core_dir}/workflows/{prefix}/{role}.md"
                if (root / rel).exists():
                    add_rec(recs, root, rel, f"建议角色 `{role}`", 14)
                    break

    if include_search:
        for rec in score_project_docs(root, data_dir, query, max_files):
            if rec.path not in recs:
                recs[rec.path] = rec

    ordered = sorted(recs.values(), key=lambda rec: (rec.priority, rec.path))
    diagnostics = {
        "branch": status["branch"],
        "status_count": status["status_count"],
        "bug_count": bug_count(root, data_dir),
        "recent_commits": status["recent_commits"],
        "matched_rules": matched_rules,
        "suggested_mode": mode,
        "data_dir": data_dir,
        "core_dir": core_dir,
    }
    return ordered[:max_files], diagnostics


def read_command_for(rec: Recommendation) -> str:
    quoted = rec.path
    if rec.path.endswith("production/session-state/active.md"):
        return f"tail -n 120 {quoted}"
    if rec.path.endswith("pipeline-core.md"):
        return f"sed -n '1,140p' {quoted}"
    if rec.path.endswith("sprint-status.yaml"):
        return f"sed -n '1,160p' {quoted}"
    return f"sed -n '1,220p' {quoted}"


def print_markdown(recs: Iterable[Recommendation], diagnostics: dict[str, object], root: Path) -> None:
    print("CCGS Context Router")
    print(f"- Branch: {diagnostics['branch'] or '(detached)'}")
    print(f"- Working tree changes: {diagnostics['status_count']}")
    print(f"- Active bugs estimate: {diagnostics['bug_count']}")
    print(f"- Suggested mode: {diagnostics['suggested_mode']}")
    if diagnostics["matched_rules"]:
        print(f"- Matched routes: {', '.join(diagnostics['matched_rules'])}")
    print("\n## Recommended Reads")
    for idx, rec in enumerate(recs, start=1):
        exists = "ok" if rec.exists else "missing"
        print(f"{idx}. `{rec.path}` ({rec.lines} lines, {exists}) - {rec.reason}")
    print("\n## Recent Commits")
    for commit in diagnostics["recent_commits"][:5]:
        print(f"- {commit}")
    print("\n## Read Commands")
    for rec in recs:
        if rec.exists:
            print(read_command_for(rec))


def main() -> None:
    parser = argparse.ArgumentParser(description="Recommend minimal CCGS context files for a task.")
    parser.add_argument("query", nargs="*", help="Task/request text to route.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="Maximum recommended files.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--no-search", action="store_true", help="Disable lightweight keyword scan across project docs.")
    args = parser.parse_args()

    root = find_root(Path(args.repo))
    query = " ".join(args.query).strip()
    recs, diagnostics = build_recommendations(root, query, args.max_files, include_search=not args.no_search)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "diagnostics": diagnostics,
                    "recommendations": [asdict(rec) for rec in recs],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_markdown(recs, diagnostics, root)


if __name__ == "__main__":
    main()
