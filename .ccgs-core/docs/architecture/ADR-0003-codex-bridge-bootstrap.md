# ADR-0003: Project-Local Codex Bridge Bootstrap

## Status

Accepted

## Date

2026-07-11

## Context

Codex needs a project instruction entry and locally discoverable Skills to use
CCGS consistently after a consumer project is cloned. The existing Bash
initialization assumes .ccgs-core is projected inside the consumer, overwrites
AGENTS.md, and creates symbolic-link bridges. Those assumptions do not hold for
external framework layouts or restricted Windows environments.

The bootstrap must also avoid modifying runtime source, replacing
consumer-owned instructions, or changing an embedded submodule pointer.

## Decision

- The public command is ccgs bootstrap with explicit project-root, codex, and
  either dry-run or write arguments.
- Dry-run renders a stable write manifest containing relative path, action, and
  desired SHA-256 for every managed file.
- Templates live under templates/codex and contain no engine-specific or
  machine-specific absolute path.
- AGENTS.md is managed through one delimited CCGS block. Existing content
  outside that block is preserved byte-for-byte.
- The bridge creates two project-local Skills:
  - .agents/skills/ccgs-context/SKILL.md
  - .agents/skills/ccgs-workflow/SKILL.md
- Skill frontmatter remains the first content in each SKILL.md. A generated
  management marker follows the closing frontmatter delimiter.
- Existing Skill files are updated only when they carry exactly one CCGS
  management marker. Unmanaged collisions fail before any write.
- Framework resolution is portable: standalone project, .ccgs-upstream, then
  CCGS_FRAMEWORK_ROOT for external use.
- Every target is validated through the repository allowlist before existing
  content is read.
- Write mode uses atomic same-directory replacement and verifies each resulting
  file against its planned hash.
- Unchanged files are not rewritten, preserving file timestamps.
- Bootstrap never updates .ccgs-upstream, a Git submodule pointer, or runtime
  source.

## Alternatives Considered

### Overwrite AGENTS.md from a complete template

Rejected because consumer projects may already contain important project
instructions owned by the user or another tool.

### Generate symbolic links to all framework directories

Rejected because external layouts, Windows permissions, Git behavior, and
consumer portability make link behavior inconsistent.

### Copy every CCGS workflow into project-local Skills

Rejected for this batch because it would duplicate a large framework tree and
create update drift. Two routing Skills provide a stable bridge while the
framework remains the source of truth.

### Allow force-overwriting Skill collisions

Rejected because a same-name consumer Skill may contain user-owned behavior.
Collision resolution must be explicit.

## Consequences

### Positive

- A cloned consumer project gains a deterministic Codex entry without copying
  the full framework.
- Existing AGENTS.md content remains intact.
- Dry-run provides an auditable write list before mutation.
- Repeated bootstrap runs are idempotent.
- Unity, Godot, Cocos Creator, and engine-neutral projects receive identical
  bridge files.
- External and embedded framework layouts share one portable resolution model.

### Negative

- External consumers must configure CCGS_FRAMEWORK_ROOT when no local framework
  entrypoint exists.
- The bridge exposes routing Skills rather than all individual CCGS workflows.
- Unmanaged same-name Skills require manual resolution before bootstrap.

## Validation Criteria

- Dry-run exactly matches the golden JSON manifest and leaves the project
  unchanged.
- First write creates exactly AGENTS.md and two declared Skill files.
- Second write reports three unchanged files and preserves their mtimes.
- Existing AGENTS.md content outside the managed block remains unchanged.
- An unmanaged Skill collision produces no partial output.
- Skill files begin with valid YAML frontmatter.
- Atomic writes leave no temporary files and match planned hashes.
- Unity, Godot, and Cocos Creator overlays produce identical manifests and
  outputs.
