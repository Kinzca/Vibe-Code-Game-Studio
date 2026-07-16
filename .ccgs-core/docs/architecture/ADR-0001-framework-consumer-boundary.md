# ADR-0001: Framework and Consumer Repository Boundary

## Status

Accepted

## Date

2026-07-11

## Context

CCGS is distributed as an independent repository and may be mounted inside a
game repository as a Git submodule. Editing a projected `.ccgs-core` directory
inside a consumer can accidentally commit framework work together with game
runtime code and private production data.

The command layer also needs to work on Windows without requiring Bash. Future
automation needs a stable way to distinguish framework source from the consumer
project receiving generated CCGS artifacts.

## Decision

- Framework source is developed, tested, committed, and released only from the
  independent CCGS repository.
- Every command resolves both `framework_root` and `project_root`.
- Diagnostic commands are read-only by default.
- Future write commands require an explicit `--project-root` and support
  `--dry-run`.
- Project writes are limited to `ccgs-data`, `.agents`, and generated AI entry
  files.
- The write policy is engine-agnostic and allowlist-based. Every path not owned
  by CCGS is protected by default, including Unity, Godot, Cocos Creator, server,
  tool, and arbitrary source directories.
- Windows uses `ccgs.cmd` as the stable entrypoint, backed by the same Python
  implementation used by other platforms.
- External services integrate through the CLI contract rather than importing
  game-specific paths or rules.

## Alternatives Considered

### Develop directly in each game repository

Rejected because framework changes, game changes, and private CCGS data would
share one commit boundary.

### Keep Bash as the only command entrypoint

Rejected because Windows environments may not have Bash or WSL available.

### Allow tools to infer writable project roots

Rejected because a wrong working directory could silently modify framework
source or game runtime files.

## Consequences

### Positive

- Framework releases can be tested without a real game repository.
- Consumer upgrades remain explicit and reviewable.
- Windmill and other orchestrators gain one stable command contract.
- Repository safety can be tested independently from workflow behavior.
- New engines do not require changes to the core write-safety policy.

### Negative

- Consumer projects need an explicit bootstrap or upgrade step.
- Framework and consumer fixtures must be maintained separately.
- Legacy projects with a copied `.ccgs-core` require a later migration plan.

## Validation Criteria

- `ccgs doctor` identifies standalone, embedded-submodule, and external layouts.
- `ccgs doctor --json` provides stable machine-readable results.
- Policy tests reject representative Unity, Godot, Cocos Creator, generic source,
  and out-of-root paths.
- Doctor and policy commands leave the consumer tree unchanged.
## Fixture Strategy

Batch 2 uses four engine-neutral lifecycle fixtures:

- `empty-project`
- `minimal-project`
- `mature-project`
- `malformed-project`

Engine details are supplied as small overlays for Unity, Godot, and Cocos
Creator. Repository-layout tests remain a separate dimension covering
standalone, embedded-submodule, and external framework use. This keeps lifecycle
behavior, engine detection, and repository topology independently testable
without maintaining a full Cartesian product of copied projects.
