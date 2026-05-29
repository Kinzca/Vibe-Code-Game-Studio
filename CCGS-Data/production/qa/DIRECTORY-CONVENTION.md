# QA 目录规范

> 本文件定义了 `CCGS-Data/production/qa/` 目录的标准结构。
> 所有 CCGS Skill 输出的 QA 相关文档必须遵循此规范放置。
> 此规范用于保持 QA 产物路径稳定，便于各 Skill 和自动化检查读取。

## 目录结构

```
CCGS-Data/production/qa/
├── DIRECTORY-CONVENTION.md   # 本文件 — 目录规范说明
├── plans/                    # 测试计划
├── signoffs/                 # QA 签字报告
├── smoke/                    # 冒烟测试报告
├── bugs/                     # Bug 报告（单条）
├── evidence/                 # 手动测试证据（截图、录屏、签字记录）
├── playtests/                # 玩家试玩报告
├── triage/                   # Bug 分诊汇总报告
├── reports/                  # 其他分析报告（耐久测试、Flaky 测试等）
└── archive/                  # 已归档的历史文件
```

## 子目录职责与产出 Skill 对照表

| 子目录 | 文件命名模板 | 产出 Skill | 说明 |
|--------|-------------|-----------|------|
| `plans/` | `qa-plan-[sprint-slug]-[date].md` | `/qa-plan`, `/team-qa` | 每个 Sprint 或 Feature 的测试计划 |
| `signoffs/` | `qa-signoff-[sprint]-[date].md` | `/team-qa` | QA 签字报告（APPROVED / NOT APPROVED） |
| `smoke/` | `smoke-[date].md` | `/smoke-check` | 冒烟测试报告（PASS / FAIL） |
| `smoke/` | `smoke-tests.md` | 手动维护 | 静态冒烟测试清单（可选） |
| `bugs/` | `BUG-[NNNN]-[short-slug].md` | `/bug-report`, `/team-qa` | 单条 Bug 报告 |
| `evidence/` | `[story-slug]-evidence.md` | `/dev-story`, `/story-done` | Visual/Feel 和 UI Story 的手动验证记录 |
| `playtests/` | `playtest-[date]-[tester].md` | `/playtest-report` | 试玩报告 |
| `triage/` | `bug-triage-[date].md` | `/bug-triage` | Bug 分诊汇总与趋势分析 |
| `reports/` | `soak-test-[date]-[duration].md` | `/soak-test` | 耐久测试协议与分析 |
| `reports/` | `flakiness-report-[date].md` | `/test-flakiness` | Flaky 测试检测报告 |
| `archive/` | *(任意已归档文件)* | 手动迁移 | Sprint 结束后的历史文件归档 |

## 规则

1. **当前 Sprint 的文件**放在对应子目录中（`plans/`、`signoffs/`、`smoke/`）。
2. **Sprint 结束后**，旧的 Plan 和 Sign-off 移入 `archive/` 归档。
3. **Bug 报告**始终保留在 `bugs/` 中，直到被 `/bug-report close` 关闭。
4. **根目录不放任何文档**（除本规范文件外），所有输出必须落入子目录。
5. **自动化读取约定**：相关 Skill 通过固定子目录读取 QA 产物，路径变更需同步更新对应 Skill 文档。

## 受影响的 Skill 列表

以下 Skill 的输出/读取路径已按此规范更新：

- `/qa-plan` → 输出至 `plans/`
- `/team-qa` → 输出至 `plans/` + `signoffs/`
- `/smoke-check` → 输出至 `smoke/`，读取 `plans/`
- `/bug-report` → 输出至 `bugs/`（未变）
- `/bug-triage` → 输出至 `triage/`，读取 `bugs/` + `plans/`
- `/soak-test` → 输出至 `reports/`，读取 `plans/`
- `/test-flakiness` → 输出至 `reports/`
- `/playtest-report` → 输出至 `playtests/`（未变）
- `/story-done` → 读取 `evidence/` + `smoke/`
- `/create-stories` → 引用 `evidence/` + `smoke/`
- `/sprint-plan` → 读取 `plans/`
- `/gate-check` → 读取 `smoke/` + `plans/` + `signoffs/`
- `/test-setup` → 引用 `smoke/`
