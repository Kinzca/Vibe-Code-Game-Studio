# CCGS Universal Version

基于 [Claude Code Game Studios](https://github.com/Donchitos/Claude-Code-Game-Studios) 重构的**通用解耦版**游戏开发 AI 框架。

核心设计：将框架引擎（`.ccgs-core`）与项目数据（`ccgs-data`）彻底分离，实现跨项目、跨引擎、跨 AI 工具的即插即用。

## 快速开始

### Windows / Codex

```powershell
$env:CCGS_PYTHON = "C:\path\to\python.exe" # only needed when Python is not on PATH
.\ccgs.cmd doctor
.\ccgs.cmd doctor --project-root D:\path\to\consumer --json
.\ccgs.cmd policy --project-root D:\path\to\consumer --target Client\Assets\Game.cs
```

`doctor` and `policy` are read-only. Framework development belongs in this
repository; a consumer game repository is changed only by an explicit future
bootstrap or upgrade command.

```bash
# 1. 克隆框架
git clone https://github.com/Kinzca/CCGS_Universal_Version.git my-game

# 2. 校验目录骨架完整性
bash .ccgs-core/init.sh

# 3. 生成你使用的 AI 工具的入口文件
bash .ccgs-core/init.sh --gen-entry claude   # Claude Code
bash .ccgs-core/init.sh --gen-entry gemini   # Gemini / Antigravity
bash .ccgs-core/init.sh --gen-entry cursor   # Cursor
bash .ccgs-core/init.sh --gen-entry codex    # OpenAI Codex
bash .ccgs-core/init.sh --gen-entry all      # 全部生成

# 3b. （可选）让 Codex 扫描 CCGS workflows 的 124 个 Skill 映射
bash .ccgs-core/init.sh --link-codex-skills

# 4. 配置引擎与技术栈
# 编辑 .ccgs-core/docs/technical-preferences.md 填入你的引擎配置
# 编辑 .ccgs-core/hooks/hooks-config.yaml 填入项目特定的校验规则

# 5. （可选）重命名数据层目录
bash .ccgs-core/init.sh --rename-data GameData  # 将 ccgs-data 重命名为 GameData

# 6. （推荐）为长项目生成低消耗上下文缓存
python3 .ccgs-core/scripts/workflow/ccgs-context-index.py --write
python3 .ccgs-core/scripts/workflow/ccgs-current-context.py --write
```

## 编辑器初始化矩阵

| 编辑器 / AI 工具 | 导入方式 | 初始化命令 | 生成入口 | 日常调用方式 |
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

## 架构总览

```
项目根目录/
├── .ccgs-core/                    # 框架引擎（不含任何项目数据）
│   ├── ccgs.env                   # 全局路径配置（唯一真源）
│   ├── init.sh                    # 一键初始化工具
│   ├── workflows/                 # Agent 工作流与 Skills
│   │   ├── pipeline-core.md       # 全链路流水线总则
│   │   ├── Tier1-Directors/       # 导演层（3 个）
│   │   ├── Tier2-Leads/           # 主管层（8 个）
│   │   ├── Tier3-Specialists/     # 专家层（38 个）
│   │   └── skills/                # Skill 库（74 个）
│   ├── rules/                     # 代码规则分发源（11 个）
│   ├── hooks/                     # 自动化钩子脚本
│   ├── docs/                      # 框架元文档与配置模板
│   ├── scripts/workflow/          # 上下文路由、缓存、会话归档脚本
│   └── tests/                     # 框架自测套件
│
├── ccgs-data/                     # 项目数据层（可重命名）
│   ├── design/                    # 设计文档（GDD、UX、关卡等）
│   ├── production/                # 生产管理（提案、QA、Sprint 等）
│   └── project-docs/              # 技术文档（架构、ADR 等）
│
├── CLAUDE.md                      # Claude Code 入口（自动生成）
├── GEMINI.md                      # Gemini 入口（自动生成）
├── .cursorrules                   # Cursor 入口（自动生成）
└── AGENTS.md                      # Codex 入口（自动生成）
```

> Codex Skill 映射由 `bash .ccgs-core/init.sh --link-codex-skills` 创建。
> 它会在 `$CODEX_HOME/skills`（默认 `~/.codex/skills`）创建真实 Skill 目录：74 个标准 CCGS Skills 会复制原始 `SKILL.md`，49 个 Agent 角色与 `pipeline-core.md` 会生成包装 `SKILL.md`，调用时再读取原始 workflow 文档。重启 Codex 后生效；如果上游 workflow 内容更新，请重新运行该命令刷新映射。

## 低消耗上下文工具

长会话或大型 Sprint 建议先生成缓存，减少 AI 每次全量读取 GDD、ADR、Story、QA 证据的成本：

```bash
python3 .ccgs-core/scripts/workflow/ccgs-context-index.py --write
python3 .ccgs-core/scripts/workflow/ccgs-current-context.py --write
python3 .ccgs-core/scripts/workflow/ccgs-context-router.py "当前任务"
```

执行 Story 前可生成专属 context pack：

```bash
python3 .ccgs-core/scripts/workflow/ccgs-story-context.py ccgs-data/production/epics/<epic>/<story>.md --write
```

生成文件位于 `ccgs-data/production/context/`，默认不加 `--write` 时只输出到终端。

## 与原版的核心差异

| 维度 | 原版 (Donchitos) | 通用版 (本仓库) |
|:---|:---|:---|
| 目录结构 | `.claude/`（平铺） | `.ccgs-core/` + `ccgs-data/`（双层解耦） |
| Agent 组织 | 48 个平铺 | 三级分层（Tier1/2/3） |
| AI 工具绑定 | 仅 Claude Code | 通用（Claude / Gemini / Cursor / Codex） |
| 流水线 | 无统一定义 | `pipeline-core.md`（Phase 0→4） |
| 数据层 | 与框架混杂 | 独立目录，可重命名 |
| 路径配置 | 无 | `ccgs.env` + `init.sh` 一键管理 |

## Acknowledgments

A special and heartfelt thanks to **[Donchitos/Claude-Code-Game-Studios](https://github.com/Donchitos/Claude-Code-Game-Studios)**.

While this universal version features a significantly diverged structure to support data-driven decoupling, the original repository served as the foundational inspiration and conceptual origin for this framework. We are deeply grateful for the groundbreaking work that made this possible.
