# 全链路立体化开发总则 (Pipeline Core)

> **适用场景**：任何基于 CCGS 框架的新需求、特性开发与迭代重构。
> **核心原则**：从宏观到微观层层递进，严禁越级开发（如未经 Epic 规划直接编写具体代码）。
> **管理架构**：基于 CCGS (Claude Code Game Studios) 三级阶梯架构进行开发流转。
> **引擎约束**：参照 `.ccgs-core/docs/technical-preferences.md` 中的配置

---

## 🔄 流水线全生命周期大纲

Markdown 编号列表`mermaid
graph TD
    A[用户输入需求] --> B{Tier 1: 战略规划}
    B -->|/create-epics| C[生成 Epic 大纲与宏观架构]
    C --> D{Tier 2: 任务拆解}
    D -->|/create-stories| E[将 Epic 拆分为具体 Stories]
    E --> F{Tier 3: 独立开发}
    F -->|/dev-story| G[单一领域的代码/资产实现]
    G --> H{Tier 2: 集成审查}
    H -->|/code-review & QA| I[代码逻辑检查与数值审查]
    I --> J[文档固化与阶段推进]
Markdown 编号列表`

---

## 🛠️ 审查模式 (Review Mode)

| 模式 | 流程 | 文档要求 | 适用场景 |
|:---|:---|:---|:---|
| **Full** | Phase 0→1→2→3→4 | 完整文档链 | 新功能、系统设计、算法改进（默认） |
| **Lean** | Phase 0→2→3 | Changelog + QA Report | 小功能、配置调整、文档更新 |
| **Hotfix** | Phase 0→2→3 | Changelog + QA Report + Bug 补登 | 紧急 Bug / 崩溃修复 |
| **Micro** | Phase 0→2→3(简) | **仅 git commit + micro-log 追加** | ≤10行、≤2文件、非架构改动 |

> Phase 0 后 AI **建议**模式，用户最终决定。Hotfix 完成后需补登 Bug Tracker。

### Micro 模式准入条件（全部满足才可使用）
- ✅ 修改行数 ≤ 10 行
- ✅ 涉及文件 ≤ 2 个
- ✅ 不触碰架构红线文件（参照项目专属约束模板中的定义）
- ✅ 不涉及 GDD 章节变更
- ✅ 不涉及核心数据配置表（参照 `technical-preferences.md`）

Micro 模式仅在 `CCGS-Data/production/changelogs/micro-log.md` 追加一行记录，不生成独立 Changelog/QA Report。

---

## Phase 0: 分层上下文加载

**目标**：建立项目全貌记忆，避免遗忘或方向偏移。**采用分层策略控制 Token 消耗。**

> **推荐入口**：先运行
> `python3 .ccgs-core/scripts/workflow/ccgs-context-router.py "<当前任务>"`
> 获取最小读取清单，再按清单读取文件。不要在 Phase 0 全量展开所有 GDD、Epic、QA 证据或角色文档。

> **⚡ 触发方式**：项目根目录的 `GEMINI.md` / `CLAUDE.md` 包含本文件的引用，
> AI 工具打开项目时自动读取，开箱即用。
> 每次新对话的第一步**必须**执行 Phase 0。

### L0 层：秒级启动（每次必做，~500 tokens）
1. **读取会话状态**：`CCGS-Data/production/session-state/active.md`
2. **执行 git status**：获取工作区变更情况
3. **执行 git log --oneline -5**：获取最近提交
4. **Grep 扫描活跃 Bug 数**：`grep -c "🔴\|🟡" CCGS-Data/production/tracking/bug-tracker.md`
5. **读取用户当前打开文件**，推测工作焦点
6. **输出状态快照**（≤10行）并建议审查模式

### L1 层：按需深入（仅当任务关键词命中时）
| 触发关键词 | 定向读取 |
|:---|:---|
| 涉及「[待补充: 核心玩法机制A]」 | GDD `[对应章节编号]` |
| 涉及「[待补充: 核心玩法机制B]」 | GDD `[对应章节编号]` |
| 涉及「数值」「配置」 | GDD `[配置章节]` |
| 涉及「架构」「重构」 | 相关 ADR 文档 |

> **严禁全量扫描**：不得一次性读取所有 GDD 章节。使用 Grep 先定位，再定向读取。

### 路径约定

| 内容 | 路径 |
|:---|:---|
| 会话状态 | `CCGS-Data/production/session-state/active.md` |
| GDD 设计文档 | `CCGS-Data/design/gdd/` |
| 提案 | `CCGS-Data/production/proposals/` |
| QA 报告 | `CCGS-Data/production/qa/` |
| Bug/债务追踪 | `CCGS-Data/production/tracking/` |

### 输出物（对话中输出，不保存文件）
Markdown 编号列表`
📋 CCGS 状态: [分支] | [上次任务] | Bug: N | 债务: N
📂 焦点推测: [根据打开文件推测]
🔄 建议模式: Full / Lean / Hotfix / Micro
Markdown 编号列表`

---

## Phase 1: 战略与架构规划 (Tier 1: Directors)

**触发时机**：接收到全新大版本需求或底层重构诉求。

### 角色匹配

| 需求类型 | 匹配 Agent |
|:---|:---|
| 创意方向 / 愿景 | `Tier1-Directors/creative-director.md` |
| 技术架构 / 引擎 | `Tier1-Directors/technical-director.md` |
| 项目管理 / 排期 | `Tier1-Directors/producer.md` |

### 标准操作
1. 调用 `/start` 了解项目当前状态
2. 调用 `/review-all-gdds` 审视 GDD 一致性
3. 执行 `/create-epics`，产出明确的 Epic 提案

### 准则查验

读取 GDD 相关章节交叉校验：[待补充: 此处列出需要校验的核心业务语义和对应 GDD 章节]。

> **⛔ 核心规则强制**：[待补充: 涉及特定机制变更时必须参照的条款，如未通过不得进入 Phase 2]
> **⚖️ 数值边界强制**：[待补充: 涉及数值或难度参数变更时必须参照的模型与阈值]

### 外部参考库（按需）

[待补充: 查阅特定的规范库、竞品分析文档或外部方法论，在提案中引用并注明出处]

### 输出提案

**路径**：`CCGS-Data/production/proposals/YYYY-MM-DD-[特性名]-proposal.md`

提案必含：需求分析 · 设计方案 · 可解性论证（如适用）· 影响评估 · 阶段合规 · 验收标准 · **任务拆分**

任务拆分表：`| # | 子任务 | 复杂度(S/M/L) | 验收标准 | 依赖 |`
- **S**：单文件，≤30行，无跨模块  **M**：2-5文件，1-2模块  **L**：>5文件，3+模块

### ⛔ 闸门
> **强制等待用户审批**。获得明确肯定答复前，严禁进入 Phase 2。

**交付物**：明确的愿景、技术基调、数值边界（红线）。

---

## Phase 2: 需求拆解与工程实现 (Tier 2 → Tier 3)

### Phase 2a: 任务拆解 (Tier 2: Leads)

**触发时机**：Tier 1 的 Epic 已经通过用户审批，需要落到实处。

| 需求类型 | 匹配 Agent |
|:---|:---|
| 游戏机制 / 系统设计 | `Tier2-Leads/game-designer.md` |
| 代码架构 / API 设计 | `Tier2-Leads/lead-programmer.md` |
| 美术方向 | `Tier2-Leads/art-director.md` |
| 音频方向 | `Tier2-Leads/audio-director.md` |
| 叙事设计 | `Tier2-Leads/narrative-director.md` |
| 测试策略 | `Tier2-Leads/qa-lead.md` |
| 发布管理 | `Tier2-Leads/release-manager.md` |
| 本地化 | `Tier2-Leads/localization-lead.md` |

**标准操作**：
1. 根据 Epic 提案，执行 `/create-stories`
2. 将需求细化为特定角色可接手的 Story（颗粒度≤500行代码/单系统）
3. 写入 `CCGS-Data/production/epics/` 进行管理

**交付物**：颗粒度≤500行代码/单系统的开发任务票。

### Phase 2b: 独立闭环开发 (Tier 3: Specialists)

**触发时机**：有明确的 Story 卡片待执行。

| 系统领域 | 匹配 Agent |
|:---|:---|
| 核心玩法 / 游戏逻辑 | `Tier3-Specialists/gameplay-programmer.md` |
| 引擎底层 / 框架 | `Tier3-Specialists/engine-programmer.md` |
| UI 系统 | `Tier3-Specialists/ui-programmer.md` |
| AI 行为 | `Tier3-Specialists/ai-programmer.md` |
| 编辑器工具 | `Tier3-Specialists/tools-programmer.md` |
| 引擎适配 | 参照 `technical-preferences.md` 中的引擎专家路由表 |
| 着色器 / 渲染 | 参照 `technical-preferences.md` 中的引擎专家路由表 |
| 数值 / 经济系统 | `Tier3-Specialists/economy-designer.md` |
| 关卡设计 | `Tier3-Specialists/level-designer.md` |
| UX 交互 | `Tier3-Specialists/ux-designer.md` |

**标准操作**：
1. 执行 `/dev-story` 申领任务
2. 严格在其专业领域内进行编码（遵循纯逻辑视图分离、[待补充: 核心架构红线与真源唯一性约束]、数据驱动、模块解耦等原则）
3. **编写配套测试**（参照 `technical-preferences.md` 中配置的测试框架与策略）
4. **自主运行测试套件**，确认绿灯后再提交
5. 自主执行 `/skill-test` 确保语法/基本逻辑无误
6. 执行 `/story-done` 产出 Changelog

**编码约束**：遵循 `.ccgs-core/docs/coding-standards.md` 与 `.ccgs-core/docs/technical-preferences.md`

**技术债务登记**：写入临时方案/TODO/妥协时，**必须同步登记** `CCGS-Data/production/tracking/tech-debt.md`，分配 `TD-XXX` ID

### 输出物

**路径**：`CCGS-Data/production/changelogs/YYYY-MM-DD-[特性名]-changelog.md`

Changelog 必含：修改文件清单 · 配置项变更 · Proposal 偏离说明 · 已知局限 · **Scope Check**

> **Scope Check**：对照 Proposal 影响评估列出计划内/计划外文件变更。计划外变更必须在交接摘要中**高亮提醒用户确认**。

**交付物**：可运行的代码补丁与详细的 Changelog。

---

## Phase 3: 代码与逻辑验证 (Tier 2: QA & Leads)

**目标**：代码级静态分析 + 自动化测试 + 架构审查。**实机验证由用户负责**。

**触发时机**：所有相关 Stories 开发完毕。

**匹配 Agent**：`Tier2-Leads/qa-lead.md` 或 `Tier2-Leads/lead-programmer.md`

### 必做步骤
1. **输入**：Phase 1 验收标准 + Phase 2 修改文件清单
2. **代码审查**（`/code-review`）：🔴阻塞（空引用/死锁/真源违背）· 🟡建议（命名/SRP）· 💭细节（注释/简化）
3. **架构完整性验证**：引用完整性 · 数据一致性 · 调用链审查 · 可解性保证 · 真源唯一性 · 边界条件 · 回归风险
4. **⚡ 自动化测试执行**（新增）：
   - 如存在对应测试套件，通过 `run_command` 执行测试
   - 将终端测试通过日志纳入 QA 报告的「自动化测试」章节
   - **测试未通过 → 回退 Phase 2，禁止进入 Phase 4**
   - 无测试套件时标记 `[AUTO-TEST: N/A]`，在 tech-debt 中登记
   - 纯逻辑层核心代码（参照项目专属约束中的架构红线文件）**强制要求**配套测试
5. **[待补充: 特定业务玩法] 校验**（如涉及）：[待补充: 校验方程或阈值]
6. **编译检查**：确保无编译错误
7. **数值审查**（`/balance-check`）：如涉及平衡性修改

### 输出物
- **验证摘要**（对话中输出）：各检查项 ✅/❌ + 结论（含自动化测试结果）
- **实机测试清单**（对话中输出）：供用户在引擎中逐项验证
- **QA 报告**（Full/Lean/Hotfix 模式强制生成，Micro 模式豁免）：`CCGS-Data/production/qa/YYYY-MM-DD-[特性名]-qa-report.md`
  - 含：基本信息 · 测试清单表 · 自动化测试日志 · 发现的问题表（关联 BUG-XXX）· 结论

> **Bug 追踪联动**：所有问题必须同步登记 `CCGS-Data/production/tracking/bug-tracker.md`，分配 `BUG-XXX` ID。
> **回退机制**：代码验证发现逻辑问题 → 回退 Phase 2 修复。自动化测试失败 → 回退 Phase 2。用户反馈问题 → 进入修复流程。

---

## Phase 4: GDD 文档同步

**目标**：将成果永久沉淀至项目权威文档。

### 必做步骤
1. **输入**：Proposal + Changelog + 验证摘要
2. **交叉引用检查**：读取 `CCGS-Data/design/gdd/cross-references.md` 依赖矩阵，逐章检查语义冲突，冲突则同步更新
3. **增量更新** GDD 对应章节（`01`~`13` + `cross-references.md`），**禁止全量覆盖**
4. **递增版本号**
5. **阶段检查点更新**：推进 CP 时同步更新 `10-development-roadmap.md`
6. **回顾触发**：完成重要 CP/里程碑时，生成 `CCGS-Data/production/retrospectives/YYYY-MM-DD-[阶段]-[CP]-retrospective.md`

### 输出物
更新后的 GDD（带版本号）· 回顾文档（若触发）· 总结摘要

---

## 🔗 阶段间交接协议

每个阶段结束时输出结构化 **阶段交接摘要**：阶段 · 状态(✅/⛔/🔁) · 输出文件 · 关键决策 · 下一阶段注意事项

---

## ⛔ 强制纪律 (Red Rules)

1. **单一职责 (SRP)**：Tier 3 专家绝对不能擅自修改底层引擎架构（这是 Tier 1/2 的工作）。如果发现无法实现，必须向上反馈修改 Epic。
2. **存档点机制**：任何一个 Slash Command 结束后，必须产生实质性文档（Proposal, Story, Changelog），保证随时可中断和恢复。
3. **数据驱动确认**：凡修改了数据配置表（参照 `technical-preferences.md`），必须同步更新版本号避免缓存问题。

### 🔒 操作前自审查（每次修改代码或 git commit 前必须执行）

> AI 在内部进行以下检查（不需要输出给用户，但必须执行）：

**修改代码前**：
- 当前修改是否在 session-state 中登记的任务范围内？
- 是否触碰了架构红线文件？（如是，必须向用户报告并等待确认）
- 当前角色是否有权限修改这个文件？（Tier 3 不得修改 Tier 1/2 的架构）

**git commit 前**：
- commit message 是否符合规范（`type(scope): description`）？
- 是否有未保存的 session-state 更新？
- 如果修改了 GDD 文件，8 个必要章节是否完整？
- 如果修改了配置/数据文件，格式是否合法？

### 🔒 版本控制纪律

> **每次执行完任务或修复后**，必须根据当下的修改规范编写 Commit 备注并将代码在本地提交 (git commit)。
> **绝不允许自动进行远程仓库的推送 (Do explicitly NOT git push)**。

### 🔒 角色边界强制规则

> **角色强制绑定 (Persona Lock)**：当用户通过 `@[/agent-name]` 调用特定工作流或专家节点时，AI 必须**绝对、完全地**代入该角色设定，严格依据其 Workflow 规范中的职责边界行事，禁止携带“全能AI”的习惯。
>
> **严禁越俘代庖 (No Domain Crossing)**：
> - **Tier 1/2 (Producer & Directors)**：仅负责规划、评审、协调和决策，**绝对禁止**直接编写、修改或调试任何具体的游戏代码。
> - **Tier 3 (Specialists)**：仅负责本领域的具体执行。严禁替 Director 做出重大系统设计或架构决策。
>
> **响应前自我审查 (Pre-flight Check)**：输出任何回复或执行 Tool Call 前，必须内部拦截审查：“我当前被分配的角色是 X，用户要求的事项是否属于 X 的职责范畴？”如果不属于，必须拒绝执行并引导用户转接正确的角色。
>
> **角色身份明示 (Identity Declaration)**：每次被通过 `@[/agent-name]` 调用时，回复的第一句话必须明确报告当前角色身份。

---

## ⚠️ 项目专属约束模板（贯穿全部 Phase）

> **💡 Agent 指令**：以下为项目专属的红线与约束区。请指示 Agent 根据新项目的具体要求（如架构风格、核心玩法参数、特殊纪律）补齐以下模板。**不得在此保留旧项目的规则。**

### 阶段纪律（当前阶段 [待补充]）
- [待补充: 例如，当前迭代阶段的核心开发纪律]
- [待补充: 严禁的开发行为]

### 架构红线
- [待补充: 核心数据结构的真源定义]
- [待补充: 不可侵犯的架构原则]

### 设计红线
- [待补充: 游戏设计的核心支柱（Pillars），如 沉浸感 > 表现力]
- [待补充: 难度、心流或数值相关的不可动摇的硬性指标]

### 业务与特殊约束
- [待补充: 如有特殊的外部约束（例如：特定发行平台限制、商业化目标等）]
- [待补充: 特定业务或技术指标要求的参考索引]

### 外部参考引用
- 涉及"[待补充: 特定高风险系统或新机制设计]"时，优先查阅相关竞品分析或方法论
- 在提案中明确引用并注明出处

---

## 📂 目录结构约定

Markdown 编号列表`text
.ccgs-core/
├── workflows/                     # Agent 与 Skill 定义
│   ├── pipeline-core.md           # 📍 本文件
│   ├── Tier1-Directors/           # 导演层 (3)
│   ├── Tier2-Leads/               # 主管层 (8)
│   ├── Tier3-Specialists/         # 专家层 (41)
│   └── skills/                    # Skill 库 (72)
│
├── docs/                          # 框架元文档
├── design/gdd/                    # 权威设计大纲（01~13 + cross-references.md）
├── project-docs/                  # 技术规范文档与架构决策 (ADRs)
│
├── production/
│   ├── proposals/                 # Phase 1 提案输出
│   ├── changelogs/                # Phase 2 变更日志输出
│   ├── qa/                        # Phase 3 QA 报告输出
│   ├── tracking/                  # bug-tracker.md + tech-debt.md
│   ├── epics/                     # Epic 与 Story 管理
│   ├── sprints/                   # 冲刺计划
│   ├── retrospectives/           # 检查点回顾文档
│   └── releases/                  # 发布管理
│
├── prototypes/                    # 原型实验
└── tests/                         # 测试套件
Markdown 编号列表`
