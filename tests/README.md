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
