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
- `Client/Assets` and `Server` are protected paths and cannot be CCGS write
  targets.
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

### Negative

- Consumer projects need an explicit bootstrap or upgrade step.
- Framework and consumer fixtures must be maintained separately.
- Legacy projects with a copied `.ccgs-core` require a later migration plan.

## Validation Criteria

- `ccgs doctor` identifies standalone, embedded, and external layouts.
- `ccgs doctor --json` provides stable machine-readable results.
- Policy tests reject writes under `Client/Assets`, `Server`, and outside the
  explicit project root.
- Doctor and policy commands leave the consumer tree unchanged.
