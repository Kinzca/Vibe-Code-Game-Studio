# ADR-0002: Repository-Safe Context Pack Contract

## Status

Accepted

## Date

2026-07-11

## Context

Long-running CCGS projects accumulate Stories, GDDs, ADRs, QA evidence, sprint
history, and session records. Loading those trees broadly makes Codex
conversations unnecessarily large. The existing workflow helper assumes the
framework is projected inside the consumer project and only understands part of
the current Story reference format, so it is not a stable contract for external
or embedded-submodule layouts.

## Decision

- The public entrypoint is ccgs context-pack with explicit project-root and
  story arguments.
- Story reads are restricted to the configured CCGS production/epics tree.
- Selection order is Story, explicit GDD references, explicit ADR references,
  matching QA evidence, and the current session state.
- Frontmatter keys are case-insensitive. Legacy GDD paths, ADR paths or IDs, and
  evidence paths in the Story body are accepted as fallback references.
- Default limits are 8 files, 6000 characters per file, and 24000 source
  characters per pack.
- Output is deterministic and contains no timestamp or absolute project path.
- Preview is the default behavior and does not modify the consumer project.
- Dry-run validates a proposed output target without writing.
- Write mode is explicit, atomic, Markdown-only, and restricted to the
  configured production/context directory.
- Missing explicit references are reported and block persistent output.
- The legacy workflow script remains available for compatibility; the unified
  CLI owns the repository-safe contract used by future automation.

## Alternatives Considered

### Load every document referenced by the current Sprint

Rejected because Sprint scope is often much larger than one Story and recreates
the context growth problem.

### Require a vector database in the first implementation

Rejected because deterministic Story references already provide a useful local
baseline without adding a service dependency. Semantic retrieval can be added
behind the same CLI contract later.

### Write Context Packs by default

Rejected because diagnostics and preparation commands must be read-only unless
the caller explicitly requests a project mutation.

## Consequences

### Positive

- Codex receives a bounded, reviewable context artifact for one Story.
- Unity, Godot, Cocos Creator, and engine-neutral projects use the same command.
- External frameworks no longer require a copied .ccgs-core inside the consumer.
- Golden tests can detect selection or formatting drift.
- Future Windmill and retrieval adapters can call one stable CLI command.

### Negative

- Story frontmatter must keep GDD and ADR references current.
- Simple frontmatter parsing intentionally supports only the subset required by
  CCGS Story files.
- Missing references stop write mode and require document repair.

## Validation Criteria

- Preview output exactly matches the mature Fixture golden file.
- Preview and dry-run leave the consumer tree unchanged.
- Write mode creates exactly one atomic Markdown artifact under
  production/context.
- Output outside production/context is rejected.
- Missing references prevent writes.
- The same mature Fixture produces identical output with Unity, Godot, and
  Cocos Creator overlays.
- Mixed-case frontmatter and body-only GDD or ADR references resolve correctly.
- File and character limits produce deterministic truncation and omission.
