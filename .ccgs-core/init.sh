#!/bin/bash
# ============================================================================
# init.sh — CCGS 框架一键初始化与维护工具
# ============================================================================
# 功能:
#   1. 校验/修复 CCGS-Data 目录骨架完整性
#   2. 重命名数据层目录（批量替换所有引用）
#   3. 生成 AI 工具入口配置文件（CLAUDE.md / GEMINI.md / .cursorrules / AGENTS.md）
#
# 用法:
#   bash .ccgs-core/init.sh                          # 校验目录骨架
#   bash .ccgs-core/init.sh --rename-data <新名称>   # 重命名数据层目录
#   bash .ccgs-core/init.sh --gen-entry <工具名>     # 生成入口文件
#   bash .ccgs-core/init.sh --link-codex-skills      # 映射 CCGS workflows 到 Codex Skills
#   bash .ccgs-core/init.sh --all                    # 全量初始化
#
# 兼容性: macOS / Linux，纯 Bash
# ============================================================================

set -euo pipefail

# --- 定位项目根目录 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/ccgs.env"

# --- 加载全局配置 ---
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    DATA_DIR="CCGS-Data"
    CORE_DIR=".ccgs-core"
fi

# ============================================================================
# 子命令: 校验并修复目录骨架
# ============================================================================
cmd_verify() {
    echo "🔍 校验 $DATA_DIR 目录骨架..."

    # 框架要求的完整目录清单
    REQUIRED_DIRS=(
        "$DATA_DIR/design/art"
        "$DATA_DIR/design/balance"
        "$DATA_DIR/design/gdd"
        "$DATA_DIR/design/levels"
        "$DATA_DIR/design/narrative/characters"
        "$DATA_DIR/design/quick-specs"
        "$DATA_DIR/design/registry"
        "$DATA_DIR/design/ux"
        "$DATA_DIR/production/changelogs"
        "$DATA_DIR/production/epics"
        "$DATA_DIR/production/milestones"
        "$DATA_DIR/production/playtests"
        "$DATA_DIR/production/proposals"
        "$DATA_DIR/production/qa/archive"
        "$DATA_DIR/production/qa/bugs"
        "$DATA_DIR/production/qa/evidence"
        "$DATA_DIR/production/qa/playtests"
        "$DATA_DIR/production/qa/smoke-tests"
        "$DATA_DIR/production/releases"
        "$DATA_DIR/production/retrospectives"
        "$DATA_DIR/production/session-logs"
        "$DATA_DIR/production/session-state"
        "$DATA_DIR/production/sprints"
        "$DATA_DIR/production/tracking"
        "$DATA_DIR/project-docs/architecture"
        "$DATA_DIR/project-docs/engine-reference"
        "$DATA_DIR/project-docs/research"
    )

    MISSING=0
    CREATED=0

    for dir in "${REQUIRED_DIRS[@]}"; do
        FULL_PATH="$PROJECT_ROOT/$dir"
        if [ ! -d "$FULL_PATH" ]; then
            mkdir -p "$FULL_PATH"
            touch "$FULL_PATH/.gitkeep"
            echo "  ✅ 创建: $dir"
            ((CREATED++))
        fi
    done

    if [ $CREATED -eq 0 ]; then
        echo "  ✅ 所有 ${#REQUIRED_DIRS[@]} 个目录均已就位"
    else
        echo "  📦 修复完成: 创建了 $CREATED 个缺失目录"
    fi
}

# ============================================================================
# 子命令: 重命名数据层目录
# ============================================================================
cmd_rename_data() {
    local NEW_NAME="$1"
    local OLD_NAME="$DATA_DIR"

    if [ "$OLD_NAME" = "$NEW_NAME" ]; then
        echo "⚠️  新名称与当前名称相同: $OLD_NAME"
        exit 1
    fi

    if [ ! -d "$PROJECT_ROOT/$OLD_NAME" ]; then
        echo "❌ 当前数据目录不存在: $OLD_NAME"
        exit 1
    fi

    if [ -d "$PROJECT_ROOT/$NEW_NAME" ]; then
        echo "❌ 目标目录已存在: $NEW_NAME"
        exit 1
    fi

    echo "🔄 重命名数据层: $OLD_NAME → $NEW_NAME"

    # Step 1: 物理重命名目录
    mv "$PROJECT_ROOT/$OLD_NAME" "$PROJECT_ROOT/$NEW_NAME"
    echo "  ✅ 目录已移动"

    # Step 2: 批量替换 .ccgs-core 中所有引用
    echo "  🔍 替换 $CORE_DIR 中的路径引用..."
    local COUNT=0

    # 使用 find + sed 批量替换（兼容 macOS 和 Linux）
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS sed 需要 -i '' 语法
        find "$PROJECT_ROOT/$CORE_DIR" -type f \( -name "*.md" -o -name "*.yaml" -o -name "*.yml" -o -name "*.sh" \) \
            -exec grep -l "$OLD_NAME/" {} \; | while read -r file; do
            sed -i '' "s|$OLD_NAME/|$NEW_NAME/|g" "$file"
            ((COUNT++)) || true
        done
    else
        # Linux sed
        find "$PROJECT_ROOT/$CORE_DIR" -type f \( -name "*.md" -o -name "*.yaml" -o -name "*.yml" -o -name "*.sh" \) \
            -exec grep -l "$OLD_NAME/" {} \; | while read -r file; do
            sed -i "s|$OLD_NAME/|$NEW_NAME/|g" "$file"
            ((COUNT++)) || true
        done
    fi

    # Step 3: 更新 ccgs.env 中的 DATA_DIR
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s|^DATA_DIR=\"$OLD_NAME\"|DATA_DIR=\"$NEW_NAME\"|" "$ENV_FILE"
    else
        sed -i "s|^DATA_DIR=\"$OLD_NAME\"|DATA_DIR=\"$NEW_NAME\"|" "$ENV_FILE"
    fi

    echo "  ✅ ccgs.env 已更新: DATA_DIR=\"$NEW_NAME\""

    # 验证
    local REMAINING
    REMAINING=$(grep -rl "$OLD_NAME/" "$PROJECT_ROOT/$CORE_DIR" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$REMAINING" -eq 0 ]; then
        echo "  ✅ 零残留引用，替换完成"
    else
        echo "  ⚠️  仍有 $REMAINING 个文件包含旧路径，请手动检查"
    fi
}

# ============================================================================
# 子命令: 生成 AI 工具入口配置文件
# ============================================================================
cmd_gen_entry() {
    local TOOL="$1"

    case "$TOOL" in
        claude)
            # --- 生成 CLAUDE.md 入口文件 ---
            cat > "$PROJECT_ROOT/CLAUDE.md" << 'ENTRY_EOF'
# CCGS Framework — Claude Code Configuration

> Auto-generated by `init.sh`. Do not edit manually.

## Pipeline Core
@.ccgs-core/workflows/pipeline-core.md

## Technical Preferences
@.ccgs-core/docs/technical-preferences.md

## Coding Standards
@.ccgs-core/docs/coding-standards.md

## Coordination Rules
@.ccgs-core/docs/coordination-rules.md

## Context Management
@.ccgs-core/docs/context-management.md
ENTRY_EOF
            echo "  ✅ 已生成 CLAUDE.md"

            # --- 生成 .claude/ 符号链接桥接层 ---
            echo "  🔗 构建 .claude/ 符号链接桥接层..."

            # 清理旧的桥接层（如果存在）
            rm -rf "$PROJECT_ROOT/.claude"
            mkdir -p "$PROJECT_ROOT/.claude"

            # agents/ → 合并 Tier1 + Tier2 + Tier3（Claude Code 需要平铺）
            mkdir -p "$PROJECT_ROOT/.claude/agents"
            for tier_dir in Tier1-Directors Tier2-Leads Tier3-Specialists; do
                if [ -d "$PROJECT_ROOT/$CORE_DIR/workflows/$tier_dir" ]; then
                    for agent_file in "$PROJECT_ROOT/$CORE_DIR/workflows/$tier_dir"/*.md; do
                        if [ -f "$agent_file" ]; then
                            local basename=$(basename "$agent_file")
                            ln -sf "../../$CORE_DIR/workflows/$tier_dir/$basename" "$PROJECT_ROOT/.claude/agents/$basename"
                        fi
                    done
                fi
            done
            local AGENT_COUNT=$(ls "$PROJECT_ROOT/.claude/agents/" 2>/dev/null | wc -l | tr -d ' ')
            echo "    → agents/: $AGENT_COUNT 个 Agent 已链接"

            # skills/ → 符号链接整个 skills 目录下的每个 Skill
            mkdir -p "$PROJECT_ROOT/.claude/skills"
            if [ -d "$PROJECT_ROOT/$CORE_DIR/workflows/skills" ]; then
                for skill_dir in "$PROJECT_ROOT/$CORE_DIR/workflows/skills"/*/; do
                    if [ -d "$skill_dir" ]; then
                        local skill_name=$(basename "$skill_dir")
                        ln -sf "../../$CORE_DIR/workflows/skills/$skill_name" "$PROJECT_ROOT/.claude/skills/$skill_name"
                    fi
                done
            fi
            local SKILL_COUNT=$(ls -d "$PROJECT_ROOT/.claude/skills"/*/ 2>/dev/null | wc -l | tr -d ' ')
            echo "    → skills/: $SKILL_COUNT 个 Skill 已链接"

            # rules/ → 符号链接规则文件
            mkdir -p "$PROJECT_ROOT/.claude/rules"
            for rule_file in "$PROJECT_ROOT/$CORE_DIR/rules"/*.md; do
                if [ -f "$rule_file" ]; then
                    local basename=$(basename "$rule_file")
                    if [ "$basename" != "README.md" ]; then
                        ln -sf "../../$CORE_DIR/rules/$basename" "$PROJECT_ROOT/.claude/rules/$basename"
                    fi
                fi
            done
            local RULE_COUNT=$(ls "$PROJECT_ROOT/.claude/rules/" 2>/dev/null | wc -l | tr -d ' ')
            echo "    → rules/: $RULE_COUNT 个规则已链接"

            # hooks/ → 符号链接钩子脚本
            mkdir -p "$PROJECT_ROOT/.claude/hooks"
            for hook_file in "$PROJECT_ROOT/$CORE_DIR/hooks"/*.sh; do
                if [ -f "$hook_file" ]; then
                    local basename=$(basename "$hook_file")
                    ln -sf "../../$CORE_DIR/hooks/$basename" "$PROJECT_ROOT/.claude/hooks/$basename"
                fi
            done
            local HOOK_COUNT=$(ls "$PROJECT_ROOT/.claude/hooks/" 2>/dev/null | wc -l | tr -d ' ')
            echo "    → hooks/: $HOOK_COUNT 个钩子已链接"

            # docs/ → 符号链接文档目录
            ln -sf "../$CORE_DIR/docs" "$PROJECT_ROOT/.claude/docs"
            echo "    → docs/: 已链接"

            echo "  ✅ .claude/ 桥接层构建完成 — Claude Code 现在可以发现所有 Agent/Skill/Rule/Hook"
            ;;
        gemini)
            local BOOTSTRAP="$PROJECT_ROOT/$CORE_DIR/docs/ai-bootstrap.md"
            {
                echo "# CCGS Framework — Gemini Configuration"
                echo ""
                echo "> Auto-generated by \`init.sh\`. Do not edit manually."
                echo ""
                echo "## Pipeline Core"
                echo "Read and follow: \`.ccgs-core/workflows/pipeline-core.md\`"
                echo ""
                echo "## Technical Preferences"
                echo "Read: \`.ccgs-core/docs/technical-preferences.md\`"
                echo ""
                echo "## Coding Standards"
                echo "Read: \`.ccgs-core/docs/coding-standards.md\`"
                echo ""
                echo "---"
                echo ""
                if [ -f "$BOOTSTRAP" ]; then
                    cat "$BOOTSTRAP"
                fi
            } > "$PROJECT_ROOT/GEMINI.md"
            echo "  ✅ 已生成 GEMINI.md（含 AI 自举协议）"
            ;;
        cursor)
            local BOOTSTRAP="$PROJECT_ROOT/$CORE_DIR/docs/ai-bootstrap.md"
            {
                cat "$PROJECT_ROOT/$CORE_DIR/workflows/pipeline-core.md"
                echo ""
                echo "---"
                echo ""
                if [ -f "$BOOTSTRAP" ]; then
                    cat "$BOOTSTRAP"
                fi
            } > "$PROJECT_ROOT/.cursorrules"
            echo "  ✅ 已生成 .cursorrules（pipeline-core + AI 自举协议）"
            ;;
        codex)
            local BOOTSTRAP="$PROJECT_ROOT/$CORE_DIR/docs/ai-bootstrap.md"
            {
                echo "# CCGS Framework — Codex Configuration"
                echo ""
                echo "> Auto-generated by \`init.sh\`. Do not edit manually."
                echo ""
                echo "## Codex Entry"
                echo ""
                echo "This project uses the CCGS Universal framework. Treat this file as the project-level Codex instruction entry."
                echo ""
                echo "## Required Context"
                echo ""
                echo "- Read and follow \`.ccgs-core/workflows/pipeline-core.md\` at the start of CCGS work."
                echo "- Read \`.ccgs-core/docs/technical-preferences.md\` before code changes."
                echo "- Read \`.ccgs-core/docs/coding-standards.md\` before code changes."
                echo "- Read \`.ccgs-core/ccgs.env\` for \`DATA_DIR\` instead of hard-coding the project data directory."
                echo ""
                echo "## Output Language"
                echo ""
                echo "- 默认使用中文与用户沟通。"
                echo "- 所有由 CCGS/Codex 生成或写入的报告、状态摘要、QA 文档、Sprint 文档、Proposal、Changelog、Review 结论等项目文档，默认使用中文。"
                echo "- 只有当用户明确要求英文或目标平台/第三方模板强制英文时，才可改用英文，并在输出中说明原因。"
                echo "- 如果读取到英文模板，保留必要字段名即可；正文分析、结论、建议与问题列表仍应翻译或改写为中文。"
                echo ""
                echo "## Slash Command Compatibility"
                echo ""
                echo "Codex does not auto-register CCGS slash commands in the app menu. When the user types a command such as \`/dev-story CCGS-Data/production/epics/.../story.md\` or \`/gameplay-programmer\`, treat it as a textual CCGS workflow invocation:"
                echo ""
                echo "1. Resolve \`/<name>\` to \`.ccgs-core/workflows/skills/<name>/SKILL.md\`."
                echo "2. If no standard Skill exists, resolve \`/<name>\` to an Agent role file under \`.ccgs-core/workflows/Tier1-Directors/\`, \`.ccgs-core/workflows/Tier2-Leads/\`, or \`.ccgs-core/workflows/Tier3-Specialists/\`."
                echo "3. If still missing, resolve \`/<name>\` to any matching Markdown workflow under \`.ccgs-core/workflows/\`, such as \`pipeline-core.md\`."
                echo "4. Read the resolved file and execute its workflow or role instructions."
                echo "5. If no match exists, consult \`.ccgs-core/docs/skills-reference.md\` and \`.ccgs-core/docs/agent-roster.md\`, then report the unsupported command clearly."
                echo ""
                echo "If \`bash .ccgs-core/init.sh --link-codex-skills\` has been run and Codex has been restarted, CCGS workflows may also be invocable as Codex Skills. Standard skills use names like \`\$dev-story\` or \`\$code-review\`; Agent role wrappers use names like \`\$gameplay-programmer\` or \`\$technical-director\`."
                echo ""
                echo "## Agent Role Compatibility"
                echo ""
                echo "When a CCGS role is requested, read the matching definition under \`.ccgs-core/workflows/Tier1-Directors/\`, \`.ccgs-core/workflows/Tier2-Leads/\`, or \`.ccgs-core/workflows/Tier3-Specialists/\` before acting. If workflow wrappers are linked, the same roles may be invoked as Codex Skills such as \`\$gameplay-programmer\`."
                echo ""
                echo "## Codex Bridge Paths"
                echo ""
                echo "- \`.agents/workflows\` links to \`.ccgs-core/workflows\`."
                echo "- \`.agents/hooks\` links to \`.ccgs-core/hooks\`."
                echo ""
                echo "---"
                echo ""
                if [ -f "$BOOTSTRAP" ]; then
                    cat "$BOOTSTRAP"
                fi
            } > "$PROJECT_ROOT/AGENTS.md"
            echo "  ✅ 已生成 AGENTS.md（Codex 项目入口 + AI 自举协议）"

            # --- 生成 .agents/ 符号链接桥接层 ---
            echo "  🔗 构建 .agents/ 符号链接桥接层..."
            mkdir -p "$PROJECT_ROOT/.agents"

            for bridge_name in workflows hooks; do
                local bridge_path="$PROJECT_ROOT/.agents/$bridge_name"
                local bridge_target="../$CORE_DIR/$bridge_name"
                if [ -L "$bridge_path" ]; then
                    local current_target
                    current_target=$(readlink "$bridge_path" 2>/dev/null || true)
                    if [ "$current_target" = "$bridge_target" ]; then
                        echo "    → $bridge_name/: 已存在"
                    else
                        rm "$bridge_path"
                        ln -s "$bridge_target" "$bridge_path"
                        echo "    → $bridge_name/: 已更新"
                    fi
                elif [ -e "$bridge_path" ]; then
                    echo "    ⚠️  跳过 .agents/$bridge_name：已存在且不是符号链接"
                else
                    ln -s "$bridge_target" "$bridge_path"
                    echo "    → $bridge_name/: 已链接"
                fi
            done

            echo "  ✅ .agents/ 桥接层构建完成 — Codex 现在可以通过 AGENTS.md 感知 CCGS"
            ;;
        all)
            cmd_gen_entry "claude"
            echo ""
            cmd_gen_entry "gemini"
            echo ""
            cmd_gen_entry "cursor"
            echo ""
            cmd_gen_entry "codex"
            ;;
        *)
            echo "❌ 未知工具: $TOOL (可选: claude / gemini / cursor / codex / all)"
            exit 1
            ;;
    esac
}

# ============================================================================
# 子命令: 将 CCGS Skills 映射到 Codex 本地 Skills 目录
# ============================================================================
cmd_link_codex_skills() {
    local CODEX_ROOT="${CODEX_HOME:-$HOME/.codex}"
    local CODEX_SKILLS_DIR="$CODEX_ROOT/skills"
    local CCGS_WORKFLOWS_DIR="$PROJECT_ROOT/$CORE_DIR/workflows"
    local CCGS_SKILLS_DIR="$CCGS_WORKFLOWS_DIR/skills"
    local CREATED=0
    local UPDATED=0
    local CONVERTED=0
    local EXISTS=0
    local SKIPPED=0

    if [ ! -d "$CCGS_WORKFLOWS_DIR" ]; then
        echo "❌ CCGS workflows 目录不存在: $CCGS_WORKFLOWS_DIR"
        exit 1
    fi

    mkdir -p "$CODEX_SKILLS_DIR"

    echo "🔗 映射 CCGS workflows 到 Codex Skills"
    echo "   源目录: $CCGS_WORKFLOWS_DIR"
    echo "   目标目录: $CODEX_SKILLS_DIR"
    echo ""

    if [ -d "$CCGS_SKILLS_DIR" ]; then
        for skill_dir in "$CCGS_SKILLS_DIR"/*; do
            if [ ! -d "$skill_dir" ] || [ ! -f "$skill_dir/SKILL.md" ]; then
                continue
            fi

            local skill_name
            skill_name=$(basename "$skill_dir")
            local target_path="$CODEX_SKILLS_DIR/$skill_name"
            local target_created=0
            local target_converted=0

            if [ -L "$target_path" ]; then
                local current_target
                current_target=$(readlink "$target_path" 2>/dev/null || true)
                if [ "$current_target" = "$skill_dir" ]; then
                    rm "$target_path"
                    mkdir -p "$target_path"
                    CONVERTED=$((CONVERTED + 1))
                    target_converted=1
                else
                    echo "  ⚠️  $skill_name: 已存在不同符号链接，跳过"
                    SKIPPED=$((SKIPPED + 1))
                    continue
                fi
            elif [ -e "$target_path" ] && [ ! -d "$target_path" ]; then
                echo "  ⚠️  $skill_name: Codex 中已有同名文件，跳过"
                SKIPPED=$((SKIPPED + 1))
                continue
            elif [ ! -e "$target_path" ]; then
                mkdir -p "$target_path"
                CREATED=$((CREATED + 1))
                target_created=1
            fi

            local managed_source=""
            if [ -f "$target_path/.ccgs-source" ]; then
                managed_source=$(cat "$target_path/.ccgs-source" 2>/dev/null || true)
            fi

            local existing_skill_target=""
            if [ -L "$target_path/SKILL.md" ]; then
                existing_skill_target=$(readlink "$target_path/SKILL.md" 2>/dev/null || true)
            fi

            if [ -z "$managed_source" ] \
                && [ "$target_created" -eq 0 ] \
                && [ "$target_converted" -eq 0 ] \
                && [ "$existing_skill_target" != "$skill_dir/SKILL.md" ]; then
                echo "  ⚠️  $skill_name: Codex 中已有同名 Skill 目录，跳过"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi

            if [ -n "$managed_source" ] && [ "$managed_source" != "$skill_dir" ]; then
                echo "  ⚠️  $skill_name: 已由其他 CCGS 源管理，跳过"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi

            local item_skipped=0
            for item in "$skill_dir"/*; do
                [ -e "$item" ] || continue
                local item_name
                item_name=$(basename "$item")
                local link_path="$target_path/$item_name"

                if [ "$item_name" = "SKILL.md" ]; then
                    if [ -L "$link_path" ]; then
                        rm "$link_path"
                    fi

                    if [ -e "$link_path" ] && [ ! -f "$link_path" ]; then
                        echo "    ⚠️  $skill_name/SKILL.md 已存在且不是普通文件，保留"
                        item_skipped=1
                    elif ! cmp -s "$item" "$link_path" 2>/dev/null; then
                        cp "$item" "$link_path"
                        UPDATED=$((UPDATED + 1))
                    fi
                    continue
                fi

                if [ -L "$link_path" ]; then
                    local item_target
                    item_target=$(readlink "$link_path" 2>/dev/null || true)
                    if [ "$item_target" != "$item" ]; then
                        rm "$link_path"
                        ln -s "$item" "$link_path"
                        UPDATED=$((UPDATED + 1))
                    fi
                elif [ -e "$link_path" ]; then
                    echo "    ⚠️  $skill_name/$item_name 已存在且不是符号链接，保留"
                    item_skipped=1
                else
                    ln -s "$item" "$link_path"
                    UPDATED=$((UPDATED + 1))
                fi
            done

            printf "%s\n" "$skill_dir" > "$target_path/.ccgs-source"
            printf "skill\n" > "$target_path/.ccgs-kind"

            if [ "$item_skipped" -eq 1 ]; then
                echo "  ⚠️  $skill_name: 部分内容未覆盖"
            elif [ "$managed_source" = "$skill_dir" ]; then
                echo "  → $skill_name: 已存在"
                EXISTS=$((EXISTS + 1))
            elif [ "$target_converted" -eq 1 ]; then
                echo "  ✅ $skill_name"
            else
                echo "  ✅ $skill_name"
            fi
        done
    fi

    while IFS= read -r doc_path; do
        case "$doc_path" in
            "$CCGS_SKILLS_DIR"/*/SKILL.md)
                continue
                ;;
        esac

        if [ ! -f "$doc_path" ]; then
            continue
        fi

        local skill_name
        skill_name=$(basename "$doc_path" .md)
        local target_path="$CODEX_SKILLS_DIR/$skill_name"
        local target_created=0
        local target_converted=0
        local doc_kind="Workflow Document"
        local doc_instruction="Read and follow this workflow document for the user's requested work."

        case "$doc_path" in
            */Tier1-Directors/*)
                doc_kind="Tier 1 Director Agent"
                doc_instruction="Read the source role definition in full, then adopt exactly this strategic director role for the user's requested work."
                ;;
            */Tier2-Leads/*)
                doc_kind="Tier 2 Lead Agent"
                doc_instruction="Read the source role definition in full, then adopt exactly this lead role for the user's requested work."
                ;;
            */Tier3-Specialists/*)
                doc_kind="Tier 3 Specialist Agent"
                doc_instruction="Read the source role definition in full, then adopt exactly this specialist role for the user's requested work."
                ;;
            */pipeline-core.md)
                doc_kind="Pipeline Core"
                doc_instruction="Read the source workflow in full, then use it as the CCGS phase and gate protocol for the user's requested work."
                ;;
        esac

        if [ -L "$target_path" ]; then
            local current_target
            current_target=$(readlink "$target_path" 2>/dev/null || true)
            if [ "$current_target" = "$doc_path" ]; then
                rm "$target_path"
                mkdir -p "$target_path"
                CONVERTED=$((CONVERTED + 1))
                target_converted=1
            else
                echo "  ⚠️  $skill_name: 已存在不同符号链接，跳过"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi
        elif [ -e "$target_path" ] && [ ! -d "$target_path" ]; then
            echo "  ⚠️  $skill_name: Codex 中已有同名文件，跳过"
            SKIPPED=$((SKIPPED + 1))
            continue
        elif [ ! -e "$target_path" ]; then
            mkdir -p "$target_path"
            CREATED=$((CREATED + 1))
            target_created=1
        fi

        local managed_source=""
        if [ -f "$target_path/.ccgs-source" ]; then
            managed_source=$(cat "$target_path/.ccgs-source" 2>/dev/null || true)
        fi

        local existing_skill_target=""
        if [ -L "$target_path/SKILL.md" ]; then
            existing_skill_target=$(readlink "$target_path/SKILL.md" 2>/dev/null || true)
        fi

        if [ -z "$managed_source" ] \
            && [ "$target_created" -eq 0 ] \
            && [ "$target_converted" -eq 0 ] \
            && [ "$existing_skill_target" != "$doc_path" ]; then
            echo "  ⚠️  $skill_name: Codex 中已有同名 Skill 目录，跳过"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi

        if [ -n "$managed_source" ] && [ "$managed_source" != "$doc_path" ]; then
            echo "  ⚠️  $skill_name: 已由其他 CCGS 源管理，跳过"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi

        local skill_file="$target_path/SKILL.md"
        local tmp_file="$target_path/SKILL.md.tmp.$$"
        if [ -L "$skill_file" ]; then
            rm "$skill_file"
        elif [ -e "$skill_file" ] && [ ! -f "$skill_file" ]; then
            echo "    ⚠️  $skill_name/SKILL.md 已存在且不是普通文件，保留"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi

        {
            printf "%s\n" "---"
            printf "name: %s\n" "$skill_name"
            printf "description: \"CCGS %s wrapper for %s. Loads the original workflow document and applies it in Codex.\"\n" "$doc_kind" "$skill_name"
            printf "argument-hint: \"[request/context]\"\n"
            printf "user-invocable: true\n"
            printf "allowed-tools: Read, Glob, Grep, Write, Edit, Bash\n"
            printf "%s\n" "---"
            printf "\n"
            printf "# CCGS %s: %s\n" "$doc_kind" "$skill_name"
            printf "\n"
            printf "Source document: \`%s\`\n" "$doc_path"
            printf "\n"
            printf "## Invocation Protocol\n"
            printf "\n"
            printf "1. Read the source document above in full before acting.\n"
            printf "2. %s\n" "$doc_instruction"
            printf "3. Keep the active role or workflow scoped to this invocation unless another CCGS Skill explicitly routes to a different role.\n"
            printf "4. For code changes, also follow \`.ccgs-core/workflows/pipeline-core.md\`, \`.ccgs-core/docs/technical-preferences.md\`, and \`.ccgs-core/docs/coding-standards.md\`.\n"
            printf "5. If this wrapper conflicts with the source document, the source document wins.\n"
            printf "\n"
            printf "This file is generated by \`bash .ccgs-core/init.sh --link-codex-skills\`; edit the source document instead of this wrapper.\n"
        } > "$tmp_file"

        if ! cmp -s "$tmp_file" "$skill_file" 2>/dev/null; then
            mv "$tmp_file" "$skill_file"
            UPDATED=$((UPDATED + 1))
        else
            rm "$tmp_file"
        fi

        printf "%s\n" "$doc_path" > "$target_path/.ccgs-source"
        printf "workflow-doc\n" > "$target_path/.ccgs-kind"

        if [ "$managed_source" = "$doc_path" ]; then
            echo "  → $skill_name: 已存在"
            EXISTS=$((EXISTS + 1))
        elif [ "$target_converted" -eq 1 ]; then
            echo "  ✅ $skill_name"
        else
            echo "  ✅ $skill_name"
        fi
    done < <(find "$CCGS_WORKFLOWS_DIR" -type f -name "*.md" | sort)

    echo ""
    echo "🎯 Codex workflow 映射完成: 新增 $CREATED，转换 $CONVERTED，刷新 $UPDATED，已存在 $EXISTS，跳过 $SKIPPED"
    echo "   已映射标准 Skills、Agent 角色定义和 workflows 下的普通 Markdown 文档。"
    echo "   重启 Codex 后，映射内容才会被重新扫描。"
}

# ============================================================================
# 主入口
# ============================================================================
case "${1:-}" in
    --rename-data)
        if [ -z "${2:-}" ]; then
            echo "用法: bash .ccgs-core/init.sh --rename-data <新目录名>"
            exit 1
        fi
        cmd_rename_data "$2"
        ;;
    --gen-entry)
        if [ -z "${2:-}" ]; then
            echo "用法: bash .ccgs-core/init.sh --gen-entry <claude|gemini|cursor|codex|all>"
            exit 1
        fi
        cmd_gen_entry "$2"
        ;;
    --link-codex-skills)
        cmd_link_codex_skills
        ;;
    --all)
        cmd_verify
        echo ""
        cmd_gen_entry "all"
        ;;
    --help|-h)
        echo "CCGS 框架初始化工具"
        echo ""
        echo "用法:"
        echo "  bash .ccgs-core/init.sh                          校验目录骨架完整性"
        echo "  bash .ccgs-core/init.sh --rename-data <新名称>   重命名数据层目录"
        echo "  bash .ccgs-core/init.sh --gen-entry <工具名>     生成 AI 入口文件 (claude|gemini|cursor|codex|all)"
        echo "  bash .ccgs-core/init.sh --link-codex-skills      映射 CCGS workflows 到 Codex Skills"
        echo "  bash .ccgs-core/init.sh --all                    全量初始化"
        echo "  bash .ccgs-core/init.sh --help                   显示此帮助"
        ;;
    *)
        cmd_verify
        ;;
esac
