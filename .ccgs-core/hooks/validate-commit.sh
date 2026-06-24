#!/bin/bash
# ============================================================================
# validate-commit.sh — CCGS pre-commit 钩子（通用引擎，零硬编码）
# ============================================================================
# 用途: 作为 Git pre-commit 钩子，校验暂存文件和 commit message
# 所有校验参数从 hooks-config.yaml 读取
# 退出码: 0 = 允许提交, 1 = 阻塞提交
# 兼容性: POSIX (macOS / Linux)，无 grep -P
# ============================================================================

set -euo pipefail

# --- 定位项目根目录和配置文件 ---
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
CONFIG="$PROJECT_ROOT/.ccgs-core/hooks/hooks-config.yaml"

WARNINGS=""
ERRORS=""

# --- 引入共享 YAML 解析库 ---
source "$PROJECT_ROOT/.ccgs-core/hooks/lib/yaml-parser.sh"

# --- 检查配置文件是否存在 ---
if [ ! -f "$CONFIG" ]; then
    echo "⚠️  hooks-config.yaml 未找到 ($CONFIG)，跳过所有校验" >&2
    exit 0
fi

# --- 获取暂存文件列表 ---
STAGED=$(git diff --cached --name-only 2>/dev/null)
if [ -z "$STAGED" ]; then
    exit 0
fi

# ============================================================================
# 校验 1: Commit Message 格式
# ============================================================================
# 读取 commit message（从 .git/COMMIT_EDITMSG，pre-commit 阶段可能不可用）
# 注意: pre-commit 钩子在 message 写入之前触发，此检查移至 commit-msg 钩子更准确
# 但为了兼容 install-hooks.sh 的 commit-msg 模式，这里也保留一份

COMMIT_MSG_PATTERN=$(yaml_get_value "$CONFIG" "commit_message" "pattern" "")
# commit message 校验在 commit-msg 钩子中执行，此处仅做文件级检查

# ============================================================================
# 校验 2: GDD 文档完整性
# ============================================================================
GDD_PATH=$(yaml_get_value "$CONFIG" "gdd_validation" "path" "ccgs-data/design/gdd/")
GDD_FILES=$(echo "$STAGED" | grep -E "^${GDD_PATH}.*\\.md$" || true)

if [ -n "$GDD_FILES" ]; then
    # 读取必要章节列表
    SECTIONS=$(yaml_get_list "$CONFIG" "gdd_validation" "required_sections")
    
    while IFS= read -r gdd_file; do
        if [ -f "$PROJECT_ROOT/$gdd_file" ]; then
            while IFS= read -r section; do
                if [ -n "$section" ] && ! grep -qi "$section" "$PROJECT_ROOT/$gdd_file" 2>/dev/null; then
                    WARNINGS="${WARNINGS}\n  GDD: $gdd_file 缺少必要章节: $section"
                fi
            done <<< "$SECTIONS"
        fi
    done <<< "$GDD_FILES"
fi

# ============================================================================
# 校验 3: JSON 数据文件格式合法性
# ============================================================================
JSON_FILES=$(echo "$STAGED" | grep -E '\.json$' || true)

if [ -n "$JSON_FILES" ]; then
    # 查找可用的 Python 命令
    PYTHON_CMD=""
    for cmd in python3 python py; do
        if command -v "$cmd" >/dev/null 2>&1; then
            PYTHON_CMD="$cmd"
            break
        fi
    done

    if [ -n "$PYTHON_CMD" ]; then
        while IFS= read -r json_file; do
            if [ -f "$PROJECT_ROOT/$json_file" ]; then
                if ! "$PYTHON_CMD" -m json.tool "$PROJECT_ROOT/$json_file" > /dev/null 2>&1; then
                    ERRORS="${ERRORS}\n  JSON 格式错误: $json_file — 请修复语法后重新提交"
                fi
            fi
        done <<< "$JSON_FILES"
    else
        WARNINGS="${WARNINGS}\n  ⚠️ 未找到 Python，跳过 JSON 格式校验"
    fi
fi

# ============================================================================
# 校验 4: YAML 数据文件格式合法性
# ============================================================================
YAML_FILES=$(echo "$STAGED" | grep -E '\.(yaml|yml)$' || true)

if [ -n "$YAML_FILES" ]; then
    PYTHON_CMD=""
    for cmd in python3 python py; do
        if command -v "$cmd" >/dev/null 2>&1; then
            PYTHON_CMD="$cmd"
            break
        fi
    done

    if [ -n "$PYTHON_CMD" ]; then
        while IFS= read -r yaml_file; do
            if [ -f "$PROJECT_ROOT/$yaml_file" ]; then
                if ! "$PYTHON_CMD" -c "import yaml; yaml.safe_load(open('$PROJECT_ROOT/$yaml_file'))" 2>/dev/null; then
                    # 降级: 如果没有 PyYAML，尝试基本语法检查
                    if ! "$PYTHON_CMD" -c "
import sys
try:
    import yaml
    yaml.safe_load(open('$PROJECT_ROOT/$yaml_file'))
except ImportError:
    pass  # 无 PyYAML 模块，跳过
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
                        ERRORS="${ERRORS}\n  YAML 格式错误: $yaml_file"
                    fi
                fi
            fi
        done <<< "$YAML_FILES"
    fi
fi

# ============================================================================
# 校验 5: 调用资产命名检查
# ============================================================================
ASSETS_SCRIPT="$PROJECT_ROOT/.ccgs-core/hooks/validate-assets.sh"
if [ -x "$ASSETS_SCRIPT" ]; then
    ASSET_RESULT=$("$ASSETS_SCRIPT" "$STAGED" 2>&1) || true
    if [ -n "$ASSET_RESULT" ]; then
        WARNINGS="${WARNINGS}\n$ASSET_RESULT"
    fi
fi

# ============================================================================
# 输出结果
# ============================================================================

# 非阻塞警告
if [ -n "$WARNINGS" ]; then
    echo -e "=== CCGS Commit 校验警告 ===$WARNINGS\n================================" >&2
fi

# 阻塞性错误
if [ -n "$ERRORS" ]; then
    echo -e "=== ❌ CCGS Commit 校验失败（提交已阻塞）===$ERRORS\n=========================================" >&2
    exit 1
fi

exit 0
