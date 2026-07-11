# ADR-0005: Windmill Adapter Boundary

## Status

Accepted

## Date

2026-07-11

## Context

Batch 4 established a stable Story state, Evidence, and Closeout CLI contract.
Windmill can schedule and expose that contract, but allowing an orchestration
service to parse project documents or edit game files would duplicate policy
and widen the write boundary.

The adapter must support checks, automatic Closeout, failure collection, and
retries while remaining independent of Unity, Godot, Cocos Creator, and game
runtime layout.

## Decision

- Windmill invokes the mounted framework through ccgs.cmd on Windows or
  ccgs.sh on Linux OSS workers.
- The adapter permits only doctor, evidence-validate, and closeout.
- Story and Evidence inputs must be relative, non-traversing, and safe for
  cmd.exe. Arbitrary shell fragments and commands are rejected.
- Windmill scripts contain no Story parsing, Evidence validation, state machine,
  or closeout write logic.
- Story check calls doctor --json, evidence-validate, and closeout --dry-run.
- Automatic Closeout repeats the check, then calls closeout --write. The CLI
  decides whether to advance to done or preserve state and write failure reasons.
- Business exit code 1 is collected as a failure report and is never retried.
- Invocation exit code 2 is permanent and is never retried.
- Timeouts, worker transport errors, and successful-process protocol errors are
  retryable within a bounded policy.
- Standalone scripts use adapter retries. The Flow fixes adapter attempts to one
  and uses Windmill native retry_if only for CCGS_RETRYABLE errors.
- Reports contain relative Story and Evidence paths and sanitized Doctor data;
  absolute project roots are not returned.
- wmill.yaml syncs only f/** and excludes secrets, variables, resources, apps,
  triggers, schedules, users, groups, settings, and workspace keys.
- The adapter never writes a report file. closeout --write is the only allowed
  mutation and remains constrained by the CCGS project allowlist.
- No Windmill instance, consumer bootstrap, or submodule upgrade is part of this
  batch.

## Alternatives Considered

### Let Windmill read and update Story Markdown directly

Rejected because it duplicates the Batch 4 state machine and bypasses atomic
writes and repository policy.

### Expose arbitrary ccgs.cmd arguments

Rejected because an orchestration input could expand the adapter beyond its
reviewed command and path boundary.

### Retry every non-zero result

Rejected because Evidence and Closeout failures are deterministic business
results. Retrying them wastes work and obscures the actual failure.

### Use only Windmill retries

Rejected for standalone script use. The adapter provides bounded local retries,
while the Flow deliberately selects Windmill-native retry to avoid stacking
both policies.

## Consequences

### Positive

- Windmill can schedule, expose, and monitor Story Closeout without owning CCGS
  workflow rules.
- Business failures produce structured, deduplicated reports.
- Transient worker failures receive bounded retries.
- Runtime source remains outside the adapter's read and write behavior.
- Unity, Godot, and Cocos Creator projects produce identical reports.

### Negative

- Workers need Python 3.10+ and the optional adapter dependencies.
- Framework and consumer project paths must be mounted on the same worker.
- Non-admin Windmill workspaces may need local folder owner configuration before
  sync.
- The Windmill CLI is needed to validate and push assets against a live instance;
  repository tests validate the local contract without one.

## Validation Criteria

- The full repository test suite passes.
- Real ccgs.cmd checks and Closeout run against disposable Fixtures.
- Passing Closeout advances review to done and repeated runs are idempotent.
- Failed Closeout preserves state, writes failure reasons, and returns them.
- Business and invocation failures do not retry.
- Transport failures retry within limits and emit CCGS_RETRYABLE when exhausted.
- Absolute, traversing, and cmd-sensitive paths are rejected.
- Reports are identical across Unity, Godot, and Cocos overlays.
- Windmill assets parse as strict JSON-compatible YAML.
- Game source paths and arbitrary subprocess logic do not appear in Windmill
  entry scripts.
