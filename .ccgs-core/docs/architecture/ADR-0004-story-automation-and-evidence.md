# ADR-0004: Story Automation and Evidence Contract

## Status

Accepted

## Date

2026-07-11

## Context

Automated workflow orchestration needs a deterministic way to decide whether a
Story can advance. Markdown evidence remains useful to people, but unrestricted
text cannot safely drive state changes. Consumer repositories may use different
engines and repository layouts, so automation cannot depend on Unity, Godot,
Cocos Creator, or a particular runtime source tree.

A failed closeout must remain visible without falsely marking the Story done.
Retries must also avoid unnecessary writes.

## Decision

- The canonical states are draft, ready, in-progress, review, blocked, and done.
- State transitions use an explicit allowlist. Same-state requests are
  idempotent retries.
- The public state command requires an explicit project root, Story path, target
  state, and dry-run or write mode.
- Stories are readable and writable only under the configured
  production/epics tree.
- Machine-readable evidence is JSON under production/qa/evidence and conforms
  to schemas/evidence.schema.json.
- Evidence records a Story ID, aggregate result, per-criterion result, and at
  least one build, test, review, or analysis check.
- Closeout passes only when the Story is in review or already done, the evidence
  is valid and belongs to the Story, every acceptance criterion passes, and all
  declared checks pass.
- A passing write advances review to done and writes one managed closeout block.
- A failing write preserves the current state and writes stable reason codes and
  messages into the same managed block.
- Dry-run never writes. Write mode uses same-directory atomic replacement.
- Repeated successful closeout of an already managed done Story performs no
  write and preserves its timestamp.
- All rules are engine-agnostic and operate only on synthetic fixtures in this
  repository.

## Alternatives Considered

### Infer completion from Markdown prose

Rejected because prose is not a stable machine contract and makes failure
classification unreliable.

### Let an orchestrator edit Story files directly

Rejected because each external service would duplicate state and safety rules.

### Advance failed Stories to blocked automatically

Rejected because evidence failure and production blocking are different facts.
Closeout preserves the current state and records reasons; an explicit state
transition may block the Story when that is the intended workflow decision.

## Consequences

### Positive

- Codex and future Windmill jobs can call the same deterministic CLI.
- Failure causes remain visible in the Story without overclaiming completion.
- Retries are safe and atomic.
- Unity, Godot, and Cocos Creator projects share one contract.
- Evidence can be validated before closeout.

### Negative

- Existing Markdown-only evidence must gain a companion JSON record before it
  can drive automatic closeout.
- The standard-library validator intentionally implements the bundled schema
  subset and must evolve with schema revisions.
- Closeout does not run engine tests itself; external runners must produce the
  evidence record.

## Validation Criteria

- Every allowed and denied transition is tested.
- Dry-run leaves the materialized project unchanged.
- Repeated state and closeout writes preserve content and timestamps.
- Invalid or missing evidence prevents done status and writes stable reasons.
- Story and evidence paths cannot escape their owned trees.
- Atomic writes leave no temporary artifacts.
- Unity, Godot, and Cocos overlays produce identical closeout reports.
- The full repository test suite passes.
