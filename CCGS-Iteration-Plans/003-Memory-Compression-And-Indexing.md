# CCGS 迭代计划 003：动态上下文压缩与记忆管理

> **制定时间**：2026-04-27
> **计划目标**：彻底解决中后期项目文档积累导致的 LLM Token 爆炸和注意力失焦问题，通过实现快照压缩与物理隔离，保障 AI Agent 的思考专注度与响应速度。

## 一、 当前架构痛点 (Pain Point)
随着项目的推进，`ccgs-data` 目录下会积累海量的 GDD、Epic、Story、Bug 报告和 ADR 文档。
目前的管线（如 `Phase 0: 上下文加载`）往往试图让 AI 全量读取这些文件，这不可避免地会引发两个致命问题：
1. **Token 浪费与触发极限**：容易迅速突破大模型的上下文窗口，并导致极大的 API 账单消耗。
2. **“注意力失焦”与幻觉**：输入的信息噪音过多。例如，让负责 UI 交互的 Agent 读取底层网络同步的繁琐协议文档，不仅毫无必要，反而会导致其生成代码时发生逻辑混淆。

## 二、 核心优化方案 (Optimization Plan)
为了防范 Token 爆炸并强化思考效率，必须从“时间跨度”和“领域空间跨度”两个维度切断多余上下文。

### 1. 领域物理隔离与按需索引 (Domain Isolation)
在系统的全局配置（如 `ccgs.env` 或是单独的 `.ccgs-core/rules/domain-index.yaml`）中，明确划分不同 Agent 的“阅读权限”。
- 建立硬性拦截规则：例如 `ui-programmer` 只被允许读取 `ccgs-data/design/ux/` 和 `ui-code.md`。如果其尝试读取网络协议目录，工具脚本应当在底层直接拦截并返回“越权警告”。
- **实施形式**：引入一种轻量级权限分发系统。各个领域的 Agent 只读该领域的专属子视图（View），而非整个工程。

### 2. 生命周期末尾的快照机制 (Memory Compression)
精细化的文档（如一个个独立的 Story 和碎片的 Sprint 报告）只在当前冲刺中有最高价值。一旦通过验收，它们的细节对全局来说就是冗余。
- **引入新命令**：开发 `/compress-memory` 技能工作流。
- **触发时机**：在每个 Sprint 的终点门禁（Gate Check）通过后，强制触发。
- **运作逻辑**：AI 获取该 Sprint 内所有已经完成的 Story、决策变动，并提炼为核心状态、依赖关系和新增接口，浓缩成一份高度结构化的 `architecture-snapshot-[N].md`。
- **垃圾回收**：后续加载上下文时，默认只读取最新的 Snapshot 以及当前 Sprint 正在活跃的 Story 列表，过期的细碎文档被视为“冷数据”归档，不再参与全局加载。

## 三、 实施路径规划 (Implementation Path)

1. **第一阶段：构建权限与索引地图 (Domain Mapping)**
   - 梳理现有的 49 个 Agent，将其归类至不同的核心领域（UI、Gameplay、Backend、Art、QA 等）。
   - 在 `.ccgs-core/docs/` 下编写 `domain-permissions.json`，映射每个领域所需要的核心读取路径白名单。

2. **第二阶段：落地快照压缩流水线 (Snapshot Pipeline)**
   - 在 `.ccgs-core/workflows/skills/` 中开发全新的 `compress-memory` 技能流。
   - 定义 `architecture-snapshot.md` 的标准化模板。包含“当前总架构图谱”、“核心已实现的 API”、“全局未解决技术债”三个极简板块。

3. **第三阶段：修改上下文加载器 (Context Loader Upgrade)**
   - 更新 `start` 和日常会话钩子 `session-start.sh`，使它们不再进行全局的大规模 Glob 查找，而是严格按照当前 Agent 的领域权限读取白名单，并强制优先挂载最新的 Snapshot 快照。
