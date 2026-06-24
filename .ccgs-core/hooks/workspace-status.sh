#!/bin/bash
# ============================================================================
# workspace-status.sh — CCGS 上下文探针（通用引擎，零硬编码）
# ============================================================================
# 用途: 一次执行输出格式固定的项目状态快照
# 供 AI 会话初始化时调用，替代逐步读取多个文件
# 所有可变路径从 hooks-config.yaml 的 workspace_status 段读取
#
# 用法:
#   bash .ccgs-core/hooks/workspace-status.sh
#
# 输出格式（3-4 行，固定结构）:
#   📋 CCGS 状态: [分支] | [最近提交] | Bug: N | 债务: N
#   📂 最近修改: [文件1, 文件2, ...]
#   🔄 当前 Sprint: [sprint 文件名 或 N/A]
#   ⚡ 检测到上次会话状态: [路径]  (仅在 active.md 存在时输出)
#
# 退出码: 0（始终成功，降级输出 N/A 或 0）
# 兼容性: POSIX (macOS / Linux)，纯 Bash，无外部依赖
# ============================================================================

# --- 定位项目根目录和配置文件 ---
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
CONFIG="$PROJECT_ROOT/.ccgs-core/hooks/hooks-config.yaml"

# --- 引入共享 YAML 解析库 ---
source "$PROJECT_ROOT/.ccgs-core/hooks/lib/yaml-parser.sh"

# ============================================================================
# 从配置读取所有可变路径（附降级默认值）
# ============================================================================
BUG_FILE=$(yaml_get_value "$CONFIG" "workspace_status" "bug_tracker" "ccgs-data/production/tracking/bug-tracker.md")
DEBT_FILE=$(yaml_get_value "$CONFIG" "workspace_status" "tech_debt" "ccgs-data/production/tracking/tech-debt.md")
SPRINT_GLOB=$(yaml_get_value "$CONFIG" "workspace_status" "sprint_glob" "ccgs-data/production/sprints/sprint-*.md")
SESSION_FILE=$(yaml_get_value "$CONFIG" "workspace_status" "session_state" "ccgs-data/production/session-state/active.md")
MARKERS=$(yaml_get_value "$CONFIG" "workspace_status" "status_markers" "🔴|🟡")

# ============================================================================
# 1. 基础状态行
# ============================================================================
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "N/A")
LAST_COMMIT=$(git log --oneline -1 2>/dev/null || echo "无提交")

# Bug 计数：统计包含活跃状态标记的行数
if [ -f "$PROJECT_ROOT/$BUG_FILE" ]; then
    BUG_COUNT=$(grep -cE "$MARKERS" "$PROJECT_ROOT/$BUG_FILE" 2>/dev/null || echo 0)
else
    BUG_COUNT=0
fi

# 技术债务计数
if [ -f "$PROJECT_ROOT/$DEBT_FILE" ]; then
    DEBT_COUNT=$(grep -cE "$MARKERS" "$PROJECT_ROOT/$DEBT_FILE" 2>/dev/null || echo 0)
else
    DEBT_COUNT=0
fi

echo "📋 CCGS 状态: $BRANCH | $LAST_COMMIT | Bug: $BUG_COUNT | 债务: $DEBT_COUNT"

# ============================================================================
# 2. 最近修改文件
# ============================================================================
RECENT=$(git diff --name-only HEAD~1 2>/dev/null | head -5 | tr '\n' ', ' | sed 's/,$//')
if [ -z "$RECENT" ]; then
    RECENT="无变更"
fi
echo "📂 最近修改: $RECENT"

# ============================================================================
# 3. Sprint 状态
# ============================================================================
# 使用 eval 展开 glob（因为从配置读取的是字符串形式的 glob）
SPRINT_FILE=""
eval "SPRINT_FILES=($PROJECT_ROOT/$SPRINT_GLOB)" 2>/dev/null
if [ ${#SPRINT_FILES[@]} -gt 0 ] && [ -f "${SPRINT_FILES[0]}" ]; then
    # 按修改时间排序取最新
    SPRINT_FILE=$(ls -t "${SPRINT_FILES[@]}" 2>/dev/null | head -1)
    if [ -n "$SPRINT_FILE" ]; then
        SPRINT_FILE=$(basename "$SPRINT_FILE")
    fi
fi
echo "🔄 当前 Sprint: ${SPRINT_FILE:-N/A}"

# ============================================================================
# 4. 会话恢复检测
# ============================================================================
if [ -f "$PROJECT_ROOT/$SESSION_FILE" ]; then
    # 读取最后一条有意义的行作为摘要
    LAST_TASK=$(grep -E '^- Story:|^## Session' "$PROJECT_ROOT/$SESSION_FILE" 2>/dev/null | tail -1 | sed 's/^- //' | sed 's/^## //')
    if [ -n "$LAST_TASK" ]; then
        echo "⚡ 检测到上次会话状态: $SESSION_FILE ($LAST_TASK)"
    else
        echo "⚡ 检测到上次会话状态: $SESSION_FILE"
    fi
fi
