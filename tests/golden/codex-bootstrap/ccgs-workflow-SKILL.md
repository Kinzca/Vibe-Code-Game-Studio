---
name: ccgs-workflow
description: Route a requested CCGS workflow or specialist role through the configured framework.
argument-hint: "[workflow or role] [request/context]"
user-invocable: true
allowed-tools: Read, Glob, Grep, Write, Edit, Bash
---

<!-- CCGS CODEX BRIDGE:MANAGED -->

# CCGS Workflow Bridge

Project data root: ccgs-data
Bridge version: 1.0

## Invocation Protocol

1. Resolve the framework root from the standalone project, .ccgs-upstream, or
   the CCGS_FRAMEWORK_ROOT environment variable.
2. Read the framework pipeline-core document before executing CCGS production
   work.
3. Resolve a workflow name under .ccgs-core/workflows/skills first.
4. If no Skill matches, resolve a role under Tier1-Directors, Tier2-Leads, or
   Tier3-Specialists and read that role definition in full.
5. Treat the current workspace as the consumer project and pass it explicitly
   to CCGS CLI commands.
6. Preserve repository boundaries. Never edit the consumer runtime as part of a
   framework bootstrap, and never update a submodule pointer implicitly.
7. Follow the resolved workflow's approval, testing, evidence, and language
   requirements.
