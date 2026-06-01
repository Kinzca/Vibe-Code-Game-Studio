# CCGS 用户使用指南

> **版本**: Universal v1.0  
> **适用工具**: Claude Code · Gemini / Antigravity · Cursor · OpenAI Codex · Cline · Windsurf

---

## 一、30 秒速览

CCGS 是一个**模拟完整游戏工作室**的 AI 开发框架。它将 49 个专业 AI 角色和 74 个标准化技能组织成三层工作室架构，驱动从创意头脑风暴到正式发布的全生命周期管线。

你只需要做两件事：
1. **初始化框架**（一次性）
2. **用斜杠命令 `/` 驱动开发**（日常）

---

## 二、安装与初始化

### 2.1 克隆框架

```bash
git clone https://github.com/Kinzca/CCGS_Universal_Version.git my-game
cd my-game
```

### 2.2 一键初始化

```bash
# 校验目录骨架完整性（缺失的文件夹会自动修复）
bash .ccgs-core/init.sh

# 为你使用的 AI 工具生成入口文件
bash .ccgs-core/init.sh --gen-entry gemini    # Gemini / Antigravity
bash .ccgs-core/init.sh --gen-entry claude    # Claude Code
bash .ccgs-core/init.sh --gen-entry cursor    # Cursor
bash .ccgs-core/init.sh --gen-entry codex     # OpenAI Codex
bash .ccgs-core/init.sh --gen-entry all       # 全部生成

# 可选：让 Codex 扫描 CCGS workflows 的 Skill 映射
bash .ccgs-core/init.sh --link-codex-skills
```

### 2.3 init.sh 命令速查

| 命令 | 用途 |
|:---|:---|
| `bash .ccgs-core/init.sh` | 校验并修复 CCGS-Data 目录骨架 |
| `bash .ccgs-core/init.sh --gen-entry <工具>` | 生成 AI 工具入口文件 (claude / gemini / cursor / codex / all) |
| `bash .ccgs-core/init.sh --link-codex-skills` | 将 CCGS workflows 映射到 Codex 本地 Skills 目录 |
| `bash .ccgs-core/init.sh --rename-data <新名称>` | 重命名数据层目录（自动批量替换所有引用） |
| `bash .ccgs-core/init.sh --all` | 全量初始化（校验 + 生成全部入口文件） |
| `bash .ccgs-core/init.sh --help` | 显示帮助信息 |

### 2.4 按编辑器初始化

| 编辑器 / AI 工具 | 如何导入项目 | 初始化命令 | 入口文件 | 调用方式 |
|:---|:---|:---|:---|:---|
| Claude Code | 打开项目根目录 | `bash .ccgs-core/init.sh --gen-entry claude` | `CLAUDE.md` | 原生 `/skill` 命令 |
| Gemini / Antigravity | 打开项目根目录 | `bash .ccgs-core/init.sh --gen-entry gemini` | `GEMINI.md` | 按入口文件说明执行 `/skill` 文本工作流 |
| Cursor | Open Folder 到项目根目录 | `bash .ccgs-core/init.sh --gen-entry cursor` | `.cursorrules` | 按入口文件说明执行 `/skill` 文本工作流 |
| OpenAI Codex | Open Workspace 到项目根目录 | `bash .ccgs-core/init.sh --gen-entry codex` | `AGENTS.md` | 文本 `/skill` 兼容；可选 `$skill` / `$agent-role` |
| 多工具并用 | 打开同一项目根目录 | `bash .ccgs-core/init.sh --gen-entry all` | 全部入口文件 | 各 IDE 读取自己的入口文件 |

Codex 若需要把 CCGS workflows 注册为本地 Skills，再运行：

```bash
bash .ccgs-core/init.sh --link-codex-skills
```

该命令会映射 124 个 Codex Skill 入口：74 个标准 CCGS Skills、49 个 Agent 角色包装器，以及 `pipeline-core` 工作流包装器。运行后重启 Codex 生效。

### 2.5 （可选）重命名数据层目录

如果你不想使用默认的 `CCGS-Data` 目录名：

```bash
bash .ccgs-core/init.sh --rename-data GameData
```

该命令会自动完成目录移动、全量路径替换和 `ccgs.env` 更新。

---

## 三、核心开发流程 (Pipeline)

所有开发工作遵循 **Phase 0 → 1 → 2 → 3 → 4** 的五阶段流水线：

```
Phase 0: 上下文加载  →  读取会话状态、GDD、Bug 追踪
Phase 1: 战略规划    →  生成 Proposal / Epic（Tier 1 导演层）
Phase 2: 独立开发    →  编码与 Changelog（Tier 3 专家层）
Phase 3: 代码验证    →  审查与 QA（Tier 2 主管层）
Phase 4: 文档同步    →  GDD 更新与沉淀
```

项目生命周期分为 **7 个阶段**，每个阶段结束时需要通过门禁 (`/gate-check`) 才能进入下一阶段：

```
Concept → Systems Design → Technical Setup → Pre-Production → Production → Polish → Release
```

---

## 四、常用斜杠命令速查

### 4.1 新手入门（不知道从哪开始）

| 命令 | 用途 |
|:---|:---|
| `/start` | **首次使用推荐**。问你在哪，然后引导到正确的工作流 |
| `/help` | 上下文感知的"下一步做什么"，读取当前阶段后给建议 |
| `/project-stage-detect` | 全项目审计——检测当前阶段、发现缺口、推荐下一步 |

### 4.2 创意与设计

| 命令 | 用途 |
|:---|:---|
| `/brainstorm` | 从零开始引导式头脑风暴，产出结构化的游戏概念文档 |
| `/map-systems` | 将游戏概念拆解为独立系统，绘制依赖图，确定设计顺序 |
| `/design-system <系统名>` | 逐章节引导式 GDD 编写 |
| `/quick-design` | 轻量级设计规格，适合小改动和数值调优 |
| `/art-bible` | 逐章节引导式视觉圣经编写 |
| `/ux-design` | UX 规范编写（屏幕/流程/HUD/交互模式） |

### 4.3 技术架构

| 命令 | 用途 |
|:---|:---|
| `/setup-engine` | 配置引擎与版本，自动填充引擎参考文档 |
| `/create-architecture` | 编写主架构蓝图文档 |
| `/architecture-decision` | 创建一份架构决策记录 (ADR) |
| `/architecture-review` | 校验所有 ADR 的完整性、依赖排序和 GDD 覆盖率 |
| `/create-control-manifest` | 从已通过的 ADR 中提取程序员规则清单 |
| `/test-setup` | 一次性搭建测试框架与 CI/CD 管线 |

### 4.4 生产管理

| 命令 | 用途 |
|:---|:---|
| `/create-epics` | 将 GDD + ADR 转化为 Epic（一个 Epic = 一个架构模块） |
| `/create-stories <epic>` | 将一个 Epic 拆分为可实现的 Story 文件 |
| `/dev-story` | 读取 Story 文件并实现它——自动路由到正确的程序员 Agent |
| `/sprint-plan` | 生成或更新 Sprint 计划 |
| `/sprint-status` | 快速查看 Sprint 进度快照 |
| `/story-done` | Story 完成审查——逐条验证验收标准 |
| `/gate-check` | 阶段门禁验证（PASS / CONCERNS / FAIL） |

### 4.4.1 低消耗上下文工具

长项目建议先生成上下文缓存，再执行 Story/ADR/QA 工作流：

```bash
python3 .ccgs-core/scripts/workflow/ccgs-context-index.py --write
python3 .ccgs-core/scripts/workflow/ccgs-current-context.py --write
python3 .ccgs-core/scripts/workflow/ccgs-story-context.py CCGS-Data/production/epics/<epic>/<story>.md --write
```

| 命令 | 用途 |
|:---|:---|
| `ccgs-context-index.py --write` | 建立 `production/context/ccgs-index.json`，记录 ADR/GDD/Story/QA 路径、状态、引用和行数 |
| `ccgs-current-context.py --write` | 生成 `production/context/current-context.md`，作为新会话低成本启动摘要 |
| `ccgs-story-context.py <story> --write` | 生成 Story 专属 context pack，减少 readiness/dev/done 前的大范围读取 |

默认不加 `--write` 时只输出到终端，适合临时查看。

### 4.5 质量保证

| 命令 | 用途 |
|:---|:---|
| `/code-review` | 代码架构审查 |
| `/design-review` | 设计文档审查 |
| `/qa-plan` | 为某个 Sprint 或功能生成 QA 测试计划 |
| `/smoke-check` | 运行关键路径冒烟测试门禁 |
| `/bug-report` | 创建结构化的 Bug 报告 |
| `/bug-triage` | 重新评估所有开放 Bug 的优先级 |
| `/balance-check` | 分析游戏平衡数据，发现异常 |

### 4.6 发布与运维

| 命令 | 用途 |
|:---|:---|
| `/release-checklist` | 生成发布前检查清单 |
| `/launch-checklist` | 完整的上线准备验证 |
| `/changelog` | 从 Git 历史自动生成变更日志 |
| `/patch-notes` | 生成面向玩家的更新说明 |
| `/hotfix` | 紧急修复流程（绕过正常 Sprint） |

### 4.7 团队协同

一次召集多个 Agent 协作完成复杂功能：

| 命令 | 协调的 Agent |
|:---|:---|
| `/team-combat` | game-designer + gameplay-programmer + ai-programmer + technical-artist + sound-designer + qa-tester |
| `/team-narrative` | narrative-director + writer + world-builder + level-designer |
| `/team-ui` | ux-designer + ui-programmer + art-director + accessibility-specialist |
| `/team-qa` | qa-lead + qa-tester + gameplay-programmer + producer |
| `/team-release` | release-manager + qa-lead + devops-engineer + producer |
| `/team-polish` | performance-analyst + technical-artist + sound-designer + qa-tester |
| `/team-audio` | audio-director + sound-designer + technical-artist + gameplay-programmer |
| `/team-level` | level-designer + narrative-director + world-builder + art-director + systems-designer + qa-tester |
| `/team-live-ops` | live-ops-designer + economy-designer + community-manager + analytics-engineer |

---

## 五、Agent 体系（49 个角色）

### 5.1 三层架构

| 层级 | 职责 | Agent 数量 |
|:---|:---|:---|
| **Tier 1 — 导演层** | 战略决策、创意仲裁、技术选型 | 3 |
| **Tier 2 — 主管层** | 领域管理、任务分配、质量把关 | 8 |
| **Tier 3 — 专家层** | 具体执行、代码实现、资产制作 | 38 |

### 5.2 "我需要做 X，该用哪个 Agent？"

| 我需要… | 使用 Agent |
|:---|:---|
| 设计一个新机制 | `game-designer` |
| 写战斗代码 | `gameplay-programmer` |
| 创建着色器 | `technical-artist` |
| 写对话 | `writer` |
| 规划下个 Sprint | `producer` |
| 审查代码质量 | `lead-programmer` |
| 写测试用例 | `qa-tester` |
| 设计关卡 | `level-designer` |
| 解决性能问题 | `performance-analyst` |
| 搭建 CI/CD | `devops-engineer` |
| 设计掉落表 | `economy-designer` |
| 解决创意冲突 | `creative-director` |
| 做架构决策 | `technical-director` |
| 获取 Unity 建议 | `unity-specialist` |
| 获取 Godot 建议 | `godot-specialist` |
| 获取 Unreal 建议 | `unreal-specialist` |
| 快速原型验证 | `prototyper` |
| 审查安全问题 | `security-engineer` |
| 检查无障碍合规 | `accessibility-specialist` |

### 5.3 引擎专精 Agent

框架内置了三大引擎的深度专精 Agent：

**Unity**: `unity-specialist` · `unity-dots-specialist` · `unity-shader-specialist` · `unity-addressables-specialist` · `unity-ui-specialist`

**Unreal**: `unreal-specialist` · `ue-gas-specialist` · `ue-blueprint-specialist` · `ue-replication-specialist` · `ue-umg-specialist`

**Godot**: `godot-specialist` · `godot-gdscript-specialist` · `godot-shader-specialist` · `godot-gdextension-specialist` · `godot-csharp-specialist`

---

## 六、目录结构说明

```
项目根目录/
├── .ccgs-core/                         # 框架引擎（不含项目数据）
│   ├── ccgs.env                        # 全局路径配置（唯一真源）
│   ├── init.sh                         # 一键初始化工具
│   ├── workflows/                      # Agent 工作流与 Skill 定义
│   │   ├── pipeline-core.md            # 全链路流水线总则
│   │   ├── Tier1-Directors/            # 导演层 Agent (3)
│   │   ├── Tier2-Leads/               # 主管层 Agent (8)
│   │   ├── Tier3-Specialists/         # 专家层 Agent (38)
│   │   └── skills/                     # Skill 库 (74)
│   ├── rules/                          # 代码规则分发源 (11)
│   ├── hooks/                          # 自动化钩子脚本
│   ├── docs/                           # 框架元文档与配置模板
│   │   └── templates/                  # 36 份文档模板
│   └── tests/                          # 框架自测套件
│
├── CCGS-Data/                          # 项目数据层（可重命名）
│   ├── design/                         # 设计文档
│   │   ├── art/                        # Art Bible、资产规格
│   │   ├── gdd/                        # 游戏设计文档 (GDD)
│   │   ├── ux/                         # UX 规范、交互范式
│   │   ├── balance/                    # 平衡数据
│   │   ├── levels/                     # 关卡设计
│   │   ├── narrative/                  # 叙事、角色
│   │   ├── quick-specs/                # 快速设计规格
│   │   └── registry/                   # 实体注册表
│   ├── production/                     # 生产管理
│   │   ├── epics/                      # Epic 文件
│   │   ├── sprints/                    # Sprint 计划
│   │   ├── changelogs/                 # 变更日志
│   │   ├── proposals/                  # 提案
│   │   ├── qa/                         # QA 报告与证据
│   │   ├── tracking/                   # Bug 追踪
│   │   └── ...                         # 其他生产管理子目录
│   └── project-docs/                   # 技术文档
│       ├── architecture/               # 架构文档、ADR、追踪矩阵
│       ├── engine-reference/           # 引擎版本参考文档
│       └── research/                   # 研究论文与参考资料
│
├── CLAUDE.md                           # Claude Code 入口（自动生成）
├── GEMINI.md                           # Gemini 入口（自动生成）
├── .cursorrules                        # Cursor 入口（自动生成）
└── AGENTS.md                           # Codex 入口（自动生成）
```

Codex 可额外运行 `bash .ccgs-core/init.sh --link-codex-skills`，在 `$CODEX_HOME/skills`（默认 `~/.codex/skills`）创建真实 Skill 目录：74 个标准 CCGS Skills 会复制原始 `SKILL.md`，49 个 Agent 角色与 `pipeline-core.md` 会生成包装 `SKILL.md`，调用时再读取原始 workflow 文档。重启 Codex 后生效；如果上游 workflow 内容更新，请重新运行该命令刷新映射。

---

## 七、典型开发路径

### Path A：从零开始（没有任何想法）

```
/start → /brainstorm → /setup-engine → /map-systems → /design-system
→ /create-architecture → /gate-check → 进入 Pre-Production
```

### Path B：知道要做什么（有明确概念）

```
/setup-engine → /map-systems → /design-system → /art-bible
→ /architecture-decision → /gate-check → 进入 Pre-Production
```

### Path C：接手已有项目

```
/project-stage-detect → /adopt → /setup-engine → /gate-check
→ 根据结果补缺 → 继续推进
```

### Path D：已在开发中（进入生产冲刺）

```
/create-epics → /create-stories → /sprint-plan
→ /dev-story → /code-review → /story-done → /sprint-status
→ /gate-check production → 进入 Polish
```

---

## 八、文档模板库

框架内置了 36 份即用型文档模板，位于 `.ccgs-core/docs/templates/`：

| 类别 | 模板 |
|:---|:---|
| **设计** | game-concept · game-design-document · game-pillars · systems-index · level-design-document · difficulty-curve · economy-model · faction-design |
| **美术与音频** | art-bible · sound-bible |
| **UX** | ux-spec · hud-design · interaction-pattern-library · accessibility-requirements · player-journey |
| **架构** | architecture-decision-record · architecture-traceability · technical-design-document |
| **生产** | sprint-plan · milestone-definition · changelog-template · release-notes · release-checklist-template · patch-notes · incident-response · post-mortem |
| **叙事** | narrative-character-sheet · pitch-document |
| **QA** | test-plan · test-evidence · project-stage-report |
| **逆向文档化** | design-doc-from-implementation · architecture-doc-from-code · concept-doc-from-prototype |

---

## 九、钩子系统

框架提供了 16 个自动化钩子脚本，位于 `.ccgs-core/hooks/`：

| 钩子 | 触发时机 | 用途 |
|:---|:---|:---|
| `validate-commit.sh` | 提交前 | 校验提交格式与代码规范 |
| `validate-push.sh` | 推送前 | 推送前质量门禁 |
| `validate-assets.sh` | 资产变更时 | 校验资产命名与格式 |
| `session-start.sh` | 会话开始 | 加载会话上下文 |
| `session-stop.sh` | 会话结束 | 保存会话状态 |
| `detect-gaps.sh` | 按需 | 检测项目缺口 |
| `distribute-rules.sh` | 按需 | 分发代码规则到对应目录 |

---

## 十、关键配置文件

| 文件 | 用途 | 何时编辑 |
|:---|:---|:---|
| `.ccgs-core/ccgs.env` | 全局路径配置 | 一般不需手动编辑 |
| `.ccgs-core/docs/technical-preferences.md` | 引擎/语言/命名/性能预算 | 运行 `/setup-engine` 后自动填充 |
| `.ccgs-core/docs/coding-standards.md` | 编码标准 | 编写代码前阅读 |
| `.ccgs-core/hooks/hooks-config.yaml` | 钩子校验规则 | 根据项目需求自定义 |
| `.ccgs-core/workflows/pipeline-core.md` | 全链路开发流程 | 每次会话开始时由 AI 自动阅读 |

---

## 十一、常见问题

### Q: 通用版和原版 (Donchitos) 有什么区别？

| 维度 | 原版 | 通用版 |
|:---|:---|:---|
| 目录结构 | `.claude/`（平铺） | `.ccgs-core/` + `CCGS-Data/`（双层解耦） |
| Agent 组织 | 48 个平铺 | 49 个三级分层（Tier1/2/3） |
| AI 工具绑定 | 仅 Claude Code | 通用（Claude / Gemini / Cursor / Codex） |
| 流水线 | 无统一定义 | `pipeline-core.md`（Phase 0→4） |
| 数据层 | 与框架混杂 | 独立目录，可重命名 |
| 路径配置 | 无 | `ccgs.env` + `init.sh` 一键管理 |

### Q: 我可以只用部分功能吗？

可以。框架的 74 个 Skill 是完全独立的，你可以只使用你需要的命令。最小可行使用路径是：`/start` → `/brainstorm` → `/design-system` → `/dev-story`。

### Q: 如何让 AI 助手感知框架？

运行 `bash .ccgs-core/init.sh --gen-entry <你的工具>` 生成入口文件。AI 助手会在会话开始时自动读取该入口文件，从而理解框架的工作方式。

### Q: 数据层目录可以改名吗？

可以。运行 `bash .ccgs-core/init.sh --rename-data <新名称>` 即可。该命令会自动完成目录重命名和全量路径替换。
