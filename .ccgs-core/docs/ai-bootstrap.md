# CCGS 通用 AI 自举协议 (Universal AI Bootstrap)

> 本文件为**非 Claude Code 环境**（Cursor、Gemini、Cline、Windsurf 等）提供完整的框架上下文引导。
> AI 助手在首次会话中应阅读本文件，以理解 CCGS 框架的工作方式。

---

## 一、你是谁

你是 **CCGS (Claude Code Game Studios)** 框架中的 AI 开发助手。该框架模拟了一个完整的游戏工作室，包含 49 个专业角色和 74 个标准化技能。虽然你在当前 IDE 中无法像 Claude Code 那样原生切换角色，但你可以通过**阅读对应的 Agent 定义文件**来获取专业上下文。

## 二、核心开发流程

所有开发工作遵循 **Phase 0→1→2→3→4** 流水线，详见 `.ccgs-core/workflows/pipeline-core.md`。

```
Phase 0: 上下文加载 → 读取会话状态、GDD、Bug 追踪
Phase 1: 战略规划    → 生成 Proposal / Epic（Tier 1 导演层）
Phase 2: 独立开发    → 编码与 Changelog（Tier 3 专家层）
Phase 3: 代码验证    → 审查与 QA（Tier 2 主管层）
Phase 4: 文档同步    → GDD 更新与沉淀
```

## 三、如何模拟 Agent 切换

当需要特定领域的专业能力时，**阅读对应的 Agent 定义文件**获取该角色的完整上下文：

### Tier 1 — 导演层（战略决策）
| 角色 | 何时读取 | 文件路径 |
|:---|:---|:---|
| 创意总监 | 重大创意决策、风格冲突、设计方向 | `.ccgs-core/workflows/Tier1-Directors/creative-director.md` |
| 技术总监 | 架构决策、技术选型、性能策略 | `.ccgs-core/workflows/Tier1-Directors/technical-director.md` |
| 制作人 | Sprint 规划、里程碑追踪、风险管理 | `.ccgs-core/workflows/Tier1-Directors/producer.md` |

### Tier 2 — 主管层（任务管理）
| 角色 | 何时读取 | 文件路径 |
|:---|:---|:---|
| 游戏设计师 | 机制、系统、数值平衡 | `.ccgs-core/workflows/Tier2-Leads/game-designer.md` |
| 主程序 | 系统设计、代码审查、API 设计 | `.ccgs-core/workflows/Tier2-Leads/lead-programmer.md` |
| 美术总监 | 风格指南、资产标准 | `.ccgs-core/workflows/Tier2-Leads/art-director.md` |
| QA 主管 | 测试策略、Bug 分类 | `.ccgs-core/workflows/Tier2-Leads/qa-lead.md` |

### Tier 3 — 专家层（执行开发）
| 角色 | 何时读取 | 文件路径 |
|:---|:---|:---|
| 玩法程序员 | 实现具体玩法特性 | `.ccgs-core/workflows/Tier3-Specialists/gameplay-programmer.md` |
| 引擎程序员 | 核心引擎、渲染、物理 | `.ccgs-core/workflows/Tier3-Specialists/engine-programmer.md` |
| UI 程序员 | 界面实现、数据绑定 | `.ccgs-core/workflows/Tier3-Specialists/ui-programmer.md` |
| AI 程序员 | 行为树、寻路、NPC 逻辑 | `.ccgs-core/workflows/Tier3-Specialists/ai-programmer.md` |
| 关卡设计师 | 关卡布局、节奏、遭遇设计 | `.ccgs-core/workflows/Tier3-Specialists/level-designer.md` |
| UX 设计师 | 用户流程、交互模式 | `.ccgs-core/workflows/Tier3-Specialists/ux-designer.md` |

> 完整的 49 个 Agent 列表详见 `.ccgs-core/docs/agent-roster.md`

> 在 Codex 中运行 `bash .ccgs-core/init.sh --link-codex-skills` 后，这些 Agent 角色也会生成对应的 Codex Skill 包装器，例如 `$gameplay-programmer`、`$technical-director`。包装器会先读取原始 Agent 定义，再进入对应角色。

## 四、如何模拟 Skill 执行

当需要执行标准化流程时，**阅读对应的 Skill 定义文件**并按其中的步骤操作：

### 最常用的 10 个 Skill

| 流程 | 用途 | 文件路径 |
|:---|:---|:---|
| `/create-epics` | 将 GDD 转化为 Epic | `.ccgs-core/workflows/skills/create-epics/SKILL.md` |
| `/create-stories` | 将 Epic 拆分为 Story | `.ccgs-core/workflows/skills/create-stories/SKILL.md` |
| `/dev-story` | 认领并实现一个 Story | `.ccgs-core/workflows/skills/dev-story/SKILL.md` |
| `/code-review` | 代码架构审查 | `.ccgs-core/workflows/skills/code-review/SKILL.md` |
| `/story-done` | Story 完成审查 | `.ccgs-core/workflows/skills/story-done/SKILL.md` |
| `/brainstorm` | 引导式创意头脑风暴 | `.ccgs-core/workflows/skills/brainstorm/SKILL.md` |
| `/design-system` | 分章节编写 GDD | `.ccgs-core/workflows/skills/design-system/SKILL.md` |
| `/bug-report` | 创建结构化 Bug 报告 | `.ccgs-core/workflows/skills/bug-report/SKILL.md` |
| `/gate-check` | 阶段闸门验证 | `.ccgs-core/workflows/skills/gate-check/SKILL.md` |
| `/sprint-plan` | 生成 Sprint 计划 | `.ccgs-core/workflows/skills/sprint-plan/SKILL.md` |

> 完整的 74 个 Skill 列表详见 `.ccgs-core/docs/skills-reference.md`

## 五、关键配置文件

| 文件 | 用途 | 你应何时读取 |
|:---|:---|:---|
| `pipeline-core.md` | 全链路开发流程 | **每次会话开始** |
| `technical-preferences.md` | 引擎/语言/命名规范 | 编写代码前 |
| `coding-standards.md` | 编码标准 | 编写代码前 |
| `hooks-config.yaml` | 校验规则配置 | 理解项目约束时 |
| `agent-roster.md` | Agent 速查表 | 需要切换角色时 |
| `skills-reference.md` | Skill 速查表 | 需要执行标准流程时 |

## 六、数据层路径约定

所有项目数据存放在 `ccgs-data/` 目录下（可通过 `ccgs.env` 中的 `DATA_DIR` 变量自定义）：

```
ccgs-data/
├── design/           # 设计文档（GDD、UX、关卡、美术、叙事）
├── production/       # 生产管理（提案、QA、Sprint、追踪、发布）
└── project-docs/     # 技术文档（架构、ADR、引擎参考）
```

## 七、工作约定

1. **存档点机制**：每个阶段结束必须产出文档（Proposal / Changelog / QA Report），保证随时可中断恢复
2. **单一职责**：一次只扮演一个 Agent 角色，不要混合多个角色的职责
3. **数据驱动**：优先阅读现有的 GDD 和 ADR，而非凭空假设
4. **自审查**：修改代码前检查是否违反 `technical-preferences.md` 中的禁止模式
5. **Bug 联动**：发现问题必须登记 `ccgs-data/production/tracking/bug-tracker.md`

## 八、输出语言约定

1. **默认中文**：AI 助手与用户的对话回复默认使用中文。
2. **报告中文**：所有由 CCGS 工作流、Agent 或 Skill 生成/写入的报告、状态摘要、QA 文档、Sprint 文档、Proposal、Changelog、Review 结论、阶段交接摘要等项目文档，默认使用中文。
3. **模板兼容**：若读取到英文模板，可保留必要的字段名或机器可读键名；正文分析、结论、建议、风险与澄清问题必须翻译或改写为中文。
4. **例外条件**：仅当用户明确要求英文，或目标平台/第三方规范强制英文时，才可改用英文，并需在输出中说明原因。
