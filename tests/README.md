# Isolated CCGS Fixtures

This directory contains synthetic, engine-neutral project templates for CCGS
workflow tests. The committed fixtures are stable source inputs. Tests must use
fixture_workspace.materialized_fixture to copy them into an operating-system
temporary directory before generating context packs, bootstrap files, state
transitions, or evidence.

Temporary workspaces are deleted automatically when the context manager exits.
The committed fixture sources must remain unchanged.

## Dimensions

- Lifecycle: empty-project, minimal-project, mature-project, malformed-project
- Engine overlays: unity, godot, cocos
- Repository layouts: standalone, embedded-submodule, external

Lifecycle fixtures and engine overlays are composed at test runtime. Repository
layouts are constructed dynamically so tests do not commit nested Git metadata.

## Run

    python tests/run_tests.py

## Context Pack Golden Tests

The mature project fixture provides one Story with GDD, ADR, QA evidence, and
session references. Its deterministic preview is stored under
tests/golden/context-packs. Preview, dry-run, write, failure, limit, and engine
overlay behavior are exercised through the public ccgs context-pack command.

## Codex Bridge Golden Tests

The Codex bootstrap suite verifies the JSON write manifest, exact generated
AGENTS and Skill files, repeated-run idempotence, mtime preservation, managed
block merging, unmanaged collision refusal, atomic cleanup, and identical
outputs across Unity, Godot, and Cocos overlays.

## Story Automation Tests

The Batch 4 suite covers the complete state transition matrix, Evidence Schema
validation, dry-run isolation, atomic writes, repeated-run timestamp
preservation, passing closeout, failure reason writeback, owned-tree boundaries,
and identical Unity, Godot, and Cocos reports.

## Windmill Adapter Tests

The Batch 5A suite verifies real ccgs.cmd delegation, read-only Story checks,
automatic passing and failing Closeout, idempotence, failure collection,
transport-only retries, permanent-error behavior, path and command boundaries,
strict JSON-compatible Windmill YAML, and identical engine-overlay reports.
## Allure Adapter Tests

The Batch 5B suite verifies normalized JSON and JUnit aggregation, Closeout
Evidence steps and attachments, stable history/test-case identifiers, unique run
UUIDs, dry-run isolation, atomic immutable writes, idempotence, conflict refusal,
status mapping, scoped paths, strict input validation, and identical reports
across Unity, Godot, and Cocos overlays.
## Qdrant Adapter Tests

The Batch 5C suite verifies all five semantic source families, deterministic
heading-aware chunks, payload Schema, stable project-scoped point IDs, offline
dry-run, incremental zero-work reruns, changed-source updates, stale deletion,
upsert-before-delete failure safety, model migrations, REST request shapes,
bounded query results, URL security, and identical Unity/Godot/Cocos plans.
## Langfuse Adapter Tests

The Batch 5D suite verifies strict Workflow Event validation, deterministic
trace/span/Score IDs, current Langfuse OTel attributes, summary-only privacy,
secret and absolute-path rejection, timezone/status handling, read-only dry-run,
trace-before-Score ordering, negative acknowledgement behavior, retry-stable
Scores, environment-only credentials, host security, and identical
Unity/Godot/Cocos reports.
