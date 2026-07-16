# AGENTS.md

<!-- CCGS CODEX BRIDGE:BEGIN -->
## CCGS Codex Bridge

CCGS is this project's production-time AI workflow, not a runtime game
dependency.

- Project data root: ccgs-data
- Load .agents/skills/ccgs-context/SKILL.md before Story implementation,
  readiness, review, or closeout work.
- Load .agents/skills/ccgs-workflow/SKILL.md when a CCGS workflow or role is
  requested.
- Keep framework changes and consumer-project changes in separate repositories.
- Do not update .ccgs-upstream or a consumer submodule pointer unless the user
  explicitly approves a framework upgrade.
- Generated CCGS writes are limited to the configured data directory,
  .agents, and generated AI entry files.

### Framework Resolution

Resolve the CCGS entrypoint in this order:

1. Use ccgs.cmd or the Python CLI in the project root for standalone use.
2. Use .ccgs-upstream/ccgs.cmd for embedded-submodule use.
3. Use the CCGS_FRAMEWORK_ROOT environment variable for external use.

Always pass the consumer project through --project-root. Do not commit a
machine-specific absolute framework path.
<!-- CCGS CODEX BRIDGE:END -->
